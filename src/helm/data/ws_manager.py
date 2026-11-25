"""WebSocket manager (DATA-5).

Heartbeat (ping when idle), reconnect with jittered exponential backoff,
resubscribe, REST gap-fill after reconnect, order-book sequence-gap detection
with re-snapshot, bar de-duplication, and `FeedStatus` emission
(LIVE / RECONNECTING / STALE) so risk can react (RISK-12).

Venue specifics live in a `FeedHandler`; the transport is injected, so the same
manager runs against `websockets` in production and fakes in chaos tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from helm.core.enums import FeedState
from helm.core.events import FeedStatus
from helm.core.models import Bar, BookDelta, BookLevel, BookSnapshot
from helm.core.timeframes import timeframe_ns


class WsTransport(Protocol):
    async def send(self, msg: str) -> None: ...

    async def recv(self) -> str: ...

    async def ping(self) -> None: ...

    async def close(self) -> None: ...


class FeedHandler(Protocol):
    def subscribe_messages(self) -> list[str]: ...

    def parse(self, raw: str) -> list[BaseModel]: ...

    async def gap_fill(self, symbol: str, timeframe: str, since_ts_open: int) -> list[Bar]:
        """Closed bars with ts_open > since_ts_open, from REST."""
        ...

    async def book_snapshot(self, symbol: str) -> BookSnapshot: ...


Emit = Callable[[BaseModel], Awaitable[None]]


@dataclass
class WsConfig:
    heartbeat_s: float = 15.0
    heartbeat_timeout_s: float = 10.0
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    stale_after_s: float = 2.0
    seed: int = 0


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    return rng.uniform(0, min(cap, base * (2 ** min(attempt, 30))))


@dataclass
class LocalBook:
    seq: int
    bids: dict[Decimal, Decimal] = field(default_factory=lambda: {})
    asks: dict[Decimal, Decimal] = field(default_factory=lambda: {})

    @classmethod
    def from_snapshot(cls, snap: BookSnapshot) -> LocalBook:
        return cls(
            seq=snap.seq,
            bids={lv.price: lv.size for lv in snap.bids if lv.size > 0},
            asks={lv.price: lv.size for lv in snap.asks if lv.size > 0},
        )

    def apply(self, delta: BookDelta) -> None:
        for side, levels in ((self.bids, delta.bids), (self.asks, delta.asks)):
            for lv in levels:
                if lv.size == 0:
                    side.pop(lv.price, None)
                else:
                    side[lv.price] = lv.size
        self.seq = delta.seq

    def levels(self) -> tuple[tuple[BookLevel, ...], tuple[BookLevel, ...]]:
        bids = tuple(BookLevel(price=p, size=s) for p, s in sorted(self.bids.items(), reverse=True))
        asks = tuple(BookLevel(price=p, size=s) for p, s in sorted(self.asks.items()))
        return bids, asks


class WsManager:
    def __init__(
        self,
        venue: str,
        connect: Callable[[], Awaitable[WsTransport]],
        handler: FeedHandler,
        emit: Emit,
        *,
        config: WsConfig | None = None,
        time_fn: Callable[[], int] | None = None,
    ) -> None:
        self.venue = venue
        self._connect = connect
        self._handler = handler
        self._emit = emit
        self.cfg = config or WsConfig()
        self._rng = random.Random(self.cfg.seed)
        self._time_fn = time_fn
        self._stop = asyncio.Event()
        self.state = FeedState.RECONNECTING
        self.last_bar: dict[tuple[str, str], int] = {}
        self.books: dict[str, LocalBook] = {}
        self.reconnects = 0
        self.resnapshots = 0
        self.dropped_dupes = 0
        self.statuses: list[FeedState] = []

    def _now(self) -> int:
        return self._time_fn() if self._time_fn else int(asyncio.get_running_loop().time() * 1e9)

    async def _set_state(self, state: FeedState, reason: str = "") -> None:
        if state is self.state and self.statuses:
            return
        self.state = state
        self.statuses.append(state)
        await self._emit(
            FeedStatus(venue=self.venue, symbol=None, state=state, reason=reason, ts=self._now())
        )

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        attempt = 0
        down_since: float | None = None
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                transport = await self._connect()
            except (ConnectionError, OSError) as exc:
                down_since = down_since or loop.time()
                await self._outage(attempt, down_since, f"connect failed: {exc}")
                attempt += 1
                continue
            try:
                for msg in self._handler.subscribe_messages():
                    await transport.send(msg)
                await self._resync()
                attempt = 0
                down_since = None
                await self._set_state(FeedState.LIVE, "subscribed")
                await self._pump(transport)
            except (ConnectionError, OSError, TimeoutError) as exc:
                self.reconnects += 1
                down_since = down_since or loop.time()
                await self._set_state(FeedState.RECONNECTING, str(exc))
                await self._outage(attempt, down_since, str(exc))
                attempt += 1
            finally:
                await transport.close()

    async def _outage(self, attempt: int, down_since: float, reason: str) -> None:
        loop = asyncio.get_running_loop()
        delay = backoff_delay(attempt, self.cfg.backoff_base_s, self.cfg.backoff_cap_s, self._rng)
        if loop.time() + delay - down_since >= self.cfg.stale_after_s:
            await self._set_state(FeedState.STALE, reason)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _pump(self, transport: WsTransport) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(transport.recv(), timeout=self.cfg.heartbeat_s)
            except TimeoutError:
                await asyncio.wait_for(transport.ping(), timeout=self.cfg.heartbeat_timeout_s)
                continue
            for ev in self._handler.parse(raw):
                await self._on_event(ev)

    async def _resync(self) -> None:
        """After (re)connect: REST gap-fill every tracked bar series, re-snapshot books."""
        for (symbol, tf), last in sorted(self.last_bar.items()):
            for bar in await self._handler.gap_fill(symbol, tf, last):
                await self._on_bar(bar)
        for symbol in sorted(self.books):
            await self._resnapshot(symbol)

    async def _resnapshot(self, symbol: str) -> None:
        self.resnapshots += 1
        snap = await self._handler.book_snapshot(symbol)
        self.books[symbol] = LocalBook.from_snapshot(snap)
        await self._emit(snap)

    async def _on_event(self, ev: BaseModel) -> None:
        if isinstance(ev, Bar):
            await self._on_bar(ev)
        elif isinstance(ev, BookSnapshot):
            self.books[ev.symbol] = LocalBook.from_snapshot(ev)
            await self._emit(ev)
        elif isinstance(ev, BookDelta):
            book = self.books.get(ev.symbol)
            if book is None or ev.seq != book.seq + 1:
                if book is not None and ev.seq <= book.seq:
                    self.dropped_dupes += 1
                    return
                await self._resnapshot(ev.symbol)
                return
            book.apply(ev)
            await self._emit(ev)
        else:
            await self._emit(ev)

    async def _on_bar(self, bar: Bar) -> None:
        if not bar.closed:
            return
        key = (bar.symbol, bar.timeframe)
        last = self.last_bar.get(key)
        if last is not None:
            if bar.ts_open <= last:
                self.dropped_dupes += 1
                return
            if bar.ts_open > last + timeframe_ns(bar.timeframe):
                for missing in await self._handler.gap_fill(bar.symbol, bar.timeframe, last):
                    if missing.ts_open < bar.ts_open:
                        await self._emit_bar(missing)
        await self._emit_bar(bar)

    async def _emit_bar(self, bar: Bar) -> None:
        last = self.last_bar.get((bar.symbol, bar.timeframe))
        if last is not None and bar.ts_open <= last:
            self.dropped_dupes += 1
            return
        self.last_bar[(bar.symbol, bar.timeframe)] = bar.ts_open
        await self._emit(bar)

    def track(self, symbol: str, timeframe: str, last_ts_open: int) -> None:
        """Seed a series so the first connect gap-fills from `last_ts_open`."""
        self.last_bar[(symbol, timeframe)] = last_ts_open
