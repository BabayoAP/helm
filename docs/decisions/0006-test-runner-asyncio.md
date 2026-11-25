# 0006 — No pytest-asyncio; async tests use `asyncio.run`
- **Context:** §5 does not list an asyncio pytest plugin.
- **Decision:** async tests wrap a local coroutine in `asyncio.run(...)`.
- **Consequence:** one fewer dependency; each test gets a fresh event loop, which also avoids cross-test state leaks.
