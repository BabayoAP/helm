"""Paper trading runner (PAP-1..3): live feed + SimBroker (L1 live-fill model).

Uses the exact engine loop of backtest and live (I1). Feed events are consumed
asynchronously; supervision (breakers), reconciliation and flatten-failure
re-alerting run as periodic tasks. All output is tagged [PAPER] (I8, PAP-2).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from helm.app.engine import Engine
from helm.core.events import FeedStatus
from helm.core.models import Bar, Quote, Trade
from helm.data.recorder import Recorder


async def dispatch(engine: Engine, ev: BaseModel) -> None:
    if isinstance(ev, Quote):
        await engine.on_quote(ev)
    elif isinstance(ev, Trade):
        await engine.on_trade(ev)
    elif isinstance(ev, Bar):
        await engine.on_bar(ev)
    elif isinstance(ev, FeedStatus):
        await engine.on_feed_status(ev)


@dataclass
class RunnerConfig:
    supervise_every_s: float = 1.0
    reconcile_every_s: float = 60.0
    flatten_realert_s: float = 30.0


class PaperRunner:
    def __init__(
        self,
        engine: Engine,
        feed: AsyncIterator[BaseModel],
        *,
        config: RunnerConfig | None = None,
        recorder: Recorder | None = None,
        on_event: Callable[[BaseModel], None] | None = None,
    ) -> None:
        self.engine = engine
        self.feed = feed
        self.cfg = config or RunnerConfig()
        self.recorder = recorder
        self.on_event = on_event
        self.events = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _every(self, seconds: float, fn: Callable[[], object]) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            if self._stop.is_set():
                return
            r = fn()
            if asyncio.iscoroutine(r):
                await r

    async def _flatten_watch(self) -> None:
        if self.engine.flatten_failures:
            self.engine.alerts.critical(
                "flatten_failed", "position still open after flatten; retrying"
            )
            await self.engine.flatten()

    async def _consume(self) -> None:
        async for ev in self.feed:
            if self.recorder is not None:
                self.recorder.record(ev)
            await dispatch(self.engine, ev)
            self.events += 1
            if self.on_event is not None:
                self.on_event(ev)
            if self._stop.is_set():
                break
        self._stop.set()

    async def run(self) -> None:
        timers = [
            asyncio.create_task(self._every(self.cfg.supervise_every_s, self.engine.supervise)),
            asyncio.create_task(self._every(self.cfg.reconcile_every_s, self.engine.reconcile)),
            asyncio.create_task(self._every(self.cfg.flatten_realert_s, self._flatten_watch)),
        ]
        try:
            await self._consume()
        finally:
            self._stop.set()
            for t in timers:
                t.cancel()
            await asyncio.gather(*timers, return_exceptions=True)
            if self.recorder is not None:
                self.recorder.flush()
