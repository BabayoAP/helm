"""Broker protocol (PRD §6.4). Implementations: SimBroker, FakeExchange, live adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from helm.core.events import OrderEvent
from helm.core.models import Balance, Fill, Order, Position


@runtime_checkable
class Broker(Protocol):
    account_id: str
    venue: str
    is_live: bool

    async def submit(self, order: Order) -> None: ...

    async def cancel(self, client_order_id: str) -> None: ...

    async def get_order(self, client_order_id: str) -> Order | None:
        """REST lookup by client_order_id; used to resolve UNKNOWN before any retry."""
        ...

    async def open_orders(self) -> list[Order]: ...

    async def positions(self) -> list[Position]: ...

    async def balances(self) -> list[Balance]: ...

    async def flatten_all(self, *, reduce_only: bool = True) -> None: ...

    def stream(self) -> AsyncIterator[OrderEvent | Fill]:
        """User-data stream of order updates and fills (may duplicate/reorder)."""
        ...
