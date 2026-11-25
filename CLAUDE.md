# Helm — agent instructions
Source of truth: PRD.md. Requirement IDs are the contract; cite them in commits/tests.
Build status per requirement: docs/STATUS.md. Decisions: docs/decisions/.

## Commands
- make check   # ruff, pyright, import-linter, pytest + risk 100% branch coverage, gitleaks — must pass before finishing any task
- make test | make lint | make fmt | make cov | make ui | make up (scripts/up.sh) | make backtest ARGS="..."
- Python 3.12 via uv (`uv sync`, `uv run ...`). `PYTHONHASHSEED=0` is exported by the Makefile.
- Tests that take hours (soaks) or benchmark are marked `slow` and excluded from `make check`.

## Hard rules
- NEVER place real orders in dev/tests. Use FakeExchange / testnet / paper only.
- NEVER read, print, log, or commit secrets. No real keys in repo, tests, or fixtures. No `.env` files.
- Live broker code is reachable only through the triple gate (EXE-1). Don't weaken it.
- Every order intent must pass RiskManager. Risk is fail-closed and pure (no I/O, imports core only).
- Strategies return OrderIntents only; no time/network/broker/os imports. Never use wall clock; use ctx.now().
- Decimal (not float) for price/qty/fees in OMS, brokers, accounting. Quantize before risk and before submit.
- No lookahead: closed bars only; every feature needs a truncation test.
- Timestamps are UTC integer ns. Keep ts_event and ts_recv.
- New dependency => docs/decisions/ note. Ambiguity => pick PRD default, write a decision note, proceed.

## Layout conventions (see docs/decisions for why)
- `helm.core` is a leaf: models, events, enums, clock, bus, ids, money, config models. Imports nothing else from helm.
- Live broker adapters live under `helm.brokers.live` (one package the contracts can forbid).
- Config *models* are in `helm.core.config`; TOML loading is in `helm.infra.config`.
- The journal (`helm.oms.journal`) is SQLite WAL + JSONL mirror; UPDATE/DELETE are blocked by triggers.
- Accounting (`helm.oms.accounting.Portfolio`) tracks cost basis; shared by backtest, paper, live.
- One engine loop: `helm.app.engine.Engine`. Backtest = `helm.app.backtest`, paper = `helm.app.paper`, recovery = `helm.app.lifecycle`.
- Live brokers are only built by `helm.app.gates.build_broker` (EXE-1 triple gate + private token). Paper accounts are `paper-*`.
- Fills (deduped by venue+trade_id) drive order fill state; order events never double count.
- Chaos tests: `tests/chaos/` (WS drops, kill -9 via `fake_exchange_rpc`). Parity: `tests/replay/test_parity.py`.
- First venue is Alpaca (0023). Adapter `helm.brokers.live.alpaca`; shared symbol/ts helpers + data feed in `helm.data.connectors.alpaca*`. Fill trade_id = `{venue_order_id}:{cum_qty}` (0025).
- Alpaca tests use `tests/alpaca_fake.py` (httpx.MockTransport). It asserts the paper host; never point a test at `api.alpaca.markets`.
- Control plane: `helm.api` / `helm.telegram` depend only on `helm.core.control.ControlPort` (0026); `helm.app.control.EnginePort` implements it. Commands always go through `Engine.control` (journaled).
- Long-running extras (API, Telegram, heartbeat, daily summary) are `services` passed to the runner (`helm.app.services`). The one-time resume code goes to Telegram + console stderr, never the structured log.
- UI: `ui/` Next.js static export (`make ui`), served by the API at `/` (0027). Backups: `helm backup|restore` with `age` (0028).

## Running things
- `uv run helm backtest --synthetic 20000` (or `--symbol venue:BASE/QUOTE` after `helm data import|fetch`)
- `uv run helm sweep --synthetic 525600` (screening only)
- `uv run helm paper --soak-hours 1` (synthetic feed) or `--feed alpaca` (needs Alpaca data keys)
- `HELM_LIVE_TRADING=1 uv run helm live` (Alpaca; paper environment by default; all EXE-1 gates required)
- `uv run helm backup --recipient age1...` / `uv run helm restore <archive> --identity <key> --to <empty dir>`
- Docs for operators: `docs/RUNBOOK.md`, `docs/GO_LIVE_CHECKLIST.md`

## Workflow
Milestones in PRD §15, in order. Tests first for risk, OMS state machine, accounting, fill models.
Small commits. Update this file and docs/STATUS.md when conventions or status change.
