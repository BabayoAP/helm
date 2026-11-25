"""In-process asyncio event bus behind an `EventBus` protocol (PRD §4 decision).

Backpressure (PRD §12): every subscriber has a bounded queue. `reliable`
subscribers (OMS, journal, recorder) apply backpressure to the publisher; lossy
subscribers (UI, metrics) drop the oldest event and count the drop, so slow
consumers never block the trading path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel


class Subscription:
    def __init__(
        self,
        name: str,
        types: tuple[type[BaseModel], ...] | None,
        maxsize: int,
        reliable: bool,
    ) -> None:
        self.name = name
        self.types = types
        self.reliable = reliable
        self.queue: asyncio.Queue[BaseModel | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def wants(self, event: BaseModel) -> bool:
        return self.types is None or isinstance(event, self.types)

    async def deliver(self, event: BaseModel) -> None:
        if self.closed:
            return
        if self.reliable:
            await self.queue.put(event)
            return
        while self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        self.queue.put_nowait(event)

    async def __aiter__(self) -> AsyncIterator[BaseModel]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def get(self) -> BaseModel | None:
        return await self.queue.get()


class EventBus(Protocol):
    async def publish(self, event: BaseModel) -> None: ...

    def subscribe(
        self,
        name: str,
        types: tuple[type[BaseModel], ...] | None = None,
        *,
        maxsize: int = 10_000,
        reliable: bool = True,
    ) -> Subscription: ...

    async def close(self) -> None: ...


@dataclass
class InProcessBus:
    subscriptions: list[Subscription] = field(default_factory=lambda: [])
    published: int = 0

    async def publish(self, event: BaseModel) -> None:
        self.published += 1
        for sub in self.subscriptions:
            if sub.wants(event):
                await sub.deliver(event)

    def subscribe(
        self,
        name: str,
        types: tuple[type[BaseModel], ...] | None = None,
        *,
        maxsize: int = 10_000,
        reliable: bool = True,
    ) -> Subscription:
        sub = Subscription(name, types, maxsize, reliable)
        self.subscriptions.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.closed = True
        self.subscriptions.remove(sub)

    async def close(self) -> None:
        for sub in self.subscriptions:
            if not sub.closed:
                sub.closed = True
                if sub.queue.full() and not sub.reliable:
                    sub.queue.get_nowait()
                await sub.queue.put(None)
