"""RES-2 rerun reproducibility, research CLI, §13 promotion gates + registry, and the
registry-backed live gate (decision 0030)."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from helm.app.gates import missing_live_gates
from helm.app.research import account_stats, rerun
from helm.backtest.friction.models import FeeSchedule, FeeTier, FrictionConfig
from helm.cli import main
from helm.core.config import BotConfig, PromotionConfig
from helm.core.enums import EngineState, LifecycleStage, Liquidity, Mode, Side
from helm.core.events import EngineStateChanged
from helm.core.models import Fill
from helm.oms.journal import Journal
from helm.research.experiments.store import ExperimentStore, code_hash, params_hash
from helm.research.promotion import (
    AccountStats,
    Evidence,
    PromotionError,
    PromotionRegistry,
    Version,
    check_gate,
    gather_evidence,
)
from helm.strategy.examples.sma_cross import SmaCross

S = LifecycleStage
SMA = "helm.strategy.examples.sma_cross:SmaCross"
PARAMS = {"fast": 10, "slow": 40, "qty": 1}


def test_friction_round_trips_losslessly() -> None:
    fc = FrictionConfig(
        scale=Decimal("1.5"),
        fee_schedule=FeeSchedule((FeeTier(Decimal(0), Decimal(2), Decimal(5)),)),
        latency_ms=(10.0, 20.0),
        seed=9,
    )
    assert FrictionConfig.from_dict(json.loads(json.dumps(fc.to_dict()))) == fc
    assert FrictionConfig.from_dict(FrictionConfig.off().to_dict()) == FrictionConfig.off()


def _backtest(db: Path, out: Path, *extra: str) -> dict[str, object]:
    args = ["backtest", "--synthetic", "1500", "--timeframe", "1h", "--db", str(db)]
    args += ["--out", str(out), "--param", "fast=10", "--param", "slow=40", "--param", "qty=1"]
    assert main([*args, *extra]) == 0
    return {}


def test_backtest_is_recorded_and_rerun_reproduces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "r.sqlite"
    _backtest(db, tmp_path / "rep")
    out = json.loads(capsys.readouterr().out)
    store = ExperimentStore(db)
    rec = store.get(out["run_id"])
    assert rec is not None and rec.kind == "backtest" and rec.result_hash == out["result_hash"]
    assert rec.code_hash == code_hash(SmaCross()) and rec.params == PARAMS
    assert json.loads(rec.data_desc)["synthetic"] == 1500
    o = rerun(store, rec.run_id)
    assert o.reproduced and not o.code_changed and not o.data_changed
    with pytest.raises(KeyError):
        rerun(store, "nope")
    store.close()
    assert main(["research", "rerun", rec.run_id, "--db", str(db)]) == 0
    assert main(["research", "runs", "--db", str(db), "--kind", "backtest"]) == 0
    assert rec.run_id in capsys.readouterr().out


def test_validate_cli_records_and_refuses_second_holdout_look(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "r.sqlite"
    base = ["research", "validate", "--synthetic", "1500", "--timeframe", "1h", "--db", str(db)]
    base += ["--out", str(tmp_path / "v"), "--param", "qty=1", "--wf-folds", "2", "--cv-folds", "2"]
    base += ["--min-trades", "3", "--holdout", "--holdout-reason", "first look"]
    rc = main(base)
    text = capsys.readouterr().out
    assert rc in (0, 1) and ("PASSED" in text or "REJECTED" in text)
    store = ExperimentStore(db)
    (val,) = store.runs(kind="validation")
    names = [c["name"] for c in val.report["checks"]]
    assert {"deflated_sharpe", "walk_forward_efficiency", "purged_cv_oos_sharpe", "holdout"} <= set(
        names
    )
    assert store.trial_count(SMA, code_hash(SmaCross())) == val.n_trials  # optimize record
    store.close()
    assert main(base) == 3  # holdout already opened for this code version
    assert "holdout already opened" in capsys.readouterr().err
    with pytest.raises(ValueError, match=r"helm\.strategy"):
        main(["research", "validate", "--strategy", "tests.factories:instrument"])


def _stats(days: float, trips: int, halted: bool = False) -> AccountStats:
    return AccountStats(days=days, round_trips=trips, fills=trips * 2, currently_halted=halted)


def test_gates_and_registry_walk_the_lifecycle(tmp_path: Path) -> None:
    db = tmp_path / "r.sqlite"
    store = ExperimentStore(db)
    reg = PromotionRegistry(db)
    v = Version(SMA, code_hash(SmaCross()), params_hash(PARAMS))
    cfg = PromotionConfig(min_trades_backtest=5)
    assert reg.stage(v) is S.DRAFT

    # DRAFT -> BACKTESTED needs a friction-on backtest with trades + lookahead pass
    g = check_gate(S.BACKTESTED, gather_evidence(store, v, lookahead_ok=True), cfg)
    assert not g.passed and "no event-driven backtest" in g.reasons[0]
    store.record(
        run_id="bt1",
        kind="backtest",
        strategy=SmaCross(),
        params=PARAMS,
        data_hash="d",
        data_desc="{}",
        spec_hash="s",
        seed=0,
        n_trials=1,
        metrics={"trade_count": 12},
        result_hash="h",
        report={"settings": {"friction": FrictionConfig().to_dict()}},
        git="x",
    )
    assert not check_gate(S.BACKTESTED, gather_evidence(store, v, lookahead_ok=False), cfg).passed
    g = check_gate(S.BACKTESTED, gather_evidence(store, v, lookahead_ok=True), cfg)
    assert g.passed
    with pytest.raises(PromotionError, match="approver"):
        reg.promote(v, S.BACKTESTED, g, approved_by="")
    with pytest.raises(PromotionError, match="one stage up"):
        reg.promote(v, S.VALIDATED, check_gate(S.VALIDATED, Evidence(), cfg), approved_by="aaron")
    reg.promote(v, S.BACKTESTED, g, approved_by="aaron", note="first")

    # BACKTESTED -> VALIDATED needs a passing validation incl. holdout, trades, DSR
    def validation(rid: str, passed: bool, holdout: bool, dsr: float) -> None:
        checks = [{"name": "min_trades", "value": 40}, {"name": "deflated_sharpe", "value": dsr}]
        if holdout:
            checks.append({"name": "holdout", "value": 1.0})
        store.record(
            run_id=rid,
            kind="validation",
            strategy=SmaCross(),
            params=PARAMS,
            data_hash="d",
            data_desc="{}",
            spec_hash="",
            seed=0,
            n_trials=30,
            metrics={},
            result_hash="",
            passed=passed,
            report={"checks": checks},
            git="x",
        )

    validation("v-fail", False, True, 0.99)
    assert not check_gate(S.VALIDATED, gather_evidence(store, v), cfg).passed
    validation("v-noholdout", True, False, 0.99)
    g = check_gate(S.VALIDATED, gather_evidence(store, v), cfg)
    assert any("holdout" in r for r in g.reasons)
    validation("v-ok", True, True, 0.97)
    g = check_gate(S.VALIDATED, gather_evidence(store, v), cfg)
    assert g.passed, g.reasons
    reg.promote(v, S.VALIDATED, g, approved_by="aaron")
    reg.promote(v, S.PAPER, check_gate(S.PAPER, Evidence(), cfg), approved_by="aaron")

    # PAPER -> LIVE_SMALL: paper days/trades, not halted, parity + rejections reviewed
    ev = Evidence(paper=_stats(3, 4))
    g = check_gate(S.LIVE_SMALL, ev, cfg)
    assert len(g.reasons) == 3  # too short, parity, rejections
    ev = Evidence(paper=_stats(15, 4, halted=True), parity_confirmed=True, rejections_reviewed=True)
    assert check_gate(S.LIVE_SMALL, ev, cfg).reasons == [
        "paper engine is HALTED: resolve before promoting"
    ]
    ev.paper = _stats(2, 25)  # enough trades instead of days
    g = check_gate(S.LIVE_SMALL, ev, cfg)
    assert g.passed
    reg.promote(v, S.LIVE_SMALL, g, approved_by="aaron")
    assert reg.stage(v) is S.LIVE_SMALL

    # LIVE needs days at capped capital + divergence report
    g = check_gate(S.LIVE, Evidence(live_small=_stats(20, 9), divergence_confirmed=False), cfg)
    assert g.reasons == ["paper-vs-live divergence report (PAP-4) not confirmed"]
    assert not check_gate(S.LIVE, Evidence(), cfg).passed
    assert not check_gate(S.DRAFT, Evidence(), cfg).passed

    # any code/param change is a new version: back to DRAFT
    other = Version(v.strategy, v.code_hash, params_hash({**PARAMS, "fast": 15}))
    assert reg.stage(other) is S.DRAFT
    reg.demote(v, S.PAPER, by="aaron", note="drawdown review")
    assert reg.stage(v) is S.PAPER
    with pytest.raises(PromotionError):
        reg.demote(v, S.LIVE, by="aaron", note="")
    hist = reg.history(SMA)
    assert [h["to_stage"] for h in hist] == [
        "BACKTESTED",
        "VALIDATED",
        "PAPER",
        "LIVE_SMALL",
        "PAPER",
    ]
    assert hist[0]["approved_by"] == "aaron" and hist[-1]["evidence"] == {"demotion": True}
    assert len(reg.history()) == 5
    with pytest.raises(sqlite3.IntegrityError):
        reg._db.execute("UPDATE promotions SET approved_by = 'x'")  # pyright: ignore[reportPrivateUsage]
    reg.close()
    store.close()


def test_promote_cli_status_request_and_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "r.sqlite"
    _backtest(db, tmp_path / "rep")
    capsys.readouterr()
    p = ["--db", str(db), "--param", "fast=10", "--param", "slow=40", "--param", "qty=1"]
    assert main(["promote", "status", *p]) == 0
    assert "DRAFT" in capsys.readouterr().out
    assert main(["promote", "request", "--to", "BACKTESTED", *p]) == 0  # dry run: gate only
    assert "re-run with --approve-as" in capsys.readouterr().out
    assert main(["promote", "request", "--to", "BACKTESTED", "--approve-as", "aaron", *p]) == 0
    assert main(["promote", "request", "--to", "VALIDATED", *p]) == 1
    assert main(["promote", "request", "--to", "LIVE", "--approve-as", "aaron", *p]) == 1
    assert main(["promote", "demote", "--to", "DRAFT", "--note", "redo", *p]) == 0
    assert main(["promote", "demote", "--to", "LIVE", *p]) == 3
    assert main(["promote", "history", *p]) == 0
    out = capsys.readouterr().out
    assert "DRAFT -> BACKTESTED" in out and "BACKTESTED -> DRAFT" in out


def test_account_stats_from_journal(tmp_path: Path) -> None:
    path = tmp_path / "j.sqlite"
    day = 86_400 * 10**9
    with Journal(path, mirror=False) as j:
        for i, (side, ts) in enumerate(((Side.BUY, 0), (Side.SELL, 3 * day))):
            f = Fill(
                client_order_id=f"c{i}",
                venue="fake",
                trade_id=str(i),
                symbol="fake:BTC/USD",
                side=side,
                price=Decimal(100 + i),
                qty=Decimal(1),
                fee_ccy="USD",
                liquidity=Liquidity.TAKER,
                ts=ts,
            )
            j.append(f, ts=ts, run_id="r", account_id="paper-a", key=f.client_order_id)
        ev = EngineStateChanged(
            previous=EngineState.RUNNING, current=EngineState.HALTED, reason="kill", ts=4 * day
        )
        j.append(ev, ts=4 * day, run_id="r", account_id="paper-a", key="engine")
    st = account_stats(str(path), "paper-a")
    assert (st.fills, st.round_trips, st.halts, st.currently_halted) == (2, 1, 1, True)
    assert st.days == pytest.approx(4.0)
    assert account_stats(str(path), "someone-else").days == 0


def test_real_money_needs_the_registry_not_just_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bot = BotConfig(
        id="b",
        strategy=SMA,
        mode=Mode.LIVE,
        account_id="alpaca-live",
        venue="alpaca",
        symbols=("alpaca:SPY/USD",),
        lifecycle=S.LIVE_SMALL,
    )
    env = {"HELM_LIVE_TRADING": "1"}
    assert missing_live_gates(bot, env) == []
    msgs = missing_live_gates(bot, env, registry_stage=S.PAPER)
    assert msgs and "promotion registry" in msgs[0]
    assert missing_live_gates(bot, env, registry_stage=S.LIVE) == []
    # end to end: config claims LIVE_SMALL, registry says DRAFT, real money => refused
    cfg = tmp_path / "helm.toml"
    cfg.write_text(
        f'[engine]\ndata_dir = "{tmp_path / "data"}"\n[venues.alpaca]\nenvironment = "live"\n'
        f'[[engine.bots]]\nid = "b"\nstrategy = "{SMA}"\nmode = "live"\n'
        'account_id = "alpaca-live"\nvenue = "alpaca"\nsymbols = ["alpaca:SPY/USD"]\n'
        'lifecycle = "LIVE_SMALL"\n'
    )
    monkeypatch.setenv("HELM_LIVE_TRADING", "1")
    assert main(["live", "--config", str(cfg), "--no-api"]) == 3
    assert "promotion registry has this code+params at DRAFT" in capsys.readouterr().err
