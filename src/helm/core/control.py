"""Control-plane port (decision 0026).

`helm.api` (FastAPI) and `helm.telegram` (bot) depend only on this protocol and
these view models, never on the engine or brokers. Every command still goes
through `Engine.control`, which journals who/when/from where (SEC-7).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from helm.core.enums import EngineState, Mode
from helm.core.events import Alert, ControlCommand
from helm.core.models import Model


class PositionView(Model):
    symbol: str
    qty: Decimal
    avg_px: Decimal
    mark: Decimal | None = None
    unrealized_pnl: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)


class OrderView(Model):
    client_order_id: str
    strategy_id: str
    symbol: str
    side: str
    type: str
    qty: Decimal
    filled_qty: Decimal
    limit_price: Decimal | None = None
    state: str


class BotView(Model):
    id: str
    symbols: tuple[str, ...]
    lifecycle: str
    paused: bool
    consecutive_rejects: int


class RiskUtilization(Model):
    name: str
    used: Decimal
    limit: Decimal

    @property
    def pct(self) -> Decimal:
        return self.used / self.limit * 100 if self.limit else Decimal(0)


class DecisionView(Model):
    intent_id: str
    verdict: str
    rule_id: str
    reason: str


class EngineSnapshot(Model):
    mode: Mode
    venue: str
    account_id: str
    state: EngineState
    equity: Decimal
    daily_pnl: Decimal
    feeds: dict[str, str]
    positions: tuple[PositionView, ...]
    orders: tuple[OrderView, ...]
    bots: tuple[BotView, ...]
    risk: tuple[RiskUtilization, ...]
    equity_curve: tuple[tuple[int, Decimal], ...]
    alerts: tuple[Alert, ...]
    decisions: tuple[DecisionView, ...]
    reconcile_ok: bool | None
    ts: int


class ControlPort(Protocol):
    def snapshot(self) -> EngineSnapshot: ...

    def logs(self, n: int = 200) -> list[dict[str, object]]: ...

    async def command(self, cmd: ControlCommand) -> str: ...

    def now(self) -> int: ...
