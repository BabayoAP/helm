"""Rate-limit governor (DATA-6).

Token bucket per (venue, api_key_id, endpoint-weight class) with a strict
priority queue: cancel > order > account > market data. A 429/418 (or a
Retry-After header) puts *every* bucket of that key into backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypeVar

from helm.core.errors import RateLimitedError

T = TypeVar("T")


class Priority(IntEnum):
    CANCEL = 0
    ORDER = 1
    ACCOUNT = 2
    MARKET_DATA = 3


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    weight: int = field(compare=False)
    future: asyncio.Future[None] = field(compare=False)


class RateGovernor:
    """One token bucket + priority dispatcher.

    `capacity` is the burst size, `refill_per_s` the steady rate. To stay under a
    venue limit of N requests per W-second sliding window, use
    capacity + refill_per_s * W <= N (see `for_window`).
    """

    def __init__(
        self,
        capacity: float,
        refill_per_s: float,
        *,
        time_fn: Callable[[], float] | None = None,
        name: str = "",
    ) -> None:
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill_per_s must be positive")
        self.name = name
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._time = time_fn
        self._tokens = capacity
        self._last: float | None = None
        self._backoff_until = 0.0
        self._heap: list[_Waiter] = []
        self._seq = itertools.count()
        self._wakeup: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self.granted: dict[Priority, int] = dict.fromkeys(Priority, 0)

    @classmethod
    def for_window(
        cls, limit: int, window_s: float, *, safety: float = 0.9, **kw: object
    ) -> RateGovernor:
        budget = limit * safety
        return cls(capacity=max(1.0, budget / 2), refill_per_s=budget / 2 / window_s, **kw)  # type: ignore[arg-type]

    def _now(self) -> float:
        return self._time() if self._time else asyncio.get_running_loop().time()

    def _refill(self) -> None:
        now = self._now()
        if self._last is not None:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.refill_per_s)
        self._last = now

    def penalize(self, retry_after_s: float | None) -> None:
        """429/418 or Retry-After: stop granting until the backoff passes."""
        delay = retry_after_s if retry_after_s and retry_after_s > 0 else 1.0
        self._refill()
        self._backoff_until = max(self._backoff_until, self._now() + delay)
        self._tokens = 0.0
        if self._wakeup is not None:
            self._wakeup.set()

    async def acquire(self, priority: Priority, weight: int = 1) -> None:
        if weight > self.capacity:
            raise ValueError(f"weight {weight} exceeds bucket capacity {self.capacity}")
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, _Waiter(int(priority), next(self._seq), weight, fut))
        self._ensure_dispatcher()
        await fut
        self.granted[priority] += 1

    def _ensure_dispatcher(self) -> None:
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        self._wakeup.set()
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._dispatch())

    async def _dispatch(self) -> None:
        assert self._wakeup is not None
        while self._heap:
            self._wakeup.clear()
            self._refill()
            now = self._now()
            if now < self._backoff_until:
                await self._sleep(self._backoff_until - now)
                continue
            head = self._heap[0]
            if head.future.cancelled():
                heapq.heappop(self._heap)
                continue
            if self._tokens >= head.weight:
                heapq.heappop(self._heap)
                self._tokens -= head.weight
                head.future.set_result(None)
                await asyncio.sleep(0)
                continue
            await self._sleep((head.weight - self._tokens) / self.refill_per_s)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake early if a higher-priority waiter or a penalty arrives."""
        assert self._wakeup is not None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), timeout=max(seconds, 0.0005))

    async def call(self, priority: Priority, fn: Callable[[], Awaitable[T]], weight: int = 1) -> T:
        await self.acquire(priority, weight)
        try:
            return await fn()
        except RateLimitedError as exc:
            self.penalize(exc.retry_after_s)
            raise


class GovernorRegistry:
    """Buckets keyed by (venue, api_key_id, weight_class); penalties apply per key."""

    def __init__(self, factory: Callable[[str, str, str], RateGovernor]) -> None:
        self._factory = factory
        self._govs: dict[tuple[str, str, str], RateGovernor] = {}

    def get(self, venue: str, key_id: str, weight_class: str = "default") -> RateGovernor:
        k = (venue, key_id, weight_class)
        if k not in self._govs:
            self._govs[k] = self._factory(venue, key_id, weight_class)
        return self._govs[k]

    def penalize_key(self, venue: str, key_id: str, retry_after_s: float | None) -> None:
        for (v, kid, _cls), gov in self._govs.items():
            if v == venue and kid == key_id:
                gov.penalize(retry_after_s)
