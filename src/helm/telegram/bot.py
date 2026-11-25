"""Telegram control bot and alert sink (OPS-1, OPS-2, SEC-5).

- A command runs only when BOTH the sender's user id and the chat id are
  allowlisted. Anyone else is ignored silently (no reply) and logged.
- Commands are rate-limited per user. `/kill` runs in one step (flatten + HALT).
- The bot refuses to start with an empty allowlist (fail-closed).
- Depends only on `helm.core.control.ControlPort` (decision 0026).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from helm.core.control import ControlPort, EngineSnapshot
from helm.core.enums import ControlAction, Severity
from helm.core.errors import ConfigError
from helm.core.events import Alert, ControlCommand

HELP = (
    "/status /positions /pnl /orders /bots\n"
    "/pause [bot] /resume <code> /flatten /kill (immediate: flatten + HALT)"
)


class TelegramClient:
    """Minimal Bot API client. The token sits only in the base URL, in memory."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}", transport=transport, timeout=40
        )

    async def get_updates(self, offset: int | None, timeout_s: int = 25) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        resp = await self._http.get("/getUpdates", params=params)
        resp.raise_for_status()
        result: list[dict[str, Any]] = resp.json().get("result", [])
        return result

    async def send(self, chat_id: int, text: str) -> None:
        resp = await self._http.post("/sendMessage", json={"chat_id": chat_id, "text": text[:4000]})
        resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()


def _fmt_status(s: EngineSnapshot) -> str:
    feeds = ", ".join(f"{k}={v}" for k, v in s.feeds.items()) or "none"
    rec = {None: "n/a", True: "ok", False: "MISMATCH"}[s.reconcile_ok]
    return (
        f"{s.mode.badge} {s.venue}/{s.account_id}\n"
        f"state: {s.state.value}\nequity: {s.equity:.2f}  day PnL: {s.daily_pnl:+.2f}\n"
        f"positions: {sum(1 for p in s.positions if p.qty)}  open orders: {len(s.orders)}\n"
        f"feeds: {feeds}\nreconcile: {rec}"
    )


def _fmt_positions(s: EngineSnapshot) -> str:
    rows = [
        f"{p.symbol}: {p.qty} @ {p.avg_px:.4f} uPnL {p.unrealized_pnl:+.2f}"
        for p in s.positions
        if p.qty
    ]
    return f"{s.mode.badge} positions\n" + ("\n".join(rows) or "flat")


def _fmt_orders(s: EngineSnapshot) -> str:
    rows = [
        f"{o.client_order_id} {o.side} {o.qty} {o.symbol} {o.type}"
        + (f" @{o.limit_price}" if o.limit_price is not None else "")
        + f" [{o.state}] filled {o.filled_qty}"
        for o in s.orders
    ]
    return f"{s.mode.badge} open orders\n" + ("\n".join(rows) or "none")


def _fmt_pnl(s: EngineSnapshot) -> str:
    realized = sum((p.realized_pnl for p in s.positions), start=s.equity * 0)
    unreal = sum((p.unrealized_pnl for p in s.positions), start=s.equity * 0)
    return (
        f"{s.mode.badge} PnL\nday: {s.daily_pnl:+.2f}\nrealized: {realized:+.2f}\n"
        f"unrealized: {unreal:+.2f}\nequity: {s.equity:.2f}"
    )


def _fmt_bots(s: EngineSnapshot) -> str:
    rows = [
        f"{b.id} [{b.lifecycle}] {'PAUSED' if b.paused else 'active'} {','.join(b.symbols)}"
        for b in s.bots
    ]
    return f"{s.mode.badge} bots\n" + ("\n".join(rows) or "none")


class TelegramBot:
    def __init__(
        self,
        port: ControlPort,
        client: TelegramClient,
        *,
        allowed_user_ids: tuple[int, ...],
        allowed_chat_ids: tuple[int, ...],
        rate_per_min: int = 20,
        log: Any = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        if not allowed_user_ids or not allowed_chat_ids:
            raise ConfigError(
                "telegram needs security.telegram_allowed_user_ids and _chat_ids (OPS-1)"
            )
        self.port = port
        self.client = client
        self.users = frozenset(allowed_user_ids)
        self.chats = frozenset(allowed_chat_ids)
        self.rate_per_min = rate_per_min
        self.log = log
        self._time = time_fn
        self._hits: dict[int, deque[float]] = {}
        self.ignored = 0
        self.rate_limited = 0
        self._offset: int | None = None

    def _now(self) -> float:
        return self._time() if self._time else asyncio.get_running_loop().time()

    def _log(self, event: str, **kw: Any) -> None:
        if self.log is not None:
            self.log.warning(event, **kw)

    def _allow_rate(self, user: int) -> bool:
        q = self._hits.setdefault(user, deque())
        now = self._now()
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.rate_per_min:
            return False
        q.append(now)
        return True

    async def handle(self, update: dict[str, Any]) -> str | None:
        """Returns the reply text (None = no reply). Never raises on bad input."""
        msg = update.get("message") or {}
        user = (msg.get("from") or {}).get("id")
        chat = (msg.get("chat") or {}).get("id")
        text = str(msg.get("text") or "").strip()
        if user not in self.users or chat not in self.chats:
            self.ignored += 1
            self._log("telegram_unauthorized", user_id=user, chat_id=chat, text=text[:40])
            return None
        if not text.startswith("/"):
            return None
        if not self._allow_rate(int(user)):
            self.rate_limited += 1
            self._log("telegram_rate_limited", user_id=user)
            return None
        parts = text.split()
        name = parts[0][1:].split("@", 1)[0].lower()
        arg = parts[1] if len(parts) > 1 else None
        snap = self.port.snapshot
        views: dict[str, Callable[[EngineSnapshot], str]] = {
            "status": _fmt_status,
            "positions": _fmt_positions,
            "orders": _fmt_orders,
            "pnl": _fmt_pnl,
            "bots": _fmt_bots,
        }
        if name in views:
            return views[name](snap())
        actions = {
            "pause": ControlAction.PAUSE,
            "resume": ControlAction.RESUME,
            "flatten": ControlAction.FLATTEN,
            "kill": ControlAction.KILL,
        }
        if name not in actions:
            return HELP
        action = actions[name]
        if action is ControlAction.RESUME and not arg:
            return "usage: /resume <code>"
        cmd = ControlCommand(
            action=action,
            source=f"telegram:{user}",
            target=arg if action is ControlAction.PAUSE else None,
            code=arg if action is ControlAction.RESUME else None,
            ts=self.port.now(),
        )
        result = await self.port.command(cmd)
        return f"{snap().mode.badge} {name}: {result}"

    async def poll_once(self) -> int:
        updates = await self.client.get_updates(self._offset)
        for u in updates:
            self._offset = int(u["update_id"]) + 1
            reply = await self.handle(u)
            if reply is not None:
                chat = u["message"]["chat"]["id"]
                await self.client.send(chat, reply)
        return len(updates)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                await self.poll_once()
                backoff = 1.0
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                self._log("telegram_poll_error", error=type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 60)


class TelegramAlertSink:
    """AlertManager sink: delivers alerts to the allowlisted chats (OPS-2).

    The one-time resume code is appended to HALT alerts: Telegram allowlisted
    chats are an authorized channel (SEC-5).
    """

    def __init__(
        self,
        client: TelegramClient,
        chat_ids: tuple[int, ...],
        *,
        resume_code: Callable[[], str | None] = lambda: None,
        min_severity: Severity = Severity.WARN,
    ) -> None:
        self.client = client
        self.chats = chat_ids
        self.resume_code = resume_code
        self.min_rank = list(Severity).index(min_severity)
        self.tasks: set[asyncio.Task[None]] = set()
        self.failures = 0

    def __call__(self, alert: Alert) -> None:
        if list(Severity).index(alert.severity) < self.min_rank and alert.key != "daily_summary":
            return
        text = alert.text
        if alert.key == "halt":
            code = self.resume_code()
            if code:
                text += f"\nResume code: {code}  (send /resume {code})"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for chat in self.chats:
            t = loop.create_task(self._send(chat, text))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def _send(self, chat: int, text: str) -> None:
        try:
            await self.client.send(chat, text)
        except httpx.HTTPError:
            self.failures += 1
