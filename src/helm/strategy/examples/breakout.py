"""Long-only Donchian breakout with a trailing channel exit. Smoke-test strategy."""

from __future__ import annotations

from decimal import Decimal

from helm.core.enums import Side
from helm.core.models import Bar, OrderIntent
from helm.strategy.api import BaseStrategy, Context
from helm.strategy.examples.common import market


class Breakout(BaseStrategy):
    id = "breakout"

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        n = int(ctx.params.get("entry_n", 20))
        m = int(ctx.params.get("exit_n", 10))
        qty = Decimal(str(ctx.params.get("qty", "0.01")))
        hist = ctx.history(bar.symbol, bar.timeframe, max(n, m) + 1)
        if len(hist) < max(n, m) + 1:
            return []
        prior = hist[:-1]  # channel excludes the current bar
        pos = ctx.position(bar.symbol).qty
        if pos <= 0 and bar.c > max(b.h for b in prior[-n:]):
            return [market(self.id, bar, Side.BUY, qty, tag="breakout")]
        if pos > 0 and bar.c < min(b.l for b in prior[-m:]):
            return [market(self.id, bar, Side.SELL, pos, reduce_only=True, tag="channel_exit")]
        return []
