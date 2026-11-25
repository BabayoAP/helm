"""Normalizer: venue payloads -> canonical events (DATA-1).

Never raises on bad input: a malformed payload is logged, dropped and counted.
Unknown venue fields are ignored by the canonical models (the raw payload stays
available to the caller for debugging).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ValidationError

from helm.core.enums import Side
from helm.core.models import Bar, Quote, Trade
from helm.core.timeframes import timeframe_ns

Parser = Callable[[dict[str, Any], int], list[BaseModel]]


def _d(v: Any) -> Decimal:
    if isinstance(v, bool) or v is None:
        raise ValueError("not a number")
    d = Decimal(str(v))
    if not d.is_finite():
        raise ValueError("non-finite")
    return d


def _ms_to_ns(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("timestamp must be numeric ms")
    return int(v) * 1_000_000


def parse_ccxt_ticker(p: dict[str, Any], ts_recv: int) -> list[BaseModel]:
    return [
        Quote(
            symbol=p["helm_symbol"],
            bid=_d(p["bid"]),
            bid_sz=_d(p.get("bidVolume") or 0),
            ask=_d(p["ask"]),
            ask_sz=_d(p.get("askVolume") or 0),
            ts_event=_ms_to_ns(p["timestamp"]),
            ts_recv=ts_recv,
        )
    ]


def parse_ccxt_trade(p: dict[str, Any], ts_recv: int) -> list[BaseModel]:
    side = p.get("side")
    return [
        Trade(
            symbol=p["helm_symbol"],
            price=_d(p["price"]),
            size=_d(p["amount"]),
            side=Side(side) if side in ("buy", "sell") else None,
            trade_id=str(p["id"]),
            ts_event=_ms_to_ns(p["timestamp"]),
            ts_recv=ts_recv,
        )
    ]


def parse_ccxt_ohlcv(p: dict[str, Any], ts_recv: int) -> list[BaseModel]:
    """`{"helm_symbol", "timeframe", "ohlcv": [ts_ms, o, h, l, c, v]}`.

    The bar is closed iff ts_close <= ts_recv.
    """
    ts, o, h, l, c, v = p["ohlcv"][:6]  # noqa: E741
    ts_open = _ms_to_ns(ts)
    ts_close = ts_open + timeframe_ns(p["timeframe"])
    return [
        Bar(
            symbol=p["helm_symbol"],
            timeframe=p["timeframe"],
            ts_open=ts_open,
            ts_close=ts_close,
            o=_d(o),
            h=_d(h),
            l=_d(l),
            c=_d(c),
            volume=_d(v),
            closed=ts_close <= ts_recv,
        )
    ]


class Normalizer:
    def __init__(self, parsers: dict[str, Parser] | None = None, log: Any = None) -> None:
        self.parsers: dict[str, Parser] = parsers or {
            "ticker": parse_ccxt_ticker,
            "trade": parse_ccxt_trade,
            "ohlcv": parse_ccxt_ohlcv,
        }
        self.dropped: Counter[str] = Counter()
        self.ok: Counter[str] = Counter()
        self._log = log

    def normalize(self, raw: str | bytes | dict[str, Any], ts_recv: int) -> list[BaseModel]:
        channel = "?"
        try:
            payload: Any = json.loads(raw) if isinstance(raw, str | bytes) else raw
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
            channel = str(payload.get("channel", "?"))  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
            parser = self.parsers[channel]
            events = parser(payload, ts_recv)  # pyright: ignore[reportUnknownArgumentType]
        except (
            KeyError,
            ValueError,
            TypeError,
            IndexError,
            InvalidOperation,
            ValidationError,
            OverflowError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            self.dropped[channel] += 1
            if self._log is not None:
                self._log.warning("normalizer_drop", channel=channel, error=type(exc).__name__)
            return []
        self.ok[channel] += len(events)
        return events
