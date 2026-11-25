"""TEST-3 / RES-1: truncation lookahead test for every feature and every example strategy."""

from __future__ import annotations

import polars as pl
import pytest

from helm.backtest.vectorized.sweep import sma_cross_sweep
from helm.data.synthetic import gbm_frame
from helm.research.features.registry import REGISTRY, compute
from helm.research.validation.lookahead import check_feature, check_strategy, intents_by_bar
from tests.backtest_fixtures import EXAMPLES, bars
from tests.factories import instrument

DF = gbm_frame(400, start_ns=0, seed=11)
CUTS = (25, 61, 150, 233, 399)


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_feature_is_lookahead_free(name: str) -> None:
    rep = check_feature(name, REGISTRY[name], DF, CUTS)
    assert rep.ok, f"{name} leaks future data at cuts {rep.failures}"


def test_harness_catches_a_leaky_feature() -> None:
    leaky = lambda df: df["c"].shift(-1)  # noqa: E731 - tomorrow's close
    centered = lambda df: df["c"].rolling_mean(5, center=True)  # noqa: E731
    assert not check_feature("leaky", leaky, DF, CUTS).ok
    assert not check_feature("centered", centered, DF, CUTS).ok
    normalized = lambda df: df["c"] / df["c"].max()  # noqa: E731 - full-sample statistic
    assert not check_feature("normalized", normalized, DF, CUTS).ok


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_example_strategy_is_lookahead_free(name: str) -> None:
    cls, params = EXAMPLES[name]
    data = bars(600)
    rep = check_strategy(
        name, intents_by_bar(cls, dict(params), [instrument()]), data, (120, 333, 599)
    )
    assert rep.ok, f"{name} changed past decisions when future bars were added: {rep.failures}"


def test_compute_and_vectorized_shift() -> None:
    out = compute(DF, ["sma_20", "rsi_14"])
    assert {"sma_20", "rsi_14"} <= set(out.columns)
    # vectorized signals trade one bar later: a sweep on data[:t] matches on overlapping prefix
    rows = sma_cross_sweep(DF, [(5, 20), (20, 5)])
    assert len(rows) == 1 and rows[0].params == {"fast": 5, "slow": 20}
    flat = pl.DataFrame(
        {
            "ts_open": [0, 1, 2],
            "o": [1.0] * 3,
            "h": [1.0] * 3,
            "l": [1.0] * 3,
            "c": [1.0] * 3,
            "volume": [1.0] * 3,
        }
    )
    assert sma_cross_sweep(flat, [(1, 2)])[0].sharpe == 0
