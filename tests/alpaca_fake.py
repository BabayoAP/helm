"""In-memory fake of the Alpaca Trading API + trade-updates stream (TEST-1 style).

Served through `httpx.MockTransport`, so adapter tests never touch the network.
Any request to the real-money host fails the test loudly.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
T0 = "2026-11-24T15:00:00.000000000Z"


def _d(x: Decimal) -> str:
    return format(x.normalize(), "f") if x != x.to_integral_value() else str(int(x))


@dataclass
class FakeAlpaca:
    account: dict[str, Any] = field(
        default_factory=lambda: {
            "account_number": "PA1234567",
            "status": "ACTIVE",
            "equity": "100000",
            "cash": "100000",
            "buying_power": "200000",
            "pattern_day_trader": False,
            "daytrade_count": 0,
            "trading_blocked": False,
            "account_blocked": False,
            "shorting_enabled": True,
        }
    )
    market_open: bool = True
    orders: dict[str, dict[str, Any]] = field(default_factory=lambda: {})
    positions: dict[str, dict[str, Decimal]] = field(default_factory=lambda: {})
    activities: list[dict[str, Any]] = field(default_factory=lambda: [])
    fail_next: list[object] = field(default_factory=lambda: [])  # status code or "timeout"
    requests: list[tuple[str, str]] = field(default_factory=lambda: [])
    page_size_cap: int = 100
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    # ------------------------------------------------------------- transport
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host != LIVE_HOST, "a test reached the REAL-MONEY Alpaca host"
        assert req.url.host == PAPER_HOST, req.url.host
        assert req.headers.get("APCA-API-KEY-ID"), "missing key header"
        self.requests.append((req.method, req.url.path))
        if self.fail_next:
            f = self.fail_next.pop(0)
            if f == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=req)
            assert isinstance(f, int)
            return httpx.Response(f, json={"message": f"simulated {f}"})
        return self.route(req)

    def route(self, req: httpx.Request) -> httpx.Response:
        path, m, q = req.url.path, req.method, req.url.params
        if path == "/v2/account":
            return httpx.Response(200, json=self.account)
        if path == "/v2/clock":
            return httpx.Response(200, json={"is_open": self.market_open, "timestamp": T0})
        if path.startswith("/v2/assets/"):
            sym = path.removeprefix("/v2/assets/")
            if sym == "NOPE":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"symbol": sym, "tradable": True})
        if path == "/v2/orders" and m == "POST":
            return self._submit(json.loads(req.content))
        if path == "/v2/orders" and m == "GET":
            open_ = [o for o in self.orders.values() if o["status"] in ("new", "partially_filled")]
            return httpx.Response(200, json=open_)
        if path == "/v2/orders:by_client_order_id":
            o = self._by_coid(q["client_order_id"])
            return httpx.Response(200, json=o) if o else httpx.Response(404, json={})
        if path.startswith("/v2/orders/") and m == "DELETE":
            o = self.orders[path.removeprefix("/v2/orders/")]
            if o["status"] not in ("new", "partially_filled"):
                return httpx.Response(422, json={"message": "order is not cancelable"})
            o["status"] = "canceled"
            return httpx.Response(204)
        if path == "/v2/positions" and m == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": s,
                        "qty": _d(p["qty"]),
                        "avg_entry_price": _d(p["avg"]),
                        "asset_class": "crypto"
                        if s.endswith("USD") and len(s) > 5
                        else "us_equity",
                        "unrealized_pl": "0",
                    }
                    for s, p in self.positions.items()
                    if p["qty"] != 0
                ],
            )
        if path == "/v2/positions" and m == "DELETE":
            for o in self.orders.values():
                if o["status"] in ("new", "partially_filled"):
                    o["status"] = "canceled"
            self.positions.clear()
            return httpx.Response(207, json=[])
        if path == "/v2/account/activities/FILL":
            rows = self.activities
            if "page_token" in q:
                idx = next(i for i, a in enumerate(rows) if a["id"] == q["page_token"])
                rows = rows[idx + 1 :]
            size = min(int(q.get("page_size", 100)), self.page_size_cap)
            return httpx.Response(200, json=rows[:size])
        return httpx.Response(404, json={"message": f"no route {m} {path}"})

    # ---------------------------------------------------------------- state
    def _by_coid(self, coid: str) -> dict[str, Any] | None:
        return next((o for o in self.orders.values() if o["client_order_id"] == coid), None)

    def _submit(self, body: dict[str, Any]) -> httpx.Response:
        if self._by_coid(body["client_order_id"]) is not None:
            return httpx.Response(422, json={"message": "client_order_id must be unique"})
        oid = f"oid-{next(self._ids)}"
        o = {
            "id": oid,
            "client_order_id": body["client_order_id"],
            "symbol": body["symbol"],
            "asset_class": "crypto" if "/" in body["symbol"] else "us_equity",
            "qty": body["qty"],
            "filled_qty": "0",
            "filled_avg_price": None,
            "side": body["side"],
            "type": body["type"],
            "time_in_force": body["time_in_force"],
            "limit_price": body.get("limit_price"),
            "stop_price": body.get("stop_price"),
            "status": "new",
            "created_at": T0,
            "updated_at": T0,
        }
        self.orders[oid] = o
        return httpx.Response(200, json=o)

    def fill(self, coid: str, qty: str, price: str) -> dict[str, Any]:
        """Execute part of an order. Returns the trade_updates stream message."""
        o = self._by_coid(coid)
        assert o is not None
        q, px = Decimal(qty), Decimal(price)
        cum = Decimal(o["filled_qty"]) + q
        o["filled_qty"] = _d(cum)
        o["filled_avg_price"] = price
        o["status"] = "filled" if cum >= Decimal(o["qty"]) else "partially_filled"
        sym = o["symbol"].replace("/", "")
        pos = self.positions.setdefault(sym, {"qty": Decimal(0), "avg": Decimal(0)})
        signed = q if o["side"] == "buy" else -q
        new_qty = pos["qty"] + signed
        if pos["qty"] == 0 or (pos["qty"] > 0) == (signed > 0):
            pos["avg"] = (pos["avg"] * abs(pos["qty"]) + px * q) / abs(new_qty)
        pos["qty"] = new_qty
        exec_id = f"exec-{next(self._ids)}"
        self.activities.append(
            {
                "id": f"20261124150000000::{exec_id}",
                "activity_type": "FILL",
                "transaction_time": T0,
                "type": "fill" if o["status"] == "filled" else "partial_fill",
                "price": price,
                "qty": qty,
                "side": o["side"],
                "symbol": o["symbol"],
                "order_id": o["id"],
                "cum_qty": o["filled_qty"],
                "order_status": o["status"],
            }
        )
        return {
            "stream": "trade_updates",
            "data": {
                "event": "fill" if o["status"] == "filled" else "partial_fill",
                "execution_id": exec_id,  # differs from the activity id on purpose
                "price": price,
                "qty": qty,
                "timestamp": T0,
                "order": dict(o),
            },
        }

    def event(self, coid: str, event: str) -> dict[str, Any]:
        o = self._by_coid(coid)
        assert o is not None
        if event in ("canceled", "expired", "rejected"):
            o["status"] = event
        return {
            "stream": "trade_updates",
            "data": {"event": event, "timestamp": T0, "order": dict(o)},
        }


class FakeWs:
    """WsTransport fake. Push str messages, or None to drop the connection."""

    def __init__(self, authorized: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        status = "authorized" if authorized else "unauthorized"
        self.inbox.put_nowait(
            json.dumps({"stream": "authorization", "data": {"status": status, "action": "auth"}})
        )

    async def send(self, msg: str) -> None:
        self.sent.append(json.loads(msg))

    async def recv(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            raise ConnectionError("dropped")
        return msg

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    def push(self, msg: dict[str, Any] | None) -> None:
        self.inbox.put_nowait(json.dumps(msg) if msg is not None else None)
