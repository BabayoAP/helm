"""Strategy API (PRD §6.3). Sandbox-friendly and lookahead-safe.

Strategies receive closed bars / quotes / fills and *return* OrderIntents. They
never call a broker, never read the wall clock (use `ctx.now()`), and never do
I/O. Import contracts in pyproject.toml enforce this.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from helm.core.models import Bar, BookSnapshot, Fill, OrderIntent, Position, Quote

ParamValue = float | int | str | bool


class StrategyLogger(Protocol):
    def info(self, event: str, **kw: Any) -> None: ...

    def warning(self, event: str, **kw: Any) -> None: ...


class Context(Protocol):
    strategy_id: str
    params: Mapping[str, ParamValue]
    state: MutableMapping[str, Any]  # persisted + journaled key/value store
    log: StrategyLogger

    def now(self) -> int: ...

    def history(self, symbol: str, timeframe: str, n: int) -> Sequence[Bar]:
        """Last `n` closed bars with ts_close <= now(). Never returns future data."""
        ...

    def position(self, symbol: str) -> Position: ...

    def equity(self) -> Decimal: ...

    def last_quote(self, symbol: str) -> Quote | None: ...


@runtime_checkable
class Strategy(Protocol):
    id: str

    def on_start(self, ctx: Context) -> None: ...

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]: ...

    def on_quote(self, q: Quote, ctx: Context) -> list[OrderIntent]: ...

    def on_book(self, b: BookSnapshot, ctx: Context) -> list[OrderIntent]: ...

    def on_fill(self, f: Fill, ctx: Context) -> list[OrderIntent]: ...

    def on_stop(self, ctx: Context) -> None: ...


class BaseStrategy:
    """Convenience base: no-op handlers so strategies implement only what they need."""

    id: str = "base"

    def __init__(self, strategy_id: str | None = None) -> None:
        if strategy_id is not None:
            self.id = strategy_id

    def on_start(self, ctx: Context) -> None:
        return None

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        return []

    def on_quote(self, q: Quote, ctx: Context) -> list[OrderIntent]:
        return []

    def on_book(self, b: BookSnapshot, ctx: Context) -> list[OrderIntent]:
        return []

    def on_fill(self, f: Fill, ctx: Context) -> list[OrderIntent]:
        return []

    def on_stop(self, ctx: Context) -> None:
        return None
