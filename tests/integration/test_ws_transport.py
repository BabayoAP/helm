"""Real `websockets` transport + WsManager + AlpacaFeedHandler against a local server
that speaks the Alpaca data-stream protocol (DATA-5). Localhost only, no venue."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from helm.app.services import QueueFeed
from helm.core.events import FeedStatus
from helm.core.models import Bar, Quote
from helm.data.connectors.alpaca import AlpacaFeedHandler
from helm.data.connectors.alpaca_common import AlpacaCredentials, parse_ts
from helm.data.ws_manager import WsConfig, WsManager
from helm.infra.ws import connect_ws

CREDS = AlpacaCredentials(key_id="PKTESTKEYID000", secret_key="test-secret-not-real-0000")


def test_transport_send_recv_ping_and_disconnect_maps_to_connection_error() -> None:
    async def run() -> None:
        async def echo(ws: ServerConnection) -> None:
            async for m in ws:
                if m == "bye":
                    await ws.close()
                    return
                await ws.send(b"bin" if m == "bin" else m)

        async with serve(echo, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            t = await connect_ws(f"ws://127.0.0.1:{port}")
            await t.send("hello")
            assert await t.recv() == "hello"
            await t.send("bin")
            assert await t.recv() == "bin"  # binary frames are decoded
            await t.ping()
            await t.send("bye")
            with pytest.raises(ConnectionError):
                await t.recv()
            with pytest.raises(ConnectionError):
                await t.send("after close")
            with pytest.raises(ConnectionError):
                await t.ping()
            await t.close()
        with pytest.raises((ConnectionError, OSError)):
            await connect_ws(f"ws://127.0.0.1:{port}")

    asyncio.run(run())


def test_alpaca_feed_end_to_end_with_reconnect() -> None:
    async def run() -> None:
        connections: list[list[dict[str, Any]]] = []

        async def alpaca_like(ws: ServerConnection) -> None:
            got: list[dict[str, Any]] = []
            connections.append(got)
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            for _ in range(2):
                got.append(json.loads(await ws.recv()))
            n = len(connections)
            bar = {
                "T": "b",
                "S": "AAPL",
                "o": 190,
                "h": 191,
                "l": 189,
                "c": 190.5,
                "v": 100,
                "t": f"2026-11-24T15:0{n}:00Z",
            }
            q = {
                "T": "q",
                "S": "AAPL",
                "bp": 190,
                "bs": 1,
                "ap": 190.1,
                "as": 1,
                "t": "2026-11-24T15:00:00Z",
            }
            await ws.send(json.dumps([bar, q]))
            if n == 1:
                await ws.close()  # drop the first connection: manager must reconnect
                return
            await ws.wait_closed()

        async with serve(alpaca_like, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            now = parse_ts("2026-11-24T16:00:00Z")
            handler = AlpacaFeedHandler(CREDS, ("alpaca:AAPL/USD",), now_fn=lambda: now)
            qf = QueueFeed()
            qf.manager = WsManager(
                "alpaca",
                lambda: connect_ws(f"ws://127.0.0.1:{port}"),
                handler,
                qf.emit,
                config=WsConfig(backoff_base_s=0.01, backoff_cap_s=0.05),
                time_fn=lambda: now,
            )
            events: list[Any] = []
            async for ev in qf:
                events.append(ev)
                if sum(isinstance(e, Bar) for e in events) == 2:
                    break
        bars = [e for e in events if isinstance(e, Bar)]
        assert [b.ts_open for b in bars] == [
            parse_ts("2026-11-24T15:01:00Z"),
            parse_ts("2026-11-24T15:02:00Z"),
        ]
        assert any(isinstance(e, Quote) for e in events)
        states = [e.state.value for e in events if isinstance(e, FeedStatus)]
        assert states[:3] == ["LIVE", "RECONNECTING", "LIVE"]
        assert connections[0][0]["action"] == "auth"
        assert connections[0][1]["bars"] == ["AAPL"]
        assert len(connections) == 2 and qf.manager.reconnects == 1

    asyncio.run(run())
