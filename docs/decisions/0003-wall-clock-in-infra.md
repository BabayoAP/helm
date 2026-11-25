# 0003 — Wall clock lives in `helm.infra.clock`, not `helm.core.clock`
- **Context:** the strategy import contract forbids `time`. import-linter follows indirect imports, so `strategy.context -> core.clock -> time` broke the contract.
- **Decision:** `core.clock` holds the `Clock` protocol and `SimClock` only; `LiveClock` (the only `time.time_ns()` caller) is in `helm.infra.clock`.
- **Consequence:** nothing reachable from strategy code can read the wall clock; strategies must use `ctx.now()` (I3).
