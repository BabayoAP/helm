# 0021 — FakeExchange over a local JSON-lines socket for kill -9 tests
- **Context:** TEST-6 needs the venue to survive the engine process being `kill -9`ed.
- **Decision:** `helm.brokers.fake_exchange_rpc` serves a FakeExchange on 127.0.0.1 (stdlib asyncio only) and provides a Broker-protocol client. Test infrastructure only.
- **Consequence:** the chaos test kills a real engine process repeatedly (until at least one kill lands mid-flight) and verifies every client_order_id reached the venue exactly once and a fresh engine rebuilt from the journal matches the venue exactly.
