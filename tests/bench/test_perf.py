"""TEST-10 performance benchmarks (non-blocking: `make bench`). NFR targets in PRD §12."""

from __future__ import annotations

import time

import pytest

from helm.app.backtest import BacktestSpec, run_backtest
from helm.backtest.vectorized.sweep import sma_cross_sweep
from helm.data.synthetic import frame_to_bars, gbm_frame
from helm.strategy.examples.sma_cross import SmaCross
from tests.factories import SYM, instrument

YEAR_1M = 525_600


@pytest.mark.slow
def test_event_driven_one_year_1m() -> None:
    bars = list(frame_to_bars(gbm_frame(YEAR_1M, start_ns=0, seed=5), SYM, "1m"))
    t = time.perf_counter()
    params = {"fast": 20, "slow": 60, "qty": "0.1"}
    r = run_backtest(
        BacktestSpec(strategy=SmaCross(), instruments=[instrument()], params=params), bars
    )
    dt = time.perf_counter() - t
    print(f"\nevent-driven 1y x 1m: {dt:.1f}s, {len(r.orders)} orders (target < 60 s)")


@pytest.mark.slow
def test_vectorized_sweep_100_sets() -> None:
    df = gbm_frame(YEAR_1M, start_ns=0, seed=5)
    grid = [(f, f + s) for f in range(5, 55, 5) for s in range(10, 110, 10)]
    t = time.perf_counter()
    rows = sma_cross_sweep(df, grid)
    dt = time.perf_counter() - t
    print(f"\nvectorized sweep {len(rows)} sets x 1y 1m: {dt:.2f}s (target < 30 s)")
    assert len(rows) == 100
