"""Alpaca symbols/timestamps (decision 0025) and market-data feed (DATA-1/3/5)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from helm.core.enums import AssetClass
from helm.core.models import Bar, Quote, Trade
from helm.data.connectors.alpaca import (
    AlpacaFeedHandler,
    AlpacaOhlcvClient,
    alpaca_timeframe,
    stream_url,
)
from helm.data.connectors.alpaca_common import (
    AlpacaCredentials,
    from_venue,
    instrument_for,
    iso,
    norm_qty,
    parse_ts,
    to_venue,
    trading_url,
)
from helm.data.connectors.ccxt_history import fetch_history
from helm.data.parquet_store import ParquetStore, SeriesKey
from helm.infra.ratelimit import RateGovernor

CREDS = AlpacaCredentials(key_id="PKTESTKEYID000", secret_key="test-secret-not-real-0000")
MIN = 60 * 10**9
T0 = parse_ts("2026-11-24T15:00:00Z")


def test_symbol_mapping_round_trips() -> None:
    assert to_venue("alpaca:AAPL/USD") == "AAPL"
    assert to_venue("alpaca:BTC/USD:crypto") == "BTC/USD"
    assert from_venue("AAPL") == "alpaca:AAPL/USD"
    assert from_venue("BTC/USD") == "alpaca:BTC/USD:crypto"
    assert from_venue("BTCUSD", "crypto") == "alpaca:BTC/USD:crypto"
    assert from_venue("ETHUSDT", "crypto") == "alpaca:ETH/USDT:crypto"
    for bad in ("kraken:BTC/USD", "alpaca:AAPL/EUR", "alpaca:ES/USD:perp"):
        with pytest.raises(ValueError):
            to_venue(bad)
    eq, cr = instrument_for("alpaca:AAPL/USD"), instrument_for("alpaca:BTC/USD:crypto")
    assert (eq.asset_class, eq.calendar, eq.lot_size) == (AssetClass.EQUITY, "us_equity_rth", 1)
    assert (cr.asset_class, cr.calendar) == (AssetClass.CRYPTO, "24x7")
    cr2 = instrument_for("alpaca:BTC/USD:crypto", {"min_trade_increment": "0.000000001"})
    assert cr2.lot_size == Decimal("0.000000001")
    assert trading_url("paper").startswith("https://paper-api.")


def test_timestamps_any_precision_and_offsets() -> None:
    assert parse_ts("2026-11-24T15:00:00Z") == T0
    assert parse_ts("2026-11-24T15:00:00.5Z") == T0 + 500_000_000
    assert parse_ts("2026-11-24T15:00:00.123456789Z") == T0 + 123_456_789
    assert parse_ts("2026-11-24T10:00:00.000001-05:00") == T0 + 1000
    assert parse_ts(iso(T0 + 42)) == T0 + 42


@given(st.integers(min_value=0, max_value=4_000_000_000 * 10**9))
def test_iso_parse_round_trip(ts: int) -> None:
    assert parse_ts(iso(ts)) == ts


@given(st.decimals(min_value=0, max_value=10**9, places=9, allow_nan=False))
def test_norm_qty_is_canonical(d: Decimal) -> None:
    assert norm_qty(d) == norm_qty(Decimal(str(d)) * 1) == norm_qty(f"{d:.12f}")


def _handler(**kw: object) -> AlpacaFeedHandler:
    return AlpacaFeedHandler(
        CREDS,
        ("alpaca:AAPL/USD", "alpaca:MSFT/USD"),
        now_fn=lambda: T0 + 5 * MIN,
        **kw,  # type: ignore[arg-type]
    )


def test_feed_handler_subscribe_and_parse() -> None:
    h = _handler(trades=True)
    auth, sub = (json.loads(m) for m in h.subscribe_messages())
    assert auth["action"] == "auth" and auth["key"] == CREDS.key_id
    assert sub == {
        "action": "subscribe",
        "bars": ["AAPL", "MSFT"],
        "quotes": ["AAPL", "MSFT"],
        "trades": ["AAPL", "MSFT"],
    }
    raw = json.dumps(
        [
            {"T": "success", "msg": "authenticated"},
            {
                "T": "b",
                "S": "AAPL",
                "o": 190,
                "h": 191,
                "l": 189.5,
                "c": 190.5,
                "v": 1200,
                "n": 33,
                "t": "2026-11-24T15:00:00Z",
            },
            {
                "T": "q",
                "S": "MSFT",
                "bp": 400.1,
                "bs": 2,
                "ap": 400.2,
                "as": 3,
                "t": "2026-11-24T15:00:01.5Z",
            },
            {"T": "t", "S": "AAPL", "p": 190.4, "s": 10, "i": 77, "t": "2026-11-24T15:00:02Z"},
            {"T": "b", "S": "AAPL", "o": 1, "h": 0.5, "l": 1, "c": 1, "t": "2026-11-24T15:01:00Z"},
            {"T": "q", "S": "AAPL"},
            {"T": "u", "S": "AAPL"},
        ]
    )
    out = h.parse(raw)
    assert [type(e) for e in out] == [Bar, Quote, Trade]
    bar = out[0]
    assert isinstance(bar, Bar) and bar.symbol == "alpaca:AAPL/USD" and bar.closed
    assert bar.ts_close - bar.ts_open == MIN and bar.c == Decimal("190.5")
    q = out[1]
    assert isinstance(q, Quote) and q.ts_recv == T0 + 5 * MIN and q.symbol == "alpaca:MSFT/USD"
    assert h.bad_messages == 2  # broken OHLC + missing fields: counted, never raised (DATA-1)
    with pytest.raises(ConnectionError, match="406"):
        h.parse(json.dumps({"T": "error", "code": 406, "msg": "connection limit exceeded"}))
    with pytest.raises(ValueError, match="asset class"):
        AlpacaFeedHandler(CREDS, ("alpaca:AAPL/USD", "alpaca:BTC/USD:crypto"), now_fn=lambda: 0)
    with pytest.raises(ValueError, match="1m"):
        AlpacaFeedHandler(CREDS, ("alpaca:AAPL/USD",), timeframe="5m", now_fn=lambda: 0)
    assert stream_url(crypto=False) == "wss://stream.data.alpaca.markets/v2/iex"
    assert stream_url(crypto=True).endswith("/v1beta3/crypto/us")
    with pytest.raises(ValueError):
        alpaca_timeframe("7m")


def _bars_server(n: int, calls: list[httpx.Request]) -> httpx.MockTransport:
    rows = [
        {"t": iso(T0 + i * MIN), "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100.5 + i, "v": 10}
        for i in range(n)
    ]

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "data.alpaca.markets"
        calls.append(req)
        start = parse_ts(req.url.params["start"])
        limit = int(req.url.params["limit"])
        sym = req.url.params["symbols"]
        page = [r for r in rows if parse_ts(str(r["t"])) >= start][:limit]
        return httpx.Response(200, json={"bars": {sym: page}, "next_page_token": None})

    return httpx.MockTransport(handle)


def test_history_fetch_resumes_and_gap_fill_returns_only_closed_bars(tmp_path: Path) -> None:
    async def run() -> None:
        calls: list[httpx.Request] = []
        client = AlpacaOhlcvClient(CREDS, crypto=False, transport=_bars_server(10, calls))
        store = ParquetStore(tmp_path)
        key = SeriesKey(AssetClass.EQUITY, "alpaca:AAPL/USD", timeframe="1m")
        gov = RateGovernor(100, 100)
        r1 = await fetch_history(
            client, store, key, start_ns=T0, end_ns=T0 + 6 * MIN, governor=gov, page_limit=4
        )
        assert r1.rows_written == 6
        r2 = await fetch_history(
            client, store, key, start_ns=T0, end_ns=T0 + 10 * MIN, governor=gov, page_limit=4
        )
        assert r2.rows_written == 4 and store.read_bars(key).height == 10  # resumed, no dupes
        assert calls[0].url.params["feed"] == "iex" and calls[0].url.params["timeframe"] == "1Min"

        h = AlpacaFeedHandler(
            CREDS, ("alpaca:AAPL/USD",), rest=client, now_fn=lambda: T0 + 5 * MIN + 1
        )
        bars = await h.gap_fill("alpaca:AAPL/USD", "1m", T0 + MIN)
        assert [b.ts_open for b in bars] == [T0 + 2 * MIN, T0 + 3 * MIN, T0 + 4 * MIN]
        assert await _handler().gap_fill("alpaca:AAPL/USD", "1m", T0) == []  # no REST client
        with pytest.raises(NotImplementedError):
            await h.book_snapshot("alpaca:AAPL/USD")
        crypto_calls: list[httpx.Request] = []
        cc = AlpacaOhlcvClient(CREDS, crypto=True, transport=_bars_server(2, crypto_calls))
        assert len(await cc.fetch_ohlcv("BTC/USD", "1m", T0 // 10**6, 10)) == 2
        assert crypto_calls[0].url.path == "/v1beta3/crypto/us/bars"
        assert "feed" not in crypto_calls[0].url.params
        await client.close()
        await cc.close()

    asyncio.run(run())
