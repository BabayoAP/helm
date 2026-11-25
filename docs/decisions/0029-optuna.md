# 0029 — Optuna for parameter search (RES-3)
- **Context:** RES-3 names Optuna (TPE) for searching declared parameter spaces, with the trial count feeding the deflated Sharpe ratio.
- **Decision:** add `optuna` (it brings in sqlalchemy, alembic and numpy). It is imported only when `--optimizer optuna` is used, so the engine never loads it. Grid search stays the default for small spaces: every point is evaluated, and the plateau check gets all neighbours. Every evaluated trial is counted, including those from earlier runs of the same code version (`ExperimentStore.trial_count`).
- **Consequence:** with a sparse Optuna sample the plateau check may find no neighbours, and it then fails rather than passes (fail-closed).
