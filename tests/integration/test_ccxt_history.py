"""DATA-3: interrupted fetch resumes without duplicates; respects the limiter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from helm.core.enums import AssetClass
from helm.data.connectors.ccxt_history import ccxt_symbol, fetch_history
from helm.data.parquet_store import ParquetStore, SeriesKey
from helm.infra.ratelimit import Priority, RateGovernor
from tests.factories import MIN, SYM

T0 = 1_704_067_200 * 10**9
KEY = SeriesKey(AssetClass.CRYPTO, SYM, "bars", "1m")


class FakeOhlcv:
    def __init__(self, n: int, fail_on_call: int | None = None) -> None:
        self.n = n
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.since: list[int] = []

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        self.calls += 1
        assert symbol == "BTC/USD" and since is not None and limit is not None
        self.since.append(since)
        if self.fail_on_call == self.calls:
            raise ConnectionError("network blip")
        start = (since * 10**6 - T0) // MIN
        return [
            [(T0 + i * MIN) // 10**6, 1, 2, 0.5, 1.5, 10]
            for i in range(max(start, 0), min(start + limit, self.n))
        ]


def test_resume_after_interruption_has_no_duplicates(tmp_path: Path) -> None:
    async def run() -> None:
        store = ParquetStore(tmp_path)
        gov = RateGovernor(100, 1000)
        end = T0 + 250 * MIN
        flaky = FakeOhlcv(250, fail_on_call=3)
        with pytest.raises(ConnectionError):
            await fetch_history(
                flaky, store, KEY, start_ns=T0, end_ns=end, governor=gov, page_limit=100
            )
        assert store.read_bars(KEY).height == 200  # two pages persisted before the blip
        ok = FakeOhlcv(250)
        res = await fetch_history(
            ok, store, KEY, start_ns=T0, end_ns=end, governor=gov, page_limit=100
        )
        assert ok.since[0] == (T0 + 200 * MIN) // 10**6  # resumed after the last stored bar
        assert res.rows_written == 50
        ts = store.read_bars(KEY)["ts_open"]
        assert ts.len() == 250 and ts.n_unique() == 250
        assert gov.granted[Priority.MARKET_DATA] == 3 + 1  # every request went through the governor
        # the still-forming bar is never stored
        res2 = await fetch_history(
            FakeOhlcv(300),
            store,
            KEY,
            start_ns=T0,
            end_ns=T0 + 300 * MIN,
            governor=gov,
            now_ns=T0 + 260 * MIN + 1,
        )
        assert res2.last_ts_open == T0 + 259 * MIN

    asyncio.run(run())


def test_ccxt_symbol_mapping() -> None:
    assert ccxt_symbol("kraken:BTC/USD") == "BTC/USD"
    assert ccxt_symbol("bybit:BTC/USDT:perp") == "BTC/USDT:USDT"
