"""Control-plane port (decision 0026).

`helm.api` (FastAPI) and `helm.telegram` (bot) depend only on this protocol and
these view models, never on the engine or brokers. Every command still goes
through `Engine.control`, which journals who/when/from where (SEC-7).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from helm.core.enums import EngineState, Mode
from helm.core.events import Alert, ControlCommand, WebhookEvent
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
    day_pnl: Decimal = Decimal(0)
    capital_pct: Decimal | None = None
    daily_loss_cap: Decimal | None = None


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


class WatchRow(Model):
    """TRK-1 watchlist row."""

    symbol: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    change_24h_pct: Decimal | None = None
    spread_pct: Decimal | None = None
    volume_z: float | None = None
    traded: bool = False
    ts: int = 0


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
    watchlist: tuple[WatchRow, ...] = ()


class ChartBar(Model):
    ts: int  # bar open, UTC ns
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


class ChartMarker(Model):
    ts: int
    side: str
    price: Decimal
    qty: Decimal
    tag: str = ""


class OrderLine(Model):
    client_order_id: str
    side: str
    price: Decimal
    qty: Decimal


class ChartView(Model):
    """UI-3: bars + trade markers + resting order lines for one symbol."""

    symbol: str
    timeframe: str
    bars: tuple[ChartBar, ...]
    markers: tuple[ChartMarker, ...]
    order_lines: tuple[OrderLine, ...]


class ExperimentView(Model):
    run_id: str
    ts: int
    kind: str
    strategy: str
    code_hash: str
    params: dict[str, object]
    passed: bool | None
    sharpe: float | None
    trades: int | None
    git_hash: str
    failures: tuple[str, ...] = ()


class PromotionView(Model):
    ts: int
    strategy: str
    code_hash: str
    params_hash: str
    from_stage: str
    to_stage: str
    approved_by: str
    note: str


class ResearchView(Model):
    """UI-5: recent experiments and the promotion log (read-only)."""

    experiments: tuple[ExperimentView, ...]
    promotions: tuple[PromotionView, ...]


class ControlPort(Protocol):
    def snapshot(self) -> EngineSnapshot: ...

    def logs(self, n: int = 200) -> list[dict[str, object]]: ...

    async def command(self, cmd: ControlCommand) -> str: ...

    def now(self) -> int: ...

    def chart(self, symbol: str, n: int = 500) -> ChartView | None: ...

    def research(self, limit: int = 100) -> ResearchView: ...


class WebhookSink(Protocol):
    """Where verified webhooks go (journal + alert). Deliberately no trading surface."""

    def receive_webhook(self, event: WebhookEvent) -> None: ...

    def now(self) -> int: ...
