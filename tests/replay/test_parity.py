"""TEST-4 / I1 / PAP-3: replay one recorded feed through the event-driven backtest driver
and through the paper runner; the order sequences must be identical and PnL equal."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from helm.app.engine import Engine
from helm.app.paper import PaperRunner, RunnerConfig, dispatch
from helm.backtest.friction.models import FrictionConfig
from helm.brokers.sim.broker import SimBroker, SimMode
from helm.core.clock import SimClock
from helm.core.config import RiskConfig
from helm.core.enums import Mode, OrderType, Side
from helm.core.models import Bar, Fill, OrderIntent, Quote
from helm.data.instruments import InstrumentRegistry
from helm.data.recorder import Recorder, replay
from helm.data.synthetic_feed import synthetic_feed
from helm.oms.accounting import Portfolio
from helm.oms.journal import Journal
from helm.strategy.api import BaseStrategy, Context
from helm.strategy.examples.sma_cross import SmaCross
from tests.factories import SYM, instrument

T0 = 1_704_067_200 * 10**9


class QuoteMaker(BaseStrategy):
    """Rests passive limits off the touch every 40 quotes (L1 resting + trade-through fills)."""

    id = "quote_maker"

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def on_quote(self, q: Quote, ctx: Context) -> list[OrderIntent]:
        self.n += 1
        if self.n % 40:
            return []
        pos = ctx.position(q.symbol).qty
        if pos <= 0:
            return [
                OrderIntent(
                    strategy_id=self.id,
                    symbol=q.symbol,
                    side=Side.BUY,
                    type=OrderType.LIMIT,
                    qty=Decimal("0.3"),
                    limit_price=q.bid - Decimal("0.01"),
                )
            ]
        return [
            OrderIntent(
                strategy_id=self.id,
                symbol=q.symbol,
                side=Side.SELL,
                type=OrderType.LIMIT,
                qty=pos,
                limit_price=q.ask + Decimal("0.01"),
                reduce_only=True,
            )
        ]


def _ts(ev: BaseModel) -> int:
    return int(getattr(ev, "ts_recv", None) or getattr(ev, "ts_close", 0))


def _engine(mode: Mode, clock: SimClock, tmp: Path, name: str) -> Engine:
    friction = FrictionConfig(seed=4, latency_ms=(50.0, 250.0))
    broker = SimBroker(
        account_id="paper-parity",
        venue="fake",
        clock=clock,
        mode=SimMode.L1,
        friction=friction,
        starting_cash=Decimal(10_000),
    )
    risk = RiskConfig(max_position_pct_equity=Decimal(50), max_order_notional_usd=Decimal(5_000))
    eng = Engine(
        mode=mode,
        account_id="paper-parity",
        run_id="parity01",
        clock=clock,
        broker=broker,
        risk_config=risk,
        journal=Journal(tmp / f"{name}.sqlite", mirror=False),
        instruments=InstrumentRegistry([instrument()]),
        portfolio=Portfolio(starting_equity=Decimal(10_000)),
        code_fn=lambda: "c",
    )
    eng.add_strategy(SmaCross(), symbols=(SYM,), params={"fast": 3, "slow": 8, "qty": "0.5"})
    eng.add_strategy(QuoteMaker(), symbols=(SYM,))
    return eng


def _summary(e: Engine) -> tuple[list[Any], list[Any], Decimal]:
    orders = [
        (
            o.client_order_id,
            o.intent.strategy_id,
            o.intent.side,
            o.intent.type,
            o.intent.qty,
            o.intent.limit_price,
            o.ts_created,
            o.state,
            o.filled_qty,
        )
        for o in sorted(e.om.all(), key=lambda o: o.client_order_id)
    ]
    fills = [
        (f.client_order_id, f.price, f.qty, f.ts, f.liquidity)
        for entry in e.journal.entries(kinds=("Fill",))
        if isinstance(f := entry.decode(), Fill)
    ]
    return orders, fills, e.portfolio.equity()


def test_backtest_and_paper_produce_identical_orders(tmp_path: Path) -> None:
    async def record() -> Sequence[BaseModel]:
        rec = Recorder(tmp_path, "feed", flush_every=5000)
        async for ev in synthetic_feed(SYM, n_events=12_000, start_ns=T0, seed=21):
            rec.record(ev)
        rec.close()
        return list(replay(rec.dir))

    events = asyncio.run(record())
    assert sum(isinstance(e, Bar) for e in events) > 30

    # --- event-driven backtest driver: synchronous loop, SimClock follows the data
    async def backtest() -> Engine:
        clock = SimClock(T0)
        eng = _engine(Mode.BACKTEST, clock, tmp_path, "bt")
        for ev in events:
            clock.advance_to(max(clock.now(), _ts(ev)))  # bars arrive when built, after ts_close
            await dispatch(eng, ev)
        return eng

    # --- paper runner: async feed task + supervision/reconciliation timers
    async def paper() -> Engine:
        clock = SimClock(T0)
        eng = _engine(Mode.PAPER, clock, tmp_path, "paper")

        async def feed() -> AsyncIterator[BaseModel]:
            for i, ev in enumerate(events):
                clock.advance_to(max(clock.now(), _ts(ev)))  # "now" = arrival time of each event
                yield ev
                if i % 200 == 0:
                    await asyncio.sleep(0)  # let the timers interleave, as they would live

        runner = PaperRunner(
            eng, feed(), config=RunnerConfig(supervise_every_s=0.001, reconcile_every_s=0.005)
        )
        await runner.run()
        return eng

    bt = asyncio.run(backtest())
    pp = asyncio.run(paper())
    bt_orders, bt_fills, bt_eq = _summary(bt)
    pp_orders, pp_fills, pp_eq = _summary(pp)
    assert len(bt_orders) > 10 and len(bt_fills) > 5
    assert {o[1] for o in bt_orders} == {"sma_cross", "quote_maker"}
    assert pp_orders == bt_orders  # 100% identical order sequence
    assert pp_fills == bt_fills
    assert abs(pp_eq - bt_eq) / bt_eq * 10_000 < 1  # PnL delta < 1 bp
    # paper risk decisions identical to backtest (PAP-3)
    assert [(d.verdict, d.rule_id) for d in pp.decisions] == [
        (d.verdict, d.rule_id) for d in bt.decisions
    ]
