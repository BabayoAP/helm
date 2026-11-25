# 0033 — Phase 2 UI: Lightweight Charts, hand-rolled sortable watchlist, read-only research
- **Context:** UI-3/4/5. The PRD names Lightweight Charts for charts and TanStack Table for the watchlist.
- **Decision:**
  - Add `lightweight-charts` 5.2.1 (pinned, MIT): candles, buy/sell markers from journal fills, and price lines for resting limit orders.
  - The watchlist is a small hand-rolled sortable table instead of TanStack Table. It's one table of a few dozen rows, so a dependency isn't worth it. Switch if virtualized or very large tables are needed (UI-7).
  - The research view is **read-only** (experiments + promotion log). Promotions stay a deliberate CLI action (`helm promote ... --approve-as`), because gate checks run backtests and lookahead checks that shouldn't hang off a button.
- **Data:** `/api/chart` and `/api/research` go through `ControlPort` like everything else (decision 0026). Watch-only symbols (`[tracking] watch_symbols`) are streamed to the watchlist but never dispatched to the engine, so they can't trip stale-feed breakers.
