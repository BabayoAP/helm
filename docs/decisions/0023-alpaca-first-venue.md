# 0023 — First venue: Alpaca (owner decision, 2026-11-25)
- **Context:** PRD §15 M4 is BLOCKING on the first venue; the PRD default was a CCXT crypto exchange with a testnet, with Alpaca planned for Phase 2. The owner chose Alpaca after comparing them: Alpaca has a real paper-trading account behind the same API as live, US equities markets close (so the bot doesn't run overnight), and it is free to start. Most US-accessible crypto testnets only test the plumbing.
- **Decision:** M4 ships `AlpacaBroker` (`helm.brokers.live.alpaca`) and an Alpaca market-data feed (`helm.data.connectors.alpaca`) instead of `CcxtBroker`. CCXT live trading moves to Phase 2 (the CCXT history fetcher from M1 stays).
- **How the modes map:**
  - `paper`: Helm's own paper mode. SimBroker fills on the Alpaca live IEX feed. No orders reach Alpaca. Accounts are `paper-*`.
  - `live` + `environment = "paper"`: the Alpaca paper account. This plays the role of the PRD's "testnet". It goes through the full EXE-1 triple gate, exactly as real money will, so the gate gets exercised before any real money is involved.
  - `live` + `environment = "live"`: real money. It needs everything above plus the venue config `environment = "live"` and `security.attest_no_withdrawal = true`. The adapter refuses a real-money URL unless the environment says live, and refuses the paper URL when it says live.
- **Consequence:** equities bring market hours (EXE-9), pattern-day-trader rules (SEC-9) and a partial free data feed (IEX only). These are documented in `docs/RUNBOOK.md` and `docs/GO_LIVE_CHECKLIST.md`.
