"""Alpaca market data: REST history (DATA-3) and the live stream handler (DATA-5).

`AlpacaOhlcvClient` implements the `OhlcvClient` protocol, so the resumable
`fetch_history` from M1 works unchanged. `AlpacaFeedHandler` plugs into
`WsManager` (heartbeat, reconnect, resubscribe, REST gap-fill, FeedStatus).

The free IEX feed covers one venue only: bars are sparse for thinly traded
names and volumes are a fraction of the consolidated tape (decision 0023).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from helm.core.errors import RateLimitedError
from helm.core.models import Bar, BookSnapshot, Quote, Trade
from helm.core.timeframes import timeframe_ns
from helm.data.connectors.alpaca_common import (
    DATA_URL,
    STREAM_DATA_URL,
    AlpacaCredentials,
    from_venue,
    is_crypto,
    iso,
    parse_ts,
    to_venue,
)

_TF = {"1m": "1Min", "5m": "5Min", "15m": "15Min", "30m": "30Min", "1h": "1Hour", "1d": "1Day"}


def alpaca_timeframe(tf: str) -> str:
    try:
        return _TF[tf]
    except KeyError:
        raise ValueError(f"alpaca has no {tf!r} bars; use one of {sorted(_TF)}") from None


def stream_url(*, crypto: bool, feed: str = "iex") -> str:
    return f"{STREAM_DATA_URL}/v1beta3/crypto/us" if crypto else f"{STREAM_DATA_URL}/v2/{feed}"


class AlpacaOhlcvClient:
    """Bars REST client. `crypto` selects the crypto endpoint (no feed parameter)."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        crypto: bool,
        feed: str = "iex",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.crypto = crypto
        self.feed = feed
        self._http = httpx.AsyncClient(
            base_url=DATA_URL, transport=transport, headers=credentials.headers(), timeout=20
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def bars(
        self, venue_symbol: str, timeframe: str, start_ns: int, limit: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "symbols": venue_symbol,
            "timeframe": alpaca_timeframe(timeframe),
            "start": iso(start_ns),
            "limit": min(limit, 10000),
            "sort": "asc",
        }
        if self.crypto:
            path = "/v1beta3/crypto/us/bars"
        else:
            path = "/v2/stocks/bars"
            params["feed"] = self.feed
        resp = await self._http.get(path, params=params)
        if resp.status_code == 429:
            raise RateLimitedError("alpaca data 429", retry_after_s=None)
        resp.raise_for_status()
        rows: list[dict[str, Any]] = (resp.json().get("bars") or {}).get(venue_symbol) or []
        return rows

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1m", since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        """CCXT-shaped rows: [ts_ms, o, h, l, c, v]."""
        rows = await self.bars(symbol, timeframe, (since or 0) * 1_000_000, limit or 1000)
        return [
            [parse_ts(r["t"]) // 1_000_000, r["o"], r["h"], r["l"], r["c"], r["v"]] for r in rows
        ]


class AlpacaFeedHandler:
    """FeedHandler for one asset class (equities via IEX/SIP, or crypto)."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        symbols: tuple[str, ...],
        *,
        timeframe: str = "1m",
        rest: AlpacaOhlcvClient | None = None,
        quotes: bool = True,
        trades: bool = False,
        now_fn: Callable[[], int],
    ) -> None:
        crypto = {is_crypto(s) for s in symbols}
        if len(crypto) != 1:
            raise ValueError("one AlpacaFeedHandler per asset class (equities or crypto)")
        if timeframe != "1m":
            raise ValueError("the alpaca stream publishes 1m bars; build larger bars from them")
        self.crypto = crypto.pop()
        self._creds = credentials
        self.symbols = symbols
        self.timeframe = timeframe
        self._venue = {to_venue(s): s for s in symbols}
        self._rest = rest
        self._quotes = quotes
        self._trades = trades
        self._now = now_fn
        self.bad_messages = 0

    def subscribe_messages(self) -> list[str]:
        sub: dict[str, Any] = {"action": "subscribe", "bars": list(self._venue)}
        if self._quotes:
            sub["quotes"] = list(self._venue)
        if self._trades:
            sub["trades"] = list(self._venue)
        auth = {"action": "auth", "key": self._creds.key_id, "secret": self._creds.secret_key}
        return [json.dumps(auth), json.dumps(sub)]

    def _canon(self, venue_symbol: str) -> str:
        return self._venue.get(venue_symbol) or from_venue(
            venue_symbol, "crypto" if self.crypto else "us_equity"
        )

    def parse(self, raw: str) -> list[BaseModel]:
        msgs = json.loads(raw)
        if isinstance(msgs, dict):
            msgs = [msgs]
        out: list[BaseModel] = []
        recv = self._now()
        for m in msgs:
            t = m.get("T")
            if t == "error":
                raise ConnectionError(f"alpaca stream error {m.get('code')}: {m.get('msg')}")
            try:
                if t == "b":
                    ts_open = parse_ts(m["t"])
                    out.append(
                        Bar(
                            symbol=self._canon(m["S"]),
                            timeframe="1m",
                            ts_open=ts_open,
                            ts_close=ts_open + timeframe_ns("1m"),
                            o=Decimal(str(m["o"])),
                            h=Decimal(str(m["h"])),
                            l=Decimal(str(m["l"])),
                            c=Decimal(str(m["c"])),
                            volume=Decimal(str(m.get("v", 0))),
                            trades=m.get("n"),
                        )
                    )
                elif t == "q":
                    out.append(
                        Quote(
                            symbol=self._canon(m["S"]),
                            bid=Decimal(str(m["bp"])),
                            bid_sz=Decimal(str(m.get("bs", 0))),
                            ask=Decimal(str(m["ap"])),
                            ask_sz=Decimal(str(m.get("as", 0))),
                            ts_event=parse_ts(m["t"]),
                            ts_recv=recv,
                        )
                    )
                elif t == "t":
                    out.append(
                        Trade(
                            symbol=self._canon(m["S"]),
                            price=Decimal(str(m["p"])),
                            size=Decimal(str(m["s"])),
                            trade_id=str(m["i"]),
                            ts_event=parse_ts(m["t"]),
                            ts_recv=recv,
                        )
                    )
            except (KeyError, ValueError, ValidationError):
                self.bad_messages += 1  # DATA-1: malformed venue data never crashes the feed
        return out

    async def gap_fill(self, symbol: str, timeframe: str, since_ts_open: int) -> list[Bar]:
        if self._rest is None:
            return []
        step = timeframe_ns(timeframe)
        rows = await self._rest.bars(to_venue(symbol), timeframe, since_ts_open + step, 10000)
        now = self._now()
        bars: list[Bar] = []
        for r in rows:
            ts_open = parse_ts(r["t"])
            if ts_open <= since_ts_open or ts_open + step > now:
                continue  # only closed bars after the last one we have
            try:
                bars.append(
                    Bar(
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_open=ts_open,
                        ts_close=ts_open + step,
                        o=Decimal(str(r["o"])),
                        h=Decimal(str(r["h"])),
                        l=Decimal(str(r["l"])),
                        c=Decimal(str(r["c"])),
                        volume=Decimal(str(r.get("v", 0))),
                        trades=r.get("n"),
                    )
                )
            except (KeyError, ValueError, ValidationError):
                self.bad_messages += 1
        return bars

    async def book_snapshot(self, symbol: str) -> BookSnapshot:
        raise NotImplementedError("alpaca feed is L1 only (no order book deltas)")
