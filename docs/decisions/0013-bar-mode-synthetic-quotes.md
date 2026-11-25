# 0013 — Bar-mode backtests synthesize a zero-spread quote at each close
- **Context:** RISK-3/RISK-4 need a fresh quote; bar-only history has none. The risk gate must run unchanged in backtest (BT-2, I1).
- **Decision:** on each closed bar with no real quotes for that symbol, the engine sets `bid = ask = close`, `ts_recv = ts_close`, feed LIVE. Spread cost is charged by the friction model (`half_spread_bps`) instead.
- **Consequence:** the same RiskManager runs in all modes; as soon as real quotes arrive for a symbol, synthetic quotes stop for it.
