# Build status

Milestones follow PRD §15. A requirement is **done** only when a test proves it (PRD §0.2).
Legend: ✅ done (test-proven) · 🟡 partial (see note) · ⏳ needs wall-clock time / owner · ⬜ not started

## M0 — Foundations ✅

| Item | Status | Proof |
|---|---|---|
| Repo layout §7, uv, Makefile, CI, pre-commit, CLAUDE.md | ✅ | `make check`; `.github/workflows/ci.yml` |
| Config loading (Appendix A) + secret-key rejection | ✅ | `tests/unit/test_infra.py` |
| Structured logging + redaction (OPS-4) | ✅ | `test_known_secret_never_appears_in_logs` |
| core models/events/clock/bus/ids/money (DATA-1 base) | ✅ | `tests/unit/test_core.py` |
| Journal append/replay, append-only (I4, SEC-7) | ✅ | `tests/unit/test_journal.py` |
| import-linter contracts (§7) | ✅ | `make imports` (5 contracts; caught a real `strategy → core.clock → time` leak, see decision 0003) |
| FakeExchange skeleton (TEST-1) | ✅ | `tests/integration/test_fake_exchange.py` |

**Exit criteria:** `make check` green ✅ · import contracts enforced ✅ · journal append/replay test passes ✅

## M1 — Data layer 🟡 (code + tests done; 24 h live recording needs a venue)

| ID | Status | Proof |
|---|---|---|
| DATA-1 canonical models + normalizer never crashes | ✅ | `tests/property/test_normalizer_fuzz.py` (Hypothesis fuzz, round-trip) |
| DATA-2 importer: dedup, OHLC sanity, monotonic, gap report, idempotent | ✅ | `tests/unit/test_data_lake.py` |
| DATA-2 1 GB CSV within 2 GB RAM | ✅ | `scripts/bench_import.py`: 1.11 GB / 18 M rows in 5.4 s, peak RSS 714 MB (2026-11-24, M-series Mac) |
| DATA-3 CCXT history fetch, resume w/o duplicates, via limiter | ✅ | `tests/integration/test_ccxt_history.py` (fake client); live run needs network |
| DATA-4 live recorder, bit-for-bit replay | 🟡 | `test_recorder_replays_bit_for_bit` ✅; **24 h live recording ⏳ needs the M4 venue** |
| DATA-5 WS manager: heartbeat, backoff, resubscribe, gap-fill, book re-snapshot, FeedStatus | ✅ | `tests/chaos/test_ws_chaos.py` (100+ drops, no dup/missing bars, book == REST snapshot, STALE raised). Venue transport/handler ships in M4 |
| DATA-6 rate-limit governor, priorities, 429 backoff | ✅ | `tests/unit/test_ratelimit.py` (10x over-demand vs strict FakeExchange: 0 x 429; cancels wait < 200 ms) |
| DATA-8 instrument metadata, quantization, unknown => reject | ✅ | `test_instrument_registry_conform_and_cache` |
| DATA-7, DATA-9 | ⬜ | Phase 2/3 adapters; equities hygiene (Phase 2) |

**Exit criteria:** 1 GB import within RAM ✅ · WS chaos test ✅ · 24 h live recording replays cleanly ⏳ (needs venue)
