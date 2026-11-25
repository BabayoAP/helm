# 0011 — WS manager takes an injected transport + FeedHandler
- **Context:** DATA-5 behaviour (heartbeat, backoff, resubscribe, gap-fill, book re-snapshot) must be chaos-tested without a network.
- **Decision:** `WsManager` depends on a `WsTransport` protocol (send/recv/ping/close) and a venue `FeedHandler` (subscribe messages, parse, REST gap-fill, book snapshot). Backoff is full-jitter exponential (base 1 s, cap 60 s) from a seeded RNG.
- **Consequence:** the venue-specific handler + `websockets` transport ship with the first venue in M4 (BLOCKING venue choice).
