"""Clocks swapped per mode (PRD §6.4, §6.6). All times are UTC integer ns.

The wall clock (`LiveClock`) lives in helm.infra.clock so that nothing reachable
from strategy code imports `time` (I3, import contracts).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


@runtime_checkable
class Clock(Protocol):
    def now(self) -> int: ...


class SimClock:
    """Deterministic clock driven by the backtest/replay loop. Never goes backwards."""

    def __init__(self, start_ns: int = 0) -> None:
        self._now = start_ns

    def now(self) -> int:
        return self._now

    def advance_to(self, ts_ns: int) -> None:
        if ts_ns < self._now:
            raise ValueError(f"SimClock cannot go backwards ({ts_ns} < {self._now})")
        self._now = ts_ns

    def advance_by(self, delta_ns: int) -> None:
        self.advance_to(self._now + delta_ns)


def ms(n: float) -> int:
    return int(n * NS_PER_MS)


def seconds(n: float) -> int:
    return int(n * NS_PER_S)
