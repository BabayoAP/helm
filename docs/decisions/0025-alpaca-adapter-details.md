# 0025 — Alpaca adapter details
- **Symbols:** equities are `alpaca:AAPL/USD` (venue symbol `AAPL`, calendar `us_equity_rth`). Crypto is `alpaca:BTC/USD:crypto` (venue symbol `BTC/USD`, calendar `24x7`).
- **Fill identity:** the trade stream's `execution_id` and the REST activity `id` are different identifiers, so deduping on either one would double-count fills after a restart. `trade_id = "{venue_order_id}:{cumulative filled qty}"` is unique per execution and can be derived from both sources. This keeps (venue, trade_id) dedupe exact across stream and recovery.
- **Fees:** Alpaca equities are commission-free. The small regulatory sell-side fees (SEC/TAF) are not reported per fill, so live fills record `fee = 0`. Backtest friction should keep a fee/slippage allowance so results aren't optimistic.
- **Order features:** Alpaca has no `reduce_only` flag. The adapter emulates it by checking the venue position first, and refuses when the order would open or flip a position. Alpaca also has no POST_ONLY; those orders are rejected before they are sent. Equity orders outside regular hours are rejected (EXE-9) using the venue `/v2/clock`, which knows about holidays.
- **Submit errors:**
  - Timeout, 5xx or a dropped connection: `AmbiguousSubmitError`, so the order goes to UNKNOWN and is resolved via `get_order` before any retry.
  - 4xx: rejected.
  - 429: `RateLimitedError`.
  - A 422 "client_order_id must be unique" means the order already reached the venue, so it counts as submitted.
- **SEC-1:** Alpaca Trading API keys have no withdrawal or transfer endpoints (those live in the Broker API / dashboard), so there are no permissions to introspect. On startup the adapter checks:
  - the account is `ACTIVE` and neither `trading_blocked` nor `account_blocked`;
  - the URL matches the configured environment;
  - in live, `attest_no_withdrawal` is set.
  Pattern-day-trader status and the day-trade count are logged and alerted at startup (SEC-9).
- **Rate limits:** Alpaca allows 200 requests per minute per account. All REST calls go through a `RateGovernor` sized below that.
