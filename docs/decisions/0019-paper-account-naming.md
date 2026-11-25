# 0019 — Paper accounts are named `paper-*`; enforced three ways (PAP-2)
- **Context:** PAP-2: it must be structurally impossible for a paper account to reach a live broker.
- **Decision:** (1) `BotConfig` rejects a live bot with a `paper-*` account and a paper/backtest bot without one; (2) `build_broker` always returns a `SimBroker` for non-live bots, whatever the environment says; (3) `LiveBrokerBase.__init__` refuses `paper-*` accounts and any caller that does not hold the private gate token from `helm.app.gates`.
- **Consequence:** tests in `tests/integration/test_engine.py` (TEST-7) cover each layer.
