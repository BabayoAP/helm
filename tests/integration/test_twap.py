"""EXE-5 TWAP: slicer math, per-slice risk, pause deferral without bursts, cancel on
stop/kill (slicer tests with simulated liquidity: FakeExchange fills each slice)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from helm.core.config import RiskConfig
from helm.core.enums import ControlAction, EngineState, OrderState, OrderType, Side
from helm.core.events import ControlCommand
from helm.core.models import Bar, OrderIntent, TwapSpec
from helm.oms.algos import twap_schedule
from helm.strategy.api import BaseStrategy, Context
from tests.factories import SYM
from tests.integration.test_engine import Rig

D = Decimal


def test_schedule_quantizes_to_lot_and_keeps_total() -> None:
    plan = twap_schedule(D("1.0003"), 4, 0, 400, D("0.0001"))
    assert [t for t, _ in plan] == [0, 100, 200, 300]
    assert [q for _, q in plan] == [D("0.25"), D("0.25"), D("0.25"), D("0.2503")]
    assert sum(q for _, q in plan) == D("1.0003")
    tiny = twap_schedule(D("0.0002"), 5, 0, 500, D("0.0001"))
    assert tiny == [(400, D("0.0002"))]  # zero slices dropped, nothing lost
    with pytest.raises(ValueError):
        twap_schedule(D(1), 0, 0, 1, D("0.1"))
    with pytest.raises(ValidationError, match="TWAP"):
        OrderIntent(
            strategy_id="s",
            symbol=SYM,
            side=Side.BUY,
            type=OrderType.STOP,
            qty=D(1),
            stop_price=D(1),
            exec_algo=TwapSpec(slices=2, duration_s=60),
        )


class TwapOnce(BaseStrategy):
    id = "twap"

    def __init__(self, qty: str, slices: int, duration_s: int) -> None:
        super().__init__()
        self.args = (qty, slices, duration_s)
        self.fired = False

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        if self.fired:
            return []
        self.fired = True
        qty, slices, dur = self.args
        return [
            OrderIntent(
                strategy_id=self.id,
                symbol=bar.symbol,
                side=Side.BUY,
                type=OrderType.MARKET,
                qty=D(qty),
                exec_algo=TwapSpec(slices=slices, duration_s=dur),
            )
        ]


def _rig(tmp: Path, qty: str, slices: int, dur: int, risk: RiskConfig | None = None) -> Rig:
    rig = Rig(tmp, risk=risk)
    rig.engine.add_strategy(TwapOnce(qty, slices, dur), symbols=(SYM,), params={})
    return rig


def _children(rig: Rig) -> list[str]:
    return [o.intent.tag for o in rig.engine.om.all()]


def test_twap_slices_over_time_each_risk_checked(tmp_path: Path) -> None:
    async def run() -> None:
        # the parent (4 * 100 = 400 USD) is above the per-order cap; every slice is under it
        rig = _rig(tmp_path, "4", 4, 180, RiskConfig(max_order_notional_usd=D(150)))
        for _ in range(6):  # bars every 60 s; slices due at 0, 45, 90, 135 s
            await rig.tick()
        tags = _children(rig)
        assert [t.split(":")[-1] for t in tags] == ["0", "1", "2", "3"]
        assert all(t.startswith("twap:r1-twap-1") for t in tags)
        assert rig.engine.portfolio.positions[SYM].qty == D(4)
        assert all(o.state is OrderState.FILLED for o in rig.engine.om.all())
        assert rig.engine.algos == []
        # risk saw each slice individually (4 approvals, parent never sent as one order)
        assert rig.engine.stats.approved == 4 and rig.engine.stats.rejected == 0

    asyncio.run(run())


def test_slice_over_a_limit_is_rejected_alone(tmp_path: Path) -> None:
    async def run() -> None:
        rig = _rig(tmp_path, "3", 2, 60, RiskConfig(max_order_notional_usd=D(120)))
        for _ in range(4):
            await rig.tick()
        # 1.5 * 100 = 150 > 120: both slices rejected individually, nothing traded
        assert rig.engine.stats.rejected == 2 and rig.engine.om.all() == []

    asyncio.run(run())


def test_pause_defers_without_burst_and_stop_or_kill_cancels(tmp_path: Path) -> None:
    async def run() -> None:
        rig = _rig(tmp_path, "4", 4, 240)  # one slice per minute
        await rig.tick()  # slice 0
        await rig.engine.pause("operator")
        for _ in range(3):  # three slices fall due while paused
            await rig.tick()
        assert len(_children(rig)) == 1 and rig.engine.algos[0].deferred > 0
        await rig.engine.control(
            ControlCommand(action=ControlAction.RESUME, source="cli", code=None, ts=0)
        )
        if rig.engine.ks.state is not EngineState.RUNNING:  # PAUSE needs no code to resume
            pytest.fail("resume from PAUSED failed")
        await rig.tick()
        assert len(_children(rig)) == 2  # one slice, not a burst of three
        await rig.engine.control(
            ControlCommand(action=ControlAction.STOP, source="cli", target="twap", ts=0)
        )
        assert rig.engine.algos == []
        await rig.tick()
        assert len(_children(rig)) == 2

        rig2 = _rig(tmp_path / "k", "4", 4, 240)
        await rig2.tick()
        await rig2.engine.halt("drill")  # kill: flatten + HALT drops remaining slices
        assert rig2.engine.algos == []

    asyncio.run(run())


def test_flatten_intents_never_go_through_twap(tmp_path: Path) -> None:
    async def run() -> None:
        rig = _rig(tmp_path, "2", 2, 60)
        for _ in range(3):
            await rig.tick()
        assert rig.engine.portfolio.positions[SYM].qty == D(2)
        assert await rig.engine.flatten()
        await rig.pump()
        assert rig.engine.portfolio.positions[SYM].qty == 0
        assert rig.engine.cancel_algos("nobody") == 0

    asyncio.run(run())


# ---------------------------------------------------------- Phase 3: VWAP, Iceberg
from helm.core.models import IcebergSpec, VwapSpec  # noqa: E402


class AlgoOnce(BaseStrategy):
    id = "algo"

    def __init__(self, intent: OrderIntent) -> None:
        super().__init__()
        self.intent = intent
        self.fired = False

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        if self.fired:
            return []
        self.fired = True
        return [self.intent]


def _parent(
    qty: str, algo: object, typ: OrderType = OrderType.MARKET, limit: str | None = None
) -> OrderIntent:
    return OrderIntent(
        strategy_id="algo",
        symbol=SYM,
        side=Side.BUY,
        type=typ,
        qty=D(qty),
        limit_price=D(limit) if limit else None,
        exec_algo=algo,  # type: ignore[arg-type]
    )


def test_vwap_participates_in_bar_volume_and_finishes_at_deadline(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)  # Rig bars carry volume 1
        spec = VwapSpec(participation=D("0.5"), duration_s=300)
        rig.engine.add_strategy(AlgoOnce(_parent("3", spec)), symbols=(SYM,), params={})
        for _ in range(4):
            await rig.tick()
        qtys = [o.intent.qty for o in rig.engine.om.all()]
        assert qtys and all(q == D("0.5") for q in qtys)  # 50% of each 1-unit bar
        assert all(o.intent.tag.startswith("vwap:r1-vwap-1") for o in rig.engine.om.all())
        for _ in range(3):  # past the 300 s deadline: the remainder goes in one slice
            await rig.tick()
        assert sum(o.intent.qty for o in rig.engine.om.all()) == D(3)
        assert rig.engine.algos == []

        rig2 = Rig(tmp_path / "nofinish")
        drop = VwapSpec(participation=D("0.1"), duration_s=60, finish=False)
        rig2.engine.add_strategy(AlgoOnce(_parent("3", drop)), symbols=(SYM,), params={})
        for _ in range(4):
            await rig2.tick()
        assert rig2.engine.algos == []
        assert sum(o.intent.qty for o in rig2.engine.om.all()) < D(3)
        assert any("remainder dropped" in a.message for a in rig2.engine.alerts.sent)

    asyncio.run(run())


def test_iceberg_shows_one_slice_at_a_time_and_stops_on_cancel(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        spec = IcebergSpec(display_qty=D(1))
        rig.engine.add_strategy(
            AlgoOnce(_parent("3", spec, OrderType.LIMIT, "100.5")), symbols=(SYM,), params={}
        )
        await rig.tick("100")  # slice 1 posted and (marketable) filled
        await rig.tick("100")
        await rig.tick("100")
        await rig.tick("100")
        orders = rig.engine.om.all()
        assert [o.intent.qty for o in orders] == [D(1), D(1), D(1)]
        assert all(o.intent.type is OrderType.LIMIT for o in orders)
        assert rig.engine.portfolio.positions[SYM].qty == D(3)
        assert rig.engine.algos == []

        # a resting slice that gets canceled stops the iceberg (no silent re-posting)
        rig2 = Rig(tmp_path / "c")
        rig2.engine.add_strategy(
            AlgoOnce(_parent("3", spec, OrderType.LIMIT, "99")), symbols=(SYM,), params={}
        )
        await rig2.tick("100")
        (first,) = rig2.engine.om.all()
        await rig2.engine._cancel(first.client_order_id)  # pyright: ignore[reportPrivateUsage]
        await rig2.pump()
        await rig2.tick("100")
        assert len(rig2.engine.om.all()) == 1 and rig2.engine.algos == []
        assert any("iceberg stopped" in a.message for a in rig2.engine.alerts.sent)

    asyncio.run(run())


def test_algo_spec_validation() -> None:
    with pytest.raises(ValidationError, match="iceberg must be a limit"):
        _parent("1", IcebergSpec(display_qty=D(1)))
    with pytest.raises(ValidationError):
        VwapSpec(participation=D("0.9"), duration_s=60)  # > 50% of volume is not VWAP
    it = _parent("2", {"kind": "vwap", "participation": "0.2", "duration_s": 60})
    assert isinstance(it.exec_algo, VwapSpec)  # discriminated by kind (journal round-trip)
    assert OrderIntent.model_validate_json(it.model_dump_json()) == it
