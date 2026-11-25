# 0002 — Live broker adapters live in `helm.brokers.live`
- **Context:** §7 lists `brokers/ccxt_broker.py`, `alpaca.py`, `ib.py` beside `sim/` and `fake_exchange.py`, and requires that research/backtest never import "any live broker module".
- **Decision:** put live adapters in `helm/brokers/live/` so one import-linter contract (`forbidden: helm.brokers.live`) covers every current and future live adapter.
- **Consequence:** a new live adapter is automatically fenced off from research/backtest; no per-file contract edits.
