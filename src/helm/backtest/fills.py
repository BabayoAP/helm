"""Bar-mode fill model (BT-3).

Orders decided on bar t are matched against bar t+1 (I3):
- market: next open +/- slippage;
- limit: fills only if price *trades through* the limit (touch != fill) under
  the conservative policy; optimistic allows touch. A limit already marketable
  at the open fills at the open;
- stop: triggers when the bar reaches the stop; fills at the worse of stop and
  open (gap) +/- slippage;
- stop-limit: triggers like a stop, then behaves as a limit;
- same-bar ambiguity (e.g. stop-loss and take-profit both inside one bar) is
  resolved pessimistically: the adverse order wins.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from helm.core.enums import OrderType, Side
from helm.core.models import Bar, OrderIntent

BPS = Decimal(10_000)


class FillPolicy(StrEnum):
    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"


@dataclass(frozen=True)
class BarFill:
    price: Decimal
    taker: bool


# extra slippage (in bps) for an order on a bar, e.g. square-root impact (BT-5)
SlippageFn = Callable[[OrderIntent, Bar], Decimal]


@dataclass
class BarFillModel:
    slippage_bps: Decimal = Decimal(0)
    policy: FillPolicy = FillPolicy.CONSERVATIVE
    impact_bps: SlippageFn | None = None

    def _slip(self, intent: OrderIntent, bar: Bar, px: Decimal) -> Decimal:
        bps = self.slippage_bps + (self.impact_bps(intent, bar) if self.impact_bps else Decimal(0))
        return px * (1 + intent.side.sign * bps / BPS)

    def _limit(self, side: Side, limit: Decimal, bar: Bar, entry: Decimal) -> Decimal | None:
        """Limit matching given the price at which the order becomes active."""
        if side is Side.BUY:
            if entry <= limit:
                return entry
            through = bar.l < limit if self.policy is FillPolicy.CONSERVATIVE else bar.l <= limit
            return limit if through else None
        if entry >= limit:
            return entry
        through = bar.h > limit if self.policy is FillPolicy.CONSERVATIVE else bar.h >= limit
        return limit if through else None

    def _stop_trigger(self, intent: OrderIntent, bar: Bar) -> Decimal | None:
        stop = intent.stop_price
        assert stop is not None
        if intent.side is Side.BUY:
            if bar.o >= stop:
                return bar.o  # gapped through: worse of stop/open
            return stop if bar.h >= stop else None
        if bar.o <= stop:
            return bar.o
        return stop if bar.l <= stop else None

    def match(self, intent: OrderIntent, bar: Bar) -> BarFill | None:
        if intent.type is OrderType.MARKET:
            return BarFill(self._slip(intent, bar, bar.o), taker=True)
        if intent.type is OrderType.LIMIT:
            assert intent.limit_price is not None
            px = self._limit(intent.side, intent.limit_price, bar, bar.o)
            return None if px is None else BarFill(px, taker=px == bar.o)
        trigger = self._stop_trigger(intent, bar)
        if trigger is None:
            return None
        if intent.type is OrderType.STOP:
            return BarFill(self._slip(intent, bar, trigger), taker=True)
        # STOP_LIMIT: after the trigger it is a limit order entering at the trigger price
        assert intent.limit_price is not None
        limit = intent.limit_price
        if intent.side is Side.BUY and trigger > limit:
            return None if not (bar.l < limit) else BarFill(limit, taker=False)
        if intent.side is Side.SELL and trigger < limit:
            return None if not (bar.h > limit) else BarFill(limit, taker=False)
        px = limit if self.policy is FillPolicy.CONSERVATIVE else trigger
        return BarFill(px, taker=False)

    def resolve_oco(
        self, intents: Sequence[OrderIntent], bar: Bar
    ) -> tuple[OrderIntent, BarFill] | None:
        """Several exits for one position in the same bar: pick the worst outcome."""
        fills = [(i, f) for i in intents if (f := self.match(i, bar)) is not None]
        if not fills:
            return None
        # for a sell, the lowest price is worst; for a buy, the highest
        return min(fills, key=lambda x: x[1].price * x[0].side.sign * -1)
