"""Long-only RSI mean reversion. Smoke-test strategy, not alpha."""

from __future__ import annotations

from decimal import Decimal

from helm.core.enums import Side
from helm.core.models import Bar, OrderIntent
from helm.strategy.api import BaseStrategy, Context
from helm.strategy.examples.common import market, rsi
from helm.strategy.params import IntRange, ParamSpace


class RsiReversion(BaseStrategy):
    id = "rsi_reversion"
    param_space = ParamSpace(
        dims={
            "period": IntRange(7, 21, 7),
            "entry": IntRange(20, 35, 5),
            "exit": IntRange(50, 70, 10),
        }
    )

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        n = int(ctx.params.get("period", 14))
        lo = Decimal(str(ctx.params.get("entry", 30)))
        hi = Decimal(str(ctx.params.get("exit", 55)))
        qty = Decimal(str(ctx.params.get("qty", "0.01")))
        hist = ctx.history(bar.symbol, bar.timeframe, n + 1)
        if len(hist) < n + 1:
            return []
        value = rsi([b.c for b in hist])
        if value is None:
            return []
        pos = ctx.position(bar.symbol).qty
        if value < lo and pos <= 0:
            return [market(self.id, bar, Side.BUY, qty, tag=f"rsi<{lo}")]
        if value > hi and pos > 0:
            return [market(self.id, bar, Side.SELL, pos, reduce_only=True, tag=f"rsi>{hi}")]
        return []
