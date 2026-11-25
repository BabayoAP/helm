"""M1 exit check: import a >=1 GB CSV while peak RSS stays under 2 GB (DATA-2).

Usage: uv run python scripts/bench_import.py [--gb 1.0] [--keep]
Writes the CSV and lake under a temp dir, prints rows, wall time, and peak RSS.
"""

from __future__ import annotations

import argparse
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

import polars as pl

from helm.core.enums import AssetClass
from helm.data.importer import import_bars
from helm.data.parquet_store import ParquetStore, SeriesKey

T0_MS = 1_262_304_000_000  # 2010-01-01


def make_csv(path: Path, target_bytes: int) -> int:
    rows = 0
    chunk = 2_000_000
    with path.open("w") as fh:
        fh.write("timestamp,open,high,low,close,volume\n")
    while path.stat().st_size < target_bytes:
        i = pl.int_range(rows, rows + chunk, eager=True)
        px = 20000.0 + (i % 5000).cast(pl.Float64) * 0.37
        df = pl.DataFrame(
            {
                "timestamp": T0_MS + i * 60_000,
                "open": px,
                "high": px + 5.123456,
                "low": px - 4.987654,
                "close": px + 0.5,
                "volume": (i % 97).cast(pl.Float64) * 1.25,
            }
        )
        with path.open("a") as fh:
            df.write_csv(fh, include_header=False)
        rows += chunk
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=1.0)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="helm-bench-"))
    try:
        csv = root / "big.csv"
        rows = make_csv(csv, int(a.gb * 1024**3))
        size = csv.stat().st_size
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        t = time.perf_counter()
        rep = import_bars(
            csv, ParquetStore(root), SeriesKey(AssetClass.CRYPTO, "bench:BTC/USD", "bars", "1m")
        )
        dt = time.perf_counter() - t
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        div = 1 if sys.platform == "darwin" else 1  # macOS reports bytes, Linux KiB
        peak_mb = peak / (1024**2 if sys.platform == "darwin" else 1024) / div
        print(
            f"csv={size / 1024**3:.2f} GB rows={rows} written={rep.rows_written} time={dt:.1f}s "
            f"peak_rss={peak_mb:.0f} MB (before import {rss_before / (1024**2 if sys.platform == 'darwin' else 1024):.0f} MB) "
            f"gaps={len(rep.validation.gaps)} dupes={rep.validation.duplicates}"
        )
        return 0 if peak_mb < 2048 and rep.rows_written == rows else 1
    finally:
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
