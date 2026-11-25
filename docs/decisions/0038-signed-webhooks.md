# 0038 — Signed webhook receiver (DATA-7)
- **Decision:** `helm.api.webhooks` is a separate FastAPI app on its own host/port (`control.webhook_host` / `webhook_port`, default `127.0.0.1:8766`), started only when `control.webhook_sources` is set. The control API stays localhost-only (SEC-3), so exposing webhooks, e.g. through a tunnel, never exposes controls.
- **Auth:**
  - `X-Helm-Signature = hex HMAC-SHA256(secret, "{timestamp}." + raw body)`, compared in constant time, with `X-Helm-Timestamp` within ±300 s.
  - Each signature is accepted once (replay cache for the window).
  - Body ≤ 16 KiB, JSON only; 60 requests/min.
  - Per-source secrets of ≥ 16 chars come from the secrets store (`webhook_<source>_secret`).
- **Effect:** a verified hook becomes a `WebhookEvent`, journaled with an INFO alert. It has no trading surface (TRK-4 default action = alert). Acting on a signal would be a strategy feature behind the RiskManager, not a webhook feature.
- **TradingView note:** TradingView can't compute HMACs. Put a small signing relay in front, or a fixed-secret variant if one is ever added; that would be a separate, weaker mode needing its own decision.
