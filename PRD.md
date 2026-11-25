# Helm — Personal Algorithmic Trading Terminal & Research Platform

**PRD v2.0** · Owner: single user · Implementer: Claude Code (autonomous agent) · Status: Ready to build

---

## 0. How Claude Code Must Use This Document

1. **Build milestone by milestone** (Section 15). Do not start milestone N+1 until every exit criterion of N passes and `make check` is green.
2. **Requirement IDs** (e.g. `RISK-4`) are the contract. Reference them in commit messages and test names. Each ID has acceptance criteria; a requirement is done only when a test proves it.
3. **Ambiguity rule:** if the spec is silent, pick the documented default (Section 4/17), write a 5-line note in `docs/decisions/NNNN-title.md`, and continue. Ask the owner only for decisions marked **BLOCKING**.
4. **Safety rules while developing:**
   - Never place a real-money order during development or tests. Use `FakeExchange`, exchange testnets, or broker paper endpoints only.
   - Never read, print, log, or commit secrets. Never create `.env` files with real keys.
   - Live mode code paths must stay behind the triple gate in `EXE-1`.
5. **Dependencies:** use only the stack in Section 5. Adding a dependency requires a decision note. Pin versions to the latest stable at build time (verify; do not trust versions from memory).
6. **Write tests first** for: risk engine, order state machine, accounting/PnL, fill simulation, lookahead detection. These are the modules where bugs cost money.
7. At M0, generate `CLAUDE.md` from Appendix B and keep it current.

---

## 1. Product Summary

**Objective.** A self-hosted, single-user platform that unifies market monitoring, data ingestion, strategy research, realistic backtesting, paper trading, and live bot execution, with **one strategy codebase running unchanged in backtest, paper, and live**, and an independent risk layer that no strategy can bypass.

**Primary user.** One technical trader/developer. No multi-tenancy, no auth beyond VPN + local token, no billing.

**Time horizon.** Strategies operating on seconds-to-days horizons. Latency matters for correctness and safety, not for racing other participants.

### Goals
- G1. Idea → backtest result in under 30 minutes of human effort.
- G2. Backtest results are trustworthy: realistic friction, no lookahead, overfitting defenses.
- G3. Live trading is safe by construction: bounded loss, idempotent orders, recoverable state.
- G4. Runs on 4 vCPU / 8 GB RAM, under $30/month, one-command deploy.
- G5. Hands-off operation: alerts and remote control via Telegram.

### Non-Goals (explicitly out of scope)
- High-frequency trading, colocation, market making, sub-millisecond latency.
- Multi-user support, public web exposure, mobile native app.
- Custody of funds, withdrawals, transfers of any kind.
- Tax reporting, order-management for third parties, financial advice features.
- Being a general-purpose charting product (TradingView parity).

### Success Metrics
| Metric | Target |
|---|---|
| Backtest↔paper parity: replaying one recorded feed through both produces the same order sequence | 100% identical; PnL delta < 1 bp after friction |
| Orders in journal with a risk decision record | 100% |
| `kill -9` mid-trading, restart | State fully recovered; 0 duplicate orders; 0 orphaned positions |
| Mean time to flatten all (Telegram `/kill` → verified flat) | < 10 s on liquid markets |
| Monthly infra cost | < $30 |

---

## 2. Design Invariants (non-negotiable)

| # | Invariant |
|---|---|
| I1 | **One code path.** Strategy code and the engine loop are identical across backtest, paper, live. Only `Broker`, `Clock`, and `DataFeed` implementations differ. |
| I2 | **Risk gate is mandatory and fail-closed.** Every order intent passes `RiskManager` before any broker. Any exception or missing data inside risk ⇒ reject. |
| I3 | **No lookahead.** Strategies see only data with `ts <= clock.now()`. Signals are computed on *closed* bars and executed no earlier than the next bar/tick. |
| I4 | **Idempotent orders + append-only journal.** Deterministic `client_order_id`; every intent, risk decision, order event, fill, and state transition is journaled before side effects. |
| I5 | **Live is opt-in, triple-gated** (`EXE-1`). |
| I6 | **Least privilege.** Trade-only keys; research/AI containers hold no credentials; withdrawal capability never exists in this system. |
| I7 | **Deterministic replay.** Same data + config + seed ⇒ byte-identical backtest result hash. |
| I8 | **Mode is always visible.** `[PAPER]` / `[LIVE]` / `[BACKTEST]` badge in UI, logs, and Telegram messages. |
| I9 | **Single writer per account.** At most one engine instance may trade a given broker account (lock enforced). |

---

## 3. Competitive Takeaways

| Platform | Adopt | Avoid |
|---|---|---|
| **QuantConnect / LEAN** | Event-driven engine; same strategy in backtest & live; clear separation of data, alpha, portfolio, risk, execution. | .NET weight, heavy local setup, cloud-centric workflow. |
| **Freqtrade** | Telegram command loop; hyperopt as a first-class workflow; Docker deploy; dry-run mode. | Crypto-only coupling; bar-only assumptions that break for multi-leg/equities. |
| **Hummingbot** | Resilient WebSocket connector patterns; order tracking with exchange-state reconciliation. | Per-connector resource weight; sprawling YAML config. |
| **TradingView** | Dense dark UI; Lightweight Charts; fast visual iteration. | Webhook-only execution (latency, state desync); backtest lookahead via repainting. |
| **Jesse** | Pythonic strategy API; strong anti-lookahead discipline; clean indicator/execution split. | Narrow multi-venue portfolio support. |
| **NautilusTrader** (additional) | Backtest/live parity architecture, typed domain models, message-bus design, nanosecond timestamps. | Steep learning curve; treat as design reference. Do not vendor it. |
| **vectorbt / Backtrader** (additional) | Vectorized sweeps for fast screening. | Vectorized results used as final evidence (they are screening-grade only). |

**Build vs. adopt rule:** adopt libraries for connectivity (CCXT, alpaca-py, ib_async, web3.py), dataframes (Polars), optimization (Optuna). Build the engine, risk, journal, and simulation: they are the product.

---

## 4. Key Decisions (changes from the v1 draft are marked ★)

| Decision | Choice | Rationale |
|---|---|---|
| ★ MVP language | **Python-only**. Rust only after profiling proves a hotspot. | Rust+PyO3 doubles build complexity for a solo developer; bar/L1 strategies don't need it. |
| ★ Event bus | In-process `asyncio` bus behind an `EventBus` protocol; **Redis Streams** only if/when processes are split. | Redis pub/sub is fire-and-forget (messages lost on disconnect): unsafe for order state. Fewer moving parts for MVP. |
| ★ Durable state | **SQLite (WAL)** for MVP; Postgres optional later. Journal = append-only table + JSONL mirror. | Single writer, tiny footprint, trivially backed up. |
| ★ Time-series | **Parquet + DuckDB** (queries) for history and recorded live data. QuestDB/Timescale are optional Phase 2+ if live tick queries need them. | Meets the 8 GB budget; avoids running a JVM/DB server for MVP. |
| Dataframes | **Polars** for research/vectorized; **`Decimal`** at order/accounting boundaries. | Floats are fine for research, not for qty/price rounding against exchange precision. |
| ★ Latency targets | Defined as **internal** processing time, excluding network RTT. | v1's "signal-to-wire < 15 ms" is not controllable over the internet. |
| ★ UI frame rate | UI state updates batched at 10 Hz (100 ms); chart canvas renders at display refresh. | v1's "60 FPS with 100 ms throttle" was self-contradictory. |
| ★ Copy-trading | **Alert-only by default.** Mirroring is Phase 3, separately flagged, hard-capped. | On-chain copy trading is latency-disadvantaged, MEV-exposed, and needs a hot wallet (conflicts with I6). |
| ★ AI-generated strategies | Run in a **credential-free sandbox**; can never self-promote. | Generated code is untrusted code. |
| ★ Promotion pipeline | Mandatory staged lifecycle with manual approvals (Section 13). | Prevents "backtest looked good → live" accidents. |
| First venue | **CCXT, one US-accessible exchange with a sandbox/testnet.** Alpaca (paper+live) in Phase 2. **BLOCKING at M4:** ask owner which venue. Default: whichever has a working testnet. | |
| Deployment | Local workstation first; same Docker Compose runs on a VPS later. | |
| Language/tooling | Python 3.12+, `uv`, `ruff`, `pyright`, `pytest`, `hypothesis`, `import-linter`. | |

---

## 5. Tech Stack

| Layer | Choice |
|---|---|
| Core | Python 3.12+, `asyncio` (+ `uvloop` on Linux), Pydantic v2 models (or `msgspec` for hot-path structs) |
| Data | Polars, DuckDB, Parquet (zstd), SQLite (WAL) |
| Optimization | Optuna; custom purged K-fold / walk-forward module |
| Connectivity | CCXT (REST + async WebSocket), `alpaca-py`, `ib_async`, `web3.py` / `solana-py`, `websockets`/`aiohttp` |
| API server | FastAPI (REST + WebSocket) |
| UI | Next.js + TypeScript, Tailwind, TradingView Lightweight Charts, TanStack Table |
| Alerts/control | Telegram Bot API (`aiogram` or `python-telegram-bot`), Discord webhook for alerts |
| Secrets | `age` + SOPS (default), or Doppler/Vault if already used; OS keyring for local dev |
| Observability | `structlog` (JSON), Prometheus metrics endpoint (optional), healthcheck endpoint, external heartbeat ping |
| Infra | Docker + Compose; Tailscale/WireGuard for access |
| Quality | `ruff`, `pyright`, `pytest`, `hypothesis`, `import-linter`, `gitleaks` pre-commit, GitHub Actions or local `make check` |

---

## 6. Architecture

```mermaid
flowchart LR
  subgraph Sources
    EX[Exchanges / Brokers\nREST + WS]
    ALT[Alt data: on-chain RPC,\nnews, webhooks]
    FILES[CSV / Parquet imports]
  end
  subgraph Engine["engine container (holds trading credentials)"]
    CONN[Connectors + RateLimiter + WS Manager]
    NORM[Normalizer]
    BUS((EventBus))
    STRAT[Strategy Runner\n(sandboxed API)]
    RISK[RiskManager\nfail-closed]
    OMS[Order Manager\nstate machine + journal]
    BRK[Broker: Live | Paper(Sim) | Backtest(Sim)]
    REC[Reconciler]
    KILL[Kill Switch / Supervisor]
  end
  subgraph Stores
    PQ[(Parquet lake)]
    DB[(SQLite: state + journal)]
  end
  subgraph Research["research container (NO credentials)"]
    BT[Backtester]
    FF[Feature factory]
    OPT[Optuna / validation]
    AI[AI hypothesis sandbox]
  end
  API[FastAPI] --- UI[Next.js UI]
  TG[Telegram bot]
  EX --> CONN --> NORM --> BUS
  ALT --> CONN
  FILES --> PQ
  NORM --> PQ
  BUS --> STRAT --> RISK --> OMS --> BRK --> EX
  OMS <--> DB
  REC <--> BRK
  KILL --> OMS
  PQ --> BT
  FF --> BT --> OPT
  AI --> BT
  BUS --> API
  API --> KILL
  TG --> KILL
```

### 6.1 Process / container model
- `engine`: connectors, bus, strategies, risk, OMS, brokers, reconciler. Holds broker credentials. **Only** container that can reach broker trading endpoints.
- `api`: FastAPI + WS bridge to UI; talks to engine via internal control interface. Cannot place orders directly; it can only send control commands (`pause`, `resume`, `flatten`, `kill`, `start`, `stop`).
- `ui`: Next.js static/SSR front end.
- `research`: backtests, optimization, AI sandbox. Mounts Parquet read-only. **No secrets, no broker network egress.** Egress only to the LLM API if enabled.
- `tailscale` sidecar (optional) for private access.

### 6.2 Event types (typed, versioned)
`Tick`, `Quote(L1)`, `Trade`, `BookDelta`, `BookSnapshot`, `Bar`, `WalletEvent`, `NewsEvent`, `OrderIntent`, `RiskDecision`, `OrderEvent` (submitted/accepted/partial/filled/canceled/rejected/expired/unknown), `Fill`, `PositionSnapshot`, `AccountSnapshot`, `FeedStatus` (LIVE/STALE/RECONNECTING), `ControlCommand`, `Alert`.

### 6.3 Strategy API (sandbox-friendly, lookahead-safe)
```python
class Strategy(Protocol):
    id: str
    def on_start(self, ctx: Context) -> None: ...
    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]: ...      # closed bars only
    def on_quote(self, q: Quote, ctx: Context) -> list[OrderIntent]: ...    # optional
    def on_book(self, b: BookSnapshot, ctx: Context) -> list[OrderIntent]: ...  # optional (Phase 2)
    def on_fill(self, f: Fill, ctx: Context) -> list[OrderIntent]: ...
    def on_stop(self, ctx: Context) -> None: ...
```
- `Context` exposes: read-only portfolio/position snapshot, `ctx.now()` (clock), `ctx.history(symbol, timeframe, n)` (never returns data past `now`), params, logger, and `ctx.state` (persisted key-value, journaled).
- Strategies **return intents**; they never call a broker. They must not import `time`, `datetime.now`, network libs, `os`, or broker modules (enforced by `import-linter` + AST check; see `RES-5`).

### 6.4 Interfaces swapped per mode
```python
class Clock(Protocol): def now(self) -> int: ...            # UTC ns
class DataFeed(Protocol): def subscribe(...); def __aiter__(...)
class Broker(Protocol):
    async def submit(self, order: Order) -> None
    async def cancel(self, client_order_id: str) -> None
    async def open_orders(self) -> list[Order]
    async def positions(self) -> list[Position]
    async def balances(self) -> list[Balance]
    async def flatten_all(self, *, reduce_only: bool = True) -> None
```
| Mode | Clock | DataFeed | Broker |
|---|---|---|---|
| Backtest | `SimClock` | Parquet replay | `SimBroker` |
| Paper | `LiveClock` | Live WS | `SimBroker` (live fills model) |
| Live | `LiveClock` | Live WS | `CcxtBroker` / `AlpacaBroker` / `IbBroker` |

### 6.5 Order state machine
`NEW → PENDING_SUBMIT → ACCEPTED → PARTIALLY_FILLED → FILLED`
Terminal alternates: `CANCELED`, `REJECTED`, `EXPIRED`. Special: **`UNKNOWN`** (submit timeout/ambiguous response): resolved by REST lookup by `client_order_id` before any retry; never blind-retry a submit.
- `client_order_id = {run_id}-{strategy_id}-{seq}` (deterministic, unique, venue-length-safe).
- Duplicate fill events are deduplicated by `(venue, trade_id)`.
- Illegal transitions raise and alert; they are never silently ignored.

### 6.6 Time
All timestamps UTC integer nanoseconds. Record both `ts_event` (venue) and `ts_recv` (local). Check NTP/clock skew at startup and every 5 minutes; skew > 250 ms ⇒ alert and treat feed-lag checks as degraded.

---

## 7. Repository Layout

```
helm/
├─ CLAUDE.md                      # seeded from Appendix B
├─ PRD.md                         # this file
├─ pyproject.toml  uv.lock  Makefile  docker-compose.yml  .pre-commit-config.yaml
├─ config/
│  ├─ helm.example.toml  risk.example.toml  promotion.example.toml
├─ docs/decisions/                # ADR-lite notes
├─ src/helm/
│  ├─ core/        # models, events, clock, bus, ids, money (Decimal helpers), errors
│  ├─ data/        # connectors/, normalizer, parquet_store, importer, recorder, validators
│  ├─ infra/       # ratelimit, ws_manager, secrets, logging, metrics, supervisor
│  ├─ strategy/    # api.py, context.py, examples/, sandbox/
│  ├─ risk/        # rules/, manager.py, killswitch.py          (imports core only)
│  ├─ oms/         # order_manager.py, state_machine.py, journal.py, reconciler.py
│  ├─ brokers/     # base.py, sim/, ccxt_broker.py, alpaca.py, ib.py, fake_exchange.py
│  ├─ backtest/    # vectorized/, event_driven/, friction/, metrics/, tearsheet/
│  ├─ research/    # features/, validation/ (purged_kfold, walk_forward), optuna_runner, experiments/
│  ├─ tracking/    # watchlist, alerts, wallets/, rules_engine
│  ├─ app/         # engine_main.py, modes (backtest|paper|live), lifecycle
│  ├─ api/         # FastAPI routes, ws bridge, control interface
│  └─ telegram/    # bot, auth, commands
├─ ui/             # Next.js app
├─ tests/          # unit/, property/, integration/, replay/, chaos/
└─ data/           # gitignored: parquet lake, sqlite, journal
```

**Import contracts (enforced in CI via `import-linter`):**
- `strategy.*` may not import `brokers`, `oms`, `infra.secrets`, network libs.
- `risk.*` may import only `core`.
- `research.*` and `backtest.*` may not import `infra.secrets` or any live broker module.
- `api.*` may not import `brokers` (control commands only).

---

## 8. Core Data Contracts

```python
Instrument:  venue, symbol (canonical "venue:BASE/QUOTE[:kind]"), asset_class, tick_size, lot_size,
             min_qty, min_notional, contract_multiplier, quote_ccy, trading_hours/calendar
Bar:         instrument, timeframe, ts_open, ts_close, o, h, l, c, volume, trades?, closed: bool
Quote:       instrument, bid, bid_sz, ask, ask_sz, ts_event, ts_recv
Trade:       instrument, price, size, side?, trade_id, ts_event, ts_recv
BookDelta:   instrument, seq, bids[], asks[], ts_event, ts_recv
WalletEvent: chain, address, tx_hash, kind(swap|transfer), token_in/out, amount_in/out, usd_value?, ts_event
OrderIntent: strategy_id, instrument, side, type(MKT|LMT|STOP|...), qty, limit_price?, tif, reduce_only, tag
Order:       client_order_id, venue_order_id?, intent, state, filled_qty, avg_px, fees, timestamps
Fill:        order ids, trade_id, price, qty, fee, fee_ccy, liquidity(maker|taker), ts
Position:    instrument, qty, avg_px, realized_pnl, unrealized_pnl
RiskDecision: intent_id, verdict(APPROVE|REJECT|MODIFY), rule_id, reason, inputs_snapshot_hash
```
- Prices/qty/fees use `Decimal` (or integer fixed-point) in OMS/brokers/accounting. Quantize to `tick_size`/`lot_size` **before** the risk check and again before submit.
- Pydantic/msgspec validation at every external boundary; unknown fields from venues are preserved in `raw` for debugging.

---

## 9. Module Specifications

### 9.1 Data & Integration Layer (`DATA-*`)

**User story:** One normalized interface so strategies consume crypto, equities, and on-chain events identically.

| ID | Requirement | Acceptance criteria |
|---|---|---|
| DATA-1 | Canonical models (Section 8) for all inbound data. | Property tests: round-trip serialize/deserialize; fuzzed venue payloads never crash the normalizer (bad payload ⇒ logged + dropped + counter). |
| DATA-2 | **Historical importer** (CLI `helm data import`, later UI): CSV + Parquet → partitioned Parquet lake `lake/{asset_class}/{venue}/{symbol}/{datatype}/{timeframe}/year=YYYY/month=MM.parquet` (zstd). | Dedup on timestamp; validates OHLC sanity (`l<=min(o,c)`, `h>=max(o,c)`, volume ≥ 0), monotonic time, and **gap report** (expected vs. actual bars); idempotent re-import; 1 GB CSV imports without exceeding 2 GB RAM (streaming/chunked). |
| DATA-3 | **Historical fetch** from venue REST (CCXT) with resume + rate-limit awareness. | Interrupted fetch resumes without duplicates; respects limiter. |
| DATA-4 | **Live recorder:** engine spools live trades/quotes/bars (L2 optional) to Parquet. | Recorded feed can be replayed bit-for-bit into any mode (used by parity test, `TEST-4`). |
| DATA-5 | **WebSocket manager:** ping/pong heartbeat (default 15 s), reconnect with jittered exponential backoff (base 1 s, cap 60 s), resubscribe, **REST gap-fill** after reconnect, order-book sequence-gap detection ⇒ re-snapshot. Emits `FeedStatus`. | Chaos test: drop the socket 100× with random delays ⇒ no duplicate/missing bars after gap-fill; book matches REST snapshot; `FeedStatus=STALE` raised during outage so risk can react. |
| DATA-6 | **Rate-limit governor:** token bucket per `(venue, api_key, endpoint-weight class)`; priority: **cancel > order > account > market data**; honors `Retry-After` and venue weight headers; 429/418 ⇒ global backoff for that key. | Load test: 10× over-demand never produces a 429 against `FakeExchange` configured with strict limits; cancels are never starved by data polling. |
| DATA-7 | Provider adapters behind `DataFeed` / `Broker`: CCXT (M1/M4), Alpaca & IBKR (Phase 2), on-chain RPC + webhooks (Phase 3), generic **signed custom webhook** receiver (HMAC, replay-protected). | Each adapter passes the shared adapter contract test suite. |
| DATA-8 | Instrument metadata service (tick/lot/min-notional, trading calendar) cached + refreshed. | Order qty/price always conform; unknown instrument ⇒ reject. |
| DATA-9 | Equities data hygiene: split/dividend-adjusted series, explicit raw vs adjusted, survivorship-bias warning in tearsheet when universe is static. | Adjusted/raw selectable and recorded in experiment metadata. |

### 9.2 Market Tracking & Social/Wallet Following (`TRK-*`)

| ID | Requirement | Acceptance criteria |
|---|---|---|
| TRK-1 | Watchlists with price, 24h change, spread %, volume anomaly (z-score vs. trailing window), funding rate (perps). | Updated via WS; UI batches at 10 Hz. |
| TRK-2 | **Alert rules**: price/indicator thresholds, spread/volume anomalies, feed/engine health, PnL thresholds → Telegram/Discord/UI. | Rule evaluation unit-tested; alerts deduplicated/throttled (no storms). |
| TRK-3 | **Wallet tracker** (Phase 3): subscribe to EVM/Solana addresses (RPC websocket logs or Alchemy/Helius webhooks); decode swaps/transfers for known routers; maintain per-wallet history. | Decodes fixtures of real transactions correctly; handles reorgs by waiting for configurable confirmations. |
| TRK-4 | **Signal rule builder**: e.g. `IF wallet X buys > $50k of token Y AND est. slippage < 2% THEN alert | paper-mirror | live-mirror`. | **Default action = alert.** Mirroring requires per-rule `execution: enabled`, passes through `RiskManager`, and enforces: per-trade cap, daily cap, token allowlist/liquidity + honeypot check, max slippage, cooldown. |
| TRK-5 | **On-chain execution safety (Phase 3, optional):** dedicated low-balance hot wallet, never the main wallet; bounded token approvals (no infinite approvals); DEX aggregator with slippage limit. Private keys live only in the encrypted keystore and only in the `engine` container. | Documented as the highest-risk feature; off by default; separate feature flag `ONCHAIN_EXECUTION=true`. Owner must explicitly accept the risk in config. |
| TRK-6 | Paper-mirror mode for any rule to measure its realized edge net of latency before enabling live. | Mirror PnL report exists before live flag can be set. |

### 9.3 Automated Strategy Research (`RES-*`)

| ID | Requirement | Acceptance criteria |
|---|---|---|
| RES-1 | **Feature factory:** registry of vectorized Polars features (technical, microstructure: order-book imbalance, trade-flow imbalance; cross-asset). | Every feature has a **truncation lookahead test**: computing on `data[:t]` must equal the first `t` rows computed on full data. CI fails otherwise. |
| RES-2 | **Experiment tracking:** every run records git hash, data hash, config, params, seed, metrics, trial count. Stored in DuckDB/SQLite; browsable in UI. | Rerun from a record reproduces the same result hash (I7). |
| RES-3 | **Optimization:** Optuna (Bayesian/TPE) over declared parameter spaces; trial count recorded and used by deflated-Sharpe. | Multiprocess-safe; resumable studies. |
| RES-4 | **Validation suite:** purged K-fold with embargo, walk-forward analysis, **locked final holdout** (touched once per strategy version; access is logged), parameter-stability/plateau check, minimum trade count, deflated Sharpe ratio + probabilistic Sharpe. | A deliberately overfit synthetic strategy is **rejected** by the suite (regression test). |
| RES-5 | **AI hypothesis assistant** (Phase 3): natural-language idea → Python strategy class → pipeline: AST allowlist check → type/lint check → lookahead truncation test → smoke backtest, all inside the credential-free sandbox (subprocess/container, no network except LLM API if enabled, CPU/mem/time limits, read-only data mount). | Malicious/accidental code (file writes, network, env access, `eval`) is blocked by tests. Output is a *draft* strategy and report. **It can never promote or deploy.** Account/position data is never sent to the LLM. |
| RES-6 | Research notebooks/CLI: `helm research run <strategy> --params ...`. | Works headless; outputs tearsheet artifact. |

### 9.4 Backtesting Engine (`BT-*`)

**Two modes, one purpose each:** *vectorized = screening*, *event-driven = evidence*. Tearsheets label which mode produced them. **Only event-driven results count toward promotion.**

| ID | Requirement | Acceptance criteria |
|---|---|---|
| BT-1 | **Vectorized mode (Polars):** parameter sweeps over years of 1m–1d data; signals auto-shifted by one bar; fixed fee/slippage assumptions. | 1 yr × 1 symbol × 1m bars sweep of 100 parameter sets in under ~30 s on reference hardware (target; adjust after profiling). |
| BT-2 | **Event-driven mode:** runs the **same engine loop, Strategy API, RiskManager, and OMS** as live, with `SimClock` + `SimBroker`. | Parity test `TEST-4` passes. |
| BT-3 | **Fill model (bar mode):** market orders fill at next bar open ± slippage; limit orders fill only if price **trades through** the limit (touch ≠ fill, configurable conservative/optimistic); stops fill at the worse of stop/open on gaps; same-bar entry+exit ambiguity resolved pessimistically. | Table-driven tests with hand-computed expectations. |
| BT-4 | **Fill model (tick/L1/L2 — Phase 2):** replay quotes/trades/book; queue-position estimate for passive orders; partial fills when trades cross the limit. | Replays recorded feed deterministically. |
| BT-5 | **Friction:** per-venue maker/taker fee schedules (incl. tiers), spread from data (or modeled), perps funding payments, volume-dependent slippage using square-root impact: $$\text{slip}(Q) = k\,\sigma\,\sqrt{\frac{Q}{V_{\text{ADV}}}}$$ (with $\sigma$ = trailing volatility, $V_{\text{ADV}}$ = average daily volume, $k$ calibrated per venue), and latency model (default 50–250 ms between signal and order arrival, sampled from a seeded distribution). | Each friction component can be toggled; tests prove costs reduce PnL monotonically as friction rises. |
| BT-6 | **Metrics/tearsheet:** CAGR, Sharpe, Sortino, Calmar, max drawdown (depth + duration), profit factor, expectancy, win rate, exposure, turnover, trade count, fees paid, deflated Sharpe, PSR; equity + drawdown curves; **block-bootstrap Monte Carlo** of trade returns for confidence bands. | Metrics validated against a reference implementation on fixtures. |
| BT-7 | **Determinism:** seeded RNG, stable ordering, result hash. | Same input ⇒ identical hash across runs and machines (`TEST-5`). |
| BT-8 | Accounting engine shared with paper/live: positions, avg price, realized/unrealized PnL, fees, margin, leverage, funding. | Property tests: $\text{equity}_T = \text{equity}_0 + \sum \text{realized} + \text{unrealized} - \sum \text{fees}$ holds for random fill sequences. |

### 9.5 Simulation / Paper Trading (`PAP-*`)

| ID | Requirement | Acceptance criteria |
|---|---|---|
| PAP-1 | Live shadow execution using live feed + `SimBroker`; limit orders rest in a simulated queue; partial fills when live trades cross price; market orders walk the live book (L2) or use quote + slippage model (L1). | Soak test 24 h without drift/leaks. |
| PAP-2 | **State isolation:** separate virtual balances, margin, PnL ledger; stored under a distinct `account_id`; all UI/log/Telegram output tagged `[PAPER]`. | It is structurally impossible for a paper `account_id` to reach a live broker (type-level + runtime assertion + test). |
| PAP-3 | Paper uses the **same RiskManager config schema**, so risk behavior is validated pre-live. | Risk rejections appear identically in paper. |
| PAP-4 | Paper-vs-live divergence report once live runs exist (fill price/latency deltas) to recalibrate friction. | Report generated on demand. |

### 9.6 Bot Execution & Management (`EXE-*`)

| ID | Requirement | Acceptance criteria |
|---|---|---|
| EXE-1 | **Triple-gated live mode:** (a) env `HELM_LIVE_TRADING=1`, (b) config `mode = "live"` for that bot, (c) strategy lifecycle state ≥ `LIVE_SMALL` (Section 13). Any missing ⇒ engine refuses live broker construction. | Tests: each missing gate blocks; error message names the gate. |
| EXE-2 | **Lifecycle controls:** `start`, `pause` (no new entries; manage/exits allowed), `stop` (cancel resting orders, keep positions unless asked), `flatten` (cancel all + reduce-only close all, verify flat), `resume` (after HALT requires confirmation code). | State machine tested; commands idempotent. |
| EXE-3 | **Startup recovery:** load journal + query broker; if unexpected positions/orders exist, **refuse to trade** unless `--adopt` (adopt as-is) or `--flatten-first`. | `kill -9` chaos test passes (success metric). |
| EXE-4 | **Reconciliation** every 60 s, on startup, and after every reconnect: compare internal vs. broker positions, balances, open orders. Mismatch > 0.1% of position notional (or any unknown order) ⇒ `PAUSE` + critical alert. | Injected discrepancy triggers pause within one cycle. |
| EXE-5 | **Execution algos:** Phase 1 market/limit/post-only; Phase 2 TWAP; Phase 3 VWAP, Iceberg. Slices obey risk per slice. | Slicer tests with simulated liquidity. |
| EXE-6 | **Multi-strategy:** per-strategy capital allocation and loss caps; global caps still apply (Phase 3 portfolio allocator). | Allocation limits enforced in `RiskManager`. |
| EXE-7 | **Bot parameter adjustment** at runtime allowed only via validated, journaled commands; risk parameters may be **tightened live, loosened only by restart**. | Test: attempt to loosen at runtime is rejected. |
| EXE-8 | Single-writer lock per broker account (I9). | Second engine instance fails to start with clear error. |
| EXE-9 | Venue-specific: market-hours/calendar awareness (equities), symbol allowlist per bot, reduce-only support check. | Orders outside allowed hours/symbols rejected. |

### 9.7 UI (`UI-*`)

High-density, dark terminal aesthetic. Desktop-first; Telegram covers mobile.

| ID | Requirement |
|---|---|
| UI-1 | **Header:** mode badge (`PAPER` blue / `LIVE` red, unmistakable), global engine status, daily PnL, feed health, **Kill button** (click → confirm popover → executes `flatten + HALT`; Telegram `/kill` is immediate). |
| UI-2 | **Dashboard:** equity curve, positions grid, open orders grid, bots table with start/pause/stop/flatten, risk utilization bars (exposure vs. caps, daily loss vs. limit). |
| UI-3 | **Chart view:** Lightweight Charts with trade entry/exit markers, indicator overlays, order lines. |
| UI-4 | **Watchlist:** dense sortable table (TanStack Table), anomaly highlighting. |
| UI-5 | **Research:** experiment table, tearsheet viewer, compare runs, promotion-state controls. |
| UI-6 | **Logs & alerts:** live structured log tail with filters; risk-decision audit view. |
| UI-7 | Performance: WS updates batched at 10 Hz; virtualized tables; no layout thrash; first meaningful paint < 2 s on LAN. |
| UI-8 | Auth: bound to Tailscale/localhost only; local session token; CSRF-safe control endpoints; no secrets ever sent to the browser. |

### 9.8 Alerting & Ops (`OPS-*`)

| ID | Requirement | Acceptance criteria |
|---|---|---|
| OPS-1 | **Telegram bot:** commands `/status /positions /pnl /orders /pause /resume <code> /flatten /kill /bots`. Authorized by **allowlisted Telegram user ID + chat ID**; unknown senders ignored silently and logged; command rate-limited. | Unauthorized user cannot trigger anything (test). `/kill` executes in one step. |
| OPS-2 | **Alert severities** (INFO/WARN/CRITICAL) with dedupe + throttling; CRITICAL always delivered (order rejection bursts, feed drops, reconciliation mismatch, drawdown warnings at 50%/80% of limit, kill-switch fired, flatten failed). Daily PnL summary. | Alert storm test: 1000 identical events ⇒ ≤ N messages. |
| OPS-3 | **Dead-man's switch:** engine pings an external heartbeat URL (e.g. healthchecks-style) every 60 s; silence ⇒ out-of-band alert. | Documented setup; disabled gracefully if unset. |
| OPS-4 | **Structured logging** (`structlog` JSON) with `run_id`, `strategy_id`, `client_order_id` correlation; secret-redaction filter. | Test: known secret strings never appear in logs. |
| OPS-5 | Healthcheck endpoint + Docker healthcheck; Compose `restart: unless-stopped`; on restart, engine starts in **PAUSED** if previous state was HALTED. | Verified in chaos test. |
| OPS-6 | Backups: nightly encrypted copy of SQLite + journal; Parquet is re-derivable/exportable. | Restore drill documented and tested once. |

---

## 10. Risk Engine (the most important module)

**Contract:** `RiskManager.evaluate(intent, portfolio, market, config) -> RiskDecision` is a **pure function** of its inputs (plus injected clock). No I/O, no strategy imports. Any exception ⇒ `REJECT(reason="risk_error")` and a CRITICAL alert. Every decision is journaled with an inputs snapshot hash.

### Pre-trade rules (evaluated in order; first rejection wins)
| ID | Rule | Default |
|---|---|---|
| RISK-1 | Engine state gate: reject new-risk orders unless `RUNNING` (exits/reduce-only allowed in `PAUSED`; only flatten allowed in `HALTED`). | — |
| RISK-2 | Instrument/market gate: symbol allowlist, trading hours, instrument metadata present, qty/price quantized, `min_notional` met. | — |
| RISK-3 | **Data freshness:** reject if last quote for symbol older than `stale_feed_ms` or `FeedStatus != LIVE`. | 2000 ms |
| RISK-4 | **Fat-finger:** reject if $\frac{|p_{\text{limit}} - p_{\text{mid}}|}{p_{\text{mid}}} > 3\%$ (market orders: estimated worst-case price vs. mid); reject if order notional > `max_order_notional`. | 3% / configurable |
| RISK-5 | **Position cap:** post-trade symbol exposure ≤ `max_position_pct_equity` of total equity. | 10% |
| RISK-6 | **Leverage cap:** post-trade gross exposure / equity ≤ `max_gross_leverage`. | 2.0× |
| RISK-7 | **Rate caps:** orders/min per strategy and globally; max open orders; `max_consecutive_rejects` ⇒ auto-`PAUSE` strategy. | 30/min, 20 open, 5 |
| RISK-8 | **Strategy allocation:** per-strategy capital and loss cap (Phase 3). | — |
| RISK-9 | **Live-capital cap by lifecycle stage:** `LIVE_SMALL` limits capital to a configured % until promoted. | 2% equity |

### Runtime circuit breakers (supervisor, independent of strategies)
| ID | Trigger | Action |
|---|---|---|
| RISK-10 | **Daily loss** (realized + unrealized vs. start-of-day equity) > limit (default 3%); day boundary and timezone configurable per asset class. | `flatten_and_halt` (configurable to `cancel_and_halt`). |
| RISK-11 | **Max drawdown from high-water mark** > limit (default 10%). | `flatten_and_halt`. |
| RISK-12 | **Feed staleness:** no data for subscribed symbol or lag (clock-skew-corrected) > 2000 ms. | Cancel all resting orders + block new entries; auto-clear when feed healthy for N s. |
| RISK-13 | **Reconciliation mismatch** (`EXE-4`). | `PAUSE` + critical alert. |
| RISK-14 | **Remote kill:** `/kill` (Telegram) or UI Kill. | `flatten + HALT`. |
| RISK-15 | **Clock skew** > 250 ms or NTP failure. | WARN; > 1 s ⇒ `PAUSE`. |

### Kill-switch semantics
- States: `RUNNING → PAUSED → HALTED`. **`HALTED` is latched and persisted**; survives restarts; clearing requires an explicit `resume <one-time-code>` from an authorized channel.
- **Flatten procedure:** cancel all open orders → submit reduce-only closes (market, or aggressive IOC limit within a price collar if market orders unavailable) → poll positions via REST → retry with backoff up to N times → verify flat. If not flat after retries ⇒ CRITICAL alert every 30 s until resolved. Flatten bypasses RISK-3/5/6/7 but **never** violates reduce-only.
- Risk config can be tightened at runtime, loosened only via restart (`EXE-7`).

### Risk testing requirements
- 100% branch coverage on `risk/`.
- **Hypothesis property tests:** for random portfolios/intents/configs, no `APPROVE` decision may result in a post-trade state that breaches RISK-4/5/6.
- Fail-closed tests: inject exceptions/None inputs ⇒ always `REJECT`.

---

## 11. Security

| ID | Requirement |
|---|---|
| SEC-1 | **No withdrawal capability.** Exchange keys: Read + Trade only (Spot/Futures as needed); withdrawal/transfer permissions disabled at the provider. On startup, adapters that can introspect key permissions **must** verify and refuse to start if withdrawal is enabled. For venues that cannot report permissions (e.g. brokers with account-wide keys), document the limitation and require the owner to confirm in config (`attest_no_withdrawal = true`) at setup. |
| SEC-2 | **Secrets:** never in git, never in plaintext `.env`. Encrypted at rest with `age`/SOPS (or Vault/Doppler); decrypted only into process memory at engine start; passed to the engine container only (Docker secrets/tmpfs). `gitleaks` pre-commit + CI. Redacting log filter (`OPS-4`). |
| SEC-3 | **Network:** exchange keys IP-whitelisted to the server's static IP **where the provider supports it** (note per-venue support in docs; some brokers don't). UI/API reachable only via Tailscale/WireGuard or localhost; ports 80/443 never exposed publicly. Outbound allowlist for the engine container where practical. |
| SEC-4 | **Credential isolation:** `research` container has no credentials and no broker egress; AI-generated code runs only there. |
| SEC-5 | **Control-plane auth:** Telegram user/chat allowlist; UI local token; control endpoints require auth + are rate-limited; `resume` requires one-time code. |
| SEC-6 | **Supply chain:** pinned lockfile; dependency audit in CI (`pip-audit`/`npm audit`); minimal base images; non-root containers; read-only root FS where feasible. |
| SEC-7 | **Auditability:** journal is append-only; risk decisions, control commands (who/when/from where), and config changes are recorded. |
| SEC-8 | **On-chain keys** (Phase 3 only): separate hot wallet, minimal balance, encrypted keystore, never shared with research container (`TRK-5`). |
| SEC-9 | **Compliance note for owner:** check applicable rules for your account type (e.g. pattern-day-trader / intraday margin rules for US margin equities accounts, exchange ToS on API usage and market-data licensing for personal vs. commercial use). Not legal or financial advice; build config flags, not assumptions. |

---

## 12. Non-Functional Requirements

| Area | Target (measured in CI/bench where noted) |
|---|---|
| Internal latency (excl. network RTT) | tick/quote received → strategy callback return: **p99 < 5 ms** (bar/L1 strategies); intent → risk decision: **p99 < 1 ms**; risk-approved → request handed to HTTP/WS client: **p99 < 15 ms**. Histograms exported. |
| Resources | Entire stack within **4 vCPU / 8 GB RAM**; engine steady-state < 1.5 GB RSS with 20 symbols. |
| Throughput | Handle ≥ 2,000 normalized events/s sustained without queue growth (L1/trades for ~20 symbols). |
| Backtest speed | Event-driven, 1 symbol, 1 yr of 1m bars: < 60 s target. Vectorized sweeps per `BT-1`. |
| Reliability | Restart recovery < 30 s; journal fsync on order events; zero lost order state on `kill -9`. |
| Backpressure | Bounded queues; slow consumers (UI) drop/coalesce, never block the trading path. |
| Portability | `docker compose up` on Linux/macOS; same images on VPS. |
| Maintainability | Fully typed (`pyright --strict` on `core`, `risk`, `oms`); `ruff` clean; modules < ~400 lines where practical. |
| Cost | < $30/month VPS or an existing mini-PC; no mandatory paid services (paid data/LLM optional). |

---

## 13. Strategy Promotion Lifecycle

```
DRAFT → BACKTESTED → VALIDATED → PAPER → LIVE_SMALL → LIVE
```
| Transition | Gate (thresholds configurable in `promotion.toml`; each transition is a **manual approval**, journaled) |
|---|---|
| DRAFT → BACKTESTED | Event-driven backtest completes; lookahead tests pass; friction enabled. |
| BACKTESTED → VALIDATED | Walk-forward + purged-CV pass configured thresholds; min trade count met; deflated Sharpe above threshold; parameter plateau check; **locked holdout** result acceptable. |
| VALIDATED → PAPER | Strategy code frozen (hash recorded). |
| PAPER → LIVE_SMALL | ≥ N days (default 14) or ≥ M trades in paper; paper/backtest parity within tolerance on recorded feed; no unresolved CRITICAL alerts; risk rejections reviewed. |
| LIVE_SMALL → LIVE | ≥ N days at capped capital (default 2%); divergence report (`PAP-4`) acceptable. |
Any code or parameter change resets the strategy to `DRAFT`/`BACKTESTED` as appropriate.

---

## 14. Testing & Quality Gates

`make check` runs: `ruff`, `pyright`, `import-linter`, `pytest` (unit, property, integration), `gitleaks`. Must be green before every commit that finishes a milestone task.

| ID | Test | Purpose |
|---|---|---|
| TEST-1 | `FakeExchange` implementing `Broker` + REST/WS surface with configurable latency, 429s, disconnects, partial fills, duplicate/out-of-order events, ambiguous submit responses. | Integration/chaos harness for adapters and OMS. |
| TEST-2 | Property tests (Hypothesis) for accounting identity, order state machine legality, risk invariants. | Catch edge cases humans don't. |
| TEST-3 | **Lookahead truncation test** harness applied to every feature and every example strategy. | Prevent leakage. |
| TEST-4 | **Backtest ↔ paper parity:** replay a recorded live feed through event-driven backtest and through paper engine; order sequences must match exactly. | Validates invariant I1. |
| TEST-5 | **Determinism:** run the same backtest twice (and in a fresh process); result hashes equal. | I7. |
| TEST-6 | **Crash recovery:** `kill -9` engine during active orders in `FakeExchange`; restart; no duplicates, state matches broker. | `EXE-3`. |
| TEST-7 | **Gate tests:** each live-mode gate individually blocks live broker creation; paper account cannot reach a live broker. | `EXE-1`, `PAP-2`. |
| TEST-8 | **Security tests:** log redaction, unauthorized Telegram command, API without token. | SEC-2/5. |
| TEST-9 | Golden-file backtests for example strategies (tolerance-checked) to detect unintended behavioral drift. | Regression safety. |
| TEST-10 | Performance benchmarks in CI (non-blocking, tracked) for the latency targets. | NFR tracking. |

---

## 15. Phased Roadmap

Effort figures are rough solo-with-Claude-Code estimates, not commitments.

### Phase 1 — MVP: single venue, bar-based strategies, safe live (≈ 4–6 weeks)

**M0 — Foundations (≈ 3–4 days)**
Repo, `uv`, `Makefile`, CI, pre-commit, `CLAUDE.md`, config loading, structured logging + redaction, `core` models/events/clock/bus, journal, `import-linter` contracts, `FakeExchange` skeleton.
*Exit:* `make check` green; import contracts enforced; journal append/replay test passes.

**M1 — Data layer (≈ 1 week)**
Parquet store, importer + validators (`DATA-2`), CCXT historical fetch (`DATA-3`), rate-limit governor (`DATA-6`), WS manager (`DATA-5`), live recorder (`DATA-4`), instrument metadata (`DATA-8`).
*Exit:* import 1 GB CSV within RAM budget; WS chaos test passes; 24 h of live recording replays cleanly.

**M2 — Strategy API + backtester (≈ 1–1.5 weeks)**
`Strategy`/`Context`, vectorized mode (`BT-1`), event-driven bar mode (`BT-2`, `BT-3`), friction (`BT-5`), accounting (`BT-8`), tearsheet (`BT-6`), determinism (`BT-7`), feature factory seed set + lookahead harness (`RES-1`, `TEST-3`), example strategies (smoke-test only, not alpha).
*Exit:* `TEST-2/3/5/9` green; tearsheet generated by CLI.

**M3 — Risk, OMS, paper, recovery (≈ 1.5 weeks)**
`RiskManager` (RISK-1…9), supervisor breakers (RISK-10…15), kill-switch state machine, order state machine + idempotent IDs, `SimBroker` live-mode fills, paper mode (`PAP-*`), reconciler, startup recovery, single-writer lock.
*Exit:* risk 100% branch coverage + property tests; `TEST-4` parity, `TEST-6` crash recovery, `TEST-7` gate tests all green; 24 h paper soak with no state drift.

**M4 — Live adapter, control plane, UI, deploy (≈ 1–1.5 weeks)** — **BLOCKING:** ask owner for first venue.
`CcxtBroker` (trade-only; testnet first; key-permission introspection), Telegram bot + alerts (`OPS-1/2/3`), FastAPI control API, minimal Next.js UI (`UI-1/2/6`), Docker Compose, backups, runbook.
*Exit (MVP done):* 7-day paper soak on live feed; testnet live run with kill/flatten drills passing; written **go-live checklist** completed; first real-money run uses `LIVE_SMALL` caps only.

### Phase 2 — V1.5: multi-asset, realistic microstructure, richer UI (≈ 4 weeks)
- `alpaca-py` and `ib_async` adapters (equities/options, market calendars, corporate actions).
- Tick/L1/L2 replay backtester with queue-position model + latency/spread modeling (`BT-4`).
- Validation suite: purged K-fold, walk-forward, locked holdout, deflated Sharpe (`RES-3/4`).
- Promotion lifecycle UI (`Section 13`).
- Lightweight Charts with trade markers, watchlists, alert rules (`UI-3/4/5`, `TRK-1/2`).
- TWAP execution (`EXE-5`).
- Optional: Redis Streams bus / QuestDB if profiling shows need.

### Phase 3 — V2.0: AI research, on-chain/social, advanced execution (≈ 4+ weeks)
- AI hypothesis sandbox pipeline (`RES-5`), automated experiment loops.
- News/sentiment ingestion; wallet tracker + alert rules (`TRK-3/4`); **paper-mirror before any live-mirror** (`TRK-6`); optional gated on-chain execution (`TRK-5`).
- VWAP/Iceberg, multi-strategy portfolio allocator (`EXE-5/6`, `RISK-8`).
- Rust acceleration **only** for profiled hotspots.

---

## 16. Known Risks & Pitfalls (design for these)

| Risk | Mitigation |
|---|---|
| Backtest overfitting / data snooping | Trial counting, deflated Sharpe, purged CV, locked holdout, promotion gates. |
| Lookahead bias (features, bar timing, adjusted data) | Truncation tests, closed-bar-only callbacks, next-bar execution. |
| Optimistic fills (limit touch = fill, no queue) | Trade-through rule, queue model, conservative defaults. |
| Duplicate/ghost orders after timeouts | Deterministic IDs, `UNKNOWN` state resolved via REST, journal. |
| WS silently stalls | Heartbeats, per-symbol staleness, RISK-12. |
| State drift vs. broker | 60 s reconciliation + startup reconciliation. |
| Decimal/precision rejections | Instrument metadata, quantization before risk and before submit. |
| Runaway strategy loop | Rate caps, consecutive-reject breaker, per-strategy loss caps. |
| Exchange outage / maintenance during open positions | Alerts, flatten retries, manual runbook. |
| Telegram token compromise | User/chat allowlist, one-time resume code, no trading beyond control commands (can only flatten/pause, never open risk). |
| Prompt-injected or buggy AI strategy | Sandbox, AST allowlist, no credentials, manual promotion. |
| Single machine failure | Backups, dead-man's switch, resilient restart to `PAUSED`. |

---

## 17. Open Decisions (defaults apply unless owner overrides)

| # | Question | Default | Blocking? |
|---|---|---|---|
| 1 | First venue & asset class | Crypto via CCXT on one US-accessible exchange with testnet; Alpaca in Phase 2 | **Yes (M4)** |
| 2 | Deployment target | Local machine; VPS later | No |
| 3 | Starting capital & `LIVE_SMALL` cap | $500 total exposure cap, 2% allocation | No (set in config) |
| 4 | LLM provider for AI assistant | Anthropic API via credential-free sandbox, Phase 3 | No |
| 5 | Paid market-data provider | None; venue-native data + recorded feed | No |
| 6 | Daily-loss day boundary | 00:00 UTC for crypto; 09:30 ET session for equities | No |
| 7 | Discord vs Telegram only | Telegram control + Telegram/Discord alerts | No |

---

## Appendix A — Example Config (`config/risk.example.toml`)

```toml
[risk]
max_position_pct_equity   = 10.0
max_gross_leverage        = 2.0
max_order_notional_usd    = 500
fat_finger_pct            = 3.0
max_orders_per_min        = 30
max_open_orders           = 20
max_consecutive_rejects   = 5
stale_feed_ms             = 2000
clock_skew_warn_ms        = 250
clock_skew_pause_ms       = 1000
daily_loss_limit_pct      = 3.0
max_drawdown_pct          = 10.0
on_daily_loss             = "flatten_and_halt"   # or "cancel_and_halt"
daily_reset               = { tz = "UTC", time = "00:00" }
reconcile_interval_s      = 60
reconcile_tolerance_pct   = 0.1
live_small_capital_pct    = 2.0

[security]
attest_no_withdrawal      = false     # must be true for venues that can't report key permissions
telegram_allowed_user_ids = []
telegram_allowed_chat_ids = []
```

## Appendix B — `CLAUDE.md` Seed

```markdown
# Helm — agent instructions
Source of truth: PRD.md. Requirement IDs are the contract; cite them in commits/tests.

## Commands
- make check   # ruff, pyright, import-linter, pytest, gitleaks — must pass before finishing any task
- make test | make lint | make fmt | make up (docker compose) | make backtest ARGS="..."

## Hard rules
- NEVER place real orders in dev/tests. Use FakeExchange / testnet / paper only.
- NEVER read, print, log, or commit secrets. No real keys in repo, tests, or fixtures.
- Live broker code is reachable only through the triple gate (EXE-1). Don't weaken it.
- Every order intent must pass RiskManager. Risk is fail-closed and pure (no I/O, imports core only).
- Strategies return OrderIntents only; no time/network/broker/os imports. Never use wall clock; use ctx.now().
- Decimal (not float) for price/qty/fees in OMS, brokers, accounting. Quantize before risk and before submit.
- No lookahead: closed bars only; every feature needs a truncation test.
- Timestamps are UTC integer ns. Keep ts_event and ts_recv.
- New dependency => docs/decisions/ note. Ambiguity => pick PRD default, write a decision note, proceed.

## Workflow
Milestones in PRD §15, in order. Tests first for risk, OMS state machine, accounting, fill models.
Small commits. Update this file when conventions change.
```

## Appendix C — Kickoff Prompt for Claude Code

```text
Read PRD.md fully. We're building milestone M0 only. First:
1) Create the repo layout from §7, pyproject (uv), Makefile (`make check`), pre-commit (ruff, gitleaks), CI, and CLAUDE.md from Appendix B.
2) Implement core models/events/clock/bus/journal per §6 and §8 with tests, plus import-linter contracts from §7.
3) Stub FakeExchange per TEST-1.
Follow §0 rules. Do not start M1 until M0's exit criteria pass; then summarize what was built, what decisions you recorded, and wait for my go-ahead.
```
