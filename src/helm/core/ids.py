"""Deterministic identifiers (PRD §6.5, I4).

client_order_id = {run_id}-{strategy_id}-{seq}, kept venue-length-safe.
Most venues accept 32-36 chars of [A-Za-z0-9-_]; we cap at 32 and fall back to
a stable hash of the strategy id when the readable form would not fit.
"""

from __future__ import annotations

import hashlib
import re

MAX_COID_LEN = 32
_SAFE = re.compile(r"[^A-Za-z0-9_]")


def _safe(part: str) -> str:
    return _SAFE.sub("_", part)


def make_run_id(seed_material: str) -> str:
    """8-char run id derived from stable material (e.g. config hash + start ts)."""
    return hashlib.sha256(seed_material.encode()).hexdigest()[:8]


def client_order_id(run_id: str, strategy_id: str, seq: int) -> str:
    if seq < 0:
        raise ValueError("seq must be non-negative")
    run = _safe(run_id)
    strat = _safe(strategy_id)
    coid = f"{run}-{strat}-{seq}"
    if len(coid) <= MAX_COID_LEN:
        return coid
    strat_h = hashlib.sha256(strategy_id.encode()).hexdigest()[:8]
    coid = f"{run[:8]}-{strat_h}-{seq}"
    if len(coid) > MAX_COID_LEN:
        raise ValueError("sequence number too large for venue-safe client_order_id")
    return coid


class SequenceAllocator:
    """Per-strategy monotonically increasing sequence, seeded from the journal on recovery."""

    def __init__(self, start: dict[str, int] | None = None) -> None:
        self._next: dict[str, int] = dict(start or {})

    def next(self, strategy_id: str) -> int:
        n = self._next.get(strategy_id, 0)
        self._next[strategy_id] = n + 1
        return n

    def observe(self, strategy_id: str, seq: int) -> None:
        """Ensure future seqs are > any seq already used (journal replay)."""
        if seq + 1 > self._next.get(strategy_id, 0):
            self._next[strategy_id] = seq + 1
