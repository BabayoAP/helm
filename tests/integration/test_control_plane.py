"""Control plane: API auth/CSRF/rate limits (UI-8, SEC-5), Telegram allowlist (OPS-1),
alerts to Telegram (OPS-2), heartbeat (OPS-3), daily summary, journaled commands (SEC-7)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helm.api.app import create_app
from helm.app.control import EnginePort, _downsample  # pyright: ignore[reportPrivateUsage]
from helm.app.engine import Engine
from helm.app.lifecycle import build_engine
from helm.app.services import (
    build_control_plane,
    console_resume_code_sink,
    daily_summary_service,
    heartbeat_service,
)
from helm.brokers.sim.broker import SimBroker
from helm.core.clock import SimClock
from helm.core.config import ControlConfig, HelmConfig, RiskConfig, SecurityConfig
from helm.core.enums import EngineState, Mode, Severity
from helm.core.errors import ConfigError
from helm.core.events import Alert
from helm.data.instruments import InstrumentRegistry
from helm.infra.logging import LogRing
from helm.oms.journal import Journal
from helm.telegram.bot import TelegramAlertSink, TelegramBot, TelegramClient
from helm.tracking.alerts import AlertManager
from tests.factories import SYM, instrument, quote

TOKEN = "t" * 32
LOCAL = {"host": "127.0.0.1:8765"}
USER, CHAT, STRANGER = 111, 222, 999


def paper_engine(tmp: Path, clock: SimClock | None = None) -> Engine:
    clock = clock or SimClock(10**18)
    eng, _ = build_engine(
        mode=Mode.PAPER,
        account_id="paper-test",
        run_id="r1",
        clock=clock,
        broker=SimBroker(account_id="paper-test", venue="fake", clock=clock),
        journal=Journal(tmp / "j.sqlite", mirror=False),
        instruments=InstrumentRegistry([instrument()]),
        risk_config=RiskConfig(),
        starting_equity=Decimal(10000),
        code_fn=lambda: "c0de",
        alerts=AlertManager(mode=Mode.PAPER, clock_fn=clock.now),
    )
    return eng


def control_sources(eng: Engine) -> list[str]:
    return [
        json.loads(e.payload)["source"]
        for e in eng.journal.entries(kinds=("ControlCommand",), account_id=eng.account_id)
    ]


# ------------------------------------------------------------------------- API
def test_api_auth_csrf_host_and_rate_limit(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)
    ring = LogRing()
    ring.records.append({"event": "hello", "level": "info"})
    port = EnginePort(eng, venue="fake", ring=ring)
    app = create_app(port, token=TOKEN, control_per_min=3, time_fn=lambda: 0.0)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    post = {**auth, "X-Helm-CSRF": "1"}

    assert c.get("/health").json() == {"ok": True, "state": "RUNNING", "mode": "paper"}
    assert c.get("/api/snapshot").status_code == 401
    assert c.get("/api/snapshot", headers={"Authorization": "Bearer wrong"}).status_code == 401
    snap = c.get("/api/snapshot", headers=auth).json()
    assert snap["mode"] == "paper" and snap["state"] == "RUNNING" and snap["account_id"]
    assert TOKEN not in json.dumps(snap)  # no secrets to the browser (UI-8)
    assert c.get("/api/logs?n=5", headers=auth).json() == [{"event": "hello", "level": "info"}]
    # CSRF: custom header required; cross-origin refused
    assert c.post("/api/control", json={"action": "pause"}, headers=auth).status_code == 403
    r = c.post(
        "/api/control",
        json={"action": "pause"},
        headers={**post, "Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert c.post("/api/control", json={"action": "pause"}).status_code == 401
    # DNS-rebinding guard
    evil = TestClient(app, base_url="http://evil.example")
    assert evil.get("/health").status_code == 400
    # rate limit (3/min configured)
    ok = c.post(
        "/api/control",
        json={"action": "pause"},
        headers={**post, "Origin": "http://127.0.0.1:8765"},
    )
    assert ok.json() == {"result": "paused"} and eng.ks.state is EngineState.PAUSED
    assert c.post("/api/control", json={"action": "start"}, headers=post).status_code == 200
    assert c.post("/api/control", json={"action": "start"}, headers=post).status_code == 200
    assert c.post("/api/control", json={"action": "start"}, headers=post).status_code == 429
    assert c.post("/api/control", json={"action": "nuke"}, headers=post).status_code == 422
    assert control_sources(eng) == ["ui", "ui", "ui"]  # SEC-7: who/where journaled
    assert ok.headers["X-Frame-Options"] == "DENY"
    with pytest.raises(ValueError, match="too short"):
        create_app(port, token="short")


def test_api_kill_from_ui_halts_and_websocket_needs_token(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)
    app = create_app(EnginePort(eng, venue="fake"), token=TOKEN, ws_hz=50)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    with c.websocket_connect("/ws?token=" + TOKEN, headers=LOCAL) as ws:
        first = json.loads(ws.receive_text())
        assert first["state"] == "RUNNING" and "ts" not in first
        r = c.post(
            "/api/control",
            json={"action": "kill"},
            headers={"Authorization": f"Bearer {TOKEN}", "X-Helm-CSRF": "1"},
        )
        assert r.json() == {"result": "halted"}
        assert json.loads(ws.receive_text())["state"] == "HALTED"
    with (
        pytest.raises(WebSocketDisconnect),
        c.websocket_connect("/ws?token=wrong", headers=LOCAL) as ws,
    ):
        ws.receive_text()
    r = c.post(
        "/api/control",
        json={"action": "resume", "code": "wrong"},
        headers={"Authorization": f"Bearer {TOKEN}", "X-Helm-CSRF": "1"},
    )
    assert r.json() == {"result": "denied"} and eng.ks.state is EngineState.HALTED


def test_ui_static_build_is_served_same_origin(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)
    ui = tmp_path / "out"
    ui.mkdir()
    (ui / "index.html").write_text("<html>helm</html>")
    c = TestClient(
        create_app(EnginePort(eng, venue="fake"), token=TOKEN, static_dir=ui),
        base_url="http://localhost",
    )
    assert "helm" in c.get("/").text


def test_snapshot_views_and_downsampling(tmp_path: Path) -> None:
    async def run() -> None:
        eng = paper_engine(tmp_path)
        await eng.on_quote(quote(ts=eng.clock.now()))
        eng.portfolio.positions.setdefault(SYM, eng.portfolio.positions.get(SYM))  # type: ignore[arg-type]
        eng.equity_curve.extend((i, Decimal(i)) for i in range(2000))
        snap = EnginePort(eng, venue="fake").snapshot()
        assert len(snap.equity_curve) == 500 and snap.equity_curve[-1] == (1999, Decimal(1999))
        assert snap.feeds == {SYM: "LIVE"} and snap.reconcile_ok is None
        assert {r.name for r in snap.risk} >= {"open orders", "drawdown (% from peak)"}
        assert snap.risk[0].pct == 0
        assert _downsample([(1, Decimal(1))], 5) == ((1, Decimal(1)),)

    asyncio.run(run())


# -------------------------------------------------------------------- Telegram
class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.updates: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            assert req.url.host == "api.telegram.org"
            if req.url.path.endswith("/getUpdates"):
                ups, self.updates = self.updates, []
                return httpx.Response(200, json={"ok": True, "result": ups})
            body = json.loads(req.content)
            self.sent.append((body["chat_id"], body["text"]))
            return httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handle)


def msg(uid: int, chat: int, text: str, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"from": {"id": uid}, "chat": {"id": chat}, "text": text},
    }


class Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))


def test_telegram_unauthorized_never_triggers_anything(tmp_path: Path) -> None:
    async def run() -> None:
        eng = paper_engine(tmp_path)
        tg = FakeTelegram()
        log = Log()
        bot = TelegramBot(
            EnginePort(eng, venue="fake"),
            TelegramClient("123:abc", transport=tg.transport()),
            allowed_user_ids=(USER,),
            allowed_chat_ids=(CHAT,),
            log=log,
            time_fn=lambda: 0.0,
        )
        for text in ("/kill", "/flatten", "/pause", "/resume c0de", "/status"):
            assert await bot.handle(msg(STRANGER, CHAT, text)) is None  # wrong user
            assert await bot.handle(msg(USER, STRANGER, text)) is None  # wrong chat
        assert await bot.handle({"weird": True}) is None
        assert eng.ks.state is EngineState.RUNNING and control_sources(eng) == []
        assert bot.ignored == 11 and log.events[0][0] == "telegram_unauthorized"
        # polling: unauthorized gets no reply at all (silent)
        tg.updates = [msg(STRANGER, CHAT, "/kill", 5)]
        assert await bot.poll_once() == 1 and tg.sent == []
        with pytest.raises(ConfigError):
            TelegramBot(
                EnginePort(eng, venue="fake"),
                TelegramClient("x"),
                allowed_user_ids=(),
                allowed_chat_ids=(CHAT,),
            )

    asyncio.run(run())


def test_telegram_commands_kill_in_one_step_and_rate_limit(tmp_path: Path) -> None:
    async def run() -> None:
        eng = paper_engine(tmp_path)
        tg = FakeTelegram()
        now = [0.0]
        bot = TelegramBot(
            EnginePort(eng, venue="fake"),
            TelegramClient("123:abc", transport=tg.transport()),
            allowed_user_ids=(USER,),
            allowed_chat_ids=(CHAT,),
            rate_per_min=6,
            time_fn=lambda: now[0],
        )
        status = await bot.handle(msg(USER, CHAT, "/status"))
        assert status is not None and status.startswith("[PAPER]") and "RUNNING" in status
        for cmd in ("/positions", "/orders", "/pnl", "/bots@helm_bot"):
            reply = await bot.handle(msg(USER, CHAT, cmd))
            assert reply is not None and reply.startswith("[PAPER]")
        assert await bot.handle(msg(USER, CHAT, "hello")) is None
        tg.updates = [msg(USER, CHAT, "/kill", 7)]
        await bot.poll_once()  # one step: flatten + HALT
        assert eng.ks.state is EngineState.HALTED
        assert tg.sent[-1] == (CHAT, "[PAPER] kill: halted")
        assert await bot.handle(msg(USER, CHAT, "/resume")) is None  # rate limited (6/min)
        assert bot.rate_limited == 1
        now[0] = 61.0
        assert await bot.handle(msg(USER, CHAT, "/resume")) == "usage: /resume <code>"
        assert await bot.handle(msg(USER, CHAT, "/resume nope")) == "[PAPER] resume: denied"
        assert await bot.handle(msg(USER, CHAT, "/resume c0de")) == "[PAPER] resume: running"
        assert await bot.handle(msg(USER, CHAT, "/pause scripted")) == "[PAPER] pause: paused"
        assert await bot.handle(msg(USER, CHAT, "/flatten")) == "[PAPER] flatten: flat"
        assert "/kill" in (await bot.handle(msg(USER, CHAT, "/help")) or "")
        assert set(control_sources(eng)) == {f"telegram:{USER}"}

    asyncio.run(run())


def test_alert_storm_to_telegram_is_bounded_and_halt_carries_resume_code(tmp_path: Path) -> None:
    async def run() -> None:
        eng = paper_engine(tmp_path)
        tg = FakeTelegram()
        client = TelegramClient("123:abc", transport=tg.transport())
        port = EnginePort(eng, venue="fake")
        sink = TelegramAlertSink(client, (CHAT,), resume_code=port.resume_code)
        eng.alerts.sinks.append(sink)
        for _ in range(1000):  # OPS-2 storm: 1000 identical CRITICAL events
            eng.alerts.critical("feed:fake", "feed fake RECONNECTING")
        eng.alerts.info("noise", "info below threshold")
        eng.alerts.info("daily_summary", "daily summary: PnL +0.00")
        await eng.halt("drill")
        await asyncio.gather(*sink.tasks)
        texts = [t for _c, t in tg.sent]
        assert sum("RECONNECTING" in t for t in texts) == 1
        assert not any("info below" in t for t in texts)
        assert any("daily summary" in t for t in texts)
        halt = next(t for t in texts if "HALTED" in t)
        assert "Resume code: c0de" in halt
        sink(Alert(severity=Severity.CRITICAL, key="x", message="no loop", mode=Mode.PAPER, ts=0))

    asyncio.run(run())


# ------------------------------------------------------------ heartbeat + misc
def test_heartbeat_pings_only_while_healthy_and_is_optional() -> None:
    async def run() -> None:
        assert heartbeat_service(None) is None  # OPS-3: disabled gracefully
        pings: list[str] = []

        def handle(req: httpx.Request) -> httpx.Response:
            pings.append(str(req.url))
            return httpx.Response(200 if len(pings) < 3 else 500)

        healthy = [True]
        log = Log()
        svc = heartbeat_service(
            "https://hc.example/ping/abc",
            every_s=0.01,
            healthy=lambda: healthy[0],
            transport=httpx.MockTransport(handle),
            log=log,
        )
        assert svc is not None
        stop = asyncio.Event()
        task = asyncio.create_task(svc(stop))
        while len(pings) < 3:
            await asyncio.sleep(0.005)
        healthy[0] = False
        n = len(pings)
        await asyncio.sleep(0.05)
        assert len(pings) <= n + 1  # silence when unhealthy => out-of-band alert fires
        stop.set()
        await task
        assert ("heartbeat_failed", {"error": "HTTPStatusError"}) in log.events

    asyncio.run(run())


def test_daily_summary_fires_once_per_day(tmp_path: Path) -> None:
    async def run() -> None:
        clock = SimClock(10**18)
        eng = paper_engine(tmp_path, clock)
        stop = asyncio.Event()
        task = asyncio.create_task(daily_summary_service(eng, check_every_s=0.005)(stop))
        await asyncio.sleep(0.03)
        assert not any(a.key == "daily_summary" for a in eng.alerts.sent)
        clock.advance_by(86_400 * 10**9)
        await asyncio.sleep(0.03)
        stop.set()
        await task
        assert sum(a.key == "daily_summary" for a in eng.alerts.sent) == 1

    asyncio.run(run())


def test_resume_code_goes_to_console_not_to_the_log(tmp_path: Path) -> None:
    import io

    async def run() -> None:
        eng = paper_engine(tmp_path)
        out = io.StringIO()
        eng.alerts.sinks.append(console_resume_code_sink(lambda: eng.last_resume_code, out))
        ring = LogRing()
        eng.alerts.info("noise", "not a halt")
        await eng.halt("drill")
        assert out.getvalue() == "HALTED. Resume code: c0de\n"
        assert not any("c0de" in json.dumps(r) for r in ring.records)

    asyncio.run(run())


def test_control_plane_wiring(tmp_path: Path) -> None:
    eng = paper_engine(tmp_path)

    class Store:
        def get(self, name: str) -> str:
            assert name == "telegram_bot_token"
            return "123:abc"

    cfg = HelmConfig(
        control=ControlConfig(telegram_enabled=True),
        security=SecurityConfig(
            telegram_allowed_user_ids=(USER,), telegram_allowed_chat_ids=(CHAT,)
        ),
    )
    cp = build_control_plane(eng, cfg, Store(), venue="fake", ring=None, log=Log())
    assert cp.login_url is not None and cp.login_url.startswith("http://127.0.0.1:8765/#token=")
    assert len(cp.services) == 3 and len(cp.sinks) == 2  # api, telegram, daily summary
    with pytest.raises(RuntimeError, match="secrets"):
        build_control_plane(eng, cfg, None, venue="fake", ring=None, log=None)
    bare = build_control_plane(
        eng, HelmConfig(), None, venue="fake", ring=None, log=None, api=False
    )
    assert bare.login_url is None and len(bare.services) == 1
