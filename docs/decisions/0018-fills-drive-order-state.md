# 0018 — Fills (not fill-state events) drive filled_qty and FILLED/PARTIAL
- **Context:** user streams can deliver a FILLED order event before its fill, duplicate fills, or reorder them.
- **Decision:** `filled_qty`, average price and PARTIAL/FILLED come only from fills deduped by `(venue, trade_id)`. A PARTIAL/FILLED *order event* only implies ACCEPTED. Events for an earlier stage are counted as stale; contradictions raise `IllegalTransitionError` → CRITICAL alert + PAUSE.
- **Consequence:** no double counting under duplicate/out-of-order delivery (TEST-1 harness).
