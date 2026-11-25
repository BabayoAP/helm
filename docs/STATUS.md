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
