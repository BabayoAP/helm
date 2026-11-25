"""Feature factory (RES-1): registry of vectorized Polars features.

Every feature is a pure function of a bar frame (ts_open, o, h, l, c, volume)
returning one Series aligned to the input rows, computed from rows <= t only.
The truncation lookahead test (TEST-3) runs over every registered feature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import polars as pl

FeatureFn = Callable[[pl.DataFrame], pl.Series]


@dataclass(frozen=True)
class Feature:
    name: str
    fn: FeatureFn
    params: dict[str, Any] = field(default_factory=lambda: {})
    warmup: int = 0

    def __call__(self, df: pl.DataFrame) -> pl.Series:
        return self.fn(df).alias(self.name)


REGISTRY: dict[str, Feature] = {}


def register(name: str, warmup: int = 0, **params: Any) -> Callable[[FeatureFn], FeatureFn]:
    def deco(fn: FeatureFn) -> FeatureFn:
        REGISTRY[name] = Feature(name, fn, params, warmup)
        return fn

    return deco


def compute(df: pl.DataFrame, names: list[str] | None = None) -> pl.DataFrame:
    feats = [REGISTRY[n] for n in (names or sorted(REGISTRY))]
    return df.with_columns([f(df) for f in feats])


# ---------------------------------------------------------------- seed set
@register("ret_1")
def ret_1(df: pl.DataFrame) -> pl.Series:
    return df["c"].pct_change()


@register("logret_1")
def logret_1(df: pl.DataFrame) -> pl.Series:
    return (df["c"] / df["c"].shift(1)).log()


@register("sma_20", warmup=19)
def sma_20(df: pl.DataFrame) -> pl.Series:
    return df["c"].rolling_mean(20)


@register("ema_20", warmup=19)
def ema_20(df: pl.DataFrame) -> pl.Series:
    return df["c"].ewm_mean(span=20, adjust=False)


@register("rsi_14", warmup=14)
def rsi_14(df: pl.DataFrame) -> pl.Series:
    d = df["c"].diff()
    gain = d.clip(lower_bound=0).rolling_mean(14)
    loss = (-d).clip(lower_bound=0).rolling_mean(14)
    return 100 - 100 / (1 + gain / loss)


@register("atr_14", warmup=14)
def atr_14(df: pl.DataFrame) -> pl.Series:
    prev_c = df["c"].shift(1)
    tr = pl.DataFrame(
        {"a": df["h"] - df["l"], "b": (df["h"] - prev_c).abs(), "c": (df["l"] - prev_c).abs()}
    ).max_horizontal()
    return tr.rolling_mean(14)


@register("rvol_60", warmup=60)
def rvol_60(df: pl.DataFrame) -> pl.Series:
    return logret_1(df).rolling_std(60)


@register("zscore_50", warmup=49)
def zscore_50(df: pl.DataFrame) -> pl.Series:
    c = df["c"]
    return (c - c.rolling_mean(50)) / c.rolling_std(50)


@register("bb_pctb_20", warmup=19)
def bb_pctb_20(df: pl.DataFrame) -> pl.Series:
    c = df["c"]
    mid = c.rolling_mean(20)
    sd = c.rolling_std(20)
    return (c - (mid - 2 * sd)) / (4 * sd)


@register("volume_z_60", warmup=59)
def volume_z_60(df: pl.DataFrame) -> pl.Series:
    """Volume anomaly z-score vs trailing window (TRK-1)."""
    v = df["volume"]
    return (v - v.rolling_mean(60)) / v.rolling_std(60)


@register("range_pos_20", warmup=19)
def range_pos_20(df: pl.DataFrame) -> pl.Series:
    hi = df["h"].rolling_max(20)
    lo = df["l"].rolling_min(20)
    return (df["c"] - lo) / (hi - lo)
