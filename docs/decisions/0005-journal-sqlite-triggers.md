# 0005 — Journal append-only via SQLite triggers + fsynced JSONL mirror
- **Context:** I4/SEC-7 require an append-only journal written before side effects; §12 requires fsync on order events.
- **Decision:** single `journal` table in SQLite WAL with `synchronous=FULL`; `BEFORE UPDATE/DELETE` triggers abort. Every append is mirrored to `<name>.jsonl` and fsynced. Events round-trip through a type registry (`core.events.EVENT_TYPES`).
- **Consequence:** fsync on every append (not only order events) for simplicity; revisit if the 2,000 events/s NFR needs market data journaling (market data goes to Parquet via the recorder, not the journal).
