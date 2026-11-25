"""Alpaca adapter contract tests (decisions 0023/0025, EXE-1, EXE-3, EXE-9, SEC-1).

Runs against `tests.alpaca_fake.FakeAlpaca` through httpx.MockTransport: no network.
Every request is asserted to hit the *paper* host; reaching the real-money host
fails the test.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from helm.app.engine import Engine
from helm.app.gates import GATE_TOKEN, LIVE_ENV, build_broker
from helm.app.lifecycle import build_engine, recover, resync
from helm.app.venues import live_factory
from helm.brokers.live.alpaca import AlpacaBroker, strategy_of
from helm.core.clock import SimClock
from helm.core.config import BotConfig, HelmConfig, RiskConfig
from helm.core.enums import (
    LifecycleStage,
    Mode,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from helm.core.errors import (
    AmbiguousSubmitError,
    BrokerError,
    DisconnectedError,
    LiveGateError,
    RateLimitedError,
)
from helm.core.events import OrderEvent
from helm.core.models import Fill, Order, OrderIntent
from helm.data.connectors.alpaca_common import AlpacaCredentials, instrument_for
from helm.data.instruments import InstrumentRegistry
from helm.oms.journal import Journal
from helm.tracking.alerts import AlertManager
from tests.alpaca_fake import FakeAlpaca, FakeWs

AAPL = "alpaca:AAPL/USD"
BTC = "alpaca:BTC/USD:crypto"
CREDS = AlpacaCredentials(key_id="PKTESTKEYID000", secret_key="test-secret-not-real-0000")
D = Decimal


def make(fx: FakeAlpaca, *, env: str = "paper", ws: Any = None, **kw: Any) -> AlpacaBroker:
    async def connect(_url: str) -> Any:
        if ws is None:
            raise ConnectionError("no ws")
        return ws() if callable(ws) else ws

    return AlpacaBroker(
        account_id="alpaca-paper",
        environment=env,
        credentials=CREDS,
        clock=SimClock(1),
        transport=fx.transport(),
        ws_connect=connect,
        _gate_token=GATE_TOKEN,
        **kw,
    )


def it(
    side: Side = Side.BUY,
    qty: str = "10",
    symbol: str = AAPL,
    tif: TimeInForce = TimeInForce.DAY,
    reduce_only: bool = False,
    typ: OrderType = OrderType.MARKET,
    limit: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        strategy_id="sma-x",
        symbol=symbol,
        side=side,
        type=typ,
        qty=D(qty),
        tif=tif,
        reduce_only=reduce_only,
        limit_price=D(limit) if limit else None,
    )


def order(coid: str, intent: OrderIntent | None = None) -> Order:
    return Order(
        client_order_id=coid,
        account_id="alpaca-paper",
        intent=intent or it(),
        ts_created=0,
        ts_updated=0,
    )


# ------------------------------------------------------------------ gates / urls
def test_alpaca_only_through_the_triple_gate_and_never_for_paper_accounts() -> None:
    with pytest.raises(LiveGateError, match="only be built"):
        AlpacaBroker(
            account_id="alpaca-paper",
            environment="paper",
            credentials=CREDS,
            clock=SimClock(),
            _gate_token=object(),
        )
    with pytest.raises(LiveGateError, match="PAP-2"):
        AlpacaBroker(
            account_id="paper-x",
            environment="paper",
            credentials=CREDS,
            clock=SimClock(),
            _gate_token=GATE_TOKEN,
        )
    bot = BotConfig(
        id="b",
        strategy="x:Y",
        mode=Mode.LIVE,
        account_id="alpaca-paper",
        venue="alpaca",
        symbols=(AAPL,),
        lifecycle=LifecycleStage.LIVE_SMALL,
    )

    class Store:
        def get(self, name: str) -> str:
            return {"alpaca_paper_key_id": "k" * 8, "alpaca_paper_secret_key": "s" * 8}[name]

    factory = live_factory(HelmConfig(), Store(), SimClock())
    with pytest.raises(LiveGateError, match=r"gate \(a\)"):
        build_broker(bot, env={}, clock=SimClock(), live_factory=factory)
    b = build_broker(bot, env={LIVE_ENV: "1"}, clock=SimClock(), live_factory=factory)
    assert isinstance(b, AlpacaBroker)
    assert b.base_url == "https://paper-api.alpaca.markets"  # default config is paper
    other = bot.model_copy(update={"venue": "kraken", "symbols": ("kraken:BTC/USD",)})
    with pytest.raises(LiveGateError, match="no live adapter"):
        build_broker(other, env={LIVE_ENV: "1"}, clock=SimClock(), live_factory=factory)
    with pytest.raises(ValueError, match="paper' or 'live"):
        make(FakeAlpaca(), env="sandbox")
    assert "redacted" in repr(CREDS) and CREDS.secret_key not in repr(CREDS)


def test_preflight_sec1_and_pdt_warnings() -> None:
    async def run() -> None:
        fx = FakeAlpaca()
        b = make(fx)
        acct, warns = await b.preflight(attest_no_withdrawal=False)  # paper: attest not needed
        assert acct.status == "ACTIVE" and warns == []
        fx.account.update(equity="20000", daytrade_count=3, pattern_day_trader=True)
        _, warns = await b.preflight(attest_no_withdrawal=False)
        assert any("pattern_day_trader" in w for w in warns)
        assert any("PDT" in w for w in warns)
        fx.account["account_number"] = "123456"  # not a paper-looking account on paper URL
        _, warns = await b.preflight(attest_no_withdrawal=False)
        assert any("does not look like a paper" in w for w in warns)
        fx.account["trading_blocked"] = True
        with pytest.raises(LiveGateError, match="blocked"):
            await b.preflight(attest_no_withdrawal=False)
        fx.account.update(trading_blocked=False, status="ACCOUNT_UPDATED")
        with pytest.raises(LiveGateError, match="not ACTIVE"):
            await b.preflight(attest_no_withdrawal=False)
        await b.close()
        live = make(FakeAlpaca(), env="live")
        assert live.base_url == "https://api.alpaca.markets"
        with pytest.raises(LiveGateError, match="attest_no_withdrawal"):
            await live.preflight(attest_no_withdrawal=False)  # refused before any request
        await live.close()

    asyncio.run(run())


# ----------------------------------------------------------------------- orders
def test_submit_maps_fields_and_handles_venue_answers() -> None:
    async def run() -> None:
        fx = FakeAlpaca()
        b = make(fx)
        await b.submit(order("r1-sma-x-0", it(typ=OrderType.LIMIT, limit="190.5")))
        o = await b.get_order("r1-sma-x-0")
        assert o is not None and o.state is OrderState.ACCEPTED and o.venue_order_id == "oid-1"
        assert o.intent.limit_price == D("190.5") and o.intent.strategy_id == "sma-x"
        sent = fx.orders["oid-1"]
        assert (sent["symbol"], sent["qty"], sent["type"], sent["time_in_force"]) == (
            "AAPL",
            "10",
            "limit",
            "day",
        )
        # idempotent resubmit of the same client_order_id (I4)
        await b.submit(order("r1-sma-x-0"))
        assert len(fx.orders) == 1
        assert await b.get_order("never-sent") is None
        # ambiguous outcomes -> AmbiguousSubmitError (UNKNOWN, resolved by lookup)
        fx.fail_next = ["timeout"]
        with pytest.raises(AmbiguousSubmitError):
            await b.submit(order("r1-sma-x-1"))
        fx.fail_next = [503]
        with pytest.raises(AmbiguousSubmitError):
            await b.submit(order("r1-sma-x-2"))
        fx.fail_next = [429]
        with pytest.raises(RateLimitedError):
            await b.submit(order("r1-sma-x-3"))
        fx.fail_next = [403]
        with pytest.raises(BrokerError, match="403"):
            await b.submit(order("r1-sma-x-4"))
        fx.fail_next = ["timeout"]
        with pytest.raises(DisconnectedError):
            await b.positions()  # non-submit transport errors are disconnects, not ambiguous
        fx.fail_next = [500]
        with pytest.raises(BrokerError):
            await b.open_orders()
        # post-only does not exist at Alpaca
        with pytest.raises(BrokerError, match="post-only"):
            await b.submit(order("r1-sma-x-5", it(tif=TimeInForce.POST_ONLY)))
        # EXE-9: equities refused while the venue clock says closed; crypto is 24x7
        fx.market_open = False
        b._clock_cache = None  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(BrokerError, match="market closed"):
            await b.submit(order("r1-sma-x-6"))
        await b.submit(order("r1-sma-x-7", it(symbol=BTC, qty="0.01", tif=TimeInForce.GTC)))
        assert fx.orders["oid-2"]["symbol"] == "BTC/USD"
        await b.close()

    asyncio.run(run())


def test_reduce_only_is_emulated_and_cancel_and_flatten() -> None:
    async def run() -> None:
        fx = FakeAlpaca()
        b = make(fx)
        with pytest.raises(BrokerError, match="reduce-only"):
            await b.submit(order("r-s-0", it(side=Side.SELL, reduce_only=True)))  # flat
        await b.submit(order("r-s-1", it(qty="5")))
        fx.fill("r-s-1", "5", "190")
        with pytest.raises(BrokerError, match="reduce-only"):
            await b.submit(order("r-s-2", it(side=Side.SELL, qty="6", reduce_only=True)))
        with pytest.raises(BrokerError, match="reduce-only"):
            await b.submit(order("r-s-3", it(side=Side.BUY, qty="1", reduce_only=True)))
        await b.submit(order("r-s-4", it(side=Side.SELL, qty="5", reduce_only=True)))
        pos = await b.positions()
        assert [(p.symbol, p.qty) for p in pos] == [(AAPL, D(5))]
        assert (await b.balances())[0].free == D(100000)
        await b.cancel("r-s-4")
        assert (await b.get_order("r-s-4")).state is OrderState.CANCELED  # type: ignore[union-attr]
        with pytest.raises(BrokerError, match="not cancelable"):
            await b.cancel("r-s-4")
        with pytest.raises(BrokerError, match="unknown"):
            await b.cancel("nope")
        await b.submit(order("r-s-5", it(symbol=BTC, qty="0.5", tif=TimeInForce.GTC)))
        fx.fill("r-s-5", "0.5", "60000")
        crypto = [p for p in await b.positions() if p.symbol == BTC]
        assert crypto and crypto[0].qty == D("0.5")  # BTCUSD -> alpaca:BTC/USD:crypto
        await b.submit(order("r-s-6", it(qty="1", typ=OrderType.LIMIT, limit="1")))
        assert [o.client_order_id for o in await b.open_orders()] == ["r-s-6"]
        await b.flatten_all()
        assert await b.positions() == [] and await b.open_orders() == []
        assert await b.asset("alpaca:NOPE/USD") is None
        await b.close()

    asyncio.run(run())


def test_order_status_mapping_and_strategy_parsing() -> None:
    b = make(FakeAlpaca())
    base = {
        "id": "o1",
        "client_order_id": "ab12-my-strat-7",
        "symbol": "AAPL",
        "qty": "3",
        "side": "sell",
        "type": "stop",
        "stop_price": "180",
        "time_in_force": "gtc",
        "created_at": "2026-11-24T15:00:00Z",
    }
    cases = {
        "new": OrderState.ACCEPTED,
        "pending_cancel": OrderState.ACCEPTED,
        "partially_filled": OrderState.PARTIALLY_FILLED,
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELED,
        "replaced": OrderState.CANCELED,
        "expired": OrderState.EXPIRED,
        "rejected": OrderState.REJECTED,
        "weird": OrderState.UNKNOWN,
    }
    for status, state in cases.items():
        assert b.order_from({**base, "status": status}).state is state
    o = b.order_from({**base, "status": "done_for_day", "filled_qty": "1"})
    assert o.state is OrderState.PARTIALLY_FILLED and o.intent.type is OrderType.STOP
    assert o.intent.strategy_id == "my-strat" and o.intent.tif is TimeInForce.GTC
    trailing = b.order_from({**base, "type": "trailing_stop", "stop_price": None, "status": "new"})
    assert trailing.intent.type is OrderType.MARKET
    assert strategy_of("manual-order") == "external"
    assert b.parse_trade_update({"event": "new"}) == []
    assert b.parse_trade_update({"event": "pending_cancel", "order": base}) == []


# ------------------------------------------------- fill identity (decision 0025)
def test_stream_and_rest_fills_share_trade_ids_so_dedupe_is_exact() -> None:
    async def run() -> None:
        fx = FakeAlpaca()
        fx.page_size_cap = 2  # force activity pagination
        b = make(fx)
        await b.submit(order("r-s-0", it(qty="10")))
        await b.submit(order("r-s-1", it(qty="1")))
        msgs = [fx.fill("r-s-0", "3", "190"), fx.fill("r-s-1", "1", "191")]
        msgs += [fx.fill("r-s-0", "3.0", "190.5"), fx.fill("r-s-0", "4", "191")]
        streamed = [
            ev
            for m in msgs
            for ev in b.parse_trade_update(m["data"])
            if isinstance(ev, Fill) and ev.client_order_id == "r-s-0"
        ]
        rest = await b.fills_for_order("r-s-0")
        assert [f.trade_id for f in streamed] == [f.trade_id for f in rest]
        assert [f.trade_id for f in rest] == ["oid-1:3", "oid-1:6", "oid-1:10"]
        assert sum((f.qty for f in rest), D(0)) == D(10)
        assert await b.fills_for_order("never") == []
        ev = b.parse_trade_update(fx.event("r-s-1", "canceled")["data"])
        assert isinstance(ev[0], OrderEvent) and ev[0].state is OrderState.CANCELED
        await b.close()

    asyncio.run(run())


# ----------------------------------------------------------------------- stream
def test_stream_auth_listen_reconnect_and_resync_hook() -> None:
    async def run() -> None:
        fx = FakeAlpaca()
        conns: list[FakeWs] = []

        def factory() -> FakeWs:
            ws = FakeWs()
            conns.append(ws)
            return ws

        b = make(fx, ws=factory)
        resyncs: list[int] = []

        async def on_reconnect() -> None:
            resyncs.append(1)

        b.on_reconnect = on_reconnect
        await b.submit(order("r-s-0", it(qty="2")))
        stream = b.stream()

        async def feed() -> None:
            while not conns:
                await asyncio.sleep(0)
            conns[0].push({"stream": "listening", "data": {}})
            conns[0].push(fx.fill("r-s-0", "1", "190"))
            conns[0].push(None)  # drop
            while len(conns) < 2:
                await asyncio.sleep(0)
            conns[1].push(fx.fill("r-s-0", "1", "191"))

        task = asyncio.create_task(feed())
        got = [await anext(stream), await anext(stream)]
        await task
        assert [type(e).__name__ for e in got] == ["Fill", "Fill"]
        assert conns[0].sent[0]["action"] == "auth" and conns[0].sent[0]["secret"]
        assert conns[0].sent[1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
        assert b.reconnects == 1 and resyncs == [1]
        assert b.stream_url() == "wss://paper-api.alpaca.markets/stream"
        await b.close()

        bad = make(fx, ws=lambda: FakeWs(authorized=False))
        with pytest.raises(BrokerError, match="authorization"):
            await bad._open_stream()  # pyright: ignore[reportPrivateUsage]
        await bad.close()

    asyncio.run(run())


# -------------------------------------------- engine end-to-end + crash recovery
def _engine(tmp: Path, fx: FakeAlpaca) -> tuple[Engine, Any]:
    journal = Journal(tmp / "j.sqlite", mirror=False)
    clock = SimClock(10**18)
    broker = make(fx)
    eng, prev = build_engine(
        mode=Mode.LIVE,
        account_id="alpaca-paper",
        run_id="r1",
        clock=clock,
        broker=broker,
        journal=journal,
        instruments=InstrumentRegistry([instrument_for(AAPL)]),
        risk_config=RiskConfig(max_order_notional_usd=D(5000), fat_finger_pct=D(50)),
        starting_equity=D(100000),
        code_fn=lambda: "code",
        alerts=AlertManager(mode=Mode.LIVE, clock_fn=clock.now),
    )
    return eng, prev


def test_engine_round_trip_and_restart_recovers_missed_fill_without_double_count(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        from helm.core.models import Quote

        fx = FakeAlpaca()
        eng, prev = _engine(tmp_path, fx)
        await recover(eng, previous=prev)
        # force US market open for the engine's RTH calendar check
        eng.instruments.upsert(instrument_for(AAPL).model_copy(update={"calendar": "24x7"}))
        ts = eng.clock.now()
        q = Quote(
            symbol=AAPL,
            bid=D(190),
            bid_sz=D(1),
            ask=D("190.1"),
            ask_sz=D(1),
            ts_event=ts,
            ts_recv=ts,
        )
        await eng.on_quote(q)
        await eng.submit_intent(None, it(qty="5"))
        (coid,) = [o.client_order_id for o in eng.om.all()]
        assert eng.om.get(coid).state is OrderState.PENDING_SUBMIT
        assert fx._by_coid(coid) is not None  # pyright: ignore[reportPrivateUsage]
        b: AlpacaBroker = eng.broker  # type: ignore[assignment]
        for ev in b.parse_trade_update(fx.event(coid, "new")["data"]):
            await eng.on_broker_event(ev)
        first = fx.fill(coid, "2", "190")
        for ev in b.parse_trade_update(first["data"]):
            await eng.on_broker_event(ev)
            await eng.on_broker_event(ev)  # duplicate stream delivery
        assert eng.portfolio.positions[AAPL].qty == D(2)
        # the rest fills while the user stream is down
        fx.fill(coid, "3", "190.2")
        assert await resync(eng) == 1
        assert eng.om.get(coid).state is OrderState.FILLED
        assert eng.portfolio.positions[AAPL].qty == D(5)
        assert eng.reconcile_result is not None and eng.reconcile_result.ok
        eng.journal.close()

        # crash + restart: journal rebuild + venue lookup, nothing double counted
        eng2, prev2 = _engine(tmp_path, fx)
        rep = await recover(eng2, previous=prev2)
        assert rep.fills_from_venue == 0 and not rep.unexpected
        assert eng2.portfolio.positions[AAPL].qty == D(5)
        eng2.journal.close()
        await b.close()

    asyncio.run(run())
