"""Core models, ids, money, clock, bus (DATA-1, PRD 6.5, 6.6, 8)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from helm.core.bus import InProcessBus
from helm.core.clock import SimClock, ms, seconds
from helm.core.enums import LifecycleStage, Mode, OrderState, OrderType, Side
from helm.core.errors import QuantizationError
from helm.core.events import Alert, decode_event, event_kind
from helm.core.ids import MAX_COID_LEN, SequenceAllocator, client_order_id, make_run_id
from helm.core.models import Bar, OrderIntent, parse_symbol
from helm.core.money import D, bps, is_multiple, quantize_down, quantize_price
from helm.infra.clock import LiveClock
from tests.factories import bar, instrument, intent, quote


def test_symbol_parsing() -> None:
    assert parse_symbol("binance:BTC/USDT:perp") == ("binance", "BTC", "USDT", "perp")
    for bad in ("BTCUSD", "x:BTC", ":A/B", "a:b:c:d"):
        with pytest.raises(ValueError):
            parse_symbol(bad)
    with pytest.raises(ValidationError):
        instrument(venue="other")
    assert instrument().base == "BTC"


def test_bar_sanity_rejected() -> None:
    with pytest.raises(ValidationError):
        bar(0, 10, 9, 8, 9.5)  # high below open
    with pytest.raises(ValidationError):
        Bar(
            symbol="a:B/C",
            timeframe="1m",
            ts_open=5,
            ts_close=5,
            o=D(1),
            h=D(1),
            l=D(1),
            c=D(1),
            volume=D(0),
        )


def test_intent_validation() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(strategy_id="s", symbol="a:B/C", side=Side.BUY, type=OrderType.LIMIT, qty=D(1))
    with pytest.raises(ValidationError):
        OrderIntent(strategy_id="s", symbol="a:B/C", side=Side.BUY, type=OrderType.STOP, qty=D(1))
    with pytest.raises(ValidationError):
        intent(type_=OrderType.LIMIT, limit="-1")
    assert intent().intent_id == intent().intent_id
    assert intent().intent_id != intent(qty="0.02").intent_id


def test_models_are_frozen_and_roundtrip() -> None:
    it = intent()
    with pytest.raises(ValidationError):
        it.qty = D(5)  # type: ignore[misc]
    assert decode_event(event_kind(it), it.model_dump_json()) == it
    assert quote().mid == Decimal("100.05")
    with pytest.raises(KeyError):
        event_kind(instrument())


def test_client_order_id_deterministic_and_safe() -> None:
    assert client_order_id("abcd1234", "sma", 7) == "abcd1234-sma-7"
    long = client_order_id("abcd1234", "a very/long strategy name with spaces" * 3, 123456)
    assert len(long) <= MAX_COID_LEN
    assert long == client_order_id("abcd1234", "a very/long strategy name with spaces" * 3, 123456)
    with pytest.raises(ValueError):
        client_order_id("r", "s", -1)
    with pytest.raises(ValueError):
        client_order_id("r", "s" * 40, 10**25)
    assert make_run_id("x") == make_run_id("x") and len(make_run_id("x")) == 8


def test_sequence_allocator_recovers_from_journal() -> None:
    seqs = SequenceAllocator()
    assert [seqs.next("a"), seqs.next("a"), seqs.next("b")] == [0, 1, 0]
    recovered = SequenceAllocator()
    recovered.observe("a", 1)
    recovered.observe("a", 0)
    assert recovered.next("a") == 2


def test_money_quantization() -> None:
    assert quantize_down(D("1.23456"), D("0.001")) == D("1.234")
    assert quantize_down(D("-1.23456"), D("0.001")) == D("-1.234")
    assert quantize_price(D("100.017"), D("0.01"), side_sign=1) == D("100.01")
    assert quantize_price(D("100.011"), D("0.01"), side_sign=-1) == D("100.02")
    assert quantize_price(D("100.015"), D("0.01")) == D("100.02")
    assert is_multiple(D("0.03"), D("0.01")) and not is_multiple(D("0.035"), D("0.01"))
    assert D(0.1) == Decimal("0.1") and D("2") == 2 and D(3) == 3
    assert bps(D("101"), D("100")) == D("100")
    with pytest.raises(QuantizationError):
        quantize_down(D(1), D(0))
    with pytest.raises(QuantizationError):
        D("abc")
    with pytest.raises(QuantizationError):
        bps(D(1), D(0))


def test_clocks() -> None:
    c = SimClock(10)
    c.advance_by(5)
    assert c.now() == 15
    with pytest.raises(ValueError):
        c.advance_to(3)
    assert LiveClock().now() > 1_600_000_000 * 10**9
    assert ms(1) == 1_000_000 and seconds(1) == 10**9


def test_enums() -> None:
    assert Mode.PAPER.badge == "[PAPER]"
    assert Side.BUY.opposite is Side.SELL and Side.SELL.sign == -1
    assert OrderState.FILLED.is_terminal and not OrderState.UNKNOWN.is_terminal
    assert LifecycleStage.LIVE.at_least(LifecycleStage.LIVE_SMALL)
    assert not LifecycleStage.PAPER.at_least(LifecycleStage.LIVE_SMALL)
    a = Alert(severity="CRITICAL", key="k", message="m", mode=Mode.LIVE, ts=0)  # type: ignore[arg-type]
    assert a.text == "[LIVE] CRITICAL: m"


def test_bus_fanout_filters_and_lossy_backpressure() -> None:
    async def run() -> tuple[list[BaseModel], list[BaseModel], int]:
        bus = InProcessBus()
        reliable = bus.subscribe("oms", (OrderIntent,))
        lossy = bus.subscribe("ui", None, maxsize=2, reliable=False)
        for i in range(5):
            await bus.publish(intent(qty=f"0.0{i + 1}"))
        await bus.publish(quote())
        await bus.close()
        got_r = [e async for e in reliable]
        got_l = [e async for e in lossy]
        return got_r, got_l, lossy.dropped

    got_r, got_l, dropped = asyncio.run(run())
    assert len(got_r) == 5  # reliable: everything of its type, nothing else
    assert dropped >= 4 and len(got_l) <= 2  # lossy UI never blocks publisher
