"""`helm paper`: run a paper bot (PAP-1..3) with recovery, account lock and soak reporting.

Until the M4 venue is chosen the feed is the seeded synthetic live feed; the
engine, risk, OMS, SimBroker and journal are the real ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import secrets
import sys
import time
from decimal import Decimal
from pathlib import Path


def cmd_paper(a: argparse.Namespace) -> int:
    from helm.app.lifecycle import build_engine, recover
    from helm.app.paper import PaperRunner, RunnerConfig
    from helm.backtest.friction.models import FrictionConfig
    from helm.brokers.sim.broker import SimBroker, SimMode
    from helm.core.enums import AssetClass, Mode
    from helm.core.models import Instrument, parse_symbol
    from helm.data.instruments import InstrumentRegistry
    from helm.data.recorder import Recorder
    from helm.data.synthetic_feed import synthetic_feed
    from helm.infra.clock import LiveClock
    from helm.infra.config import load_config
    from helm.infra.lock import AccountLock
    from helm.infra.logging import configure_logging, get_logger
    from helm.oms.journal import Journal
    from helm.strategy.loader import load_strategy
    from helm.tracking.alerts import AlertManager

    cfg = load_config(a.config, a.risk)
    bots = [b for b in cfg.engine.bots if b.mode is Mode.PAPER and (a.bot is None or b.id == a.bot)]
    if not bots:
        print("no paper bot found in config")
        return 2
    bot = bots[0]
    configure_logging(level=cfg.engine.log_level, mode_badge=Mode.PAPER.badge)
    log = get_logger(bot=bot.id, account=bot.account_id)
    data_dir = Path(cfg.engine.data_dir)
    acct_dir = data_dir / "accounts" / bot.account_id
    clock = LiveClock()
    symbol = bot.symbols[0]
    venue, _base, quote_ccy, _ = parse_symbol(symbol)
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        tick_size=Decimal("0.01"),
        lot_size=Decimal("0.0001"),
        min_notional=Decimal(5),
        quote_ccy=quote_ccy,
    )

    async def run() -> int:
        with AccountLock(data_dir / "locks", bot.account_id):
            journal = Journal(acct_dir / "journal.sqlite")
            broker = SimBroker(
                account_id=bot.account_id,
                venue=bot.venue,
                clock=clock,
                mode=SimMode.L1,
                friction=FrictionConfig(),
                starting_cash=cfg.engine.starting_equity,
            )
            alerts = AlertManager(
                mode=Mode.PAPER,
                clock_fn=clock.now,
                sinks=[lambda al: log.warning("alert", severity=al.severity.value, text=al.text)],
            )
            engine, previous = build_engine(
                mode=Mode.PAPER,
                account_id=bot.account_id,
                run_id=secrets.token_hex(4),
                clock=clock,
                broker=broker,
                journal=journal,
                instruments=InstrumentRegistry([inst]),
                risk_config=cfg.risk,
                starting_equity=cfg.engine.starting_equity,
                code_fn=lambda: secrets.token_hex(4),
                alerts=alerts,
            )
            rep = await recover(
                engine, adopt=a.adopt, flatten_first=a.flatten_first, previous=previous
            )
            log.info("recovered", **{k: v for k, v in rep.__dict__.items() if k != "unexpected"})
            if engine.ks.state.value == "PAUSED" and a.auto_resume:
                await engine.control(
                    __import__("helm.core.events", fromlist=["ControlCommand"]).ControlCommand(
                        action="resume", source="cli", ts=clock.now()
                    )
                )  # type: ignore[arg-type]
            engine.add_strategy(
                load_strategy(bot.strategy),
                symbols=bot.symbols,
                params=bot.params,
                lifecycle=bot.lifecycle,
            )
            feed = synthetic_feed(
                symbol,
                n_events=None,
                start_ns=clock.now(),
                step_ms=a.step_ms,
                seed=a.seed,
                timeframe=bot.timeframe,
                realtime=True,
                now_fn=clock.now,
            )
            recorder = (
                Recorder(data_dir, f"paper-{bot.account_id}-{time.strftime('%Y%m%d-%H%M%S')}")
                if a.record
                else None
            )
            runner = PaperRunner(engine, feed, config=RunnerConfig(), recorder=recorder)
            deadline = time.monotonic() + a.soak_hours * 3600 if a.soak_hours else None
            rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            samples: list[dict[str, object]] = []

            async def monitor() -> None:
                while True:
                    await asyncio.sleep(a.report_every_s)
                    rec = await engine.reconcile()
                    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    unit = 1024**2 if sys.platform == "darwin" else 1024
                    s = {
                        "events": runner.events,
                        "orders": len(engine.om.all()),
                        "equity": str(engine.portfolio.equity()),
                        "venue_equity": str(broker.ledger.equity()),
                        "reconcile_ok": rec.ok,
                        "rss_mb": round(rss / unit, 1),
                        "state": engine.ks.state.value,
                    }
                    samples.append(s)
                    log.info("soak", **s)
                    if deadline and time.monotonic() >= deadline:
                        runner.stop()
                        return

            mon = asyncio.create_task(monitor())
            try:
                await runner.run()
            finally:
                mon.cancel()
                journal.close()
            if a.soak_hours:
                ok = all(bool(s["reconcile_ok"]) for s in samples)
                report = {
                    "hours": a.soak_hours,
                    "samples": len(samples),
                    "reconcile_always_ok": ok,
                    "rss_start_mb": round(
                        rss0 / (1024**2 if sys.platform == "darwin" else 1024), 1
                    ),
                    "rss_end_mb": samples[-1]["rss_mb"] if samples else None,
                    "events": runner.events,
                }
                (acct_dir / "soak-report.json").write_text(json.dumps(report, indent=2))
                print(json.dumps(report))
                return 0 if ok else 1
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "paper", help="run a paper bot (synthetic feed until the M4 venue is chosen)"
    )
    p.add_argument("--config", default="config/helm.example.toml")
    p.add_argument("--risk", default="config/risk.example.toml")
    p.add_argument("--bot", default=None)
    p.add_argument(
        "--adopt", action="store_true", help="EXE-3: adopt unexplained venue state as-is"
    )
    p.add_argument(
        "--flatten-first", action="store_true", help="EXE-3: flatten unexplained state, then start"
    )
    p.add_argument(
        "--auto-resume",
        action="store_true",
        help="resume from PAUSED after a restart (never from HALTED)",
    )
    p.add_argument("--soak-hours", type=float, default=0.0)
    p.add_argument("--report-every-s", type=float, default=60.0)
    p.add_argument("--step-ms", type=int, default=250)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--record", action="store_true", help="record the feed to Parquet (DATA-4)")
    p.set_defaults(handler=cmd_paper)
