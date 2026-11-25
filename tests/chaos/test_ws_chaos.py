"""DATA-5 chaos: drop the socket 100x with random delays; no duplicate or missing bars,
book matches REST snapshot, FeedStatus STALE raised during outages."""

from __future__ import annotations

import asyncio
import json
import random
from decimal import Decimal

import pytest
from pydantic import BaseModel

from helm.core.enums import FeedState
from helm.core.events import FeedStatus
from helm.core.models import Bar, BookDelta, BookLevel, BookSnapshot
from helm.data.ws_manager import LocalBook, WsConfig, WsManager, backoff_delay
from tests.factories import MIN, SYM, bar

N_BARS = 600


class FakeVenue:
    """Ground truth: bar i exists from 'time' i; book evolves with seq."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.cursor = 0  # next bar index the socket will push
        self.book_seq = 0
        self.bids: dict[Decimal, Decimal] = {Decimal(99): Decimal(1)}
        self.asks: dict[Decimal, Decimal] = {Decimal(101): Decimal(1)}
        self.drops = 0

    def truth(self, i: int) -> Bar:
        return bar(i, 10, 11, 9, 10 + (i % 5) / 10)

    def next_delta(self) -> BookDelta:
        self.book_seq += 1
        px = Decimal(95 + self.rng.randint(0, 4))
        sz = Decimal(self.rng.randint(0, 3))
        side = self.bids if self.rng.random() < 0.5 else self.asks
        if side is self.asks:
            px += 6
        if sz == 0:
            side.pop(px, None)
        else:
            side[px] = sz
        lv = (BookLevel(price=px, size=sz),)
        return BookDelta(
            symbol=SYM,
            seq=self.book_seq,
            bids=lv if side is self.bids else (),
            asks=lv if side is self.asks else (),
            ts_event=0,
            ts_recv=0,
        )

    def snapshot(self) -> BookSnapshot:
        return BookSnapshot(
            symbol=SYM,
            seq=self.book_seq,
            bids=tuple(
                BookLevel(price=p, size=s) for p, s in sorted(self.bids.items(), reverse=True)
            ),
            asks=tuple(BookLevel(price=p, size=s) for p, s in sorted(self.asks.items())),
            ts_event=0,
            ts_recv=0,
        )


class FakeTransport:
    def __init__(self, venue: FakeVenue, drop_after: int) -> None:
        self.v = venue
        self.left = drop_after
        self.closed = False

    async def send(self, msg: str) -> None:
        if self.closed:
            raise ConnectionError("closed")

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if self.left <= 0 or self.v.cursor >= N_BARS:
            if self.v.cursor >= N_BARS:
                await asyncio.sleep(3600)
            self.v.drops += 1
            # while disconnected the venue keeps producing bars + book updates we never see
            for _ in range(self.v.rng.randint(0, 3)):
                self.v.cursor += 1
                self.v.next_delta()
            raise ConnectionError("socket dropped")
        self.left -= 1
        r = self.v.rng.random()
        if r < 0.5:
            i = self.v.cursor
            self.v.cursor += 1
            msgs = [{"t": "bar", "i": i}]
            if self.v.rng.random() < 0.1 and i > 0:
                msgs.append({"t": "bar", "i": i - 1})  # duplicate / late bar
            if self.v.rng.random() < 0.05:  # silently skipped bar (gap without disconnect)
                self.v.cursor += 1
            return json.dumps(msgs)
        d = self.v.next_delta()
        if self.v.rng.random() < 0.05:  # sequence gap
            d = self.v.next_delta()
        return json.dumps([{"t": "delta", "d": d.model_dump(mode="json")}])

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class Handler:
    def __init__(self, venue: FakeVenue) -> None:
        self.v = venue

    def subscribe_messages(self) -> list[str]:
        return ['{"op":"subscribe"}']

    def parse(self, raw: str) -> list[BaseModel]:
        out: list[BaseModel] = []
        for m in json.loads(raw):
            out.append(
                self.v.truth(m["i"]) if m["t"] == "bar" else BookDelta.model_validate(m["d"])
            )
        return out

    async def gap_fill(self, symbol: str, timeframe: str, since_ts_open: int) -> list[Bar]:
        start = since_ts_open // MIN + 1
        return [self.v.truth(i) for i in range(start, self.v.cursor)]

    async def book_snapshot(self, symbol: str) -> BookSnapshot:
        return self.v.snapshot()


@pytest.mark.chaos
def test_ws_survives_100_drops_without_dupes_or_gaps() -> None:
    async def run() -> None:
        rng = random.Random(42)
        venue = FakeVenue(rng)
        got: list[BaseModel] = []

        async def emit(ev: BaseModel) -> None:
            got.append(ev)

        async def connect() -> FakeTransport:
            if rng.random() < 0.1:
                raise ConnectionError("refused")
            return FakeTransport(venue, drop_after=rng.randint(1, 12))

        mgr = WsManager(
            "fake",
            connect,
            Handler(venue),
            emit,
            config=WsConfig(
                backoff_base_s=0.0005,
                backoff_cap_s=0.004,
                stale_after_s=0.001,
                heartbeat_s=5,
                seed=7,
            ),
            time_fn=lambda: 0,
        )
        await emit(venue.snapshot())
        mgr.books[SYM] = LocalBook.from_snapshot(venue.snapshot())
        task = asyncio.create_task(mgr.run())
        while venue.drops < 100 or venue.cursor < N_BARS:
            await asyncio.sleep(0.001)
            if venue.drops >= 100 and venue.cursor < N_BARS:
                venue.cursor = N_BARS  # finish: final resync fills to the end
        # one more reconnect so the final gap-fill runs
        while mgr.last_bar.get((SYM, "1m"), -1) < (N_BARS - 1) * MIN:
            await asyncio.sleep(0.001)
        mgr.stop()
        task.cancel()
        bars = [e for e in got if isinstance(e, Bar)]
        assert [b.ts_open // MIN for b in bars] == list(range(len(bars)))
        assert len(bars) >= N_BARS
        assert venue.drops >= 100 and mgr.reconnects >= 100
        assert mgr.resnapshots > 0
        snap = venue.snapshot()
        book = mgr.books[SYM]
        assert book.seq == snap.seq
        assert book.levels() == (snap.bids, snap.asks)
        states = [e.state for e in got if isinstance(e, FeedStatus)]
        assert (
            FeedState.STALE in states
            and FeedState.RECONNECTING in states
            and FeedState.LIVE in states
        )

    asyncio.run(asyncio.wait_for(run(), timeout=60))


def test_backoff_is_jittered_and_capped() -> None:
    rng = random.Random(1)
    delays = [backoff_delay(a, 1.0, 60.0, rng) for a in range(12)]
    assert all(0 <= d <= 60 for d in delays) and max(delays[:2]) <= 2
