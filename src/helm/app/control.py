"""`ControlPort` implementation around a running Engine (decision 0026)."""

from __future__ import annotations

from decimal import Decimal

from helm.app.engine import Engine
from helm.core.control import (
    BotView,
    DecisionView,
    EngineSnapshot,
    OrderView,
    PositionView,
    RiskUtilization,
)
from helm.core.events import ControlCommand
from helm.infra.logging import LogRing

MAX_CURVE_POINTS = 500


def _downsample(curve: list[tuple[int, Decimal]], n: int) -> tuple[tuple[int, Decimal], ...]:
    if len(curve) <= n:
        return tuple(curve)
    step = len(curve) / n
    picked = [curve[int(i * step)] for i in range(n - 1)]
    return (*picked, curve[-1])


class EnginePort:
    def __init__(self, engine: Engine, *, venue: str, ring: LogRing | None = None) -> None:
        self.engine = engine
        self.venue = venue
        self.ring = ring

    def now(self) -> int:
        return self.engine.clock.now()

    def resume_code(self) -> str | None:
        """Only for authorized channels (Telegram allowlist, local console)."""
        return self.engine.last_resume_code

    def snapshot(self) -> EngineSnapshot:
        e = self.engine
        pf = e.portfolio
        rc = e.risk_config
        equity = pf.equity()
        positions = tuple(
            PositionView(
                symbol=s,
                qty=p.qty,
                avg_px=p.avg_px,
                mark=p.mark,
                unrealized_pnl=pf.unrealized_for(s),
                realized_pnl=p.realized_pnl,
            )
            for s, p in sorted(pf.positions.items())
            if p.qty != 0 or p.realized_pnl != 0
        )
        orders = tuple(
            OrderView(
                client_order_id=o.client_order_id,
                strategy_id=o.intent.strategy_id,
                symbol=o.intent.symbol,
                side=o.intent.side.value,
                type=o.intent.type.value,
                qty=o.intent.qty,
                filled_qty=o.filled_qty,
                limit_price=o.intent.limit_price,
                state=o.state.value,
            )
            for o in e.om.working_orders()
        )
        bots = tuple(
            BotView(
                id=s.strategy.id,
                symbols=s.symbols,
                lifecycle=s.lifecycle.value,
                paused=s.paused,
                consecutive_rejects=s.consecutive_rejects,
            )
            for s in e.slots
        )
        gross = sum((abs(pf.exposure(s)) for s in pf.positions), Decimal(0))
        loss_today = max(Decimal(0), -e.daily_pnl)
        hwm = max(e._hwm, equity)  # pyright: ignore[reportPrivateUsage]
        risk = (
            RiskUtilization(
                name="gross exposure (x equity)",
                used=gross / equity if equity > 0 else Decimal(0),
                limit=rc.max_gross_leverage,
            ),
            RiskUtilization(
                name="daily loss (% of SOD equity)",
                used=loss_today / e.start_of_day_equity * 100
                if e.start_of_day_equity > 0
                else Decimal(0),
                limit=rc.daily_loss_limit_pct,
            ),
            RiskUtilization(
                name="drawdown (% from peak)",
                used=(hwm - equity) / hwm * 100 if hwm > 0 else Decimal(0),
                limit=rc.max_drawdown_pct,
            ),
            RiskUtilization(
                name="open orders",
                used=Decimal(len(orders)),
                limit=Decimal(rc.max_open_orders),
            ),
        )
        rec = e.reconcile_result
        return EngineSnapshot(
            mode=e.mode,
            venue=self.venue,
            account_id=e.account_id,
            state=e.ks.state,
            equity=equity,
            daily_pnl=e.daily_pnl,
            feeds={s: h.state.value for s, h in e.feeds.items()},
            positions=positions,
            orders=orders,
            bots=bots,
            risk=risk,
            equity_curve=_downsample(e.equity_curve, MAX_CURVE_POINTS),
            alerts=tuple(e.alerts.sent[-50:]),
            decisions=tuple(
                DecisionView(
                    intent_id=d.intent_id,
                    verdict=d.verdict.value,
                    rule_id=d.rule_id,
                    reason=d.reason,
                )
                for d in e.decisions[-100:]
            ),
            reconcile_ok=None if rec is None else rec.ok,
            ts=e.clock.now(),
        )

    def logs(self, n: int = 200) -> list[dict[str, object]]:
        return self.ring.tail(n) if self.ring is not None else []

    async def command(self, cmd: ControlCommand) -> str:
        return await self.engine.control(cmd)
