# 0035 — Multi-strategy allocation and loss caps (EXE-6, RISK-8)
- **Decision:**
  - Each bot can set `capital_pct` (its share of equity) and `daily_loss_cap` (account currency).
  - The RiskManager (pure) enforces the tighter of the bot's `capital_pct` and the global `max_strategy_capital_pct` on new exposure. A bot setting can never loosen the global cap.
  - The supervisor gets each strategy's PnL since the daily reset. A strategy at or past its loss cap trips `RISK-8 PAUSE_STRATEGY`: that strategy is paused, its resting orders and TWAPs are cancelled, and a CRITICAL alert goes out. Positions are kept and exits stay allowed, the same as the RISK-7 pause. Other strategies and the engine keep running, and the global daily-loss / drawdown halts still apply.
- **Attribution:** each strategy slot keeps its own `Portfolio` built from its own fills. On restart it is rebuilt from journaled fills, matched to the strategy through its order ids. This also fixed a gap: per-strategy positions (RISK-8 exposure) used to start empty after a restart.
- **Day start after restart:** the strategy's PnL at the daily reset is approximated from fills before the reset, marked at fill prices.
