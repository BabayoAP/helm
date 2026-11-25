"""Execution algorithms (EXE-5). Phase 2: TWAP.

Pure planning only. The engine owns the schedule, submits each due slice
through the normal intent pipeline (so every slice is risk-checked, journaled
and quantized), defers slices while the engine or strategy is paused, and
drops what is left on stop / flatten / halt. Algos are not resumed after a
restart: a stale parent intent is never re-executed (same policy as I4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from helm.core.models import OrderIntent


def twap_schedule(
    qty: Decimal, slices: int, start_ns: int, duration_ns: int, lot: Decimal
) -> list[tuple[int, Decimal]]:
    """(due_ts, qty) per slice. Slice sizes are floored to the lot size and the
    remainder goes to the last slice; slices that would be zero are dropped."""
    if slices < 1 or qty <= 0 or lot <= 0:
        raise ValueError("slices, qty and lot must be positive")
    each = (qty / slices / lot).to_integral_value(rounding=ROUND_DOWN) * lot
    step = duration_ns // slices
    plan: list[tuple[int, Decimal]] = []
    for i in range(slices):
        q = each if i < slices - 1 else qty - each * (slices - 1)
        if q > 0:
            plan.append((start_ns + i * step, q))
    return plan


@dataclass
class TwapState:
    algo_id: str
    parent: OrderIntent
    strategy_id: str
    schedule: list[tuple[int, Decimal]]
    sent: int = 0
    deferred: int = 0
    rejected: int = 0
    children: list[str] = field(default_factory=lambda: [])

    @property
    def done(self) -> bool:
        return self.sent >= len(self.schedule)

    @property
    def remaining_qty(self) -> Decimal:
        return sum((q for _, q in self.schedule[self.sent :]), Decimal(0))

    def take_next(self, now: int) -> tuple[int, Decimal] | None:
        """The next due slice, or None. A late slice (pause, sparse ticks) pushes the
        rest of the schedule back by the delay, so slices never bunch up into a burst."""
        if self.done or self.schedule[self.sent][0] > now:
            return None
        due_ts, qty = self.schedule[self.sent]
        late = now - due_ts
        if late > 0:
            self.schedule[self.sent + 1 :] = [
                (t + late, q) for t, q in self.schedule[self.sent + 1 :]
            ]
        k = self.sent
        self.sent += 1
        return k, qty

    def child(self, k: int, qty: Decimal) -> OrderIntent:
        return self.parent.model_copy(
            update={"qty": qty, "exec_algo": None, "tag": f"twap:{self.algo_id}:{k}"}
        )
