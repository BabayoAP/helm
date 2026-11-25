"""Watchlist (TRK-1): last price, 24h change, spread %, volume anomaly z-score.

Fed by the same market-data events the engine sees (plus watch-only symbols the
engine never trades). Pure state: no I/O, no clock reads; `now` comes from events.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from helm.core.control import WatchRow
from helm.core.models import Bar, Quote, Trade

DAY_NS = 86_400 * 10**9
HUNDRED = Decimal(100)


@dataclass
class _SymState:
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    ts: int = 0
    closes: deque[tuple[int, Decimal]] = field(default_factory=lambda: deque(maxlen=3000))
    volumes: deque[float] = field(default_factory=lambda: deque(maxlen=500))


class Watchlist:
    def __init__(
        self, symbols: Iterable[str], *, traded: Iterable[str] = (), z_window: int = 60
    ) -> None:
        self.traded = frozenset(traded)
        self.z_window = z_window
        self._s: dict[str, _SymState] = {s: _SymState() for s in dict.fromkeys([*symbols, *traded])}

    @property
    def symbols(self) -> list[str]:
        return list(self._s)

    def update(self, ev: BaseModel) -> None:
        sym = getattr(ev, "symbol", None)
        st = self._s.get(sym) if isinstance(sym, str) else None
        if st is None:
            return
        if isinstance(ev, Quote):
            st.bid, st.ask, st.ts = ev.bid, ev.ask, max(st.ts, ev.ts_recv)
            if st.last is None:
                st.last = ev.mid
        elif isinstance(ev, Trade):
            st.last, st.ts = ev.price, max(st.ts, ev.ts_recv)
        elif isinstance(ev, Bar) and ev.closed:
            st.last, st.ts = ev.c, max(st.ts, ev.ts_close)
            st.closes.append((ev.ts_close, ev.c))
            st.volumes.append(float(ev.volume))

    def _change_24h(self, st: _SymState) -> Decimal | None:
        if st.last is None or not st.closes:
            return None
        cutoff = st.ts - DAY_NS
        ref = None
        for ts, c in st.closes:  # oldest close at or after 24h ago
            if ts >= cutoff:
                ref = c
                break
        if ref is None or ref == 0 or st.closes[0][0] > cutoff:
            return None  # not 24h of history yet
        return (st.last / ref - 1) * HUNDRED

    def _volume_z(self, st: _SymState) -> float | None:
        if len(st.volumes) < self.z_window + 1:
            return None
        hist = list(st.volumes)[-self.z_window - 1 : -1]  # trailing window, excluding the last
        sd = statistics.pstdev(hist)
        if sd == 0 or math.isnan(sd):
            return None
        return (st.volumes[-1] - statistics.fmean(hist)) / sd

    def row(self, symbol: str) -> WatchRow:
        st = self._s[symbol]
        spread = None
        if st.bid is not None and st.ask is not None and st.bid > 0:
            mid = (st.bid + st.ask) / 2
            spread = (st.ask - st.bid) / mid * HUNDRED
        return WatchRow(
            symbol=symbol,
            last=st.last,
            bid=st.bid,
            ask=st.ask,
            change_24h_pct=self._change_24h(st),
            spread_pct=spread,
            volume_z=self._volume_z(st),
            traded=symbol in self.traded,
            ts=st.ts,
        )

    def rows(self) -> tuple[WatchRow, ...]:
        return tuple(self.row(s) for s in self._s)
