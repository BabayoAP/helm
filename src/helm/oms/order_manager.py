"""Order manager: idempotent ids, legal transitions, fill dedupe, journal-first (I4).

Every state change is journaled *before* the caller performs the side effect it
describes (e.g. `mark_pending` is journaled before `broker.submit`). On restart,
`recover()` rebuilds orders, the fill-dedupe set and the id sequences from the
journal so no client_order_id is ever reused.
"""

from __future__ import annotations

from decimal import Decimal

from helm.core.clock import Clock
from helm.core.enums import OrderEventKind, OrderState
from helm.core.errors import IllegalTransitionError
from helm.core.events import OrderEvent
from helm.core.ids import SequenceAllocator, client_order_id
from helm.core.models import Fill, Order, OrderIntent
from helm.oms.journal import Journal
from helm.oms.state_machine import is_stale, transition

_KIND = {
    OrderState.NEW: OrderEventKind.SUBMITTED,
    OrderState.PENDING_SUBMIT: OrderEventKind.SUBMITTED,
    OrderState.ACCEPTED: OrderEventKind.ACCEPTED,
    OrderState.PARTIALLY_FILLED: OrderEventKind.PARTIAL,
    OrderState.FILLED: OrderEventKind.FILLED,
    OrderState.CANCELED: OrderEventKind.CANCELED,
    OrderState.REJECTED: OrderEventKind.REJECTED,
    OrderState.EXPIRED: OrderEventKind.EXPIRED,
    OrderState.UNKNOWN: OrderEventKind.UNKNOWN,
}


class OrderManager:
    def __init__(
        self,
        *,
        run_id: str,
        account_id: str,
        journal: Journal,
        clock: Clock,
        seqs: SequenceAllocator | None = None,
    ) -> None:
        self.run_id = run_id
        self.account_id = account_id
        self.journal = journal
        self.clock = clock
        self.seqs = seqs or SequenceAllocator()
        self._orders: dict[str, Order] = {}
        self._fill_keys: set[tuple[str, str]] = set()
        self.stale_events = 0
        self.illegal_events = 0
        self.orphan_fills = 0

    # ---------------------------------------------------------------- journal
    def _record(self, order: Order, reason: str = "") -> None:
        ev = OrderEvent(
            kind=_KIND[order.state],
            client_order_id=order.client_order_id,
            state=order.state,
            order=order,
            reason=reason,
            ts=self.clock.now(),
        )
        self.journal.append(
            ev, ts=ev.ts, run_id=self.run_id, account_id=self.account_id, key=order.client_order_id
        )
        self._orders[order.client_order_id] = order

    def _set_state(self, coid: str, state: OrderState, reason: str = "", **upd: object) -> Order:
        cur = self.get(coid)
        transition(cur.state, state)
        new = cur.model_copy(update={"state": state, "ts_updated": self.clock.now(), **upd})
        self._record(new, reason)
        return new

    # -------------------------------------------------------------- lifecycle
    def create(self, intent: OrderIntent) -> Order:
        seq = self.seqs.next(intent.strategy_id)
        now = self.clock.now()
        order = Order(
            client_order_id=client_order_id(self.run_id, intent.strategy_id, seq),
            account_id=self.account_id,
            intent=intent,
            ts_created=now,
            ts_updated=now,
        )
        self._record(order)
        return order

    def mark_pending(self, coid: str) -> Order:
        return self._set_state(coid, OrderState.PENDING_SUBMIT)

    def mark_unknown(self, coid: str, reason: str) -> Order:
        return self._set_state(coid, OrderState.UNKNOWN, reason)

    def mark_rejected(self, coid: str, reason: str) -> Order:
        return self._set_state(coid, OrderState.REJECTED, reason)

    def reject_new(self, coid: str, reason: str) -> Order:
        return self._set_state(coid, OrderState.REJECTED, reason)

    def apply_event(self, ev: OrderEvent) -> Order | None:
        """Apply a venue order update. Returns the new order, or None if stale."""
        cur = self.get(ev.client_order_id)
        if ev.state in (OrderState.PARTIALLY_FILLED, OrderState.FILLED):
            # fills (deduped by trade id) drive filled_qty and fill states; a fill-state
            # event only tells us the venue has the order
            if cur.state in (OrderState.PENDING_SUBMIT, OrderState.UNKNOWN):
                return self._set_state(
                    ev.client_order_id,
                    OrderState.ACCEPTED,
                    "implied by fill event",
                    venue_order_id=ev.order.venue_order_id or cur.venue_order_id,
                )
            self.stale_events += 1
            return None
        if is_stale(cur.state, ev.state):
            self.stale_events += 1
            return None
        # fills drive filled_qty; a FILLED/PARTIAL event without its fill yet only moves state
        try:
            return self._set_state(
                ev.client_order_id,
                ev.state,
                ev.reason,
                venue_order_id=ev.order.venue_order_id or cur.venue_order_id,
            )
        except IllegalTransitionError:
            self.illegal_events += 1
            raise

    def adopt_state(self, venue_order: Order, reason: str) -> Order | None:
        """Resolve UNKNOWN (or reconcile) from a REST lookup of the venue's view."""
        cur = self.get(venue_order.client_order_id)
        if cur.state is venue_order.state or is_stale(cur.state, venue_order.state):
            return None
        return self._set_state(
            cur.client_order_id,
            venue_order.state,
            reason,
            venue_order_id=venue_order.venue_order_id,
        )

    def on_fill(self, fill: Fill) -> bool:
        """Apply a fill once. Returns False for duplicates or fills of unknown orders."""
        if fill.dedup_key in self._fill_keys:
            return False
        if fill.client_order_id not in self._orders:
            self.orphan_fills += 1
            return False
        cur = self._orders[fill.client_order_id]
        filled = cur.filled_qty + fill.qty
        state = OrderState.FILLED if filled >= cur.intent.qty else OrderState.PARTIALLY_FILLED
        # a fill may arrive before the ack (reordered stream): PENDING/UNKNOWN -> PARTIAL/FILLED
        # is legal. A fill on a terminal order (overfill, fill after cancel) is illegal: raise
        # before anything is recorded so the caller alerts and reconciles.
        if state is not cur.state or cur.state.is_terminal:
            try:
                transition(cur.state, state)
            except IllegalTransitionError:
                self.illegal_events += 1
                raise
        self._fill_keys.add(fill.dedup_key)
        self.journal.append(
            fill,
            ts=fill.ts,
            run_id=self.run_id,
            account_id=self.account_id,
            key=fill.client_order_id,
        )
        avg = (cur.avg_px * cur.filled_qty + fill.price * fill.qty) / filled
        self._record(
            cur.model_copy(
                update={
                    "filled_qty": filled,
                    "avg_px": avg,
                    "fees": cur.fees + fill.fee,
                    "ts_updated": self.clock.now(),
                    "state": state,
                }
            )
        )
        return True

    # ---------------------------------------------------------------- queries
    def get(self, coid: str) -> Order:
        return self._orders[coid]

    def find(self, coid: str) -> Order | None:
        return self._orders.get(coid)

    def all(self) -> list[Order]:
        return list(self._orders.values())

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state.is_open]

    def working_orders(self) -> list[Order]:
        """Orders that may still execute: open, pending, or unknown."""
        return [
            o
            for o in self._orders.values()
            if not o.state.is_terminal and o.state is not OrderState.NEW
        ]

    def unknown_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state is OrderState.UNKNOWN]

    def filled_qty_total(self) -> Decimal:
        return sum((o.filled_qty for o in self._orders.values()), Decimal(0))

    # --------------------------------------------------------------- recovery
    @classmethod
    def recover(
        cls, *, run_id: str, account_id: str, journal: Journal, clock: Clock
    ) -> OrderManager:
        om = cls(run_id=run_id, account_id=account_id, journal=journal, clock=clock)
        for entry in journal.entries(kinds=("OrderEvent", "Fill"), account_id=account_id):
            ev = entry.decode()
            if isinstance(ev, OrderEvent):
                om._orders[ev.client_order_id] = ev.order
                seq = ev.client_order_id.rpartition("-")[2]
                if seq.isdigit():
                    om.seqs.observe(ev.order.intent.strategy_id, int(seq))
            elif isinstance(ev, Fill):
                om._fill_keys.add(ev.dedup_key)
        return om
