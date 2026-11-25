"""EXE-6 / RISK-8: per-strategy allocation and daily loss caps, global caps still apply;
per-strategy attribution survives a restart."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from helm.app.lifecycle import build_engine
from helm.brokers.fake_exchange import FakeExchange, FakeExchangeConfig
from helm.core.clock import SimClock
from helm.core.config import RiskConfig
from helm.core.enums import EngineState, Mode, OrderType, Side
from helm.core.models import Bar, OrderIntent
from helm.data.instruments import InstrumentRegistry
from helm.oms.journal import Journal
from helm.strategy.api import BaseStrategy, Context
from tests.factories import SYM, instrument
from tests.integration.test_engine import NS, Rig

D = Decimal


class BuyOnce(BaseStrategy):
    def __init__(self, sid: str, qty: str) -> None:
        super().__init__(sid)
        self.qty = qty
        self.done = False

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        if self.done:
            return []
        self.done = True
        return [
            OrderIntent(
                strategy_id=self.id,
                symbol=SYM,
                side=Side.BUY,
                type=OrderType.MARKET,
                qty=D(self.qty),
            )
        ]


def test_loss_cap_pauses_one_strategy_and_allocation_caps_entries(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path, risk=RiskConfig(max_position_pct_equity=D(100), fat_finger_pct=D(50)))
        e = rig.engine
        loser = e.add_strategy(BuyOnce("loser", "4"), symbols=(SYM,), daily_loss_cap=D(50))
        small = e.add_strategy(BuyOnce("small", "4"), symbols=(SYM,), capital_pct=D(3))
        await rig.tick("100")  # loser buys 4 (400 USD); small's 400 USD > its 3% of 10k: RISK-8
        await rig.tick("100")
        assert loser.positions == {SYM: D(4)}
        assert small.positions == {}
        assert any(d.rule_id == "RISK-8" for d in e.decisions)
        await rig.tick("85")  # loser down ~60 > its 50 cap
        assert loser.paused and not small.paused
        assert e.ks.state is EngineState.RUNNING  # only that strategy, not the engine
        assert any(a.key == "loss_cap:loser" for a in e.alerts.sent)
        snap_bots = {b.strategy.id: b for b in e.slots}
        assert snap_bots["loser"].day_pnl < -50

    asyncio.run(run())


def test_strategy_attribution_survives_restart(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path, risk=RiskConfig(max_position_pct_equity=D(100), fat_finger_pct=D(50)))
        rig.engine.add_strategy(BuyOnce("s", "3"), symbols=(SYM,))
        await rig.tick("100")
        await rig.tick("101")
        rig.engine.journal.close()

        clock = SimClock(rig.clock.now() + NS)
        fx = FakeExchange(account_id="paper-t", config=FakeExchangeConfig(), clock=clock)
        eng, _ = build_engine(
            mode=Mode.PAPER,
            account_id="paper-t",
            run_id="r2",
            clock=clock,
            broker=fx,  # type: ignore[arg-type]
            journal=Journal(tmp_path / "j.sqlite", mirror=False),
            instruments=InstrumentRegistry([instrument()]),
            risk_config=RiskConfig(),
            starting_equity=D(10_000),
            code_fn=lambda: "c",
        )
        slot = eng.add_strategy(BuyOnce("s", "3"), symbols=(SYM,))
        assert slot.positions == {SYM: D(3)}  # RISK-8 exposure starts from the truth
        assert slot.pnl.positions[SYM].qty == D(3)
        other = eng.add_strategy(BuyOnce("t", "1"), symbols=(SYM,))
        assert other.positions == {}

    asyncio.run(run())
