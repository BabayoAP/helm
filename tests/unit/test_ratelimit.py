"""DATA-6: rate-limit governor."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from helm.brokers.fake_exchange import FakeExchange, FakeExchangeConfig
from helm.core.errors import RateLimitedError
from helm.infra.ratelimit import GovernorRegistry, Priority, RateGovernor
from tests.factories import SYM


def test_load_10x_overdemand_never_429_and_cancels_not_starved() -> None:
    async def run() -> None:
        limit, window = 10, 0.1  # venue: 10 requests per 100 ms (strict)
        fx = FakeExchange(config=FakeExchangeConfig(rate_limit_per_s=limit, rate_window_s=window))
        fx.set_price(SYM, Decimal(100), Decimal(101))
        gov = RateGovernor.for_window(limit, window)
        loop = asyncio.get_running_loop()
        cancel_waits: list[float] = []

        async def market_data() -> None:
            await gov.call(Priority.MARKET_DATA, fx.balances)

        async def cancel() -> None:
            t = loop.time()
            await gov.call(Priority.CANCEL, lambda: fx.cancel("nope"))
            cancel_waits.append(loop.time() - t)

        # demand ~10x the limit: 300 data requests queued at once (limit allows ~30 in that time)
        flood = [asyncio.create_task(market_data()) for _ in range(300)]
        await asyncio.sleep(0.05)
        cancels = [asyncio.create_task(cancel()) for _ in range(5)]
        await asyncio.gather(*cancels)
        assert max(cancel_waits) < 0.2  # cancels jump ahead of 290+ queued data requests
        await asyncio.gather(*flood)
        assert fx.rate_limited_count == 0
        assert gov.granted[Priority.MARKET_DATA] == 300 and gov.granted[Priority.CANCEL] == 5

    asyncio.run(run())


def test_penalty_on_429_backs_off_whole_key() -> None:
    async def run() -> None:
        reg = GovernorRegistry(lambda v, k, c: RateGovernor(5, 1000, name=f"{v}/{k}/{c}"))
        a = reg.get("x", "key1", "orders")
        b = reg.get("x", "key1", "data")
        other = reg.get("x", "key2", "data")
        assert reg.get("x", "key1", "orders") is a

        async def boom() -> None:
            raise RateLimitedError("429", retry_after_s=0.2)

        with pytest.raises(RateLimitedError):
            await a.call(Priority.ORDER, boom)
        reg.penalize_key("x", "key1", 0.2)
        loop = asyncio.get_running_loop()
        t = loop.time()
        await b.acquire(Priority.MARKET_DATA)
        assert loop.time() - t >= 0.15
        t = loop.time()
        await other.acquire(Priority.MARKET_DATA)
        assert loop.time() - t < 0.05
        a.penalize(None)  # default 1 s backoff when header missing
        with pytest.raises(ValueError):
            await a.acquire(Priority.ORDER, weight=99)

    asyncio.run(run())


def test_governor_validation() -> None:
    with pytest.raises(ValueError):
        RateGovernor(0, 1)
    g = RateGovernor.for_window(100, 1.0, safety=0.8)
    assert g.capacity + g.refill_per_s * 1.0 <= 80 + 1e-9
