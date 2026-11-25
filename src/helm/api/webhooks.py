"""DATA-7: generic signed webhook receiver (HMAC-SHA256, replay-protected).

Runs as its own small app on its own port, so it can be exposed (e.g. through a
tunnel) without exposing the control API, which stays localhost-only (SEC-3).

Request: POST /hook/{source}
  X-Helm-Timestamp: sender time, integer seconds since the epoch
  X-Helm-Signature: hex HMAC-SHA256(secret, f"{timestamp}.{raw body}")
Rejected: unknown source, bad/missing signature, timestamp outside the window,
a signature already seen (replay), non-JSON or oversized body, over the rate.
Accepted hooks are journaled and alerted via `WebhookSink`; nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping

from fastapi import FastAPI, HTTPException, Request

from helm.core.control import WebhookSink
from helm.core.events import WebhookEvent

MAX_BODY = 16 * 1024


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def create_webhook_app(
    sink: WebhookSink,
    secrets: Mapping[str, str],
    *,
    window_s: int = 300,
    per_min: int = 60,
    time_fn: Callable[[], float] = time.time,
) -> FastAPI:
    if not secrets or any(len(v) < 16 for v in secrets.values()):
        raise ValueError("every webhook source needs a secret of at least 16 characters")
    app = FastAPI(title="Helm webhooks", docs_url=None, redoc_url=None, openapi_url=None)
    seen: OrderedDict[str, float] = OrderedDict()  # signature -> expiry (replay cache)
    hits: deque[float] = deque()

    @app.post("/hook/{source}")
    async def hook(source: str, request: Request) -> dict[str, str]:
        now = time_fn()
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= per_min:
            raise HTTPException(status_code=429, detail="rate limited")
        hits.append(now)
        secret = secrets.get(source)
        if secret is None:
            raise HTTPException(status_code=404, detail="unknown source")
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(status_code=413, detail="body too large")
        ts_h, sig = request.headers.get("x-helm-timestamp"), request.headers.get("x-helm-signature")
        if not ts_h or not sig or not ts_h.isdigit():
            raise HTTPException(status_code=401, detail="missing signature headers")
        ts = int(ts_h)
        if abs(now - ts) > window_s:
            raise HTTPException(status_code=401, detail="stale or future timestamp")
        if not hmac.compare_digest(sign(secret, ts, body), sig.lower()):
            raise HTTPException(status_code=401, detail="bad signature")
        for k in [k for k, exp in seen.items() if exp < now]:
            del seen[k]
        if sig in seen:
            raise HTTPException(status_code=409, detail="replayed")
        seen[sig] = now + window_s
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be JSON") from None
        sink.receive_webhook(
            WebhookEvent(
                source=source,
                payload_json=json.dumps(payload, sort_keys=True)[:MAX_BODY],
                signature=sig,
                ts_sent=ts * 1_000_000_000,
                ts_recv=sink.now(),
            )
        )
        return {"status": "accepted"}

    return app
