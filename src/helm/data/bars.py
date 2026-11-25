"""Time-bar builder from trades (paper/live feeds that deliver trades, not bars)."""

from __future__ import annotations

from decimal import Decimal

from helm.core.models import Bar, Trade
from helm.core.timeframes import floor_ts, timeframe_ns


class BarBuilder:
    def __init__(self, timeframe: str = "1m") -> None:
        self.timeframe = timeframe
        self.step = timeframe_ns(timeframe)
        self._open: dict[str, dict[str, Decimal | int]] = {}

    def on_trade(self, t: Trade) -> list[Bar]:
        """Feed a trade; returns bars that closed *before* this trade's bucket."""
        out = self.close_until(t.symbol, t.ts_event)
        start = floor_ts(t.ts_event, self.timeframe)
        cur = self._open.get(t.symbol)
        if cur is None:
            self._open[t.symbol] = {
                "ts": start,
                "o": t.price,
                "h": t.price,
                "l": t.price,
                "c": t.price,
                "v": t.size,
            }
        else:
            cur["h"] = max(Decimal(cur["h"]), t.price)
            cur["l"] = min(Decimal(cur["l"]), t.price)
            cur["c"] = t.price
            cur["v"] = Decimal(cur["v"]) + t.size
        return out

    def close_until(self, symbol: str, ts_ns: int) -> list[Bar]:
        """Close the open bar if `ts_ns` is at/after its end (timer or next trade)."""
        cur = self._open.get(symbol)
        if cur is None or ts_ns < int(cur["ts"]) + self.step:
            return []
        del self._open[symbol]
        ts = int(cur["ts"])
        return [
            Bar(
                symbol=symbol,
                timeframe=self.timeframe,
                ts_open=ts,
                ts_close=ts + self.step,
                o=Decimal(cur["o"]),
                h=Decimal(cur["h"]),
                l=Decimal(cur["l"]),
                c=Decimal(cur["c"]),
                volume=Decimal(cur["v"]),
            )
        ]
