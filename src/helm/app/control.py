"""`ControlPort` implementation around a running Engine (decision 0026)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from helm.app.engine import Engine
from helm.core.control import (
    BotView,
    ChartBar,
    ChartMarker,
    ChartView,
    DecisionView,
    EngineSnapshot,
    ExperimentView,
    OrderLine,
    OrderView,
    PositionView,
    PromotionView,
    ResearchView,
    RiskUtilization,
)
from helm.core.events import ControlCommand, WebhookEvent
from helm.core.models import Fill
from helm.infra.logging import LogRing
from helm.tracking.watchlist import Watchlist

MAX_CURVE_POINTS = 500


def _downsample(curve: list[tuple[int, Decimal]], n: int) -> tuple[tuple[int, Decimal], ...]:
    if len(curve) <= n:
        return tuple(curve)
    step = len(curve) / n
    picked = [curve[int(i * step)] for i in range(n - 1)]
    return (*picked, curve[-1])


class EnginePort:
    def __init__(
        self,
        engine: Engine,
        *,
        venue: str,
        ring: LogRing | None = None,
        watchlist: Watchlist | None = None,
        research_db: Path | None = None,
    ) -> None:
        self.engine = engine
        self.venue = venue
        self.ring = ring
        self.watchlist = watchlist
        self.research_db = research_db

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
                day_pnl=s.day_pnl,
                capital_pct=s.capital_pct,
                daily_loss_cap=s.daily_loss_cap,
            )
            for s in e.slots
        )
        gross = sum((abs(pf.exposure(s)) for s in pf.positions), Decimal(0))
        loss_today = max(Decimal(0), -e.daily_pnl)
        hwm = e.high_water_mark
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
            watchlist=self.watchlist.rows() if self.watchlist is not None else (),
        )

    def logs(self, n: int = 200) -> list[dict[str, object]]:
        return self.ring.tail(n) if self.ring is not None else []

    def receive_webhook(self, event: WebhookEvent) -> None:
        """DATA-7 sink: journal + INFO alert. A webhook can never trade by itself."""
        self.engine.journal.append(
            event,
            ts=event.ts_recv,
            run_id=self.engine.run_id,
            account_id=self.engine.account_id,
            key=f"webhook:{event.source}",
        )
        self.engine.alerts.info(
            f"webhook:{event.source}", f"webhook {event.source}: {event.payload_json[:300]}"
        )

    async def command(self, cmd: ControlCommand) -> str:
        return await self.engine.control(cmd)

    # ------------------------------------------------------------------ UI-3
    def chart(self, symbol: str, n: int = 500) -> ChartView | None:
        e = self.engine
        tfs = e.history.timeframes(symbol)
        tf = tfs[0] if tfs else "1m"
        bars = e.history.last(symbol, tf, n, e.clock.now())
        if not bars and symbol not in e.quotes:
            return None
        since = bars[0].ts_open if bars else 0
        markers: list[ChartMarker] = []
        for entry in e.journal.entries(kinds=("Fill",), account_id=e.account_id):
            if entry.ts < since:
                continue
            f = entry.decode()
            if isinstance(f, Fill) and f.symbol == symbol:
                order = e.om.find(f.client_order_id)
                markers.append(
                    ChartMarker(
                        ts=f.ts,
                        side=f.side.value,
                        price=f.price,
                        qty=f.qty,
                        tag=order.intent.tag if order is not None else "",
                    )
                )
        lines = tuple(
            OrderLine(
                client_order_id=o.client_order_id,
                side=o.intent.side.value,
                price=o.intent.limit_price,
                qty=o.remaining,
            )
            for o in e.om.working_orders()
            if o.intent.symbol == symbol and o.intent.limit_price is not None
        )
        return ChartView(
            symbol=symbol,
            timeframe=tf,
            bars=tuple(
                ChartBar(ts=b.ts_open, o=b.o, h=b.h, l=b.l, c=b.c, v=b.volume) for b in bars
            ),
            markers=tuple(markers[-2000:]),
            order_lines=lines,
        )

    # ------------------------------------------------------------------ UI-5
    def research(self, limit: int = 100) -> ResearchView:
        if self.research_db is None or not self.research_db.exists():
            return ResearchView(experiments=(), promotions=())
        from helm.research.experiments.store import ExperimentStore  # noqa: PLC0415
        from helm.research.promotion import PromotionRegistry  # noqa: PLC0415

        store = ExperimentStore(self.research_db)
        reg = PromotionRegistry(self.research_db)
        try:
            exps = tuple(
                ExperimentView(
                    run_id=r.run_id,
                    ts=r.ts,
                    kind=r.kind,
                    strategy=r.strategy,
                    code_hash=r.code_hash,
                    params=r.params,
                    passed=r.passed,
                    sharpe=r.metrics.get("sharpe", r.metrics.get("best_is_sharpe")),
                    trades=r.metrics.get("trade_count"),
                    git_hash=r.git_hash,
                    failures=tuple(
                        f"{c['name']}: {c.get('detail', '')}"
                        for c in r.report.get("checks", [])
                        if not c.get("passed", True)
                    ),
                )
                for r in store.runs(limit=limit)
            )
            promos = tuple(
                PromotionView(
                    ts=h["ts"],
                    strategy=h["strategy"],
                    code_hash=h["code_hash"],
                    params_hash=h["params_hash"],
                    from_stage=h["from_stage"],
                    to_stage=h["to_stage"],
                    approved_by=h["approved_by"],
                    note=h["note"],
                )
                for h in reg.history()
            )
        finally:
            store.close()
            reg.close()
        return ResearchView(experiments=exps, promotions=promos)
