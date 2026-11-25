"""M2: event-driven backtester (BT-2/3/5/7), determinism (TEST-5), golden files (TEST-9)."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from helm.app.backtest import run_backtest, write_report
from helm.backtest.friction.models import FrictionConfig
from helm.core.enums import Side, Verdict
from tests.backtest_fixtures import EXAMPLES, bars, spec

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "golden"


def test_determinism_same_process_and_fresh_process() -> None:
    data = bars(2000)
    h1 = run_backtest(spec("sma_cross"), data).result_hash
    h2 = run_backtest(spec("sma_cross"), data).result_hash
    assert h1 == h2
    code = (
        "from helm.app.backtest import run_backtest;"
        "from tests.backtest_fixtures import bars, spec;"
        "print(run_backtest(spec('sma_cross'), bars(2000)).result_hash)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": str(ROOT), "PYTHONHASHSEED": "123", "PATH": ""},
    )
    assert out.stdout.strip() == h1
    assert (
        run_backtest(spec("sma_cross", seed=1), data).result_hash != h1
    )  # seed is part of the input


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_golden_files(name: str) -> None:
    golden = json.loads((GOLDEN / f"{name}.json").read_text())
    r = run_backtest(spec(name), bars())
    m = r.metrics.to_dict()
    assert len(r.orders) == golden["orders"] and len(r.fills) == golden["fills"]
    assert m["trade_count"] == golden["trade_count"]
    assert m["end_equity"] == pytest.approx(golden["end_equity"], rel=1e-9)
    assert m["fees_paid"] == pytest.approx(golden["fees_paid"], rel=1e-9)
    assert m["max_drawdown"] == pytest.approx(golden["max_drawdown"], rel=1e-9)
    assert [list(x) for x in r.order_sequence()[:5]] == golden["first_orders"]
    assert golden["orders"] > 0


def test_friction_reduces_pnl_monotonically() -> None:
    data = bars(3000)
    base = FrictionConfig(latency=False)
    finals = [
        run_backtest(spec("sma_cross", base.scaled(Decimal(k))), data).metrics.end_equity
        for k in (0, 1, 2, 4)
    ]
    assert finals == sorted(finals, reverse=True) and finals[0] > finals[-1]
    for component in ("fees", "spread", "impact"):
        only = FrictionConfig.off()
        setattr(only, component, True)
        on = run_backtest(spec("sma_cross", only), data).metrics.end_equity
        off = run_backtest(spec("sma_cross", FrictionConfig.off()), data).metrics.end_equity
        assert on <= off, component


def test_signals_execute_at_next_bar_open_and_every_order_has_a_risk_decision() -> None:
    data = bars(1500)
    r = run_backtest(spec("sma_cross", FrictionConfig.off()), data)
    by_close = {b.ts_close: i for i, b in enumerate(data)}
    for o, f in zip(
        sorted(r.orders, key=lambda o: o.ts_created),
        sorted(r.fills, key=lambda f: f.ts),
        strict=True,
    ):
        i = by_close[o.ts_created]  # decided at the close of bar i ...
        assert (
            f.ts == data[i + 1].ts_close and f.price == data[i + 1].o
        )  # ... filled at bar i+1 open (I3)
        assert o.intent.side in (Side.BUY, Side.SELL)
    approved = [d for d in r.decisions if d.verdict is Verdict.APPROVE]
    assert len(approved) == len(r.orders)  # 100% of orders carry a risk decision
    assert {d.intent_id for d in approved} == {o.intent.intent_id for o in r.orders}


def test_tearsheet_written(tmp_path: Path) -> None:
    s = spec("breakout")
    r = run_backtest(s, bars(1500))
    path = write_report(r, s, tmp_path, title="breakout test")
    page = path.read_text()
    assert "EVENT-DRIVEN" in page and r.result_hash in page and "<svg" in page
    meta = json.loads((tmp_path / "result.json").read_text())
    assert meta["engine"] == "event_driven" and "sharpe" in meta["metrics"]


def test_cli_backtest_synthetic(tmp_path: Path) -> None:
    from helm.cli import main

    assert (
        main(
            [
                "backtest",
                "--synthetic",
                "800",
                "--param",
                "fast=5",
                "--param",
                "slow=20",
                "--out",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert list(tmp_path.glob("*/tearsheet.html"))
    assert (
        main(["sweep", "--synthetic", "3000", "--fast", "5", "10", "--slow", "30", "--top", "2"])
        == 0
    )
    assert main(["backtest", "--data-dir", str(tmp_path / "empty"), "--out", str(tmp_path)]) == 2
