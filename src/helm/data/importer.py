"""Historical importer: CSV / Parquet -> Parquet lake (DATA-2).

Streaming: the input is read in batches (Polars streaming engine), each batch is
normalised + validated and spilled to per-month staging files, then each month
partition is merged into the lake. Peak memory is about one batch plus one
month partition, independent of input size.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from helm.data.parquet_store import ParquetStore, SeriesKey
from helm.data.validators import BAR_SCHEMA, ValidationReport, clean_bars, gap_report, misaligned

_ALIASES: dict[str, tuple[str, ...]] = {
    "ts_open": ("ts_open", "timestamp", "time", "datetime", "date", "ts", "open_time", "t"),
    "o": ("o", "open"),
    "h": ("h", "high"),
    "l": ("l", "low"),
    "c": ("c", "close"),
    "volume": ("volume", "vol", "v", "base_volume"),
}


@dataclass
class ImportReport:
    source: str
    series: str
    validation: ValidationReport = field(default_factory=ValidationReport)
    rows_written: int = 0
    misaligned: int = 0
    batches: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "series": self.series,
            "rows_written": self.rows_written,
            "misaligned": self.misaligned,
            "batches": self.batches,
            **self.validation.to_dict(),
        }


def resolve_columns(columns: list[str]) -> dict[str, str]:
    """Map input column names -> canonical names. Raises if a required column is missing."""
    lower = {c.lower().strip(): c for c in columns}
    mapping: dict[str, str] = {}
    for canon, aliases in _ALIASES.items():
        for a in aliases:
            if a in lower:
                mapping[lower[a]] = canon
                break
        else:
            raise ValueError(f"input has no column for {canon!r} (tried {aliases}); got {columns}")
    return mapping


def _epoch_unit_factor(max_abs: float) -> int:
    """Infer epoch unit from magnitude: s, ms, us or ns -> multiplier to ns."""
    if max_abs >= 1e17:
        return 1
    if max_abs >= 1e14:
        return 1_000
    if max_abs >= 1e11:
        return 1_000_000
    return 1_000_000_000


def normalize_ts(s: pl.Series) -> pl.Series:
    """Any timestamp column -> Int64 UTC ns."""
    dtype = s.dtype
    if dtype.is_numeric():
        mx = s.abs().max()
        factor = _epoch_unit_factor(float(mx) if mx is not None else 0.0)  # type: ignore[arg-type]
        return (
            (s.cast(pl.Float64) * factor).round(0).cast(pl.Int64)
            if dtype.is_float()
            else s.cast(pl.Int64) * factor
        )
    if isinstance(dtype, pl.Datetime):
        dt = s if dtype.time_zone is None else s.dt.convert_time_zone("UTC")
        return dt.dt.epoch("ns").alias(s.name)
    if isinstance(dtype, pl.Date):
        return s.cast(pl.Datetime("ns")).dt.epoch("ns")
    text = s.cast(pl.String).str.strip_chars()
    parsed = text.str.to_datetime(strict=False, time_unit="ns")
    if parsed.null_count() > text.null_count():
        cleaned = text.str.replace("T", " ").str.replace(r"(Z|[+-]00:?00)$", "")
        parsed = cleaned.str.to_datetime("%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="ns")
        if parsed.null_count() > text.null_count():
            parsed = cleaned.str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False, time_unit="ns")
    tz = parsed.dtype.time_zone if isinstance(parsed.dtype, pl.Datetime) else None
    if tz is not None:
        parsed = parsed.dt.convert_time_zone("UTC")
    return parsed.dt.epoch("ns")


def normalize_batch(df: pl.DataFrame, mapping: dict[str, str]) -> pl.DataFrame:
    df = df.select([pl.col(src).alias(dst) for src, dst in mapping.items()])
    df = df.with_columns(normalize_ts(df["ts_open"]).alias("ts_open"))
    return df.select(
        pl.col("ts_open").cast(pl.Int64),
        *(pl.col(c).cast(pl.Float64, strict=False) for c in ("o", "h", "l", "c", "volume")),
    )


def _scan(path: Path) -> pl.LazyFrame:
    if path.suffix.lower() == ".parquet":
        return pl.scan_parquet(path)
    return pl.scan_csv(path, infer_schema_length=10_000, try_parse_dates=False)


def import_bars(
    path: Path | str,
    store: ParquetStore,
    key: SeriesKey,
    *,
    batch_rows: int = 500_000,
) -> ImportReport:
    path = Path(path)
    report = ImportReport(source=str(path), series=str(store.series_dir(key)))
    lf = _scan(path)
    mapping = resolve_columns(lf.collect_schema().names())
    lf = lf.select(list(mapping))
    staging = Path(
        tempfile.mkdtemp(prefix="helm-import-", dir=store.root if store.root.exists() else None)
    )
    try:
        staged: dict[tuple[int, int], list[Path]] = {}
        last_ts: int | None = None
        for batch in lf.collect_batches(chunk_size=batch_rows):
            report.batches += 1
            df = clean_bars(normalize_batch(batch, mapping), report.validation)
            if df.height == 0:
                continue
            ts = df["ts_open"]
            report.validation.non_monotonic += int((ts.diff().drop_nulls() < 0).sum())
            if last_ts is not None and int(ts[0]) < last_ts:
                report.validation.non_monotonic += 1
            last_ts = int(ts[-1])
            report.misaligned += misaligned(ts, key.timeframe)
            df = ParquetStore.with_partition_cols(df)
            for (year, month), part in df.group_by("_year", "_month"):
                ym = (int(year), int(month))  # type: ignore[arg-type]
                chunk = staging / f"{ym[0]}-{ym[1]:02d}-{report.batches}.parquet"
                part.drop("_year", "_month").write_parquet(chunk, compression="lz4")
                staged.setdefault(ym, []).append(chunk)
        for ym in sorted(staged):
            part = pl.read_parquet(staged[ym]).cast(BAR_SCHEMA)
            unique = part.unique(subset=["ts_open"], keep="first", maintain_order=True)
            report.validation.duplicates += part.height - unique.height
            report.rows_written += store.write_bars(key, unique)
        if staged:
            ts_all = store.scan_bars(key).select("ts_open").collect()["ts_open"]
            report.validation.gaps = gap_report(ts_all, key.timeframe)
            report.validation.actual_bars = ts_all.len()
            report.validation.expected_bars = ts_all.len() + report.validation.missing_bars
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return report
