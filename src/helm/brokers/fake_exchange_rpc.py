"""FakeExchange over a local socket (TEST-1/TEST-6).

The venue runs in its own process so an engine can be `kill -9`ed and restarted
against unchanged venue state. JSON-lines RPC over TCP on 127.0.0.1; stdlib
asyncio only. Test infrastructure, never used for real trading.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from helm.brokers.fake_exchange import FakeExchange
from helm.core.errors import AmbiguousSubmitError, BrokerError, DisconnectedError, RateLimitedError
from helm.core.events import EVENT_TYPES, OrderEvent
from helm.core.models import Balance, Fill, Order, Position

_ERRORS: dict[str, type[BrokerError]] = {
    "AmbiguousSubmitError": AmbiguousSubmitError,
    "DisconnectedError": DisconnectedError,
    "RateLimitedError": RateLimitedError,
    "BrokerError": BrokerError,
}


_MODELS: dict[str, type[BaseModel]] = {
    **EVENT_TYPES,
    "Order": Order,
    "Position": Position,
    "Balance": Balance,
}


def _enc(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return {"__kind": type(obj).__name__, "v": obj.model_dump(mode="json")}
    if isinstance(obj, list):
        return [_enc(x) for x in obj]  # pyright: ignore[reportUnknownVariableType]
    return obj


def _dec(obj: Any) -> Any:
    if isinstance(obj, dict) and "__kind" in obj:
        return _MODELS[obj["__kind"]].model_validate(obj["v"])
    if isinstance(obj, list):
        return [_dec(x) for x in obj]  # pyright: ignore[reportUnknownVariableType]
    return obj


async def serve(fx: FakeExchange, host: str = "127.0.0.1", port: int = 0) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await _session(reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            return  # client was killed (kill -9 chaos test)

    async def _session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            req = json.loads(line)
            method, args = req["m"], req.get("a", [])
            try:
                if method == "set_price":
                    fx.set_price(args[0], Decimal(args[1]), Decimal(args[2]))
                    result: Any = None
                elif method == "events_since":
                    result = _enc(fx.event_log[int(args[0]) :])
                elif method == "event_count":
                    result = len(fx.event_log)
                elif method == "stats":
                    result = {"submit_calls": fx.submit_calls, "accepted": fx.accepted_submits}
                elif method == "submit":
                    await fx.submit(_dec(args[0]))
                    result = None
                else:
                    fn = getattr(fx, method)
                    result = _enc(await fn(*[_dec(a) for a in args]))
                resp = {"ok": True, "r": result}
            except BrokerError as exc:
                resp = {"ok": False, "e": type(exc).__name__, "msg": str(exc)}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, host, port, limit=2**26)


class FakeExchangeClient:
    """Broker-protocol client for a FakeExchange served by `serve()`."""

    is_live = False

    def __init__(self, host: str, port: int, *, account_id: str, venue: str = "fake") -> None:
        self.host, self.port = host, port
        self.account_id = account_id
        self.venue = venue
        self._rw: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
        self._lock = asyncio.Lock()
        self._cursor: int | None = None

    async def _call(self, method: str, *args: Any) -> Any:
        async with self._lock:
            if self._rw is None:
                self._rw = await asyncio.open_connection(self.host, self.port, limit=2**26)
            r, w = self._rw
            w.write((json.dumps({"m": method, "a": [_enc(a) for a in args]}) + "\n").encode())
            await w.drain()
            resp = json.loads(await r.readline())
        if not resp["ok"]:
            raise _ERRORS.get(resp["e"], BrokerError)(resp["msg"])
        return _dec(resp["r"])

    async def submit(self, order: Order) -> None:
        await self._call("submit", order)

    async def cancel(self, client_order_id: str) -> None:
        await self._call("cancel", client_order_id)

    async def get_order(self, client_order_id: str) -> Order | None:
        return await self._call("get_order", client_order_id)

    async def open_orders(self) -> list[Order]:
        return await self._call("open_orders")

    async def fills_for_order(self, client_order_id: str) -> list[Fill]:
        return await self._call("fills_for_order", client_order_id)

    async def positions(self) -> list[Position]:
        return await self._call("positions")

    async def balances(self) -> list[Balance]:
        return await self._call("balances")

    async def flatten_all(self, *, reduce_only: bool = True) -> None:
        await self._call("flatten_all")

    async def set_price(self, symbol: str, bid: Decimal, ask: Decimal) -> None:
        await self._call("set_price", symbol, str(bid), str(ask))

    async def stats(self) -> dict[str, dict[str, int]]:
        return await self._call("stats")

    def drain(self) -> list[OrderEvent | Fill]:
        """Not available synchronously over RPC; use `poll()`."""
        return []

    async def poll(self) -> list[OrderEvent | Fill]:
        """New user-stream events since the last poll (starts at the venue's current end)."""
        if self._cursor is None:
            self._cursor = await self._call("event_count")
            return []
        evs: list[OrderEvent | Fill] = await self._call("events_since", self._cursor)
        self._cursor += len(evs)
        return evs

    async def stream(self) -> AsyncIterator[OrderEvent | Fill]:
        while True:
            for ev in await self.poll():
                yield ev
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        if self._rw is not None:
            self._rw[1].close()
            self._rw = None
