# Helm

Self-hosted, single-user algorithmic trading terminal and research platform.
One strategy codebase runs unchanged in **backtest, paper, and live**, behind an
independent, fail-closed risk layer that no strategy can bypass.

- **Spec:** [PRD.md](PRD.md) (requirement IDs such as `RISK-4` are the contract)
- **Status:** [docs/STATUS.md](docs/STATUS.md) (milestone + requirement traceability)
- **Decisions:** [docs/decisions/](docs/decisions/)
- **Runbook:** [docs/RUNBOOK.md](docs/RUNBOOK.md) · **Go-live checklist:** [docs/GO_LIVE_CHECKLIST.md](docs/GO_LIVE_CHECKLIST.md)
- **Agent instructions:** [CLAUDE.md](CLAUDE.md)

First venue: **Alpaca** (decision 0023). Its paper account plays the role of a testnet.

## Quick start

```bash
uv sync                 # Python 3.12 + locked deps
make check              # ruff, pyright, import-linter, pytest (+ risk 100% branch cov), gitleaks
uv run helm config check
```

Copy `config/*.example.toml` to `config/*.toml` (gitignored) to customise
(`config/helm.alpaca.example.toml` for Alpaca).
Secrets never go in config files or `.env`: see SEC-2 and `docs/RUNBOOK.md`.

```bash
uv run helm backtest --synthetic 20000                 # tearsheet from synthetic data
uv run helm paper                                      # paper bot on a synthetic feed, no keys needed
uv run helm paper --config config/helm.toml --feed alpaca   # paper on the live Alpaca feed
make ui                                                # build the control UI (served by the engine)
```

The engine prints `control UI: http://127.0.0.1:8765/#token=...` on start.

Research and promotion (is a strategy any good, and is it allowed near real money?):

```bash
uv run helm research validate --synthetic 3000 --timeframe 1h --param qty=1   # walk-forward, purged CV, DSR, plateau
uv run helm research draft --idea "buy pullbacks in an uptrend"              # AI draft -> sandboxed checks, never deploys
uv run helm backtest --replay data/recordings/<session>                       # L1 replay with queue model
uv run helm promote status --param fast=10 --param slow=40 --param qty=1      # DRAFT -> ... -> LIVE_SMALL -> LIVE
```

## Safety

- No real-money orders in development or tests: `FakeExchange`, testnets, paper only.
- Live trading is triple-gated (EXE-1): `HELM_LIVE_TRADING=1` **and** bot `mode = "live"` **and** lifecycle ≥ `LIVE_SMALL`.
- Exchange keys must be trade-only; withdrawal capability never exists in this system (SEC-1).
- Real money additionally needs `[venues.alpaca] environment = "live"` and `attest_no_withdrawal = true`; the URL comes only from that setting.

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
  app/       engine, backtest/paper/live CLIs, lifecycle/recovery, gates, control port, services
  api/       FastAPI control plane (talks to the engine only via helm.core.control)
  telegram/  Telegram bot + alert sink
  tracking/  alert manager, watchlist, alert rules
  research/  experiments, validation suite, promotion registry, reports, strategy sandbox
ui/          Next.js static UI (served same-origin by the API)
Dockerfile, docker-compose.yml, scripts/up.sh   deploy (OPS-5)
```
