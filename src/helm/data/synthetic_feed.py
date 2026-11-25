"""Synthetic live feed: quotes + trades on a seeded random walk, bars built from trades.

Lets paper mode (and soak tests) run end to end before a venue is chosen (M4).
`realtime=True` paces events on the wall clock; otherwise timestamps advance by
`step_ms` per event and events are yielded as fast as possible.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from decimal import Decimal

from pydantic import BaseModel

from helm.core.enums import Side
from helm.core.models import Quote, Trade
from helm.data.bars import BarBuilder


async def synthetic_feed(
    symbol: str,
    *,
    n_events: int | None,
    start_ns: int,
    step_ms: int = 250,
    seed: int = 0,
    s0: Decimal = Decimal(100),
    timeframe: str = "1m",
    realtime: bool = False,
    now_fn: Callable[[], int] | None = None,
) -> AsyncIterator[BaseModel]:
    rng = random.Random(seed)
    mid = s0
    tick = Decimal("0.01")
    builder = BarBuilder(timeframe)
    i = 0
    trade_id = 0
    while n_events is None or i < n_events:
        ts = now_fn() if (realtime and now_fn) else start_ns + i * step_ms * 1_000_000
        mid = max(tick * 100, (mid * (1 + Decimal(repr(rng.gauss(0, 0.0004))))).quantize(tick))
        half = tick * rng.randint(1, 3)
        q = Quote(
            symbol=symbol,
            bid=mid - half,
            bid_sz=Decimal(rng.randint(1, 20)),
            ask=mid + half,
            ask_sz=Decimal(rng.randint(1, 20)),
            ts_event=ts,
            ts_recv=ts,
        )
        yield q
        if rng.random() < 0.6:
            trade_id += 1
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            px = q.ask if side is Side.BUY else q.bid
            t = Trade(
                symbol=symbol,
                price=px,
                size=Decimal(rng.randint(1, 5)) / 10,
                side=side,
                trade_id=str(trade_id),
                ts_event=ts,
                ts_recv=ts,
            )
            for bar in builder.on_trade(t):
                yield bar
            yield t
        i += 1
        if realtime:
            await asyncio.sleep(step_ms / 1000)
        elif i % 500 == 0:
            await asyncio.sleep(0)
