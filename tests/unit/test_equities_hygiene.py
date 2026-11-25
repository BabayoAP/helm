"""DATA-9: adjusted vs raw equities bars, kept apart and recorded; survivorship warning."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import polars as pl
import pytest

from helm.app.backtest import data_warnings
from helm.app.research import DataSource
from helm.core.enums import AssetClass
from helm.data.connectors.alpaca import AlpacaOhlcvClient
from helm.data.connectors.alpaca_common import AlpacaCredentials, instrument_for
from helm.data.parquet_store import ParquetStore, SeriesKey
from tests.factories import instrument

CREDS = AlpacaCredentials(key_id="PKTESTKEYID000", secret_key="test-secret-not-real-0000")


def test_adjustment_parameter_is_sent_for_stocks_only() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"bars": {}})

    async def run() -> None:
        t = httpx.MockTransport(handle)
        await AlpacaOhlcvClient(CREDS, crypto=False, adjustment="all", transport=t).bars(
            "AAPL", "1d", 0, 10
        )
        await AlpacaOhlcvClient(CREDS, crypto=True, transport=t).bars("BTC/USD", "1d", 0, 10)

    asyncio.run(run())
    assert seen[0].url.params["adjustment"] == "all"
    assert "adjustment" not in seen[1].url.params
    with pytest.raises(ValueError, match="adjustment"):
        AlpacaOhlcvClient(CREDS, crypto=False, adjustment="splits")


def test_adjusted_and_raw_series_never_mix(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    df = pl.DataFrame(
        {"ts_open": [0], "o": [100.0], "h": [101.0], "l": [99.0], "c": [100.5], "volume": [10.0]}
    )
    adj = pl.DataFrame(
        {"ts_open": [0], "o": [50.0], "h": [50.5], "l": [49.5], "c": [50.25], "volume": [20.0]}
    )
    sym = "alpaca:AAPL/USD"
    store.write_bars(SeriesKey(AssetClass.EQUITY, sym, "bars", "1d"), df)
    store.write_bars(SeriesKey(AssetClass.EQUITY, sym, "bars_adj", "1d"), adj)

    def src(adjusted: bool) -> DataSource:
        return DataSource(
            symbol=sym,
            timeframe="1d",
            asset_class="equity",
            start="1970-01-01",
            data_dir=str(tmp_path),
            adjusted=adjusted,
        )

    raw = src(False).load()
    adjusted = src(True).load()
    assert float(raw[0].c) == 100.5 and float(adjusted[0].c) == 50.25
    # the choice is part of the experiment record (RES-2 metadata)
    assert json.loads(src(True).describe())["adjusted"] is True


def test_equities_tearsheets_warn_about_survivorship_and_raw_prices() -> None:
    eq = [instrument_for("alpaca:AAPL/USD")]
    assert data_warnings([instrument()], None) == []  # crypto: no corporate actions
    w = data_warnings(eq, False)
    assert "Survivorship bias" in w[0] and "Raw (unadjusted)" in w[1]
    assert len(data_warnings(eq, True)) == 1
    assert "unknown" in data_warnings(eq, None)[1]
