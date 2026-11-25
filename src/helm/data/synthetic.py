"""Seeded synthetic bars (GBM with regime noise) for demos, golden files and benchmarks."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from decimal import Decimal

import polars as pl

from helm.core.models import Bar
from helm.core.timeframes import timeframe_ns


def gbm_frame(
    n: int,
    *,
    start_ns: int,
    timeframe: str = "1m",
    s0: float = 100.0,
    mu: float = 0.0,
    sigma: float = 0.6,
    seed: int = 0,
) -> pl.DataFrame:
    """OHLCV frame of n bars. sigma/mu are annualized."""
    rng = random.Random(seed)
    step = timeframe_ns(timeframe)
    dt = step / (365 * 86_400 * 10**9)
    ts, o, h, l, c, v = [], [], [], [], [], []  # noqa: E741
    px = s0
    for i in range(n):
        open_ = px
        z = rng.gauss(0, 1)
        close = open_ * math.exp((mu - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * z)
        wick = abs(rng.gauss(0, sigma * math.sqrt(dt))) * open_
        hi = max(open_, close) + wick
        lo = max(1e-9, min(open_, close) - wick * rng.random())
        ts.append(start_ns + i * step)
        o.append(round(open_, 2))
        c.append(round(close, 2))
        h.append(round(max(hi, open_, close), 2))
        l.append(round(min(lo, open_, close), 2))
        v.append(round(abs(rng.gauss(10, 3)) + 0.1, 4))
        px = close
    df = pl.DataFrame({"ts_open": ts, "o": o, "h": h, "l": l, "c": c, "volume": v})
    # rounding can break OHLC sanity by a cent: repair
    return df.with_columns(
        pl.max_horizontal("h", "o", "c").alias("h"), pl.min_horizontal("l", "o", "c").alias("l")
    )


def frame_to_bars(df: pl.DataFrame, symbol: str, timeframe: str) -> Iterator[Bar]:
    step = timeframe_ns(timeframe)
    for ts, o, h, l, c, v in df.select("ts_open", "o", "h", "l", "c", "volume").iter_rows():  # noqa: E741
        yield Bar(
            symbol=symbol,
            timeframe=timeframe,
            ts_open=ts,
            ts_close=ts + step,
            o=Decimal(repr(o)),
            h=Decimal(repr(h)),
            l=Decimal(repr(l)),
            c=Decimal(repr(c)),
            volume=Decimal(repr(v)),
        )
