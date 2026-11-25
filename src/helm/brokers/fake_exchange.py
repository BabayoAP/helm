"""FakeExchange: an in-memory venue for integration and chaos tests (TEST-1).

Implements the `Broker` protocol plus fault injection: latency, strict rate
limits (429), disconnects, partial fills, duplicate / out-of-order user-stream
events, ambiguous submit responses, and random rejects. Like a real venue it
rejects a reused client_order_id, which is the last line of defence against
duplicate orders.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from helm.core.clock import Clock
from helm.core.enums import Liquidity, OrderEventKind, OrderState, OrderType, Side
from helm.core.errors import AmbiguousSubmitError, BrokerError, DisconnectedError, RateLimitedError
from helm.core.events import OrderEvent
from helm.core.models import Balance, Fill, Order, OrderIntent, Position
from helm.core.money import ZERO
from helm.infra.clock import LiveClock


@dataclass
class FakeExchangeConfig:
    venue: str = "fake"
    quote_ccy: str = "USD"
    starting_cash: Decimal = Decimal("100000")
    fee_rate: Decimal = Decimal("0.001")
    latency_s: float = 0.0
    rate_limit_per_s: int | None = None  # requests allowed per rate_window_s
    rate_window_s: float = 1.0
    p_ambiguous_submit: float = 0.0
    p_reject: float = 0.0
    partial_fill_parts: int = 1
    duplicate_events: bool = False
    reorder_events: bool = False
    seed: int = 0


@dataclass
class _Pos:
    qty: Decimal = ZERO
    avg_px: Decimal = ZERO
    realized: Decimal = ZERO


@dataclass
class FakeExchange:
    account_id: str = "fake-acct"
    config: FakeExchangeConfig = field(default_factory=FakeExchangeConfig)
    clock: Clock = field(default_factory=LiveClock)
    is_live: bool = False

    def __post_init__(self) -> None:
        self.venue = self.config.venue
        self._rng = random.Random(self.config.seed)
        self._orders: dict[str, Order] = {}
        self._prices: dict[str, tuple[Decimal, Decimal]] = {}
        self._pos: dict[str, _Pos] = {}
        self._cash = self.config.starting_cash
        self._events: asyncio.Queue[OrderEvent | Fill] = asyncio.Queue()
        self._req_times: deque[float] = deque()
        self._trade_seq = 0
        self.disconnected = False
        self.submit_calls: dict[str, int] = {}
        self.accepted_submits: dict[str, int] = {}
        self.rate_limited_count = 0

    # ------------------------------------------------------------------ helpers
    def set_price(self, symbol: str, bid: Decimal, ask: Decimal) -> None:
        self._prices[symbol] = (bid, ask)
        for order in list(self._orders.values()):
            if order.intent.symbol == symbol and order.state.is_open:
                self._try_match(order)

    def inject_position(self, symbol: str, qty: Decimal, avg_px: Decimal) -> None:
        """Simulate external state (e.g. manual trade on the venue) for reconciliation tests."""
        self._pos[symbol] = _Pos(qty=qty, avg_px=avg_px)

    async def _io(self, weight: int = 1) -> None:
        if self.disconnected:
            raise DisconnectedError(f"{self.venue}: disconnected")
        if self.config.latency_s:
            await asyncio.sleep(self.config.latency_s)
        limit = self.config.rate_limit_per_s
        if limit is not None:
            now = asyncio.get_running_loop().time()
            while self._req_times and now - self._req_times[0] >= self.config.rate_window_s:
                self._req_times.popleft()
            if len(self._req_times) + weight > limit:
                self.rate_limited_count += 1
                raise RateLimitedError(f"{self.venue}: 429 too many requests", retry_after_s=1.0)
            for _ in range(weight):
                self._req_times.append(now)

    def _emit(self, ev: OrderEvent | Fill) -> None:
        self._events.put_nowait(ev)
        if self.config.duplicate_events and self._rng.random() < 0.5:
            self._events.put_nowait(ev)

    def _order_event(self, order: Order, kind: OrderEventKind, reason: str = "") -> None:
        self._emit(
            OrderEvent(
                kind=kind,
                client_order_id=order.client_order_id,
                state=order.state,
                order=order,
                reason=reason,
                ts=self.clock.now(),
            )
        )

    def _update(self, order: Order, **changes: object) -> Order:
        new = order.model_copy(update={**changes, "ts_updated": self.clock.now()})
        self._orders[order.client_order_id] = new
        return new

    # --------------------------------------------------------------- matching
    def _marketable_price(self, intent: OrderIntent) -> Decimal | None:
        if intent.symbol not in self._prices:
            return None
        bid, ask = self._prices[intent.symbol]
        px = ask if intent.side is Side.BUY else bid
        if intent.type is OrderType.MARKET:
            return px
        if intent.type is OrderType.LIMIT and intent.limit_price is not None:
            if intent.side is Side.BUY and ask <= intent.limit_price:
                return ask
            if intent.side is Side.SELL and bid >= intent.limit_price:
                return bid
        return None

    def _try_match(self, order: Order) -> None:
        px = self._marketable_price(order.intent)
        if px is None:
            return
        parts = max(1, self.config.partial_fill_parts)
        remaining = order.remaining
        if order.intent.reduce_only:
            pos = self._pos.get(order.intent.symbol, _Pos()).qty
            allowed = max(ZERO, -pos if order.intent.side is Side.BUY else pos)
            remaining = min(remaining, allowed)
            if remaining <= 0:
                order = self._update(order, state=OrderState.CANCELED)
                self._order_event(order, OrderEventKind.CANCELED, "reduce_only_nothing_to_close")
                return
        chunk = remaining / parts
        fills: list[Fill] = []
        for i in range(parts):
            qty = chunk if i < parts - 1 else remaining - chunk * (parts - 1)
            if qty <= 0:
                continue
            fills.append(self._fill(order, px, qty))
        if self.config.reorder_events and len(fills) > 1:
            self._rng.shuffle(fills)
        for f in fills:
            self._emit(f)
        current = self._orders[order.client_order_id]
        if order.intent.reduce_only and current.state.is_open:
            current = self._update(current, state=OrderState.CANCELED)
            self._order_event(current, OrderEventKind.CANCELED, "reduce_only_remainder")

    def _fill(self, order: Order, px: Decimal, qty: Decimal) -> Fill:
        intent = order.intent
        self._trade_seq += 1
        fee = (px * qty * self.config.fee_rate).quantize(Decimal("0.00000001"))
        self._apply_position(intent.symbol, intent.side, qty, px)
        self._cash -= fee
        current = self._orders[order.client_order_id]
        filled = current.filled_qty + qty
        avg = (current.avg_px * current.filled_qty + px * qty) / filled
        state = OrderState.FILLED if filled >= intent.qty else OrderState.PARTIALLY_FILLED
        current = self._update(
            current, filled_qty=filled, avg_px=avg, fees=current.fees + fee, state=state
        )
        fill = Fill(
            client_order_id=order.client_order_id,
            venue_order_id=current.venue_order_id,
            venue=self.venue,
            trade_id=f"T{self._trade_seq}",
            symbol=intent.symbol,
            side=intent.side,
            price=px,
            qty=qty,
            fee=fee,
            fee_ccy=self.config.quote_ccy,
            liquidity=Liquidity.TAKER,
            ts=self.clock.now(),
        )
        kind = OrderEventKind.FILLED if state is OrderState.FILLED else OrderEventKind.PARTIAL
        self._order_event(current, kind)
        return fill

    def _apply_position(self, symbol: str, side: Side, qty: Decimal, px: Decimal) -> None:
        p = self._pos.setdefault(symbol, _Pos())
        signed = qty * side.sign
        self._cash -= signed * px
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            total = p.qty + signed
            p.avg_px = (p.avg_px * abs(p.qty) + px * qty) / abs(total)
            p.qty = total
            return
        closing = min(abs(signed), abs(p.qty))
        direction = Decimal(1) if p.qty > 0 else Decimal(-1)
        p.realized += (px - p.avg_px) * closing * direction
        p.qty += signed
        if p.qty == 0:
            p.avg_px = ZERO
        elif (p.qty > 0) != (direction > 0):
            p.avg_px = px

    # ------------------------------------------------------------ Broker API
    async def submit(self, order: Order) -> None:
        coid = order.client_order_id
        self.submit_calls[coid] = self.submit_calls.get(coid, 0) + 1
        await self._io()
        if coid in self._orders:
            raise BrokerError(f"duplicate client_order_id {coid}")
        if self._rng.random() < self.config.p_reject:
            rejected = order.model_copy(update={"state": OrderState.REJECTED})
            self._orders[coid] = rejected
            self._order_event(rejected, OrderEventKind.REJECTED, "venue_reject")
            return
        accepted = order.model_copy(
            update={
                "state": OrderState.ACCEPTED,
                "venue_order_id": f"V{len(self._orders) + 1}",
                "ts_updated": self.clock.now(),
            }
        )
        self._orders[coid] = accepted
        self.accepted_submits[coid] = self.accepted_submits.get(coid, 0) + 1
        self._order_event(accepted, OrderEventKind.ACCEPTED)
        self._try_match(accepted)
        if self._rng.random() < self.config.p_ambiguous_submit:
            raise AmbiguousSubmitError(f"{self.venue}: timeout after submit {coid}")

    async def cancel(self, client_order_id: str) -> None:
        await self._io()
        order = self._orders.get(client_order_id)
        if order is None or not order.state.is_open:
            return
        order = self._update(order, state=OrderState.CANCELED)
        self._order_event(order, OrderEventKind.CANCELED)

    async def get_order(self, client_order_id: str) -> Order | None:
        await self._io()
        return self._orders.get(client_order_id)

    async def open_orders(self) -> list[Order]:
        await self._io()
        return [o for o in self._orders.values() if o.state.is_open]

    async def positions(self) -> list[Position]:
        await self._io()
        return [
            Position(symbol=s, qty=p.qty, avg_px=p.avg_px, realized_pnl=p.realized)
            for s, p in sorted(self._pos.items())
            if p.qty != 0
        ]

    async def balances(self) -> list[Balance]:
        await self._io()
        return [Balance(asset=self.config.quote_ccy, free=self._cash)]

    async def flatten_all(self, *, reduce_only: bool = True) -> None:
        for o in await self.open_orders():
            await self.cancel(o.client_order_id)
        for p in await self.positions():
            side = Side.SELL if p.qty > 0 else Side.BUY
            self._trade_seq += 1
            intent = OrderIntent(
                strategy_id="flatten",
                symbol=p.symbol,
                side=side,
                type=OrderType.MARKET,
                qty=abs(p.qty),
                reduce_only=reduce_only,
            )
            now = self.clock.now()
            await self.submit(
                Order(
                    client_order_id=f"flat-{self._trade_seq}",
                    account_id=self.account_id,
                    intent=intent,
                    ts_created=now,
                    ts_updated=now,
                )
            )

    async def stream(self) -> AsyncIterator[OrderEvent | Fill]:
        while True:
            yield await self._events.get()

    def drain_events(self) -> list[OrderEvent | Fill]:
        out: list[OrderEvent | Fill] = []
        while not self._events.empty():
            out.append(self._events.get_nowait())
        return out
