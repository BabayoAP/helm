# 0017 — Order state machine, OrderManager and RiskManager landed during M2
- **Context:** BT-2 requires the event-driven backtester to run "the same engine loop, Strategy API, RiskManager, and OMS as live". §15 lists RiskManager/OMS under M3.
- **Decision:** build them (tests first) before the engine so the backtester never had a stub risk gate. M3 then adds the remaining pieces: breakers wiring, paper mode, reconciler, recovery, lock, gates, chaos tests.
- **Consequence:** no throwaway "allow-all" gate ever existed in the codebase.
