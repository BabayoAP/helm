# Helm

Self-hosted, single-user algorithmic trading terminal and research platform.
One strategy codebase runs unchanged in **backtest, paper, and live**, behind an
independent, fail-closed risk layer that no strategy can bypass.

- **Spec:** [PRD.md](PRD.md) (requirement IDs such as `RISK-4` are the contract)
- **Status:** [docs/STATUS.md](docs/STATUS.md) (milestone + requirement traceability)
- **Decisions:** [docs/decisions/](docs/decisions/)
- **Agent instructions:** [CLAUDE.md](CLAUDE.md)

## Quick start

```bash
uv sync                 # Python 3.12 + locked deps
make check              # ruff, pyright, import-linter, pytest (+ risk 100% branch cov), gitleaks
uv run helm config check
```

Copy `config/*.example.toml` to `config/*.toml` (gitignored) to customise.
Secrets never go in config files or `.env`: see SEC-2 and `helm.infra.secrets`.

## Safety

- No real-money orders in development or tests: `FakeExchange`, testnets, paper only.
- Live trading is triple-gated (EXE-1): `HELM_LIVE_TRADING=1` **and** bot `mode = "live"` **and** lifecycle ≥ `LIVE_SMALL`.
- Exchange keys must be trade-only; withdrawal capability never exists in this system (SEC-1).

## Layout

```
src/helm/
  core/      models, events, enums, clock, bus, ids, money, config models (leaf package)
  data/      parquet store, importer, validators, connectors, recorder, rate limiter, WS manager
  infra/     logging + redaction, config loading, secrets, wall clock
  strategy/  Strategy API, Context, examples, sandbox
  risk/      RiskManager + rules + kill switch (imports core only)
  oms/       order state machine, order manager, journal, reconciler
  brokers/   Broker protocol, sim/, fake_exchange, live/ (gated)
  backtest/  vectorized (screening) + event-driven (evidence), friction, metrics, tearsheet
  research/  features, validation, optimisation, experiments
  app/       engine main, modes, lifecycle
  api/ telegram/ tracking/   control plane + alerts (M4+)
```
