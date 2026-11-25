"""Concrete strategy Context used identically in backtest, paper and live (I1, I3)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from decimal import Decimal
from typing import Any

from helm.core.clock import Clock
from helm.core.errors import LookaheadError
from helm.core.models import Bar, Position, Quote
from helm.strategy.api import ParamValue, StrategyLogger


class _NullLogger:
    def info(self, event: str, **kw: Any) -> None:
        return None

    def warning(self, event: str, **kw: Any) -> None:
        return None


class BarHistory:
    """Rolling closed-bar store shared by all contexts in an engine."""

    def __init__(self, maxlen: int = 5000) -> None:
        self._maxlen = maxlen
        self._bars: dict[tuple[str, str], deque[Bar]] = {}

    def add(self, bar: Bar) -> None:
        if not bar.closed:
            raise LookaheadError("only closed bars may enter history (I3)")
        dq = self._bars.setdefault((bar.symbol, bar.timeframe), deque(maxlen=self._maxlen))
        if dq and bar.ts_close <= dq[-1].ts_close:
            return  # duplicate / out-of-order bar: ignore
        dq.append(bar)

    def last(self, symbol: str, timeframe: str, n: int, now: int) -> list[Bar]:
        dq = self._bars.get((symbol, timeframe))
        if not dq or n <= 0:
            return []
        out: list[Bar] = []
        for b in reversed(dq):
            if b.ts_close <= now:
                out.append(b)
                if len(out) == n:
                    break
        out.reverse()
        return out


class EngineContext:
    def __init__(
        self,
        strategy_id: str,
        clock: Clock,
        history: BarHistory,
        *,
        params: Mapping[str, ParamValue] | None = None,
        position_fn: Callable[[str], Position] | None = None,
        equity_fn: Callable[[], Decimal] | None = None,
        quote_fn: Callable[[str], Quote | None] | None = None,
        state: MutableMapping[str, Any] | None = None,
        log: StrategyLogger | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.params: Mapping[str, ParamValue] = dict(params or {})
        self.state: MutableMapping[str, Any] = state if state is not None else {}
        self.log: StrategyLogger = log or _NullLogger()
        self._clock = clock
        self._history = history
        self._position_fn = position_fn or (lambda s: Position(symbol=s))
        self._equity_fn = equity_fn or (lambda: Decimal(0))
        self._quote_fn = quote_fn or (lambda _s: None)

    def now(self) -> int:
        return self._clock.now()

    def history(self, symbol: str, timeframe: str, n: int) -> Sequence[Bar]:
        return self._history.last(symbol, timeframe, n, self.now())

    def position(self, symbol: str) -> Position:
        return self._position_fn(symbol)

    def equity(self) -> Decimal:
        return self._equity_fn()

    def last_quote(self, symbol: str) -> Quote | None:
        q = self._quote_fn(symbol)
        if q is not None and q.ts_recv > self.now():
            return None
        return q
