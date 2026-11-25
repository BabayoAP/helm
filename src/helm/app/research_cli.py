"""`helm research` (RES-2/3/4) and `helm promote` (PRD §13)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB = "data/research.sqlite"


def _params(pairs: list[str]) -> dict[str, float | int | str | bool]:
    from helm.app.cli import _params as parse  # pyright: ignore[reportPrivateUsage]

    return parse(pairs)


def _source(a: argparse.Namespace) -> Any:
    from helm.app.research import DataSource

    return DataSource(
        symbol=a.symbol,
        timeframe=a.timeframe,
        synthetic=a.synthetic,
        seed=a.seed,
        start=a.start,
        end=a.end,
        asset_class=a.asset_class,
        data_dir=a.data_dir,
        adjusted=a.adjusted,
    )


def _settings(a: argparse.Namespace) -> Any:
    from helm.app.research import RunSettings, instrument_for_symbol
    from helm.backtest.friction.models import FrictionConfig
    from helm.infra.config import load_config

    st = RunSettings(
        strategy=a.strategy,
        instrument=instrument_for_symbol(a.symbol),
        friction=FrictionConfig() if not a.no_friction else FrictionConfig.off(),
        seed=a.seed,
        return_period=a.return_period,
    )
    if a.risk:
        st.risk = load_config(risk_path=a.risk).risk
    return st


def _version(strategy: str, params: dict[str, Any]) -> Any:
    from helm.research.experiments.store import code_hash, params_hash
    from helm.research.promotion import Version
    from helm.strategy.loader import load_strategy

    return Version(strategy, code_hash(load_strategy(strategy)), params_hash(params))


# ------------------------------------------------------------------ research
def cmd_validate(a: argparse.Namespace) -> int:
    from helm.app.research import backtest_evaluator
    from helm.infra.config import load_config
    from helm.research.experiments.store import (
        ExperimentStore,
        HoldoutLockedError,
        code_hash,
        strategy_path,
    )
    from helm.research.validation.suite import ValidationConfig, validate
    from helm.strategy.loader import load_strategy

    promo = load_config(promotion_path=a.promotion).promotion if a.promotion else None
    strat = load_strategy(a.strategy)
    space = getattr(strat, "param_space", None)
    if space is None:
        print(f"{a.strategy} declares no param_space (helm.strategy.params)", file=sys.stderr)
        return 2
    if a.param:  # fixed (not searched) params such as order size
        from helm.strategy.params import ParamSpace

        space = ParamSpace(
            dims={k: d for k, d in space.dims.items() if k not in _params(a.param)},
            fixed={**space.fixed, **_params(a.param)},
            constraint=space.constraint,
        )
    settings = _settings(a)
    source = _source(a)
    bars = source.load()
    if len(bars) < 200:
        print("not enough bars: fetch/import data or use --synthetic N", file=sys.stderr)
        return 2
    store = ExperimentStore(a.db)
    from helm.app.backtest import data_hash

    dh = data_hash(bars)
    cfg = ValidationConfig(
        holdout_frac=a.holdout_frac,
        wf_folds=a.wf_folds,
        cv_folds=a.cv_folds,
        min_trades=promo.min_trades_backtest if promo else a.min_trades,
        min_dsr=float(promo.min_deflated_sharpe) if promo else a.min_dsr,
        optimizer=a.optimizer,
        optuna_trials=a.optuna_trials,
        seed=a.seed,
        prior_trials=store.trial_count(strategy_path(strat), code_hash(strat)),
    )

    def open_holdout() -> None:
        store.open_holdout(
            strat, dh, who=getpass.getuser(), reason=a.holdout_reason, force=a.force_holdout
        )

    try:
        rep = validate(
            backtest_evaluator(settings),
            space,
            bars,
            cfg,
            open_holdout=open_holdout if a.holdout else None,
        )
    except HoldoutLockedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    import secrets

    # unique per run: two validations of the same version are both evidence (and both count)
    run_id = f"val-{code_hash(strat)[:8]}-{dh[:8]}-{secrets.token_hex(4)}"
    store.record(
        run_id=run_id,
        kind="validation",
        strategy=strat,
        params=rep.best_params,
        data_hash=dh,
        data_desc=source.describe(),
        spec_hash="",
        seed=a.seed,
        n_trials=rep.n_trials,
        metrics={"best_is_sharpe": max(s for _, s in rep.trials)},
        result_hash="",
        passed=rep.passed,
        report={**rep.to_dict(), "settings": settings.to_dict()},
    )
    store.record(
        run_id=f"opt-{run_id}",
        kind="optimize",
        strategy=strat,
        params=rep.best_params,
        data_hash=dh,
        data_desc=source.describe(),
        spec_hash="",
        seed=a.seed,
        n_trials=len(rep.trials),
        metrics={},
        result_hash="",
    )
    out = Path(a.out) / f"{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep.to_dict(), indent=2, default=str))
    print(f"{'PASSED' if rep.passed else 'REJECTED'}  {a.strategy}  best {rep.best_params}")
    if rep.checks and rep.checks[0].value == 0:
        print(
            "  !! the best parameter set made 0 trades: check order size (--param qty=...),"
            " instrument min notional and risk limits before reading anything into this"
        )
    print(f"trials: {rep.n_trials} (incl. {cfg.prior_trials} earlier)  run: {run_id}")
    for c in rep.checks:
        print(f"  {'ok ' if c.passed else 'XX '} {c.name:26} {c.detail}")
    print(f"report: {out}")
    store.close()
    return 0 if rep.passed else 1


def cmd_runs(a: argparse.Namespace) -> int:
    from helm.research.experiments.store import ExperimentStore

    store = ExperimentStore(a.db)
    for r in store.runs(strategy=a.strategy, kind=a.kind, limit=a.limit):
        verdict = {None: "", True: "PASS", False: "FAIL"}[r.passed]
        sharpe = r.metrics.get("sharpe", r.metrics.get("best_is_sharpe", ""))
        print(
            f"{r.run_id:40} {r.kind:10} {verdict:4} {r.strategy.split(':')[-1]:14} "
            f"{json.dumps(r.params, sort_keys=True):34} sharpe={sharpe} git={r.git_hash[:12]}"
        )
    store.close()
    return 0


def cmd_rerun(a: argparse.Namespace) -> int:
    from helm.app.research import rerun
    from helm.research.experiments.store import ExperimentStore

    store = ExperimentStore(a.db)
    out = rerun(store, a.run_id)
    store.close()
    print(json.dumps(out.__dict__, indent=2))
    return 0 if out.reproduced else 1


# ------------------------------------------------------------------ promote
def _lookahead_ok(strategy: str, params: dict[str, Any], symbol: str) -> bool:
    from helm.app.research import DataSource, instrument_for_symbol
    from helm.research.validation.lookahead import check_strategy, intents_by_bar
    from helm.strategy.loader import load_strategy

    bars = DataSource(symbol=symbol, synthetic=900, seed=7).load()
    run = intents_by_bar(
        lambda: load_strategy(strategy), dict(params), [instrument_for_symbol(symbol)]
    )
    return check_strategy(strategy, run, bars, (300, 600)).ok


def cmd_promote(a: argparse.Namespace) -> int:
    from helm.app.research import account_stats
    from helm.core.enums import LifecycleStage
    from helm.infra.config import load_config
    from helm.research.experiments.store import ExperimentStore
    from helm.research.promotion import (
        PromotionError,
        PromotionRegistry,
        check_gate,
        gather_evidence,
    )

    params = _params(a.param)
    v = _version(a.strategy, params)
    reg = PromotionRegistry(a.db)
    if a.action == "history":
        for h in reg.history(a.strategy):
            print(
                f"{h['from_stage']:>10} -> {h['to_stage']:<10} {h['code_hash']}/{h['params_hash']}"
                f" by {h['approved_by']}: {h['note']}"
            )
        return 0
    cur = reg.stage(v)
    if a.action == "status":
        print(f"{v}: {cur.value}")
        return 0
    target = LifecycleStage(a.to.upper())
    try:
        if a.action == "demote":
            reg.demote(v, target, by=a.approve_as or getpass.getuser(), note=a.note)
            print(f"{v}: {cur.value} -> {target.value}")
            return 0
        store = ExperimentStore(a.db)
        ev = gather_evidence(
            store,
            v,
            lookahead_ok=_lookahead_ok(a.strategy, params, a.symbol)
            if target is LifecycleStage.BACKTESTED
            else None,
        )
        if a.paper_journal:
            ev.paper = account_stats(a.paper_journal, a.paper_account)
        if a.live_journal:
            ev.live_small = account_stats(a.live_journal, a.live_account)
        ev.parity_confirmed = a.confirm_parity
        ev.rejections_reviewed = a.confirm_rejections_reviewed
        ev.divergence_confirmed = a.confirm_divergence
        promo = load_config(promotion_path=a.promotion).promotion if a.promotion else None
        from helm.core.config import PromotionConfig

        gate = check_gate(target, ev, promo or PromotionConfig())
        print(f"{v}: {cur.value} -> {target.value}: gate {'PASSED' if gate.passed else 'FAILED'}")
        for r in gate.reasons:
            print(f"  XX {r}")
        if not gate.passed:
            return 1
        if not a.approve_as:
            print(
                "gate passed; re-run with --approve-as <your name> to approve (manual, journaled)"
            )
            return 0
        reg.promote(v, target, gate, approved_by=a.approve_as, note=a.note)
        print(f"approved by {a.approve_as}: now {target.value}")
        return 0
    except PromotionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    finally:
        reg.close()


def _data_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", default="helm.strategy.examples.sma_cross:SmaCross")
    p.add_argument("--symbol", default="fake:BTC/USD")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--asset-class", default="crypto")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--synthetic", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--adjusted", action="store_true", help="split/dividend-adjusted bars (DATA-9)")
    p.add_argument("--risk", default=None)
    p.add_argument("--no-friction", action="store_true")
    p.add_argument("--return-period", default="1h")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    r = sub.add_parser("research", help="validation suite, experiments, reruns (RES-2/3/4)")
    rs = r.add_subparsers(dest="research_cmd", required=True)
    v = rs.add_parser("validate", help="walk-forward + purged CV + DSR + plateau (+ holdout)")
    _data_args(v)
    v.add_argument("--db", default=DEFAULT_DB)
    v.add_argument(
        "--param", action="append", default=[], help="fix a param (not searched), e.g. qty=1"
    )
    v.add_argument("--promotion", default=None, help="promotion TOML for thresholds")
    v.add_argument("--optimizer", choices=["grid", "optuna"], default="grid")
    v.add_argument("--optuna-trials", type=int, default=50)
    v.add_argument("--holdout-frac", type=float, default=0.2)
    v.add_argument("--wf-folds", type=int, default=4)
    v.add_argument("--cv-folds", type=int, default=5)
    v.add_argument("--min-trades", type=int, default=30)
    v.add_argument("--min-dsr", type=float, default=0.95)
    v.add_argument("--holdout", action="store_true", help="open the locked holdout (logged, once)")
    v.add_argument("--holdout-reason", default="")
    v.add_argument(
        "--force-holdout", action="store_true", help="re-look (logged, weakens evidence)"
    )
    v.add_argument("--out", default="reports/validation")
    v.set_defaults(handler=cmd_validate)
    ru = rs.add_parser("runs", help="list recorded experiments")
    ru.add_argument("--db", default=DEFAULT_DB)
    ru.add_argument("--strategy", default=None, help="module:Class")
    ru.add_argument("--kind", default=None)
    ru.add_argument("--limit", type=int, default=50)
    ru.set_defaults(handler=cmd_runs)
    rr = rs.add_parser("rerun", help="rerun a recorded backtest; result hash must match")
    rr.add_argument("run_id")
    rr.add_argument("--db", default=DEFAULT_DB)
    rr.set_defaults(handler=cmd_rerun)

    p = sub.add_parser("promote", help="strategy promotion lifecycle (PRD §13)")
    p.add_argument("action", choices=["status", "request", "demote", "history"])
    p.add_argument("--strategy", default="helm.strategy.examples.sma_cross:SmaCross")
    p.add_argument("--param", action="append", default=[], help="key=value (the version's params)")
    p.add_argument("--symbol", default="fake:BTC/USD", help="for the lookahead check")
    p.add_argument("--to", default="BACKTESTED")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--promotion", default=None)
    p.add_argument("--approve-as", default=None, help="your name: makes the approval explicit")
    p.add_argument("--note", default="")
    p.add_argument("--paper-journal", default=None)
    p.add_argument("--paper-account", default="paper-main")
    p.add_argument("--live-journal", default=None)
    p.add_argument("--live-account", default="alpaca-live")
    p.add_argument("--confirm-parity", action="store_true")
    p.add_argument("--confirm-rejections-reviewed", action="store_true")
    p.add_argument("--confirm-divergence", action="store_true")
    p.set_defaults(handler=cmd_promote)
