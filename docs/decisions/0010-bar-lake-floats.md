# 0010 — Bar lake stores Float64 OHLCV; Decimal at the event boundary
- **Context:** PRD §4: floats are fine for research, Decimal at order/accounting boundaries.
- **Decision:** lake columns are Float64; `ParquetStore.iter_bars` converts with `Decimal(repr(x))`, which is deterministic and exact for the stored float's shortest repr.
- **Consequence:** vectorized research reads Parquet directly; the event-driven engine sees Decimal bars.
