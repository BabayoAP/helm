"""Execution algorithms (EXE-5): TWAP (Phase 2), VWAP by participation and Iceberg
(Phase 3).

Each algo only decides *what to send next*; the engine owns the rest for all of
them: every child is an ordinary intent (quantized, risk-checked, journaled), at
most one child per algo per engine tick, nothing is sent while the engine or the
strategy is paused, and stop / flatten / halt drop what is left. Algos are not
resumed after a restart: a stale parent is never re-executed (same policy as I4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from helm.core.enums import OrderState
from helm.core.models import Bar, IcebergSpec, OrderIntent, VwapSpec

ZERO = Decimal(0)


def floor_lot(qty: Decimal, lot: Decimal) -> Decimal:
    return (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot


def twap_schedule(
    qty: Decimal, slices: int, start_ns: int, duration_ns: int, lot: Decimal
) -> list[tuple[int, Decimal]]:
    """(due_ts, qty) per slice. Slice sizes are floored to the lot size and the
    remainder goes to the last slice; slices that would be zero are dropped."""
    if slices < 1 or qty <= 0 or lot <= 0:
        raise ValueError("slices, qty and lot must be positive")
    each = floor_lot(qty / slices, lot)
    step = duration_ns // slices
    plan: list[tuple[int, Decimal]] = []
    for i in range(slices):
        q = each if i < slices - 1 else qty - each * (slices - 1)
        if q > 0:
            plan.append((start_ns + i * step, q))
    return plan


class AlgoView(Protocol):
    """What an algo may look at: time, the last closed bar, its own children."""

    def now(self) -> int: ...

    def last_bar(self, symbol: str) -> Bar | None: ...

    def order_state(self, client_order_id: str) -> OrderState | None: ...


@dataclass
class AlgoBase:
    algo_id: str
    parent: OrderIntent
    strategy_id: str
    sent: int = 0
    sent_qty: Decimal = ZERO
    deferred: int = 0
    rejected: int = 0
    stopped: str = ""
    children: list[str] = field(default_factory=lambda: [])

    @property
    def remaining_qty(self) -> Decimal:
        return self.parent.qty - self.sent_qty

    @property
    def done(self) -> bool:
        return bool(self.stopped) or self.remaining_qty <= 0

    def next_qty(self, view: AlgoView) -> Decimal | None:  # pragma: no cover - abstract
        raise NotImplementedError

    def record(self, view: AlgoView, qty: Decimal, coid: str | None) -> None:
        self.sent += 1
        self.sent_qty += qty
        if coid is None:
            self.rejected += 1
        else:
            self.children.append(coid)

    def child(self, qty: Decimal) -> OrderIntent:
        return self.parent.model_copy(
            update={
                "qty": qty,
                "exec_algo": None,
                "tag": f"{self.algo_id.rsplit('-', 2)[-2]}:{self.algo_id}:{self.sent}",
            }
        )


@dataclass
class TwapState(AlgoBase):
    schedule: list[tuple[int, Decimal]] = field(default_factory=lambda: [])

    @property
    def done(self) -> bool:
        return bool(self.stopped) or self.sent >= len(self.schedule)

    @property
    def remaining_qty(self) -> Decimal:
        return sum((q for _, q in self.schedule[self.sent :]), ZERO)

    def next_qty(self, view: AlgoView) -> Decimal | None:
        if self.done or self.schedule[self.sent][0] > view.now():
            return None
        return self.schedule[self.sent][1]

    def record(self, view: AlgoView, qty: Decimal, coid: str | None) -> None:
        """A late slice (pause, sparse ticks) pushes the rest of the schedule back by
        the delay, so slices never bunch up into a burst."""
        late = view.now() - self.schedule[self.sent][0]
        if late > 0:
            self.schedule[self.sent + 1 :] = [
                (t + late, q) for t, q in self.schedule[self.sent + 1 :]
            ]
        super().record(view, qty, coid)


@dataclass
class VwapState(AlgoBase):
    spec: VwapSpec | None = None
    lot: Decimal = Decimal("0.00000001")
    start_ns: int = 0
    last_bar_ts: int = 0

    def next_qty(self, view: AlgoView) -> Decimal | None:
        assert self.spec is not None
        if self.done:
            return None
        if view.now() >= self.start_ns + self.spec.duration_s * 1_000_000_000:
            if not self.spec.finish:
                self.stopped = "duration over (finish=false): remainder dropped"
                return None
            return self.remaining_qty  # catch up at the end
        bar = view.last_bar(self.parent.symbol)
        if bar is None or bar.ts_close <= self.last_bar_ts:
            return None
        self.last_bar_ts = bar.ts_close  # consider each bar once, even if it yields 0
        q = min(floor_lot(self.spec.participation * bar.volume, self.lot), self.remaining_qty)
        return q if q > 0 else None


@dataclass
class IcebergState(AlgoBase):
    spec: IcebergSpec | None = None
    lot: Decimal = Decimal("0.00000001")

    def next_qty(self, view: AlgoView) -> Decimal | None:
        assert self.spec is not None
        if self.done:
            return None
        if self.children:
            st = view.order_state(self.children[-1])
            if st is None or not st.is_terminal:
                return None  # the visible slice is still working
            if st is not OrderState.FILLED:
                self.stopped = f"slice {self.children[-1]} ended {st.value}: iceberg stopped"
                return None
        q = min(floor_lot(self.spec.display_qty, self.lot), self.remaining_qty)
        return q if q > 0 else self.remaining_qty

    def record(self, view: AlgoView, qty: Decimal, coid: str | None) -> None:
        super().record(view, qty, coid)
        if coid is None:  # a rejected slice would just be re-sent every tick
            self.stopped = "slice rejected by risk/venue: iceberg stopped"
