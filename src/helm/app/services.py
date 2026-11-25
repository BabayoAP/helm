"""Long-running services shared by `helm paper` and `helm live`.

- Alpaca live feed through WsManager (DATA-5) as an async iterator.
- Dead-man's switch heartbeat (OPS-3): disabled gracefully when no URL is set.
- Daily PnL summary alert (OPS-2).
- Control plane: FastAPI on localhost + Telegram bot + alert sink (OPS-1, UI-8).

Every service is `async fn(stop: asyncio.Event)`, run by the engine runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets as pysecrets
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from helm.app.control import EnginePort
from helm.app.engine import Engine
from helm.core.config import BotConfig, HelmConfig
from helm.core.enums import Severity
from helm.core.events import Alert
from helm.data.connectors.alpaca import AlpacaFeedHandler, AlpacaOhlcvClient, stream_url
from helm.data.connectors.alpaca_common import AlpacaCredentials, is_crypto
from helm.data.ws_manager import WsManager
from helm.infra.logging import LogRing
from helm.infra.secrets import SecretStore
from helm.infra.ws import connect_ws
from helm.risk.breakers import day_start_ns

Service = Callable[[asyncio.Event], Coroutine[Any, Any, None]]


async def _sleep(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep unless stopped. Returns True if stop was requested."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()


# ------------------------------------------------------------------ credentials
def alpaca_credentials(store: SecretStore, name: str) -> AlpacaCredentials:
    return AlpacaCredentials(
        key_id=store.get(f"{name}_key_id"), secret_key=store.get(f"{name}_secret_key")
    )


# ------------------------------------------------------------------------- feed
@dataclass
class QueueFeed:
    """Adapts WsManager's push callback into the async iterator the runner consumes."""

    manager: WsManager | None = None
    queue: asyncio.Queue[BaseModel] = field(default_factory=lambda: asyncio.Queue(maxsize=100_000))

    async def emit(self, ev: BaseModel) -> None:
        await self.queue.put(ev)

    async def __aiter__(self) -> AsyncIterator[BaseModel]:
        assert self.manager is not None
        task = asyncio.create_task(self.manager.run())
        try:
            while True:
                get = asyncio.create_task(self.queue.get())
                done, _ = await asyncio.wait({get, task}, return_when=asyncio.FIRST_COMPLETED)
                if get in done:
                    yield get.result()
                    continue
                get.cancel()
                task.result()  # manager ended: surface its exception, if any
                return
        finally:
            self.manager.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def alpaca_feed(
    bot: BotConfig, creds: AlpacaCredentials, *, data_feed: str, now_fn: Callable[[], int]
) -> AsyncIterator[BaseModel]:
    crypto = is_crypto(bot.symbols[0])
    rest = AlpacaOhlcvClient(creds, crypto=crypto, feed=data_feed)
    handler = AlpacaFeedHandler(
        creds, bot.symbols, timeframe=bot.timeframe, rest=rest, quotes=True, now_fn=now_fn
    )
    url = stream_url(crypto=crypto, feed=data_feed)
    qf = QueueFeed()
    qf.manager = WsManager("alpaca", lambda: connect_ws(url), handler, qf.emit, time_fn=now_fn)
    return qf.__aiter__()


# -------------------------------------------------------------------- heartbeat
def heartbeat_service(
    url: str | None,
    *,
    every_s: float = 60.0,
    healthy: Callable[[], bool] = lambda: True,
    transport: httpx.AsyncBaseTransport | None = None,
    log: Any = None,
) -> Service | None:
    """OPS-3: ping `url` while the engine is healthy. None (disabled) when unset."""
    if not url:
        return None

    async def run(stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(transport=transport, timeout=10) as http:
            while True:
                if healthy():
                    try:
                        (await http.get(url)).raise_for_status()
                    except httpx.HTTPError as exc:
                        if log is not None:
                            log.warning("heartbeat_failed", error=type(exc).__name__)
                if await _sleep(stop, every_s):
                    return

    return run


# ---------------------------------------------------------------- daily summary
def daily_summary_service(engine: Engine, *, check_every_s: float = 60.0) -> Service:
    """OPS-2: one summary alert per trading day, at the configured daily reset."""

    async def run(stop: asyncio.Event) -> None:
        reset = engine.risk_config.daily_reset
        day = day_start_ns(engine.clock.now(), reset)
        start_equity = engine.portfolio.equity()
        while not await _sleep(stop, check_every_s):
            now = engine.clock.now()
            today = day_start_ns(now, reset)
            if today == day:
                continue
            eq = engine.portfolio.equity()
            engine.alerts.info(
                "daily_summary",
                f"daily summary: PnL {eq - start_equity:+.2f}, equity {eq:.2f}, "
                f"fills {engine.stats.fills}, rejects {engine.stats.rejected}, "
                f"state {engine.ks.state.value}",
            )
            day, start_equity = today, eq

    return run


# ---------------------------------------------------------------- control plane
@dataclass
class ControlPlane:
    port: EnginePort
    services: list[Service]
    login_url: str | None
    sinks: list[Callable[[Alert], None]]


def _api_service(app: Any, host: str, port: int) -> Service:
    import uvicorn  # noqa: PLC0415

    async def run(stop: asyncio.Event) -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
        )
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        task = asyncio.create_task(server.serve())
        await stop.wait()
        server.should_exit = True
        await task

    return run


def build_control_plane(
    engine: Engine,
    cfg: HelmConfig,
    store: SecretStore | None,
    *,
    venue: str,
    ring: LogRing | None,
    log: Any,
    ui_dir: Path | None = None,
    api: bool = True,
) -> ControlPlane:
    from helm.api.app import create_app  # noqa: PLC0415
    from helm.telegram.bot import TelegramAlertSink, TelegramBot, TelegramClient  # noqa: PLC0415

    port = EnginePort(engine, venue=venue, ring=ring)
    services: list[Service] = []
    sinks: list[Callable[[Alert], None]] = []
    login_url: str | None = None
    cc = cfg.control
    if api:
        token = pysecrets.token_urlsafe(24)
        from helm.infra.logging import SECRETS  # noqa: PLC0415

        SECRETS.register(token)
        hosts = ("127.0.0.1", "localhost", cc.api_host)
        app = create_app(port, token=token, allowed_hosts=hosts, static_dir=ui_dir)
        services.append(_api_service(app, cc.api_host, cc.api_port))
        login_url = f"http://{cc.api_host}:{cc.api_port}/#token={token}"
    if cc.telegram_enabled:
        if store is None:
            raise RuntimeError("telegram enabled but no secrets store")
        client = TelegramClient(store.get("telegram_bot_token"))
        sec = cfg.security
        bot = TelegramBot(
            port,
            client,
            allowed_user_ids=sec.telegram_allowed_user_ids,
            allowed_chat_ids=sec.telegram_allowed_chat_ids,
            rate_per_min=cc.telegram_rate_per_min,
            log=log,
        )
        services.append(bot.run)
        sink = TelegramAlertSink(
            client,
            sec.telegram_allowed_chat_ids,
            resume_code=port.resume_code,
            min_severity=Severity.WARN,
        )
        sinks.append(sink)
    hb = heartbeat_service(
        cfg.engine.heartbeat_url,
        every_s=cc.heartbeat_every_s,
        healthy=lambda: engine.flatten_failures == 0,
        log=log,
    )
    if hb is not None:
        services.append(hb)
    services.append(daily_summary_service(engine))
    return ControlPlane(port=port, services=services, login_url=login_url, sinks=sinks)
