# Go-live checklist

PRD §15 M4 exit: the MVP is done when this is completed and the first real-money run uses `LIVE_SMALL` caps only. Tick items in a copy of this file kept outside git, or in the PR that flips `environment = "live"`. **Nothing in this repository places real-money orders on its own.**

## A. Strategy evidence (PRD §13)
- [ ] Backtest over realistic friction, with enough trades to mean something (`promotion.min_trades_backtest`). Treat the result as a way to rule ideas out, not to prove them.
- [ ] Helm paper on the live Alpaca feed for at least `promotion.min_paper_days` (default 14) and `min_paper_trades`.
- [ ] Paper PnL is within your expectations from the backtest; differences are understood.
- [ ] `helm research validate ... --holdout` PASSED for the exact params you will trade (overfitting checks: DSR, walk-forward, purged CV, plateau, holdout).
- [ ] Promoted step by step with `helm promote request --to <STAGE> --approve-as <you>` up to `LIVE_SMALL`. `helm live` refuses real money unless the registry says so for this exact code + params.
- [ ] Bot lifecycle set to `LIVE_SMALL` (not `LIVE`) in `helm.toml`.

## B. Soaks and drills
- [ ] 7-day soak on the live feed: `helm paper --feed alpaca --soak-hours 168`. `soak-report.json` shows `reconcile_always_ok: true` and flat memory use.
- [ ] Alpaca paper account ("testnet") run with `helm live`, `environment = "paper"`, for at least a few sessions.
- [ ] **Kill drill (UI):** KILL → confirm. Positions flat in the Alpaca dashboard, engine HALTED, resume code arrives on Telegram and the console, a wrong code is refused, the right code resumes.
- [ ] **Kill drill (Telegram):** `/kill` from your phone. Flat + HALTED in one step.
- [ ] **Flatten drill:** with an open position, `/flatten`. Verified flat at the venue.
- [ ] **Restart drill:** `docker compose restart engine` (or kill the process) during an open order. On restart: no duplicate orders, the missed fill is applied once, state matches the Alpaca dashboard.
- [ ] **Unexplained-state drill:** open a small position by hand in the Alpaca paper dashboard, restart. Helm refuses to trade; `--adopt` and `--flatten-first` both behave.
- [ ] **Stream drop drill:** turn the network off for ~1 min while an order is working. Helm resyncs and reconciles on reconnect.
- [ ] **Heartbeat drill:** stop the engine. The dead-man service alerts you within its grace period.
- [ ] **Backup restore drill:** `helm restore ...` into an empty directory succeeds.

## C. Security
- [ ] Real-money keys created **only** for the live account, stored encrypted (`secrets/helm.env.age`), private age key kept off the server.
- [ ] `security.attest_no_withdrawal = true` set after confirming Alpaca trading keys cannot withdraw (SEC-1).
- [ ] Telegram allowlists contain only your user id and chat id. A message from another account is ignored (test it).
- [ ] UI reachable only on localhost / Tailscale. Port 8765 is not exposed publicly (`docker compose ps` shows `127.0.0.1:8765`).
- [ ] `make check` green and CI green (gitleaks, pip-audit, npm audit) on the commit you deploy.

## D. Risk caps for the first real-money run
- [ ] `risk.toml` tightened for live: small `max_order_notional_usd`, `live_small_max_exposure_usd`, `live_small_capital_pct`.
- [ ] `daily_loss_limit_pct` and `max_drawdown_pct` set to amounts you are genuinely fine losing.
- [ ] `symbol_allowlist` set to exactly the symbols the bot trades.
- [ ] Account type understood: pattern-day-trader rules (margin < $25k) and settlement (cash account) (SEC-9).

## E. The switch (deliberate, reviewed)
- [ ] New config with `[venues.alpaca] environment = "live"` and `credentials = "alpaca_live"` (separate secret names from paper).
- [ ] Bot `mode = "live"`, `account_id` without the `paper-` prefix, lifecycle `LIVE_SMALL`.
- [ ] `HELM_LIVE_TRADING=1` set only for the engine of this bot.
- [ ] Watch the first session live in the UI with Telegram open. Know where the KILL button is.
