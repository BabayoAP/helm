"""Bar-frame validation and gap reports (DATA-2).

Canonical bar frame columns: ts_open (i64 ns), o, h, l, c, volume (f64), and
optionally trades (i64). Research uses floats; the event boundary converts to
Decimal (PRD §4).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl

from helm.core.timeframes import timeframe_ns

BAR_COLUMNS = ("ts_open", "o", "h", "l", "c", "volume")
BAR_SCHEMA = pl.Schema(
    {
        "ts_open": pl.Int64(),
        "o": pl.Float64(),
        "h": pl.Float64(),
        "l": pl.Float64(),
        "c": pl.Float64(),
        "volume": pl.Float64(),
    }
)


@dataclass(frozen=True)
class Gap:
    start_ns: int  # first missing bar ts_open
    end_ns: int  # last missing bar ts_open
    missing_bars: int


@dataclass
class ValidationReport:
    rows_in: int = 0
    invalid_ohlc: int = 0
    negative_volume: int = 0
    null_rows: int = 0
    duplicates: int = 0
    non_monotonic: int = 0
    expected_bars: int = 0
    actual_bars: int = 0
    gaps: list[Gap] = field(default_factory=lambda: [])

    @property
    def missing_bars(self) -> int:
        return sum(g.missing_bars for g in self.gaps)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["missing_bars"] = self.missing_bars
        return d


def _ohlc_ok() -> pl.Expr:
    return (
        (pl.col("l") <= pl.min_horizontal("o", "c"))
        & (pl.col("h") >= pl.max_horizontal("o", "c"))
        & (pl.col("l") <= pl.col("h"))
    ).fill_null(False)


def sanity_mask(df: pl.DataFrame) -> pl.Series:
    """True for rows that pass OHLC + volume sanity."""
    return df.select(_ohlc_ok() & (pl.col("volume") >= 0).fill_null(False)).to_series()


def clean_bars(df: pl.DataFrame, report: ValidationReport) -> pl.DataFrame:
    """Drop null / insane rows and count each class of problem. Does not dedupe."""
    report.rows_in += df.height
    nulls = df.select(pl.any_horizontal(pl.col(*BAR_COLUMNS).is_null())).to_series()
    report.null_rows += int(nulls.sum())
    df = df.filter(~nulls)
    flags = df.select(ohlc=_ohlc_ok(), vol=pl.col("volume") >= 0)
    report.invalid_ohlc += int((~flags["ohlc"]).sum())
    report.negative_volume += int((flags["ohlc"] & ~flags["vol"]).sum())
    return df.filter(flags["ohlc"] & flags["vol"])


def finalize(df: pl.DataFrame, timeframe: str, report: ValidationReport) -> pl.DataFrame:
    """Dedupe on timestamp (first wins), sort, count non-monotonic input, gap report."""
    ts = df["ts_open"]
    if df.height > 1:
        report.non_monotonic += int((ts.diff().drop_nulls() < 0).sum())
    before = df.height
    df = df.unique(subset=["ts_open"], keep="first", maintain_order=True).sort("ts_open")
    report.duplicates += before - df.height
    report.gaps = gap_report(df["ts_open"], timeframe)
    report.actual_bars = df.height
    report.expected_bars = df.height + sum(g.missing_bars for g in report.gaps)
    return df


def gap_report(ts_open: pl.Series, timeframe: str, *, max_gaps: int = 10_000) -> list[Gap]:
    """Missing bars between first and last bar (expected vs actual)."""
    if ts_open.len() < 2:
        return []
    step = timeframe_ns(timeframe)
    s = ts_open.sort()
    diffs = s.diff()
    frame = pl.DataFrame({"prev": s.shift(1), "diff": diffs}).with_row_index()
    holes = frame.filter(pl.col("diff") > step)
    gaps: list[Gap] = []
    for prev, diff in holes.select("prev", "diff").iter_rows():
        missing = int(diff // step) - 1
        if missing <= 0:
            continue
        gaps.append(
            Gap(start_ns=int(prev) + step, end_ns=int(prev) + missing * step, missing_bars=missing)
        )
        if len(gaps) >= max_gaps:
            break
    return gaps


def misaligned(ts_open: pl.Series, timeframe: str) -> int:
    """Count bars whose ts_open is not a multiple of the timeframe."""
    return int((ts_open % timeframe_ns(timeframe) != 0).sum())
