"""DATA-2 importer + Parquet lake, DATA-8 instruments, DATA-4 recorder."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from helm.core.enums import AssetClass, OrderType, Side
from helm.data.importer import import_bars, normalize_ts, resolve_columns
from helm.data.instruments import InstrumentRegistry, UnknownInstrumentError, instrument_from_ccxt
from helm.data.parquet_store import ParquetStore, SeriesKey, symbol_dir
from helm.data.recorder import Recorder, replay
from helm.data.validators import gap_report
from tests.factories import MIN, SYM, bar, instrument, intent, quote

KEY = SeriesKey(AssetClass.CRYPTO, SYM, "bars", "1m")
T0 = 1_704_067_200 * 10**9  # 2024-01-01T00:00Z


def _csv(
    path: Path, rows: list[tuple[object, ...]], header: str = "timestamp,open,high,low,close,volume"
) -> Path:
    path.write_text(header + "\n" + "\n".join(",".join(map(str, r)) for r in rows) + "\n")
    return path


def test_import_validates_dedupes_reports_gaps_and_is_idempotent(tmp_path: Path) -> None:
    ms = T0 // 10**6
    rows: list[tuple[object, ...]] = [(ms + i * 60_000, 10, 11, 9, 10.5, 5) for i in range(10)]
    rows.append(rows[3])  # duplicate
    rows.append((ms + 20 * 60_000, 10, 9, 8, 9.5, 1))  # high < open: insane
    rows.append((ms + 21 * 60_000, 10, 11, 9, 10, -1))  # negative volume
    rows.append((ms + 15 * 60_000, 10, 11, 9, 10, 1))  # after a 5-bar gap, out of order below
    rows.append((ms + 12 * 60_000, 10, 11, 9, 10, 1))
    src = _csv(tmp_path / "in.csv", rows)
    store = ParquetStore(tmp_path)
    rep = import_bars(src, store, KEY, batch_rows=4)
    v = rep.validation
    assert rep.rows_written == 12
    assert (v.duplicates, v.invalid_ohlc, v.negative_volume) == (1, 1, 1)
    assert v.non_monotonic >= 1
    assert [(g.missing_bars) for g in v.gaps] == [2, 2]  # 10,11 missing; 13,14 missing
    assert v.expected_bars == 16 and v.actual_bars == 12
    part = store.partition_path(KEY, 2024, 1)
    assert part.exists() and "crypto/fake/BTC-USD/bars/1m/year=2024/month=01.parquet" in str(part)
    again = import_bars(src, store, KEY)
    assert again.rows_written == 0
    assert store.read_bars(KEY).height == 12
    assert rep.to_dict()["missing_bars"] == 4


def test_import_spans_months_and_formats(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    jan31 = T0 + 30 * 86400 * 10**9 + 23 * 3600 * 10**9 + 59 * MIN
    rows: list[tuple[object, ...]] = [
        ("2024-01-31T23:59:00Z", 1, 2, 0.5, 1.5, 3),
        ("2024-02-01 00:00:00", 1, 2, 0.5, 1.5, 3),
    ]
    src = _csv(tmp_path / "iso.csv", rows, header="Date,Open,High,Low,Close,Volume")
    rep = import_bars(src, store, KEY)
    assert rep.rows_written == 2 and len(store.partitions(KEY)) == 2
    assert store.read_bars(KEY)["ts_open"].to_list() == [jan31, jan31 + MIN]
    pq = tmp_path / "in.parquet"
    pl.DataFrame(
        {"t": [T0 // 10**9], "o": [1.0], "h": [1.0], "l": [1.0], "c": [1.0], "v": [0.0]}
    ).write_parquet(pq)
    assert import_bars(pq, store, KEY).rows_written == 1


def test_timestamp_normalization_units() -> None:
    for scale in (1, 10**3, 10**6, 10**9):
        s = pl.Series("t", [T0 // 10**9 * scale])
        assert normalize_ts(s).to_list() == [T0]
    assert normalize_ts(pl.Series("t", [float(T0 // 10**6)])).to_list() == [T0]
    dt = pl.Series("t", [T0]).cast(pl.Datetime("ns")).dt.replace_time_zone("UTC")
    assert normalize_ts(dt).to_list() == [T0]
    assert normalize_ts(pl.Series("t", [T0]).cast(pl.Datetime("ns")).cast(pl.Date)).to_list() == [
        T0
    ]
    with pytest.raises(ValueError, match="volume"):
        resolve_columns(["time", "open", "high", "low", "close"])


def test_store_reads_decimal_bars_and_duckdb(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    assert store.last_ts(KEY) is None and store.read_bars(KEY).height == 0
    df = pl.DataFrame(
        {
            "ts_open": [T0, T0 + MIN],
            "o": [0.1, 0.2],
            "h": [0.3, 0.3],
            "l": [0.1, 0.2],
            "c": [0.2, 0.3],
            "volume": [1.0, 2.0],
        }
    )
    assert store.write_bars(KEY, df) == 2
    assert store.write_bars(KEY, pl.DataFrame(schema=df.schema)) == 0
    bars = list(store.iter_bars(KEY))
    assert bars[0].o == Decimal("0.1") and bars[1].ts_close == T0 + 2 * MIN
    assert store.last_ts(KEY) == T0 + MIN
    assert store.read_bars(KEY, start_ns=T0 + 1, end_ns=T0 + 2 * MIN).height == 1
    out = store.query("SELECT count(*) AS n FROM read_parquet('{lake}/**/*.parquet')")
    assert out["n"][0] == 2
    assert symbol_dir("binance:BTC/USDT:perp") == "BTC-USDT-perp"


def test_gap_report_edge_cases() -> None:
    assert gap_report(pl.Series([T0]), "1m") == []
    gaps = gap_report(pl.Series([T0, T0 + 4 * MIN]), "1m")
    assert gaps[0].missing_bars == 3 and gaps[0].start_ns == T0 + MIN


def test_instrument_registry_conform_and_cache(tmp_path: Path) -> None:
    async def run() -> None:
        calls = 0

        async def loader() -> list:  # type: ignore[type-arg]
            nonlocal calls
            calls += 1
            return [instrument()]

        reg = InstrumentRegistry(loader=loader, cache_path=tmp_path / "inst.json", ttl_ns=100)
        assert await reg.refresh(0) and not await reg.refresh(50) and await reg.refresh(200)
        assert calls == 2 and SYM in reg and reg.find("x:Y/Z") is None
        with pytest.raises(UnknownInstrumentError):
            reg.get("x:Y/Z")
        it = reg.conform(intent(qty="0.123456", type_=OrderType.LIMIT, limit="100.019"))
        assert it.qty == Decimal("0.1234") and it.limit_price == Decimal("100.01")
        sell = reg.conform(intent(side=Side.SELL, type_=OrderType.LIMIT, limit="100.011"))
        assert sell.limit_price == Decimal("100.02")
        offline = InstrumentRegistry(cache_path=tmp_path / "inst.json")
        assert offline.get(SYM) == instrument()
        assert not await offline.refresh(0)
        offline.upsert(instrument("fake:ETH/USD"))
        assert [i.symbol for i in offline.all()] == ["fake:BTC/USD", "fake:ETH/USD"]

    asyncio.run(run())


def test_instrument_from_ccxt() -> None:
    m = {
        "base": "BTC",
        "quote": "USDT",
        "spot": True,
        "precision": {"price": 0.01, "amount": 0.00001},
        "limits": {"amount": {"min": 0.00001}, "cost": {"min": 5}},
        "active": True,
    }
    inst = instrument_from_ccxt("kraken", m)
    assert inst is not None and inst.symbol == "kraken:BTC/USDT" and inst.min_notional == 5
    perp = instrument_from_ccxt("x", {**m, "spot": False, "type": "swap", "contractSize": 1})
    assert perp is not None and perp.symbol == "x:BTC/USDT:swap"
    assert instrument_from_ccxt("x", {**m, "active": False}) is None


def test_recorder_replays_bit_for_bit(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, "s1", flush_every=3)
    events = [bar(i, 1, 2, 0.5, 1.5) for i in range(5)] + [
        quote(ts=7),
        quote("99.99", "100.01", ts=8),
    ]
    for ev in events:
        rec.record(ev)
    rec.close()
    assert list(replay(rec.dir)) == events
    assert [e.model_dump_json() for e in replay(rec.dir)] == [e.model_dump_json() for e in events]
    assert len(list(replay(rec.dir, kinds=("Quote",)))) == 2
    rec2 = Recorder(tmp_path, "s1")  # resume appends with continuing seq
    asyncio.run(rec2.on_event(quote(ts=9)))
    rec2.close()
    assert len(list(replay(rec.dir))) == 8
    assert list(replay(tmp_path / "nope")) == []
