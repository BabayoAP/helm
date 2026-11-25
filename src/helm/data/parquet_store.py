"""Partitioned Parquet lake (DATA-2, PRD §4).

Layout: lake/{asset_class}/{venue}/{symbol}/{datatype}/{timeframe}/year=YYYY/month=MM.parquet
(zstd). Writes merge with the existing partition, dedupe on timestamp and sort,
so re-imports are idempotent. Each partition is rewritten atomically
(write temp file, then rename).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl

from helm.core.enums import AssetClass
from helm.core.models import Bar, parse_symbol
from helm.core.timeframes import timeframe_ns
from helm.data.validators import BAR_SCHEMA

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def symbol_dir(symbol: str) -> str:
    """'binance:BTC/USDT:perp' -> 'BTC-USDT-perp' (venue is its own directory level)."""
    _venue, base, quote, kind = parse_symbol(symbol)
    name = f"{base}-{quote}" + (f"-{kind}" if kind else "")
    return _SAFE.sub("_", name)


@dataclass(frozen=True)
class SeriesKey:
    asset_class: AssetClass
    symbol: str  # canonical venue:BASE/QUOTE[:kind]
    datatype: str = "bars"
    timeframe: str = "1m"

    @property
    def venue(self) -> str:
        return parse_symbol(self.symbol)[0]


class ParquetStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def series_dir(self, key: SeriesKey) -> Path:
        return (
            self.root
            / "lake"
            / key.asset_class.value
            / key.venue
            / symbol_dir(key.symbol)
            / key.datatype
            / key.timeframe
        )

    def partition_path(self, key: SeriesKey, year: int, month: int) -> Path:
        return self.series_dir(key) / f"year={year:04d}" / f"month={month:02d}.parquet"

    def partitions(self, key: SeriesKey) -> list[Path]:
        d = self.series_dir(key)
        return sorted(d.glob("year=*/month=*.parquet")) if d.exists() else []

    # ----------------------------------------------------------------- writes
    @staticmethod
    def with_partition_cols(df: pl.DataFrame) -> pl.DataFrame:
        dt = pl.from_epoch(pl.col("ts_open"), time_unit="ns")
        return df.with_columns(_year=dt.dt.year(), _month=dt.dt.month())

    def write_bars(self, key: SeriesKey, df: pl.DataFrame) -> int:
        """Merge `df` into the lake. Returns number of *new* rows written."""
        if df.height == 0:
            return 0
        df = self.with_partition_cols(df.select(list(BAR_SCHEMA)).cast(BAR_SCHEMA))
        added = 0
        for (year, month), part in df.group_by("_year", "_month", maintain_order=True):
            added += self._merge_partition(key, int(year), int(month), part.drop("_year", "_month"))  # type: ignore[arg-type]
        return added

    def _merge_partition(self, key: SeriesKey, year: int, month: int, new: pl.DataFrame) -> int:
        path = self.partition_path(key, year, month)
        existing = pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=BAR_SCHEMA)
        merged = (
            pl.concat([existing, new], how="vertical")
            .unique(subset=["ts_open"], keep="first", maintain_order=True)
            .sort("ts_open")
        )
        added = merged.height - existing.height
        if added == 0 and path.exists():
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        merged.write_parquet(tmp, compression="zstd", statistics=True)
        os.replace(tmp, path)
        return added

    # ------------------------------------------------------------------ reads
    def scan_bars(
        self, key: SeriesKey, start_ns: int | None = None, end_ns: int | None = None
    ) -> pl.LazyFrame:
        parts = self.partitions(key)
        if not parts:
            return pl.DataFrame(schema=BAR_SCHEMA).lazy()
        lf = pl.scan_parquet(parts)
        if start_ns is not None:
            lf = lf.filter(pl.col("ts_open") >= start_ns)
        if end_ns is not None:
            lf = lf.filter(pl.col("ts_open") < end_ns)
        return lf.sort("ts_open")

    def read_bars(
        self, key: SeriesKey, start_ns: int | None = None, end_ns: int | None = None
    ) -> pl.DataFrame:
        return self.scan_bars(key, start_ns, end_ns).collect()

    def last_ts(self, key: SeriesKey) -> int | None:
        parts = self.partitions(key)
        if not parts:
            return None
        v = pl.read_parquet(parts[-1], columns=["ts_open"])["ts_open"].max()
        return int(v) if v is not None else None  # type: ignore[arg-type]

    def iter_bars(
        self, key: SeriesKey, start_ns: int | None = None, end_ns: int | None = None
    ) -> Iterator[Bar]:
        """Decimal `Bar` events for the event-driven engine (floats -> Decimal via repr)."""
        step = timeframe_ns(key.timeframe)
        df = self.read_bars(key, start_ns, end_ns)
        for ts, o, h, l, c, v in df.select("ts_open", "o", "h", "l", "c", "volume").iter_rows():  # noqa: E741
            yield Bar(
                symbol=key.symbol,
                timeframe=key.timeframe,
                ts_open=ts,
                ts_close=ts + step,
                o=Decimal(repr(o)),
                h=Decimal(repr(h)),
                l=Decimal(repr(l)),
                c=Decimal(repr(c)),
                volume=Decimal(repr(v)),
            )

    def query(self, sql: str) -> pl.DataFrame:
        """Ad-hoc DuckDB SQL over the lake; `lake` is the lake root path in the query."""
        con = duckdb.connect()
        try:
            rel = con.sql(sql.replace("{lake}", str(self.root / "lake")))
            return pl.DataFrame(rel.fetchall(), schema=rel.columns, orient="row")
        finally:
            con.close()
