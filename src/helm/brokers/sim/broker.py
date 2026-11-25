"""SimBroker: simulated venue used by backtest (bar mode) and paper (L1 mode).

Bar mode (BT-3): working orders are matched against the *next* bar with
`BarFillModel`; taker fills pay spread + fixed + square-root impact slippage.
L1 mode (PAP-1): market orders fill at the live quote +/- slippage; limit orders
rest and fill (partially) when live trades print through the limit or the quote
crosses it. Orders reach the sim venue after a seeded latency (BT-5).

Events are queued and drained synchronously by the engine after each market
event, which keeps backtests deterministic (I7). The broker keeps its own
ledger (the "venue's view") so reconciliation has something independent to
compare against.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from enum import StrEnum

from helm.backtest.fills import BarFillModel, FillPolicy
from helm.backtest.friction.models import Friction, FrictionConfig
from helm.core.clock import Clock
from helm.core.enums import Liquidity, OrderEventKind, OrderState, OrderType, Side, TimeInForce
from helm.core.errors import BrokerError
from helm.core.events import OrderEvent
from helm.core.models import Balance, Bar, Fill, Order, OrderIntent, Position, Quote, Trade
from helm.core.money import ZERO
from helm.oms.accounting import Portfolio

BPS = Decimal(10_000)
FUNDING_PERIOD_NS = 8 * 3600 * 10**9


class SimMode(StrEnum):
    BAR = "bar"
    L1 = "l1"


class SimBroker:
    is_live = False

    def __init__(
        self,
        *,
        account_id: str,
        venue: str,
        clock: Clock,
        mode: SimMode = SimMode.BAR,
        friction: FrictionConfig | None = None,
        fill_policy: FillPolicy = FillPolicy.CONSERVATIVE,
        starting_cash: Decimal = Decimal(10_000),
        quote_ccy: str = "USD",
        multipliers: Mapping[str, Decimal] | None = None,
        queue_model: bool = False,
    ) -> None:
        self.account_id = account_id
        self.queue_model = queue_model  # BT-4: queue position for resting limits (L1)
        # size still ahead of each resting order at its price; None = level not at the touch yet
        self._queue_ahead: dict[str, Decimal | None] = {}
        self.venue = venue
        self.clock = clock
        self.mode = mode
        self.friction = Friction(friction or FrictionConfig())
        self.fill_model = BarFillModel(
            slippage_bps=ZERO, policy=fill_policy, impact_bps=self.friction.taker_slippage_bps
        )
        self.quote_ccy = quote_ccy
        self.ledger = Portfolio(starting_equity=starting_cash, multipliers=dict(multipliers or {}))
        self._orders: dict[str, Order] = {}
        self._working: dict[str, Order] = {}
        self._arrival: dict[str, int] = {}
        self._events: deque[OrderEvent | Fill] = deque()
        self._quotes: dict[str, Quote] = {}
        self._trade_seq = 0
        self._last_funding_period: int | None = None
        self.funding_events: list[tuple[str, Decimal]] = []
        self.submits: dict[str, int] = {}
        self._rested: set[str] = set()
        self._fills: dict[str, list[Fill]] = {}

    # ---------------------------------------------------------------- events
    def _event(self, order: Order, kind: OrderEventKind, reason: str = "") -> None:
        self._events.append(
            OrderEvent(
                kind=kind,
                client_order_id=order.client_order_id,
                state=order.state,
                order=order,
                reason=reason,
                ts=self.clock.now(),
            )
        )

    def _set(self, coid: str, **upd: object) -> Order:
        o = self._orders[coid].model_copy(update={**upd, "ts_updated": self.clock.now()})
        self._orders[coid] = o
        if o.state.is_open:
            self._working[coid] = o
        else:
            self._working.pop(coid, None)
        return o

    def drain(self) -> list[OrderEvent | Fill]:
        out = list(self._events)
        self._events.clear()
        return out

    async def stream(self) -> AsyncIterator[OrderEvent | Fill]:
        while True:
            while self._events:
                yield self._events.popleft()
            await asyncio.sleep(0.01)

    # ---------------------------------------------------------------- fills
    def _fill(self, order: Order, price: Decimal, qty: Decimal, liquidity: Liquidity) -> None:
        intent = order.intent
        if intent.reduce_only:
            pos = self.ledger.position(intent.symbol).qty
            allowed = max(ZERO, -pos if intent.side is Side.BUY else pos)
            qty = min(qty, allowed)
            if qty <= 0:
                self._event(
                    self._set(order.client_order_id, state=OrderState.CANCELED),
                    OrderEventKind.CANCELED,
                    "reduce_only_nothing_to_close",
                )
                return
        self._trade_seq += 1
        mult = self.ledger.multipliers.get(intent.symbol, Decimal(1))
        fee = self.friction.fee(price * qty * mult, liquidity)
        fill = Fill(
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            venue=self.venue,
            trade_id=f"{self.account_id}-T{self._trade_seq}",
            symbol=intent.symbol,
            side=intent.side,
            price=price,
            qty=qty,
            fee=fee,
            fee_ccy=self.quote_ccy,
            liquidity=liquidity,
            ts=self.clock.now(),
        )
        self.ledger.apply_fill(fill)
        self._fills.setdefault(fill.client_order_id, []).append(fill)
        self.friction.volume_30d += price * qty * mult
        cur = self._orders[order.client_order_id]
        filled = cur.filled_qty + qty
        state = OrderState.FILLED if filled >= intent.qty else OrderState.PARTIALLY_FILLED
        avg = (cur.avg_px * cur.filled_qty + price * qty) / filled
        new = self._set(
            order.client_order_id, filled_qty=filled, avg_px=avg, fees=cur.fees + fee, state=state
        )
        self._events.append(fill)
        self._event(
            new, OrderEventKind.FILLED if state is OrderState.FILLED else OrderEventKind.PARTIAL
        )
        if intent.reduce_only and new.state is OrderState.PARTIALLY_FILLED:
            self._event(
                self._set(order.client_order_id, state=OrderState.CANCELED),
                OrderEventKind.CANCELED,
                "reduce_only_remainder",
            )

    def _arrived(self, coid: str, ts: int) -> bool:
        return self._arrival.get(coid, 0) <= ts

    # ------------------------------------------------------------- bar mode
    def on_bar(self, bar: Bar) -> None:
        """Match working orders for this symbol against `bar` (they were placed before it)."""
        self.ledger.mark(bar.symbol, bar.c)
        for coid in list(self._working):
            order = self._working.get(coid)
            if (
                order is None
                or order.intent.symbol != bar.symbol
                or not self._arrived(coid, bar.ts_close)
            ):
                continue
            match = self.fill_model.match(order.intent, bar)
            if match is not None:
                self._fill(
                    order,
                    match.price,
                    order.remaining,
                    Liquidity.TAKER if match.taker else Liquidity.MAKER,
                )
            elif order.intent.tif in (TimeInForce.IOC, TimeInForce.FOK):
                self._event(
                    self._set(coid, state=OrderState.CANCELED),
                    OrderEventKind.CANCELED,
                    "ioc_unfilled",
                )
        self.friction.impact.update(bar)
        self._funding(bar.symbol, bar.ts_close)

    def _funding(self, symbol: str, ts: int) -> None:
        rate = self.friction.funding_rate()
        period = ts // FUNDING_PERIOD_NS
        if rate == 0:
            return
        if self._last_funding_period is not None and period > self._last_funding_period:
            self.ledger.apply_funding(symbol, rate)
            self.funding_events.append((symbol, rate))
        self._last_funding_period = period

    # -------------------------------------------------------------- L1 mode
    def on_quote(self, q: Quote) -> None:
        self._quotes[q.symbol] = q
        self.ledger.mark(q.symbol, q.mid)
        for coid in list(self._working):
            order = self._working.get(coid)
            if (
                order is None
                or order.intent.symbol != q.symbol
                or not self._arrived(coid, q.ts_recv)
            ):
                continue
            self._match_quote(order, q)
        self._funding(q.symbol, q.ts_recv)

    def _slip(self, intent: OrderIntent, px: Decimal) -> Decimal:
        bps = self.friction.cfg.fixed_slippage_bps * self.friction.cfg.scale
        if self.friction.cfg.impact:
            bps += (
                self.friction.impact.slip_bps(intent.symbol, intent.qty) * self.friction.cfg.scale
            )
        return px * (1 + intent.side.sign * bps / BPS)

    def _match_quote(self, order: Order, q: Quote) -> None:
        it = order.intent
        touch = q.ask if it.side is Side.BUY else q.bid
        if it.type is OrderType.MARKET:
            self._fill(order, self._slip(it, touch), order.remaining, Liquidity.TAKER)
            return
        if it.type in (OrderType.STOP, OrderType.STOP_LIMIT):
            assert it.stop_price is not None
            triggered = touch >= it.stop_price if it.side is Side.BUY else touch <= it.stop_price
            if not triggered:
                return
            if it.type is OrderType.STOP:
                self._fill(order, self._slip(it, touch), order.remaining, Liquidity.TAKER)
                return
        assert it.limit_price is not None
        marketable = touch <= it.limit_price if it.side is Side.BUY else touch >= it.limit_price
        coid = order.client_order_id
        if marketable:
            if coid in self._rested:  # resting order crossed by the market: maker at limit
                self._fill(order, it.limit_price, order.remaining, Liquidity.MAKER)
            else:  # aggressive on arrival: taker at the touch
                self._fill(order, touch, order.remaining, Liquidity.TAKER)
        elif it.tif in (TimeInForce.IOC, TimeInForce.FOK):
            self._event(
                self._set(coid, state=OrderState.CANCELED), OrderEventKind.CANCELED, "ioc_unfilled"
            )
        else:
            if self.queue_model and coid not in self._rested:
                self._queue_ahead[coid] = self._join_queue(it, q)
            self._rested.add(coid)
            if self.queue_model:
                self._update_queue(coid, it, q)

    @staticmethod
    def _same_side(it: OrderIntent, q: Quote) -> tuple[Decimal, Decimal]:
        return (q.bid, q.bid_sz) if it.side is Side.BUY else (q.ask, q.ask_sz)

    def _join_queue(self, it: OrderIntent, q: Quote) -> Decimal | None:
        """Queue ahead on arrival: the displayed size at our price if we join the touch,
        nothing if we improve it, unknown (None) if we rest behind the touch."""
        assert it.limit_price is not None
        best, size = self._same_side(it, q)
        if it.limit_price == best:
            return size
        improves = it.limit_price > best if it.side is Side.BUY else it.limit_price < best
        return Decimal(0) if improves else None

    def _update_queue(self, coid: str, it: OrderIntent, q: Quote) -> None:
        assert it.limit_price is not None
        best, size = self._same_side(it, q)
        ahead = self._queue_ahead.get(coid)
        if best == it.limit_price:
            # our level is the touch: size can only have shrunk from ahead of us (pessimistic:
            # displayed size at the level never shrinks below what is still in front of us)
            self._queue_ahead[coid] = size if ahead is None else min(ahead, size)
        else:
            beyond = best < it.limit_price if it.side is Side.BUY else best > it.limit_price
            if beyond:  # the touch moved past our price without crossing: we are first now
                self._queue_ahead[coid] = Decimal(0)

    def on_trade(self, t: Trade) -> None:
        """Resting limits fill (partially) when a live trade prints *through* the limit."""
        for coid in list(self._working):
            order = self._working.get(coid)
            if (
                order is None
                or order.intent.symbol != t.symbol
                or not self._arrived(coid, t.ts_recv)
            ):
                continue
            it = order.intent
            if it.type is not OrderType.LIMIT or it.limit_price is None:
                continue
            through = t.price < it.limit_price if it.side is Side.BUY else t.price > it.limit_price
            if through:
                self._fill(order, it.limit_price, min(order.remaining, t.size), Liquidity.MAKER)
            elif self.queue_model and t.price == it.limit_price and coid in self._rested:
                # BT-4: a print at our price eats the queue ahead first, then us
                ahead = self._queue_ahead.get(coid)
                if ahead is None:
                    continue
                ours = min(order.remaining, max(Decimal(0), t.size - ahead))
                self._queue_ahead[coid] = max(Decimal(0), ahead - t.size)
                if ours > 0:
                    self._fill(order, it.limit_price, ours, Liquidity.MAKER)

    # ------------------------------------------------------------ Broker API
    async def submit(self, order: Order) -> None:
        coid = order.client_order_id
        self.submits[coid] = self.submits.get(coid, 0) + 1
        if coid in self._orders:
            raise BrokerError(f"duplicate client_order_id {coid}")
        if order.account_id != self.account_id:
            raise BrokerError(f"order for account {order.account_id} sent to {self.account_id}")
        now = self.clock.now()
        self._orders[coid] = order.model_copy(
            update={
                "state": OrderState.ACCEPTED,
                "venue_order_id": f"S{len(self._orders) + 1}",
                "ts_updated": now,
            }
        )
        self._working[coid] = self._orders[coid]
        self._arrival[coid] = now + self.friction.latency_ns()
        self._event(self._orders[coid], OrderEventKind.ACCEPTED)
        q = self._quotes.get(order.intent.symbol)
        if self.mode is SimMode.L1 and q is not None and self._arrival[coid] <= now:
            self._match_quote(self._orders[coid], q)  # zero latency: match the current quote

    async def cancel(self, client_order_id: str) -> None:
        if client_order_id in self._working:
            self._event(
                self._set(client_order_id, state=OrderState.CANCELED), OrderEventKind.CANCELED
            )

    async def get_order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    async def open_orders(self) -> list[Order]:
        return list(self._working.values())

    async def fills_for_order(self, client_order_id: str) -> list[Fill]:
        return list(self._fills.get(client_order_id, []))

    async def positions(self) -> list[Position]:
        return list(self.ledger.snapshot())

    async def balances(self) -> list[Balance]:
        return [Balance(asset=self.quote_ccy, free=self.ledger.cash())]

    async def flatten_all(self, *, reduce_only: bool = True) -> None:
        for coid in list(self._working):
            await self.cancel(coid)
        for p in self.ledger.snapshot():
            self._trade_seq += 1
            now = self.clock.now()
            intent = OrderIntent(
                strategy_id="venue-flatten",
                symbol=p.symbol,
                side=Side.SELL if p.qty > 0 else Side.BUY,
                type=OrderType.MARKET,
                qty=abs(p.qty),
                reduce_only=reduce_only,
            )
            await self.submit(
                Order(
                    client_order_id=f"vflat-{self._trade_seq}",
                    account_id=self.account_id,
                    intent=intent,
                    ts_created=now,
                    ts_updated=now,
                )
            )
