"""Alpaca trading adapter (decisions 0023, 0025).

REST via httpx (injectable transport for contract tests), trade-updates stream
via an injected websocket transport. Only `helm.app.gates.build_broker` can
construct it (EXE-1). The base URL comes from `environment` alone, never from a
free-form URL, so a paper config can never point at the real-money endpoint.

Never place orders against `environment="live"` from dev or tests.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from helm.brokers.live.base import LiveBrokerBase
from helm.core.clock import Clock
from helm.core.enums import (
    AssetClass,
    Liquidity,
    OrderEventKind,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
)
from helm.core.errors import (
    AmbiguousSubmitError,
    BrokerError,
    DisconnectedError,
    LiveGateError,
    RateLimitedError,
)
from helm.core.events import OrderEvent
from helm.core.models import Balance, Fill, Order, OrderIntent, Position
from helm.data.connectors.alpaca_common import (
    VENUE,
    AlpacaCredentials,
    from_venue,
    is_crypto,
    iso,
    norm_qty,
    parse_ts,
    to_venue,
    trading_url,
)
from helm.data.ws_manager import WsTransport
from helm.infra.ratelimit import Priority, RateGovernor

_TYPE = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "stop",
    OrderType.STOP_LIMIT: "stop_limit",
}
_TYPE_BACK = {v: k for k, v in _TYPE.items()}
_TIF = {
    TimeInForce.GTC: "gtc",
    TimeInForce.DAY: "day",
    TimeInForce.IOC: "ioc",
    TimeInForce.FOK: "fok",
}
_TIF_BACK = {v: k for k, v in _TIF.items()}
_OPEN_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "pending_cancel",
    "pending_replace",
    "stopped",
    "calculated",
    "held",
    "suspended",
    "done_for_day",
}
_EVENT_KIND = {
    "new": (OrderEventKind.ACCEPTED, OrderState.ACCEPTED),
    "accepted": (OrderEventKind.ACCEPTED, OrderState.ACCEPTED),
    "pending_new": (OrderEventKind.ACCEPTED, OrderState.ACCEPTED),
    "canceled": (OrderEventKind.CANCELED, OrderState.CANCELED),
    "expired": (OrderEventKind.EXPIRED, OrderState.EXPIRED),
    "rejected": (OrderEventKind.REJECTED, OrderState.REJECTED),
    "replaced": (OrderEventKind.CANCELED, OrderState.CANCELED),
}
NS = 1_000_000_000
WsConnect = Callable[[str], Awaitable[WsTransport]]


def _governor() -> RateGovernor:
    # Alpaca: 200 req/min per account; stay well under it (DATA-6)
    return RateGovernor.for_window(limit=180, window_s=60.0, name="alpaca")


@dataclass(frozen=True)
class AccountInfo:
    account_number: str
    status: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    pattern_day_trader: bool
    daytrade_count: int
    trading_blocked: bool
    account_blocked: bool
    shorting_enabled: bool


def strategy_of(client_order_id: str) -> str:
    """`{run_id}-{strategy}-{seq}` -> strategy (strategy ids may contain dashes)."""
    parts = client_order_id.split("-")
    if len(parts) >= 3 and parts[-1].isdigit():
        return "-".join(parts[1:-1])
    return "external"


class AlpacaBroker(LiveBrokerBase):
    def __init__(
        self,
        *,
        account_id: str,
        environment: str,
        credentials: AlpacaCredentials,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: WsConnect | None = None,
        governor: RateGovernor | None = None,
        timeout_s: float = 10.0,
        _gate_token: object,
    ) -> None:
        super().__init__(account_id=account_id, venue=VENUE, _gate_token=_gate_token)
        self.environment = environment
        self.base_url = trading_url(environment)
        self._creds = credentials
        self.clock = clock
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            headers=credentials.headers(),
            timeout=timeout_s,
        )
        self._ws_connect = ws_connect
        self.governor = governor or _governor()
        self._clock_cache: tuple[float, bool] | None = None
        self.reconnects = 0
        self.on_reconnect: Callable[[], Awaitable[None]] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ http
    async def _req(
        self,
        method: str,
        path: str,
        *,
        priority: Priority = Priority.ACCOUNT,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        ambiguous: bool = False,
        allow_404: bool = False,
    ) -> Any:
        async def do() -> httpx.Response:
            return await self._http.request(method, path, params=params, json=body)

        try:
            resp = await self.governor.call(priority, do)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if ambiguous:
                raise AmbiguousSubmitError(f"alpaca {method} {path}: {exc!r}") from exc
            raise DisconnectedError(f"alpaca {method} {path}: {exc!r}") from exc
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after")
            secs = float(retry) if retry else None
            self.governor.penalize(secs)
            raise RateLimitedError("alpaca 429 too many requests", retry_after_s=secs)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 500:
            msg = f"alpaca {method} {path}: HTTP {resp.status_code}"
            raise AmbiguousSubmitError(msg) if ambiguous else BrokerError(msg)
        if resp.status_code >= 400:
            raise BrokerError(f"alpaca {method} {path}: HTTP {resp.status_code} {_msg(resp)}")
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def close(self) -> None:
        self._stop.set()
        await self._http.aclose()

    # -------------------------------------------------------------- preflight
    async def account(self) -> AccountInfo:
        d = await self._req("GET", "/v2/account")
        return AccountInfo(
            account_number=str(d.get("account_number", "")),
            status=str(d.get("status", "")),
            equity=Decimal(str(d.get("equity", "0"))),
            cash=Decimal(str(d.get("cash", "0"))),
            buying_power=Decimal(str(d.get("buying_power", "0"))),
            pattern_day_trader=bool(d.get("pattern_day_trader", False)),
            daytrade_count=int(d.get("daytrade_count", 0) or 0),
            trading_blocked=bool(d.get("trading_blocked", False)),
            account_blocked=bool(d.get("account_blocked", False)),
            shorting_enabled=bool(d.get("shorting_enabled", False)),
        )

    async def preflight(self, *, attest_no_withdrawal: bool) -> tuple[AccountInfo, list[str]]:
        """SEC-1 / decision 0025 startup checks. Raises on anything unsafe; returns warnings."""
        if self.environment == "live" and not attest_no_withdrawal:
            raise LiveGateError(
                "real-money Alpaca needs security.attest_no_withdrawal = true (SEC-1)"
            )
        acct = await self.account()
        if acct.status != "ACTIVE":
            raise LiveGateError(f"alpaca account status is {acct.status!r}, not ACTIVE")
        if acct.trading_blocked or acct.account_blocked:
            raise LiveGateError("alpaca account is trading_blocked/account_blocked")
        warnings: list[str] = []
        looks_paper = acct.account_number.startswith("PA")
        if (self.environment == "paper") != looks_paper:
            warnings.append(
                f"account number prefix does not look like a {self.environment} account"
            )
        if acct.pattern_day_trader:
            warnings.append("account is flagged pattern_day_trader (SEC-9)")
        if acct.equity < 25000 and acct.daytrade_count >= 3:
            warnings.append(
                f"{acct.daytrade_count} day trades in 5 days with equity < $25k: "
                "a 4th may trigger PDT restrictions (SEC-9)"
            )
        return acct, warnings

    async def asset(self, symbol: str) -> dict[str, Any] | None:
        """`/v2/assets/{symbol}` metadata (DATA-8), or None if Alpaca does not know it."""
        return await self._req("GET", f"/v2/assets/{to_venue(symbol)}", allow_404=True)

    async def market_open(self) -> bool:
        """Venue clock (knows holidays), cached for 30 s (EXE-9)."""
        now = time.monotonic()
        if self._clock_cache is not None and now - self._clock_cache[0] < 30:
            return self._clock_cache[1]
        d = await self._req("GET", "/v2/clock")
        is_open = bool(d.get("is_open", False))
        self._clock_cache = (now, is_open)
        return is_open

    # ----------------------------------------------------------------- orders
    async def submit(self, order: Order) -> None:
        it = order.intent
        if it.tif is TimeInForce.POST_ONLY:
            raise BrokerError("alpaca has no post-only orders (decision 0025)")
        if not is_crypto(it.symbol) and not await self.market_open():
            raise BrokerError(f"market closed for {it.symbol} (EXE-9)")
        if it.reduce_only:
            await self._check_reduce_only(it)
        body: dict[str, Any] = {
            "symbol": to_venue(it.symbol),
            "qty": norm_qty(it.qty),
            "side": it.side.value,
            "type": _TYPE[it.type],
            "time_in_force": _TIF[it.tif],
            "client_order_id": order.client_order_id,
        }
        if it.limit_price is not None:
            body["limit_price"] = str(it.limit_price)
        if it.stop_price is not None:
            body["stop_price"] = str(it.stop_price)
        try:
            await self._req(
                "POST", "/v2/orders", priority=Priority.ORDER, body=body, ambiguous=True
            )
        except BrokerError as exc:
            if isinstance(exc, (AmbiguousSubmitError, RateLimitedError)):
                raise
            if "client_order_id must be unique" in str(exc).lower():
                return  # it already reached the venue: idempotent resubmit (I4)
            raise

    async def _check_reduce_only(self, it: OrderIntent) -> None:
        held = Decimal(0)
        for p in await self.positions():
            if p.symbol == it.symbol:
                held = p.qty
        if held == 0 or (held > 0) == (it.side is Side.BUY) or it.qty > abs(held):
            raise BrokerError(
                f"reduce-only {it.side.value} {it.qty} would open/flip position {held} "
                "(alpaca has no reduce_only flag; emulated, decision 0025)"
            )

    async def _venue_order(self, client_order_id: str) -> dict[str, Any] | None:
        return await self._req(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id},
            allow_404=True,
        )

    async def get_order(self, client_order_id: str) -> Order | None:
        d = await self._venue_order(client_order_id)
        return None if d is None else self.order_from(d)

    async def cancel(self, client_order_id: str) -> None:
        d = await self._venue_order(client_order_id)
        if d is None:
            raise BrokerError(f"cancel: {client_order_id} unknown to alpaca")
        await self._req("DELETE", f"/v2/orders/{d['id']}", priority=Priority.CANCEL)

    async def open_orders(self) -> list[Order]:
        rows = await self._req("GET", "/v2/orders", params={"status": "open", "limit": 500})
        return [self.order_from(d) for d in rows or []]

    async def fills_for_order(self, client_order_id: str) -> list[Fill]:
        d = await self._venue_order(client_order_id)
        if d is None:
            return []
        venue_id = str(d["id"])
        params: dict[str, Any] = {"direction": "asc", "page_size": 100}
        if d.get("created_at"):
            # activities `after` is exclusive; back off 1 s for clock granularity
            params["after"] = iso(parse_ts(str(d["created_at"])) - NS)
        fills: list[Fill] = []
        while True:
            rows = await self._req("GET", "/v2/account/activities/FILL", params=params)
            if not rows:
                return fills
            for a in rows:
                if str(a.get("order_id")) == venue_id:
                    fills.append(self._fill_from_activity(a, client_order_id))
            # page until empty: a short page is not proof of the end (venue caps vary)
            params["page_token"] = rows[-1]["id"]

    async def positions(self) -> list[Position]:
        rows = await self._req("GET", "/v2/positions")
        out: list[Position] = []
        for p in rows or []:
            out.append(
                Position(
                    symbol=from_venue(str(p["symbol"]), str(p.get("asset_class", "us_equity"))),
                    qty=Decimal(str(p["qty"])),
                    avg_px=Decimal(str(p.get("avg_entry_price") or "0")),
                    unrealized_pnl=Decimal(str(p.get("unrealized_pl") or "0")),
                )
            )
        return out

    async def balances(self) -> list[Balance]:
        acct = await self.account()
        return [Balance(asset="USD", free=acct.cash)]

    async def flatten_all(self, *, reduce_only: bool = True) -> None:
        await self._req(
            "DELETE", "/v2/positions", priority=Priority.CANCEL, params={"cancel_orders": "true"}
        )

    # ---------------------------------------------------------------- parsing
    def order_from(self, d: dict[str, Any]) -> Order:
        coid = str(d.get("client_order_id") or d["id"])
        status = str(d.get("status", ""))
        filled = Decimal(str(d.get("filled_qty") or "0"))
        if status == "filled":
            state = OrderState.FILLED
        elif status == "partially_filled" or (status in _OPEN_STATUSES and filled > 0):
            state = OrderState.PARTIALLY_FILLED
        elif status in _OPEN_STATUSES:
            state = OrderState.ACCEPTED
        elif status in ("canceled", "replaced"):
            state = OrderState.CANCELED
        elif status == "expired":
            state = OrderState.EXPIRED
        elif status == "rejected":
            state = OrderState.REJECTED
        else:
            state = OrderState.UNKNOWN
        typ = _TYPE_BACK.get(
            str(d.get("type") or d.get("order_type") or "market"), OrderType.MARKET
        )
        limit = d.get("limit_price")
        stop = d.get("stop_price")
        if typ in (OrderType.LIMIT, OrderType.STOP_LIMIT) and limit is None:
            typ = OrderType.MARKET  # e.g. trailing stops: not ours; keep it parseable
        if typ is OrderType.STOP and stop is None:
            typ = OrderType.MARKET
        qty = Decimal(str(d.get("qty") or d.get("filled_qty") or "0"))
        intent = OrderIntent(
            strategy_id=strategy_of(coid),
            symbol=from_venue(str(d["symbol"]), str(d.get("asset_class", "us_equity"))),
            side=Side(str(d.get("side", "buy"))),
            type=typ,
            qty=qty if qty > 0 else Decimal(1),
            limit_price=Decimal(str(limit)) if limit is not None else None,
            stop_price=Decimal(str(stop)) if stop is not None else None,
            tif=_TIF_BACK.get(str(d.get("time_in_force", "day")), TimeInForce.DAY),
        )
        created = parse_ts(str(d["created_at"])) if d.get("created_at") else self.clock.now()
        updated = parse_ts(str(d["updated_at"])) if d.get("updated_at") else created
        avg = d.get("filled_avg_price")
        return Order(
            client_order_id=coid,
            account_id=self.account_id,
            intent=intent,
            state=state,
            venue_order_id=str(d["id"]),
            filled_qty=filled,
            avg_px=Decimal(str(avg)) if avg else Decimal(0),
            ts_created=created,
            ts_updated=updated,
        )

    def _fill(
        self,
        *,
        coid: str,
        venue_order_id: str,
        cum_qty: object,
        symbol: str,
        asset_class: str,
        side: str,
        price: object,
        qty: object,
        ts: int,
    ) -> Fill:
        return Fill(
            client_order_id=coid,
            venue_order_id=venue_order_id,
            venue=VENUE,
            trade_id=f"{venue_order_id}:{norm_qty(cum_qty)}",  # decision 0025
            symbol=from_venue(symbol, asset_class),
            side=Side(side),
            price=Decimal(str(price)),
            qty=Decimal(str(qty)),
            fee=Decimal(0),
            fee_ccy="USD",
            liquidity=Liquidity.TAKER,
            ts=ts,
        )

    def _fill_from_activity(self, a: dict[str, Any], coid: str) -> Fill:
        return self._fill(
            coid=coid,
            venue_order_id=str(a["order_id"]),
            cum_qty=a["cum_qty"],
            symbol=str(a["symbol"]),
            asset_class="crypto" if "/" in str(a["symbol"]) else "us_equity",
            side=str(a["side"]),
            price=a["price"],
            qty=a["qty"],
            ts=parse_ts(str(a["transaction_time"])),
        )

    def parse_trade_update(self, data: dict[str, Any]) -> list[OrderEvent | Fill]:
        event = str(data.get("event", ""))
        od: dict[str, Any] = data.get("order") or {}
        if not od:
            return []
        order = self.order_from(od)
        ts = parse_ts(str(data["timestamp"])) if data.get("timestamp") else self.clock.now()
        if event in ("fill", "partial_fill"):
            return [
                self._fill(
                    coid=order.client_order_id,
                    venue_order_id=str(od["id"]),
                    cum_qty=od.get("filled_qty") or data["qty"],
                    symbol=str(od["symbol"]),
                    asset_class=str(od.get("asset_class", "us_equity")),
                    side=str(od.get("side", "buy")),
                    price=data["price"],
                    qty=data["qty"],
                    ts=ts,
                )
            ]
        mapped = _EVENT_KIND.get(event)
        if mapped is None:
            return []  # pending_cancel, done_for_day, calculated...: no state change for us
        kind, state = mapped
        return [
            OrderEvent(
                kind=kind,
                client_order_id=order.client_order_id,
                state=state,
                order=order.model_copy(update={"state": state}),
                reason=str(od.get("status", event)),
                ts=ts,
            )
        ]

    # ----------------------------------------------------------------- stream
    def stream_url(self) -> str:
        return self.base_url.replace("https://", "wss://") + "/stream"

    async def _open_stream(self) -> WsTransport:
        if self._ws_connect is None:
            raise DisconnectedError("no websocket connector configured")
        ws = await self._ws_connect(self.stream_url())
        await ws.send(
            json.dumps(
                {"action": "auth", "key": self._creds.key_id, "secret": self._creds.secret_key}
            )
        )
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("stream") == "authorization":
                if (msg.get("data") or {}).get("status") != "authorized":
                    await ws.close()
                    raise BrokerError("alpaca stream authorization failed")
                break
        await ws.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
        return ws

    async def stream(self) -> AsyncIterator[OrderEvent | Fill]:
        """Trade updates with reconnect + jittered backoff. After every reconnect
        `on_reconnect` runs so the caller can resync missed fills (EXE-4)."""
        attempt = 0
        rng = random.Random()
        first = True
        while not self._stop.is_set():
            try:
                ws = await self._open_stream()
            except (ConnectionError, OSError, TimeoutError, DisconnectedError):
                attempt += 1
                await asyncio.sleep(min(60.0, 2**attempt) * (0.5 + rng.random()))
                continue
            if not first:
                self.reconnects += 1
                if self.on_reconnect is not None:
                    await self.on_reconnect()
            first, attempt = False, 0
            try:
                while not self._stop.is_set():
                    msg = json.loads(await ws.recv())
                    if msg.get("stream") != "trade_updates":
                        continue
                    for ev in self.parse_trade_update(msg.get("data") or {}):
                        yield ev
            except (ConnectionError, OSError, TimeoutError):
                attempt += 1
            finally:
                await ws.close()


def _msg(resp: httpx.Response) -> str:
    try:
        d = resp.json()
    except ValueError:
        return resp.text[:200]
    return str(d.get("message", d))[:200] if isinstance(d, dict) else str(d)[:200]


def asset_class_of(symbol: str) -> AssetClass:
    return AssetClass.CRYPTO if is_crypto(symbol) else AssetClass.EQUITY
