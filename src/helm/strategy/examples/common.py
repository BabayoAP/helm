"""Small Decimal helpers shared by example strategies (no numpy, no I/O)."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from decimal import Decimal

from helm.core.enums import OrderType, Side
from helm.core.models import Bar, OrderIntent


def sma(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def rsi(closes: Sequence[Decimal]) -> Decimal | None:
    """Simple-average RSI over len(closes)-1 changes."""
    if len(closes) < 2:
        return None
    gains = Decimal(0)
    losses = Decimal(0)
    for a, b in itertools.pairwise(closes):
        d = b - a
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return Decimal(100)
    rs = gains / losses
    return Decimal(100) - Decimal(100) / (1 + rs)


def market(
    strategy_id: str,
    bar: Bar,
    side: Side,
    qty: Decimal,
    *,
    reduce_only: bool = False,
    tag: str = "",
) -> OrderIntent:
    return OrderIntent(
        strategy_id=strategy_id,
        symbol=bar.symbol,
        side=side,
        type=OrderType.MARKET,
        qty=qty,
        reduce_only=reduce_only,
        tag=tag,
    )
