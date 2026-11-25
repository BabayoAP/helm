"""Truncation lookahead harness (RES-1, TEST-3).

A feature/strategy is lookahead-free iff computing it on data[:t] gives exactly
the first t outputs of computing it on the full data, for every cut t tested.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import polars as pl

from helm.core.models import Bar, OrderIntent


@dataclass(frozen=True)
class LookaheadReport:
    name: str
    cuts: tuple[int, ...]
    failures: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def _same(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a is b
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def check_feature(
    name: str,
    fn: Callable[[pl.DataFrame], pl.Series],
    df: pl.DataFrame,
    cuts: Sequence[int],
    tol: float = 1e-9,
) -> LookaheadReport:
    full = fn(df).to_list()
    failures: list[int] = []
    for t in cuts:
        part = fn(df.head(t)).to_list()
        if len(part) != t or not all(_same(x, y, tol) for x, y in zip(part, full[:t], strict=True)):
            failures.append(t)
    return LookaheadReport(name, tuple(cuts), tuple(failures))


StrategyRun = Callable[[Sequence[Bar]], list[tuple[int, OrderIntent]]]


def check_strategy(
    name: str, run: StrategyRun, bars: Sequence[Bar], cuts: Sequence[int]
) -> LookaheadReport:
    """`run(bars)` returns (bar_index, intent) pairs; intents up to t must not change."""
    full = run(bars)
    failures: list[int] = []
    for t in cuts:
        part = run(bars[:t])
        if part != [x for x in full if x[0] < t]:
            failures.append(t)
    return LookaheadReport(name, tuple(cuts), tuple(failures))


def intents_by_bar(
    strategy_factory: Callable[[], object], params: dict[str, object], instruments: Sequence[object]
) -> StrategyRun:
    """Adapter: run a strategy through the real event-driven engine and record intents per bar."""
    from helm.app.backtest import BacktestSpec, run_backtest_async  # noqa: PLC0415

    def run(bars: Sequence[Bar]) -> list[tuple[int, OrderIntent]]:
        if not bars:
            return []
        spec = BacktestSpec(strategy=strategy_factory(), instruments=instruments, params=params)  # type: ignore[arg-type]
        res = asyncio.run(run_backtest_async(spec, bars))
        index = {b.ts_close: i for i, b in enumerate(bars)}
        return [(index[o.ts_created], o.intent) for o in res.orders]

    return run
