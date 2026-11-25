"""Historical OHLCV fetch via CCXT REST (DATA-3).

Resumable: starts from the last bar already in the lake, writes every page
before requesting the next, so an interrupted fetch resumes without
duplicates. Every request passes the rate-limit governor (DATA-6).
Market data only: no credentials are needed or accepted here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import polars as pl

from helm.core.models import parse_symbol
from helm.core.timeframes import timeframe_ns
from helm.data.parquet_store import ParquetStore, SeriesKey
from helm.data.validators import ValidationReport, clean_bars
from helm.infra.ratelimit import Priority, RateGovernor


class OhlcvClient(Protocol):
    """Subset of `ccxt.async_support.Exchange` we use."""

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]: ...


@dataclass
class FetchResult:
    pages: int = 0
    rows_written: int = 0
    last_ts_open: int | None = None
    report: ValidationReport | None = None


def ccxt_symbol(symbol: str) -> str:
    _venue, base, quote, kind = parse_symbol(symbol)
    return f"{base}/{quote}" + (f":{quote}" if kind in ("perp", "swap") else "")


async def fetch_history(
    client: OhlcvClient,
    store: ParquetStore,
    key: SeriesKey,
    *,
    start_ns: int,
    end_ns: int,
    governor: RateGovernor,
    page_limit: int = 1000,
    now_ns: int | None = None,
) -> FetchResult:
    """Fetch closed bars in [start, end) and merge into the lake, resuming if possible."""
    step = timeframe_ns(key.timeframe)
    res = FetchResult(report=ValidationReport())
    last = store.last_ts(key)
    cursor = max(start_ns, last + step) if last is not None and last >= start_ns else start_ns
    while cursor < end_ns:

        async def _page(since_ms: int = cursor // 1_000_000) -> list[list[Any]]:
            return await client.fetch_ohlcv(
                ccxt_symbol(key.symbol), key.timeframe, since_ms, page_limit
            )

        rows = await governor.call(Priority.MARKET_DATA, _page)
        res.pages += 1
        if not rows:
            break
        df = pl.DataFrame(
            [r[:6] for r in rows], schema=["ts_open", "o", "h", "l", "c", "volume"], orient="row"
        ).with_columns(
            (pl.col("ts_open").cast(pl.Int64) * 1_000_000).alias("ts_open"),
            *(pl.col(c).cast(pl.Float64) for c in ("o", "h", "l", "c", "volume")),
        )
        df = df.filter((pl.col("ts_open") >= cursor) & (pl.col("ts_open") < end_ns))
        if now_ns is not None:  # never store the still-forming bar
            df = df.filter(pl.col("ts_open") + step <= now_ns)
        df = clean_bars(df, res.report)  # type: ignore[arg-type]
        if df.height == 0:
            break
        res.rows_written += store.write_bars(key, df)
        newest = int(df["ts_open"].max())  # type: ignore[arg-type]
        res.last_ts_open = newest
        if newest + step <= cursor:
            break
        cursor = newest + step
    return res
