"""Timeframe strings <-> nanosecond durations."""

from __future__ import annotations

from helm.core.clock import NS_PER_S

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_ns(tf: str) -> int:
    """'1m' -> 60e9 ns. Supports s/m/h/d/w."""
    if len(tf) < 2 or tf[-1] not in _UNITS or not tf[:-1].isdigit() or int(tf[:-1]) <= 0:
        raise ValueError(f"bad timeframe {tf!r}")
    return int(tf[:-1]) * _UNITS[tf[-1]] * NS_PER_S


def floor_ts(ts_ns: int, tf: str) -> int:
    step = timeframe_ns(tf)
    return ts_ns - (ts_ns % step)
