"""FastAPI control plane (UI-1/2/6/7/8, SEC-5, OPS-5).

- Binds 127.0.0.1 by default (Tailscale only by explicit config, SEC-3).
- Every `/api/*` and `/ws` call needs the local session token
  (`Authorization: Bearer`, or `?token=` for the websocket).
- Control POSTs also need `X-Helm-CSRF: 1` and, when the browser sends one, a
  same-origin `Origin`. There is no CORS middleware, so a cross-site page cannot
  send the custom header. Host headers are allowlisted (DNS-rebinding guard).
- Control endpoints are rate-limited. Commands are journaled by the engine with
  source "ui".
- `/health` is unauthenticated and minimal (Docker healthcheck).
- No secret ever reaches the browser: snapshots are view models only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from helm.core.control import ControlPort
from helm.core.enums import ControlAction
from helm.core.events import ControlCommand

CSRF_HEADER = "x-helm-csrf"
NS_PER_S = 1_000_000_000


class ControlRequest(BaseModel):
    action: Literal["start", "stop", "pause", "resume", "flatten", "kill"]
    target: str | None = None
    code: str | None = None


class _RateLimit:
    def __init__(self, per_min: int) -> None:
        self.per_min = per_min
        self.times: deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self.times and now - self.times[0] >= 60:
            self.times.popleft()
        if len(self.times) >= self.per_min:
            return False
        self.times.append(now)
        return True


def create_app(
    port: ControlPort,
    *,
    token: str,
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
    static_dir: Path | None = None,
    control_per_min: int = 30,
    ws_hz: float = 10.0,
    time_fn: Callable[[], float] | None = None,
) -> FastAPI:
    if len(token) < 16:
        raise ValueError("session token too short")
    app = FastAPI(title="Helm control", docs_url=None, redoc_url=None, openapi_url=None)
    limiter = _RateLimit(control_per_min)
    clock = time_fn or (lambda: asyncio.get_running_loop().time())

    def host_ok(host: str | None) -> bool:
        return (host or "").rsplit(":", 1)[0].strip("[]") in allowed_hosts

    @app.middleware("http")
    async def _guard_host(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not host_ok(request.headers.get("host")):
            return JSONResponse({"detail": "host not allowed"}, status_code=400)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _token_ok(candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate.encode(), token.encode())

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        cand = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        if not _token_ok(cand):
            raise HTTPException(status_code=401, detail="unauthorized")

    def csrf(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="missing CSRF header")
        origin = request.headers.get("origin")
        if origin is not None:
            host = origin.split("://", 1)[-1]
            if not host_ok(host) or host != request.headers.get("host"):
                raise HTTPException(status_code=403, detail="cross-origin control refused")

    @app.get("/health")
    def health() -> dict[str, Any]:
        snap = port.snapshot()
        return {"ok": True, "state": snap.state.value, "mode": snap.mode.value}

    @app.get("/api/snapshot", dependencies=[Depends(auth)])
    def snapshot() -> Response:
        return Response(port.snapshot().model_dump_json(), media_type="application/json")

    @app.get("/api/chart", dependencies=[Depends(auth)])
    def chart(symbol: str, n: Annotated[int, Query(ge=10, le=5000)] = 500) -> Response:
        view = port.chart(symbol, n)
        if view is None:
            raise HTTPException(status_code=404, detail="no data for symbol")
        return Response(view.model_dump_json(), media_type="application/json")

    @app.get("/api/research", dependencies=[Depends(auth)])
    def research(limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> Response:
        return Response(port.research(limit).model_dump_json(), media_type="application/json")

    @app.get("/api/logs", dependencies=[Depends(auth)])
    def logs(n: Annotated[int, Query(ge=1, le=2000)] = 200) -> list[dict[str, object]]:
        return port.logs(n)

    @app.post("/api/control", dependencies=[Depends(auth), Depends(csrf)])
    async def control(req: ControlRequest) -> dict[str, str]:
        if not limiter.allow(clock()):
            raise HTTPException(status_code=429, detail="too many control commands")
        cmd = ControlCommand(
            action=ControlAction(req.action),
            source="ui",
            target=req.target,
            code=req.code,
            ts=port.now(),
        )
        return {"result": await port.command(cmd)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        ok = host_ok(websocket.headers.get("host")) and _token_ok(
            websocket.query_params.get("token")
        )
        if not ok:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        last = ""
        try:
            while True:
                body = port.snapshot().model_dump_json(exclude={"ts"})
                if body != last:  # UI-7: batched, at most ws_hz updates/s, only on change
                    await websocket.send_text(body)
                    last = body
                await asyncio.sleep(1 / ws_hz)
        except (WebSocketDisconnect, RuntimeError):
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app
