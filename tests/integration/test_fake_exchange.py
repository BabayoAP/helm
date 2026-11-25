"""FakeExchange harness behaviour (TEST-1)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from helm.brokers.base import Broker
from helm.brokers.fake_exchange import FakeExchange, FakeExchangeConfig
from helm.core.clock import SimClock
from helm.core.enums import OrderState, OrderType, Side
from helm.core.errors import AmbiguousSubmitError, BrokerError, DisconnectedError, RateLimitedError
from helm.core.models import Fill
from tests.factories import SYM, intent, order


def _fx(**kw: object) -> FakeExchange:
    fx = FakeExchange(config=FakeExchangeConfig(**kw), clock=SimClock())  # type: ignore[arg-type]
    fx.set_price(SYM, Decimal("100"), Decimal("101"))
    return fx


def test_is_a_broker_and_market_fill() -> None:
    async def run() -> None:
        fx = _fx()
        assert isinstance(fx, Broker)
        await fx.submit(order())
        o = await fx.get_order("r1-s1-0")
        assert o is not None and o.state is OrderState.FILLED and o.avg_px == Decimal("101")
        (pos,) = await fx.positions()
        assert pos.qty == Decimal("0.01")
        fills = [e for e in fx.drain_events() if isinstance(e, Fill)]
        assert len(fills) == 1 and fills[0].fee > 0
        (bal,) = await fx.balances()
        assert bal.free < Decimal("100000")

    asyncio.run(run())


def test_limit_rests_then_fills_on_trade_through_and_cancel() -> None:
    async def run() -> None:
        fx = _fx(partial_fill_parts=3)
        await fx.submit(order("a", intent(type_=OrderType.LIMIT, limit="99")))
        await fx.submit(order("b", intent(type_=OrderType.LIMIT, limit="98")))
        assert {o.client_order_id for o in await fx.open_orders()} == {"a", "b"}
        fx.set_price(SYM, Decimal("98.5"), Decimal("99"))
        a = await fx.get_order("a")
        assert a is not None and a.state is OrderState.FILLED
        await fx.cancel("b")
        await fx.cancel("b")  # idempotent
        await fx.cancel("zzz")
        b = await fx.get_order("b")
        assert b is not None and b.state is OrderState.CANCELED
        assert sum(isinstance(e, Fill) for e in fx.drain_events()) == 3

    asyncio.run(run())


def test_duplicate_client_order_id_rejected_by_venue() -> None:
    async def run() -> None:
        fx = _fx()
        await fx.submit(order())
        with pytest.raises(BrokerError, match="duplicate"):
            await fx.submit(order())
        assert fx.accepted_submits["r1-s1-0"] == 1

    asyncio.run(run())


def test_faults_ambiguous_disconnect_ratelimit_reject() -> None:
    async def run() -> None:
        fx = _fx(p_ambiguous_submit=1.0)
        with pytest.raises(AmbiguousSubmitError):
            await fx.submit(order())
        assert (await fx.get_order("r1-s1-0")) is not None  # venue did accept it
        fx.disconnected = True
        with pytest.raises(DisconnectedError):
            await fx.positions()
        rl = _fx(rate_limit_per_s=2)
        await rl.balances()
        await rl.balances()
        with pytest.raises(RateLimitedError):
            await rl.balances()
        rj = _fx(p_reject=1.0)
        await rj.submit(order())
        o = await rj.get_order("r1-s1-0")
        assert o is not None and o.state is OrderState.REJECTED

    asyncio.run(run())


def test_flatten_and_reduce_only() -> None:
    async def run() -> None:
        fx = _fx(duplicate_events=True, reorder_events=True, partial_fill_parts=2)
        await fx.submit(order("a", intent(qty="0.5")))
        await fx.submit(order("b", intent(side=Side.SELL, qty="0.2")))
        await fx.submit(order("c", intent(type_=OrderType.LIMIT, limit="50")))
        await fx.flatten_all()
        assert await fx.positions() == [] and await fx.open_orders() == []
        await fx.submit(order("d", intent(side=Side.SELL, qty="1", reduce_only=True)))
        d = await fx.get_order("d")
        assert d is not None and d.state is OrderState.CANCELED
        fx.inject_position(SYM, Decimal("-1"), Decimal("100"))
        await fx.submit(order("e", intent(qty="3", reduce_only=True)))
        assert await fx.positions() == []
        events = fx.drain_events()
        assert len(events) > 8
        stream = fx.stream()
        fx.set_price(SYM, Decimal("1"), Decimal("2"))
        await fx.submit(order("f", intent(side=Side.SELL, qty="0.1")))
        await fx.submit(order("g", intent(qty="0.3")))  # flips the short to long
        first = await anext(stream)
        assert first.client_order_id == "f"
        (pos,) = await fx.positions()
        assert pos.qty == Decimal("0.2") and pos.avg_px == Decimal("2")

    asyncio.run(run())
