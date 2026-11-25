# 0008 — M1 data dependencies
- **Context:** PRD §5 data + connectivity stack. Versions verified at build time (2026-11-24).
- **Decision:** `polars` 2.2, `duckdb` 2.0, `ccxt` 4.5, `websockets` 17.4. Polars 2 removed `read_csv_batched`, so the importer streams with `scan_csv(...).collect_batches()`. DuckDB results are converted with `fetchall()` instead of `.pl()` to avoid pulling in `pyarrow`.
- **Consequence:** no pyarrow dependency; the 1 GB CSV import peaks at ~714 MB RSS (see STATUS).
