# 0009 — Recorder stores canonical JSON payloads in Parquet
- **Context:** DATA-4 requires recorded feeds to replay bit-for-bit into any mode; prices are Decimal.
- **Decision:** each recorded event is a row (seq, ts_recv, kind, symbol, payload) where payload is the model's canonical JSON. Replay decodes through the same event registry as the journal.
- **Consequence:** exact Decimal round-trip and identical model equality on replay. Slightly larger files than typed columns; the bar *lake* still uses typed float columns for research speed.
