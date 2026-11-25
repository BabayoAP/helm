"""Reconciliation: internal state vs broker state (EXE-4, RISK-13).

Mismatch if any symbol's position differs by more than `tolerance_pct` of its
notional, or the broker has an open order we do not know (or vice versa).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from helm.core.models import Order, Position

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class ReconcileResult:
    mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def describe(self) -> str:
        return "; ".join(self.mismatches)


def reconcile(
    internal_positions: Mapping[str, Decimal],
    broker_positions: Sequence[Position],
    internal_open: Sequence[Order],
    broker_open: Sequence[Order],
    marks: Mapping[str, Decimal],
    tolerance_pct: Decimal,
) -> ReconcileResult:
    out: list[str] = []
    venue = {p.symbol: p.qty for p in broker_positions}
    for sym in sorted(set(venue) | {s for s, q in internal_positions.items() if q != 0}):
        ours = internal_positions.get(sym, Decimal(0))
        theirs = venue.get(sym, Decimal(0))
        if ours == theirs:
            continue
        mark = marks.get(sym)
        base = max(abs(ours), abs(theirs))
        if mark is None or base == 0:
            out.append(f"{sym}: position {ours} vs venue {theirs} (no mark)")
            continue
        diff_pct = abs(ours - theirs) / base * HUNDRED
        if diff_pct > tolerance_pct:
            out.append(f"{sym}: position {ours} vs venue {theirs} ({diff_pct:.3f}%)")
    ours_ids = {o.client_order_id for o in internal_open}
    theirs_ids = {o.client_order_id for o in broker_open}
    for coid in sorted(theirs_ids - ours_ids):
        out.append(f"unknown venue order {coid}")
    for coid in sorted(ours_ids - theirs_ids):
        out.append(f"order {coid} open internally but not at venue")
    return ReconcileResult(tuple(out))
