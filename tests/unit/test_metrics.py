"""BT-6 metrics vs hand-computed fixtures."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from helm.backtest.metrics.metrics import (
    NS_PER_DAY,
    block_bootstrap,
    cagr,
    compute_metrics,
    daily_equity,
    deflated_sharpe,
    expected_max_sharpe,
    max_drawdown,
    moments,
    probabilistic_sharpe,
    returns,
    round_trips,
    sharpe,
    sortino,
)
from helm.core.enums import Liquidity, Side
from helm.core.models import Fill

D = Decimal


def test_returns_sharpe_sortino_hand_computed() -> None:
    eq = [100.0, 110.0, 99.0, 108.9]
    r = returns(eq)
    assert r == pytest.approx([0.1, -0.1, 0.1])
    mean, sd = 0.1 / 3, math.sqrt(((0.1 - 0.1 / 3) ** 2 * 2 + (-0.1 - 0.1 / 3) ** 2) / 2)
    assert sharpe(r, 1) == pytest.approx(mean / sd)
    assert sharpe(r, 365) == pytest.approx(mean / sd * math.sqrt(365))
    assert sortino(r, 1) == pytest.approx(mean / math.sqrt(0.01 / 2))
    assert sharpe([0.01], 1) == 0 and sortino([0.01], 1) == 0 and sortino([0.1, 0.1], 1) == 0


def test_drawdown_and_cagr() -> None:
    d = NS_PER_DAY
    curve = [(0, 100.0), (d, 120.0), (2 * d, 90.0), (3 * d, 96.0), (4 * d, 121.0)]
    depth, dur = max_drawdown(curve)
    assert depth == pytest.approx(0.25) and dur == 2 * d
    year = int(365.25 * d)
    assert cagr([(0, 100.0), (year, 121.0)]) == pytest.approx(0.21)
    assert cagr([(0, 100.0), (2 * year, 121.0)]) == pytest.approx(0.1)
    assert (
        cagr([(0, 100.0)]) == 0
        and cagr([(0, 100.0), (0, 50.0)]) == 0
        and cagr([(0, 100.0), (year, 0.0)]) == -1
    )


def test_daily_equity_keys_are_day_starts() -> None:
    curve = [(5, D(1)), (NS_PER_DAY + 7, D(2)), (NS_PER_DAY + 9, D(3))]
    assert daily_equity(curve) == [(0, 1.0), (NS_PER_DAY, 3.0)]


def test_psr_dsr_properties() -> None:
    assert probabilistic_sharpe(0.0, 100, 0.0, 3.0) == pytest.approx(0.5)
    assert probabilistic_sharpe(0.1, 400, 0.0, 3.0) > 0.97
    assert probabilistic_sharpe(0.1, 1, 0.0, 3.0) == 0
    assert probabilistic_sharpe(5.0, 100, 5.0, 3.0) == 0  # degenerate denominator
    assert (
        expected_max_sharpe(1, 0.01) == 0
        and expected_max_sharpe(100, 0.01) > expected_max_sharpe(10, 0.01) > 0
    )
    # more trials => lower deflated Sharpe for the same observed Sharpe
    assert (
        deflated_sharpe(0.1, 400, 0, 3, 1)
        > deflated_sharpe(0.1, 400, 0, 3, 50)
        > deflated_sharpe(0.1, 400, 0, 3, 1000)
    )
    skew, kurt = moments([0.0] * 30 + [10.0])  # one big outlier: right-skewed, fat-tailed
    assert skew > 1 and kurt > 3
    assert moments([1.0]) == (0.0, 3.0)


def _f(side: Side, qty: str, px: str, ts: int, fee: str = "0") -> Fill:
    return Fill(
        client_order_id="c",
        venue="v",
        trade_id=f"t{ts}",
        symbol="v:A/B",
        side=side,
        price=D(px),
        qty=D(qty),
        fee=D(fee),
        fee_ccy="B",
        liquidity=Liquidity.TAKER,
        ts=ts,
    )


def test_round_trips_fifo_with_fees_and_flip() -> None:
    fills = [
        _f(Side.BUY, "1", "100", 1, "1"),
        _f(Side.BUY, "1", "110", 2, "1"),
        _f(Side.SELL, "3", "120", 3, "3"),
        _f(Side.BUY, "1", "100", 4, "0"),
    ]
    trips = round_trips(fills)
    assert [round(t.pnl, 6) for t in trips] == [
        18.0,
        8.0,
        19.0,
    ]  # (20-1-1), (10-1-1), short 120->100 minus 1 fee
    assert trips[0].ret == pytest.approx(0.18)


def test_compute_metrics_end_to_end_and_bootstrap() -> None:
    d = NS_PER_DAY
    curve = [(i * d, D(100 + i)) for i in range(30)]
    fills = [
        _f(Side.BUY, "1", "100", 1),
        _f(Side.SELL, "1", "105", 2),
        _f(Side.BUY, "1", "105", 3),
        _f(Side.SELL, "1", "100", 4),
    ]
    m = compute_metrics(
        curve, fills, exposure_flags=[True, False], fees_paid=D(2), turnover=D(410), n_trials=5
    )
    assert m.trade_count == 2 and m.win_rate == 0.5 and m.profit_factor == pytest.approx(1.0)
    assert m.max_drawdown == 0 and m.calmar == 0 and m.exposure == 0.5 and m.fees_paid == 2
    assert m.total_return == pytest.approx(0.29) and m.sharpe > 0
    assert m.to_dict()["n_trials"] == 5
    bb = block_bootstrap([0.1, -0.05, 0.02], sims=200, seed=1)
    assert bb["p05"] <= bb["p50"] <= bb["p95"] and 0 <= bb["p_loss"] <= 1
    assert block_bootstrap([], seed=1)["p50"] == 0
    assert block_bootstrap([0.1, -0.05], sims=50, seed=3) == block_bootstrap(
        [0.1, -0.05], sims=50, seed=3
    )
    empty = compute_metrics([], [])
    assert empty.total_return == 0 and empty.profit_factor == 0
    only_wins = compute_metrics(curve, fills[:2])
    assert math.isinf(only_wins.profit_factor)
