"""DATA-7 signed webhook receiver: HMAC, freshness window, replay protection, size and
rate limits; accepted hooks are journaled + alerted and can never trade."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helm.api.webhooks import MAX_BODY, create_webhook_app, sign
from helm.app.control import EnginePort
from helm.app.services import build_control_plane
from helm.core.config import ControlConfig, HelmConfig
from tests.integration.test_control_plane import paper_engine

SECRET = "test-only-" + "x" * 20  # obviously fake, low entropy
NOW = 1_800_000_000.0


def _post(
    c: TestClient,
    body: bytes,
    *,
    ts: int = int(NOW),
    secret: str = SECRET,
    source: str = "tradingview",
    sig: str | None = None,
) -> int:
    headers = {"X-Helm-Timestamp": str(ts), "X-Helm-Signature": sig or sign(secret, ts, body)}
    return c.post(f"/hook/{source}", content=body, headers=headers).status_code


def test_signed_hooks_accepted_everything_else_refused(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)
    port = EnginePort(eng, venue="fake")
    now = [NOW]
    c = TestClient(
        create_webhook_app(port, {"tradingview": SECRET}, per_min=8, time_fn=lambda: now[0])
    )
    body = json.dumps({"ticker": "SPY", "action": "buy"}).encode()

    assert _post(c, body) == 200
    assert _post(c, body) == 409  # exact replay
    assert _post(c, b'{"a": 1}', secret="wrong-secret-xxxxxxxxxx") == 401
    assert _post(c, b'{"a": 2}', ts=int(NOW) - 301) == 401  # stale
    assert _post(c, b'{"a": 3}', ts=int(NOW) + 301) == 401  # from the future
    assert _post(c, b'{"a": 4}', source="unknown") == 404
    assert _post(c, b"not json") == 400
    assert _post(c, b"x" * (MAX_BODY + 1)) == 413
    assert c.post("/hook/tradingview", content=b"{}").status_code == 429  # 9th in a minute
    now[0] += 61
    assert c.post("/hook/tradingview", content=b"{}").status_code == 401  # no headers

    entries = list(eng.journal.entries(kinds=("WebhookEvent",)))
    assert len(entries) == 1
    ev = json.loads(entries[0].payload)
    assert ev["source"] == "tradingview" and json.loads(ev["payload_json"])["ticker"] == "SPY"
    assert [a.key for a in eng.alerts.sent] == ["webhook:tradingview"]
    assert eng.om.all() == []  # a webhook never trades
    with pytest.raises(ValueError, match="16 characters"):
        create_webhook_app(port, {"x": "short"})


def test_webhook_service_wiring(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)

    class Store:
        def get(self, name: str) -> str:
            assert name == "webhook_tradingview_secret"
            return SECRET

    cfg = HelmConfig(control=ControlConfig(webhook_sources=("tradingview",)))
    cp = build_control_plane(eng, cfg, Store(), venue="fake", ring=None, log=None, api=False)
    assert len(cp.services) == 2  # webhook listener + daily summary
    with pytest.raises(RuntimeError, match="webhooks"):
        build_control_plane(eng, cfg, None, venue="fake", ring=None, log=None, api=False)
