# 0024 — M4 dependencies: httpx, FastAPI, uvicorn (no alpaca-py)
- **Context:** M4 needs an async REST client for Alpaca and Telegram, and the control API (PRD §5 names FastAPI).
- **Decision:** add `httpx` (async client, and `MockTransport` lets adapter contract tests run without network), `fastapi` and `uvicorn`. Do **not** add `alpaca-py`: its REST client is synchronous (requests), and its stream runs its own event loop. That would break I1 (one asyncio engine loop) and stop us injecting fake transports in tests. The Alpaca REST and stream protocols are small enough to call directly.
- **Also:** `age` (CLI, installed via brew/apt rather than a Python dependency) encrypts backups (OPS-6, decision 0028).
