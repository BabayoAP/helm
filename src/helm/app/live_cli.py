"""`helm live`: run a live bot on Alpaca (decision 0023).

With `venues.alpaca.environment = "paper"` this is the Alpaca paper account
(the PRD's "testnet"). It still needs every EXE-1 gate:
HELM_LIVE_TRADING=1, bot mode "live", lifecycle >= LIVE_SMALL.

Startup order: gates -> preflight (SEC-1) -> account lock -> journal ->
recovery (EXE-3) -> control plane -> feed + user stream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path


def _store(kind: str):  # type: ignore[no-untyped-def]
    from helm.infra.secrets import EnvSecretStore, FileSecretStore

    return FileSecretStore() if kind == "file" else EnvSecretStore()


def _starting_equity(acct_dir: Path, venue_equity: Decimal) -> Decimal:
    """The first run records the venue equity; later runs reuse it so the
    journal-rebuilt portfolio stays consistent across restarts."""
    p = acct_dir / "starting_equity.json"
    if p.exists():
        return Decimal(json.loads(p.read_text())["starting_equity"])
    acct_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"starting_equity": str(venue_equity)}))
    return venue_equity


def _registry_stage(strategy: str, params: Mapping[str, object], db: Path):  # type: ignore[no-untyped-def]
    from helm.research.experiments.store import code_hash, params_hash
    from helm.research.promotion import PromotionRegistry, Version
    from helm.strategy.loader import load_strategy

    reg = PromotionRegistry(db)
    try:
        return reg.stage(Version(strategy, code_hash(load_strategy(strategy)), params_hash(params)))
    finally:
        reg.close()


def cmd_live(a: argparse.Namespace) -> int:
    from helm.app.gates import build_broker
    from helm.app.lifecycle import build_engine, recover, resync
    from helm.app.paper import PaperRunner, RunnerConfig
    from helm.app.services import (
        alpaca_credentials,
        alpaca_feed,
        build_control_plane,
        make_watchlist,
    )
    from helm.app.venues import live_factory
    from helm.brokers.live.alpaca import AlpacaBroker
    from helm.core.enums import LifecycleStage, Mode
    from helm.core.errors import HelmError
    from helm.data.connectors.alpaca_common import instrument_for
    from helm.data.instruments import InstrumentRegistry
    from helm.infra.clock import LiveClock
    from helm.infra.config import load_config
    from helm.infra.lock import AccountLock
    from helm.infra.logging import LogRing, configure_logging, get_logger
    from helm.oms.journal import Journal
    from helm.strategy.loader import load_strategy
    from helm.tracking.alerts import AlertManager

    cfg = load_config(a.config, a.risk)
    if a.api_host:  # e.g. 0.0.0.0 inside a container; publish the port on 127.0.0.1 only
        cfg = cfg.model_copy(
            update={"control": cfg.control.model_copy(update={"api_host": a.api_host})}
        )
    bots = [b for b in cfg.engine.bots if b.mode is Mode.LIVE and (a.bot is None or b.id == a.bot)]
    if not bots:
        print("no live bot found in config", file=sys.stderr)
        return 2
    bot = bots[0]
    env_name = cfg.venues.alpaca.environment
    ring = LogRing()
    configure_logging(
        level=cfg.engine.log_level, mode_badge=f"[LIVE:{env_name.upper()}]", ring=ring
    )
    log = get_logger(bot=bot.id, account=bot.account_id, venue=bot.venue)
    store = _store(a.secrets)
    clock = LiveClock()
    data_dir = Path(cfg.engine.data_dir)
    acct_dir = data_dir / "accounts" / bot.account_id

    async def run() -> int:
        stage = _registry_stage(bot.strategy, bot.params, data_dir / "research.sqlite")
        real_money = env_name == "live"
        if not real_money and not stage.at_least(LifecycleStage.LIVE_SMALL):
            log.warning(
                "promotion_registry_below_live_small",
                stage=stage.value,
                note="allowed on the Alpaca paper account only; real money requires it",
            )
        try:
            broker = build_broker(
                bot,
                env=os.environ,
                clock=clock,
                live_factory=live_factory(cfg, store, clock),
                registry_stage=stage if real_money else None,
            )
        except HelmError as exc:
            log.error("live_refused", error=str(exc))
            print(f"refusing to run live: {exc}", file=sys.stderr)
            return 3
        assert isinstance(broker, AlpacaBroker)
        try:
            acct, warnings = await broker.preflight(
                attest_no_withdrawal=cfg.security.attest_no_withdrawal
            )
            log.info(
                "preflight_ok",
                environment=env_name,
                status=acct.status,
                pdt=acct.pattern_day_trader,
            )
            insts = [instrument_for(s, await broker.asset(s)) for s in bot.symbols]
            with AccountLock(data_dir / "locks", bot.account_id):
                journal = Journal(acct_dir / "journal.sqlite")
                alerts = AlertManager(
                    mode=Mode.LIVE,
                    clock_fn=clock.now,
                    sinks=[
                        lambda al: log.warning("alert", severity=al.severity.value, text=al.text)
                    ],
                )
                engine, previous = build_engine(
                    mode=Mode.LIVE,
                    account_id=bot.account_id,
                    run_id=secrets.token_hex(4),
                    clock=clock,
                    broker=broker,
                    journal=journal,
                    instruments=InstrumentRegistry(insts),
                    risk_config=cfg.risk,
                    starting_equity=_starting_equity(acct_dir, acct.equity),
                    code_fn=lambda: secrets.token_hex(4),
                    alerts=alerts,
                )
                watchlist = make_watchlist(cfg, bot)
                cp = build_control_plane(
                    engine,
                    cfg,
                    store,
                    venue=f"alpaca-{env_name}",
                    ring=ring,
                    log=log,
                    ui_dir=Path(a.ui_dir) if a.ui_dir else None,
                    api=not a.no_api,
                    watchlist=watchlist,
                )
                alerts.sinks.extend(cp.sinks)
                for w in warnings:
                    alerts.warn("preflight", w)
                rep = await recover(
                    engine, adopt=a.adopt, flatten_first=a.flatten_first, previous=previous
                )
                log.info(
                    "recovered", **{k: v for k, v in rep.__dict__.items() if k != "unexpected"}
                )

                async def _on_reconnect() -> None:
                    n = await resync(engine)
                    log.info("user_stream_resync", fills_applied=n)

                broker.on_reconnect = _on_reconnect
                engine.add_strategy(
                    load_strategy(bot.strategy),
                    symbols=bot.symbols,
                    params=bot.params,
                    lifecycle=bot.lifecycle,
                    capital_pct=bot.capital_pct,
                    daily_loss_cap=bot.daily_loss_cap,
                )
                feed = alpaca_feed(
                    bot,
                    alpaca_credentials(store, cfg.venues.alpaca.credentials),
                    data_feed=cfg.venues.alpaca.data_feed,
                    now_fn=clock.now,
                    extra_symbols=cfg.tracking.watch_symbols,
                )
                runner = PaperRunner(
                    engine,
                    feed,
                    config=RunnerConfig(reconcile_every_s=cfg.risk.reconcile_interval_s),
                    broker_events=broker.stream(),
                    services=cp.services,
                    on_event=watchlist.update,
                    engine_symbols=frozenset(bot.symbols),
                )
                if cp.login_url:
                    print(f"control UI: {cp.login_url}", file=sys.stderr)
                try:
                    await runner.run()
                finally:
                    journal.close()
        except HelmError as exc:
            log.error("live_refused", error=str(exc))
            print(f"refusing to run live: {exc}", file=sys.stderr)
            return 3
        finally:
            await broker.close()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("live", help="run a live bot (Alpaca; triple-gated, EXE-1)")
    p.add_argument("--config", default="config/helm.example.toml")
    p.add_argument("--risk", default="config/risk.example.toml")
    p.add_argument("--bot", default=None)
    p.add_argument("--secrets", choices=["env", "file"], default="env")
    p.add_argument("--adopt", action="store_true", help="EXE-3: adopt unexplained venue state")
    p.add_argument("--flatten-first", action="store_true", help="EXE-3: flatten, then start")
    p.add_argument("--ui-dir", default="ui/out", help="static UI build to serve")
    p.add_argument("--no-api", action="store_true", help="do not start the control API")
    p.add_argument("--api-host", default=None, help="override control.api_host")
    p.set_defaults(handler=cmd_live)
