"""`websockets` transport for WsManager and broker user streams (DATA-5, decision 0011).

Normalizes library errors to `ConnectionError` so reconnect logic stays
transport-agnostic, and decodes binary frames (Alpaca sends some as bytes).
"""

from __future__ import annotations

import asyncio

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException


class WebsocketTransport:
    def __init__(self, conn: ClientConnection) -> None:
        self._conn = conn

    async def send(self, msg: str) -> None:
        try:
            await self._conn.send(msg)
        except WebSocketException as exc:
            raise ConnectionError(f"ws send failed: {exc}") from exc

    async def recv(self) -> str:
        try:
            data = await self._conn.recv()
        except WebSocketException as exc:
            raise ConnectionError(f"ws closed: {exc}") from exc
        return data.decode() if isinstance(data, bytes) else data

    async def ping(self) -> None:
        try:
            waiter = await self._conn.ping()
            await asyncio.wait_for(waiter, timeout=10)
        except WebSocketException as exc:
            raise ConnectionError(f"ws ping failed: {exc}") from exc

    async def close(self) -> None:
        try:
            await self._conn.close()
        except WebSocketException:
            return


async def connect_ws(url: str) -> WebsocketTransport:
    try:
        conn = await connect(url, ping_interval=None, max_size=2**24, open_timeout=15)
    except WebSocketException as exc:
        raise ConnectionError(f"ws connect failed: {exc}") from exc
    return WebsocketTransport(conn)
