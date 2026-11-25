"""Vectorized screening sweeps (BT-1). Screening-grade only: never promotion evidence.

Signals are computed on closed bars and automatically shifted one bar, so a
signal at bar t trades at bar t+1 (I3). Costs are a fixed bps per unit of
position change.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class SweepRow:
    params: dict[str, int]
    total_return: float
    sharpe: float
    trades: int
    exposure: float


def sma_cross_sweep(
    df: pl.DataFrame,
    grid: Iterable[tuple[int, int]],
    *,
    cost_bps: float = 10.0,
    periods_per_year: float = 365 * 1440,
) -> list[SweepRow]:
    """Long-only SMA cross over a bar frame for every (fast, slow) in grid."""
    c = df["c"]
    ret = c.pct_change().fill_null(0.0)
    cost = cost_bps / 10_000
    cache: dict[int, pl.Series] = {}

    def ma(n: int) -> pl.Series:
        if n not in cache:
            cache[n] = c.rolling_mean(n)
        return cache[n]

    rows: list[SweepRow] = []
    for fast, slow in grid:
        if fast >= slow:
            continue
        signal = (ma(fast) > ma(slow)).cast(pl.Float64).fill_null(0.0)
        pos = signal.shift(1).fill_null(0.0)  # trade on the next bar
        turns = pos.diff().abs().fill_null(0.0)
        strat = pos * ret - turns * cost
        equity = (1 + strat).cum_prod()
        mean = float(strat.mean() or 0.0)  # type: ignore[arg-type]
        sd = float(strat.std() or 0.0)  # type: ignore[arg-type]
        rows.append(
            SweepRow(
                params={"fast": fast, "slow": slow},
                total_return=float(equity[-1]) - 1 if len(equity) else 0.0,
                sharpe=mean / sd * math.sqrt(periods_per_year) if sd > 0 else 0.0,
                trades=int(turns.sum() // 2),
                exposure=float(pos.mean() or 0.0),  # type: ignore[arg-type]
            )
        )
    return rows
