# 0031 — Validation suite design (RES-4)
- **Evaluator injection:** `helm.research.validation.suite` takes `evaluate(params, bars) -> EvalResult` and never imports the engine. `helm.app.research.backtest_evaluator` supplies the event-driven backtester, which is the only promotion evidence (I1). Unit tests use synthetic evaluators: an overfit "luckiest of 200 noise streams", which must be rejected, and a genuine drift, which must pass.
- **Checks:**
  - minimum trades;
  - deflated Sharpe, using the variance of the trial Sharpes as the null spread;
  - parameter plateau: median neighbour Sharpe / best Sharpe;
  - anchored walk-forward: OOS Sharpe and efficiency;
  - purged K-fold with purge and embargo (train is passed as contiguous segments, so no backtest jumps a gap);
  - locked holdout.
  Any failed check rejects.
- **Returns:** Sharpe and DSR use equity sampled every `--return-period` (default 1h). Daily returns are too few on short minute-bar samples.
- **Holdout:** never evaluated unless `--holdout` is given. Each opening is logged per code version, and a second look needs `--force-holdout` plus a reason, which is also logged.
