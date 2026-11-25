# 0001 — M0 dependency baseline
- **Context:** PRD §5 stack; §0.5 says pin latest stable at build time (verified via `uv add`, 2026-11-24).
- **Decision:** runtime `pydantic` 2.14, `structlog` 26.1. Dev: `ruff` 0.17, `pyright` 1.1.414, `pytest` 9.2, `hypothesis` 6.170, `import-linter` 2.17, `pre-commit` 4.7. Versions are locked in `uv.lock`.
- **Additions beyond §5:** `pytest-cov` (needed to prove the 100% branch coverage on `risk/` that §10 requires). `msgspec` deferred: Pydantic first, switch hot-path structs only if profiling shows a need.
- **Consequence:** `make check` reproducible from `uv sync --locked`.
