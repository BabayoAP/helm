"""Long-only SMA crossover. Smoke-test strategy, not alpha (PRD §15 M2)."""

from __future__ import annotations

from decimal import Decimal

from helm.core.enums import Side
from helm.core.models import Bar, OrderIntent
from helm.strategy.api import BaseStrategy, Context
from helm.strategy.examples.common import market, sma


class SmaCross(BaseStrategy):
    id = "sma_cross"

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        fast = int(ctx.params.get("fast", 10))
        slow = int(ctx.params.get("slow", 30))
        qty = Decimal(str(ctx.params.get("qty", "0.01")))
        hist = ctx.history(bar.symbol, bar.timeframe, slow + 1)
        if len(hist) < slow + 1:
            return []
        closes = [b.c for b in hist]
        f_now, s_now = sma(closes[-fast:]), sma(closes[-slow:])
        f_prev, s_prev = sma(closes[-fast - 1 : -1]), sma(closes[-slow - 1 : -1])
        pos = ctx.position(bar.symbol).qty
        if f_prev <= s_prev and f_now > s_now and pos <= 0:
            return [market(self.id, bar, Side.BUY, qty, tag="cross_up")]
        if f_prev >= s_prev and f_now < s_now and pos > 0:
            return [market(self.id, bar, Side.SELL, pos, reduce_only=True, tag="cross_down")]
        return []
