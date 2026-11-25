"""M3 engine behaviour: kill switch drills, breakers wiring, reconciliation, UNKNOWN orders,
lifecycle controls (EXE-2), runtime risk changes (EXE-7), gates (TEST-7), lock (EXE-8),
alerts (OPS-2)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from helm.app.engine import Engine
from helm.app.gates import LIVE_ENV, build_broker, missing_live_gates
from helm.brokers.fake_exchange import FakeExchange, FakeExchangeConfig
from helm.brokers.live.base import LiveBrokerBase
from helm.brokers.sim.broker import SimBroker
from helm.core.clock import SimClock
from helm.core.config import BotConfig, RiskConfig
from helm.core.enums import (
    ControlAction,
    EngineState,
    FeedState,
    LifecycleStage,
    Mode,
    OrderState,
    OrderType,
    Severity,
    Side,
)
from helm.core.errors import AccountLockError, LiveGateError
from helm.core.events import ControlCommand, FeedStatus
from helm.core.models import Bar, OrderIntent, Quote
from helm.data.instruments import InstrumentRegistry
from helm.infra.lock import AccountLock
from helm.oms.accounting import Portfolio
from helm.oms.journal import Journal
from helm.strategy.api import BaseStrategy, Context
from helm.tracking.alerts import AlertManager
from tests.factories import SYM, instrument

NS = 10**9
D = Decimal


class Scripted(BaseStrategy):
    """Emits pre-programmed intents on the given bar numbers."""

    id = "scripted"

    def __init__(self, plan: dict[int, list[OrderIntent]], raise_on: int | None = None) -> None:
        super().__init__()
        self.plan = plan
        self.n = -1
        self.raise_on = raise_on

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderIntent]:
        self.n += 1
        if self.n == self.raise_on:
            raise RuntimeError("boom")
        return self.plan.get(self.n, [])


def buy(q: str = "1", **kw: Any) -> OrderIntent:
    return OrderIntent(
        strategy_id="scripted", symbol=SYM, side=Side.BUY, type=OrderType.MARKET, qty=D(q), **kw
    )


class Rig:
    def __init__(
        self,
        tmp: Path,
        *,
        risk: RiskConfig | None = None,
        equity: str = "10000",
    ) -> None:
        self.clock = SimClock(1_000 * NS)
        self.fx = FakeExchange(
            account_id="paper-t",
            config=FakeExchangeConfig(starting_cash=D(equity)),
            clock=self.clock,
        )
        self.engine = Engine(
            mode=Mode.PAPER,
            account_id="paper-t",
            run_id="r1",
            clock=self.clock,
            broker=self.fx,  # type: ignore[arg-type]
            risk_config=risk or RiskConfig(),
            journal=Journal(tmp / "j.sqlite", mirror=False),
            instruments=InstrumentRegistry([instrument()]),
            portfolio=Portfolio(starting_equity=D(equity)),
            code_fn=lambda: "resume-code",
        )

    async def tick(self, px: str = "100", *, bar: bool = True) -> None:
        self.clock.advance_by(60 * NS)
        p = D(px)
        self.fx.set_price(SYM, p, p + D("0.01"))
        now = self.clock.now()
        await self.engine.on_quote(
            Quote(
                symbol=SYM,
                bid=p,
                bid_sz=D(9),
                ask=p + D("0.01"),
                ask_sz=D(9),
                ts_event=now,
                ts_recv=now,
            )
        )
        if bar:
            await self.engine.on_bar(
                Bar(
                    symbol=SYM,
                    timeframe="1m",
                    ts_open=now - 60 * NS,
                    ts_close=now,
                    o=p,
                    h=p,
                    l=p,
                    c=p,
                    volume=D(1),
                )
            )
        await self.pump()

    async def pump(self) -> None:
        for ev in self.fx.drain_events():
            await self.engine.on_broker_event(ev)


def test_kill_flattens_and_latches_halt(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.engine.add_strategy(Scripted({0: [buy()], 1: [buy("0.2")]}), symbols=(SYM,))
        await rig.tick()
        await rig.tick()
        assert (await rig.fx.positions())[0].qty == D("1.2")
        rest = OrderIntent(
            strategy_id="scripted",
            symbol=SYM,
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=D(1),
            limit_price=D(99),
        )
        await rig.engine.submit_intent(rig.engine.slots[0], rest)
        await rig.pump()
        assert len(await rig.fx.open_orders()) == 1
        result = await rig.engine.control(
            ControlCommand(action=ControlAction.KILL, source="telegram:1", ts=rig.clock.now())
        )
        await rig.pump()
        assert result == "halted" and rig.engine.ks.state is EngineState.HALTED
        assert await rig.fx.positions() == [] and await rig.fx.open_orders() == []  # verified flat
        assert rig.engine.portfolio.position(SYM).qty == 0
        assert rig.engine.last_resume_code == "resume-code"
        # HALTED: strategies can't open risk; resume needs the one-time code
        assert await rig.engine.submit_intent(rig.engine.slots[0], buy()) is None
        assert (
            rig.engine.decisions[-1].rule_id == "RISK-1"
            and "HALTED" in rig.engine.decisions[-1].reason
        )
        assert (
            await rig.engine.control(ControlCommand(action=ControlAction.RESUME, source="ui", ts=0))
            == "denied"
        )
        assert (
            await rig.engine.control(
                ControlCommand(action=ControlAction.RESUME, source="ui", code="resume-code", ts=0)
            )
            == "running"
        )
        assert rig.engine.ks.state is EngineState.RUNNING
        kinds = {e.kind for e in rig.engine.journal.entries()}
        assert {
            "ControlCommand",
            "EngineStateChanged",
            "RiskDecision",
            "IntentEvent",
            "OrderEvent",
            "Fill",
        } <= kinds

    asyncio.run(run())


def test_daily_loss_breaker_flattens_and_halts(tmp_path: Path) -> None:
    async def run() -> None:
        risk = RiskConfig(
            max_position_pct_equity=D(100), max_order_notional_usd=D(10**6), max_gross_leverage=D(5)
        )
        rig = Rig(tmp_path, risk=risk)
        rig.engine.add_strategy(Scripted({0: [buy("50")]}), symbols=(SYM,))
        await rig.tick("100")
        await rig.tick("100")
        assert rig.engine.portfolio.position(SYM).qty == 50
        await rig.tick("93")  # 50 * -7 = -350 = -3.5% of equity > 3% daily limit
        await rig.pump()
        assert rig.engine.ks.state is EngineState.HALTED
        assert await rig.fx.positions() == []
        assert any(
            a.severity is Severity.CRITICAL and a.key == "RISK-10" for a in rig.engine.alerts.sent
        )

    asyncio.run(run())


def test_stale_feed_blocks_entries_and_cancels(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.engine.add_strategy(Scripted({}), symbols=(SYM,))
        await rig.tick()
        rest = OrderIntent(
            strategy_id="scripted",
            symbol=SYM,
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=D(1),
            limit_price=D(99),
        )
        await rig.engine.submit_intent(rig.engine.slots[0], rest)
        await rig.pump()
        assert len(await rig.fx.open_orders()) == 1
        await rig.engine.on_feed_status(
            FeedStatus(venue="fake", symbol=None, state=FeedState.STALE, ts=rig.clock.now())
        )
        await rig.pump()
        assert (
            SYM in rig.engine.blocked and await rig.fx.open_orders() == []
        )  # RISK-12: cancel + block
        assert await rig.engine.submit_intent(rig.engine.slots[0], buy()) is None
        assert rig.engine.decisions[-1].rule_id == "RISK-3"
        await rig.engine.on_feed_status(
            FeedStatus(venue="fake", symbol=None, state=FeedState.LIVE, ts=rig.clock.now())
        )
        for _ in range(12):  # healthy for >= feed_healthy_clear_s (10 s)
            rig.clock.advance_by(NS)
            await rig.tick(bar=False)
        await rig.engine.supervise()
        assert SYM not in rig.engine.blocked

    asyncio.run(run())


def test_reconcile_mismatch_pauses_within_one_cycle(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.engine.add_strategy(Scripted({}), symbols=(SYM,))
        await rig.tick()
        assert (await rig.engine.reconcile()).ok
        rig.fx.inject_position(SYM, D("0.5"), D(100))  # EXE-4: injected discrepancy
        res = await rig.engine.reconcile()
        assert not res.ok and rig.engine.ks.state is EngineState.PAUSED
        assert any(a.key == "RISK-13" for a in rig.engine.alerts.sent)

    asyncio.run(run())


def test_ambiguous_submit_resolved_by_lookup_never_blind_retry(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.fx.config.p_ambiguous_submit = 1.0  # type: ignore[union-attr]
        rig.engine.add_strategy(Scripted({0: [buy()]}), symbols=(SYM,))
        await rig.tick()
        order = rig.engine.om.all()[0]
        assert rig.fx.submit_calls[order.client_order_id] == 1  # type: ignore[union-attr]
        assert (
            rig.engine.om.get(order.client_order_id).state is OrderState.FILLED
        )  # adopted from venue lookup

    asyncio.run(run())


def test_unknown_absent_order_resubmitted_with_same_id(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.engine.add_strategy(Scripted({}), symbols=(SYM,))
        await rig.tick()
        o = rig.engine.om.create(buy())
        rig.engine.om.mark_pending(o.client_order_id)
        rig.engine.om.mark_unknown(
            o.client_order_id, "timeout"
        )  # e.g. request never reached the venue
        await rig.engine.resolve_unknown()
        await rig.pump()
        assert rig.fx.accepted_submits == {o.client_order_id: 1}  # type: ignore[union-attr]

    asyncio.run(run())


def test_lifecycle_controls_idempotent_and_strategy_errors_pause(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        rig.engine.add_strategy(Scripted({}, raise_on=1), symbols=(SYM,))
        await rig.tick()
        await rig.tick()
        slot = rig.engine.slots[0]
        assert slot.paused and rig.engine.stats.strategy_errors == 1
        c = rig.engine.control
        for _ in range(2):
            assert (
                await c(ControlCommand(action=ControlAction.START, source="ui", ts=0)) == "started"
            )
        assert not slot.paused
        for _ in range(2):
            assert (
                await c(
                    ControlCommand(action=ControlAction.STOP, source="ui", target="scripted", ts=0)
                )
                == "stopped"
            )
        for _ in range(2):
            assert (
                await c(ControlCommand(action=ControlAction.PAUSE, source="ui", ts=0)) == "paused"
            )
        assert rig.engine.ks.state is EngineState.PAUSED
        for _ in range(2):
            assert (
                await c(ControlCommand(action=ControlAction.FLATTEN, source="ui", ts=0)) == "flat"
            )
        for _ in range(2):
            assert (
                await c(ControlCommand(action=ControlAction.RESUME, source="ui", ts=0)) == "running"
            )

    asyncio.run(run())


def test_consecutive_rejects_auto_pause(tmp_path: Path) -> None:
    async def run() -> None:
        rig = Rig(tmp_path)
        bad = [
            OrderIntent(
                strategy_id="scripted",
                symbol="fake:NOPE/USD",
                side=Side.BUY,
                type=OrderType.MARKET,
                qty=D(1),
            )
        ]
        rig.engine.add_strategy(Scripted({i: bad for i in range(10)}), symbols=(SYM,))
        for _ in range(6):
            await rig.tick()
        assert rig.engine.slots[0].paused
        assert any(a.key == "rejects:scripted" for a in rig.engine.alerts.sent)

    asyncio.run(run())


def test_risk_tighten_live_loosen_needs_restart(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    cfg = rig.engine.risk_config
    assert rig.engine.update_risk_config(cfg.model_copy(update={"max_gross_leverage": D(1)})) == []
    assert rig.engine.risk_config.max_gross_leverage == 1
    assert rig.engine.update_risk_config(cfg) == ["max_gross_leverage"]  # loosening refused (EXE-7)
    assert rig.engine.risk_config.max_gross_leverage == 1
    changes = [e for e in rig.engine.journal.entries(kinds=("ConfigChanged",))]
    assert len(changes) == 2


# ------------------------------------------------------------------ TEST-7
def _bot(**kw: object) -> BotConfig:
    base: dict[str, object] = {
        "id": "b",
        "strategy": "x:Y",
        "mode": Mode.LIVE,
        "account_id": "live-1",
        "venue": "fake",
        "symbols": (SYM,),
        "lifecycle": LifecycleStage.LIVE_SMALL,
    }
    base.update(kw)
    return BotConfig.model_validate(base)


class _FakeLive(LiveBrokerBase):
    pass


def _factory(bot: BotConfig, token: object) -> Any:
    return _FakeLive(account_id=bot.account_id, venue=bot.venue, _gate_token=token)


def _wrong_account(bot: BotConfig, token: object) -> Any:
    return _FakeLive(account_id="live-other", venue="x", _gate_token=token)


def test_each_live_gate_blocks_and_names_itself() -> None:
    clock = SimClock()
    env_on = {LIVE_ENV: "1"}
    live = build_broker(_bot(), env=env_on, clock=clock, live_factory=_factory)
    assert live.is_live
    with pytest.raises(LiveGateError, match=r"gate \(a\)"):
        build_broker(_bot(), env={}, clock=clock, live_factory=_factory)
    with pytest.raises(LiveGateError, match=r"gate \(c\)"):
        build_broker(
            _bot(lifecycle=LifecycleStage.PAPER), env=env_on, clock=clock, live_factory=_factory
        )
    paper = _bot(mode=Mode.PAPER, account_id="paper-1")
    assert isinstance(
        build_broker(paper, env=env_on, clock=clock, live_factory=_factory), SimBroker
    )
    assert any("gate (b)" in m for m in missing_live_gates(paper, env_on))
    assert len(missing_live_gates(_bot(lifecycle=LifecycleStage.DRAFT), {})) == 2
    with pytest.raises(LiveGateError, match="no live adapter"):
        build_broker(_bot(), env=env_on, clock=clock)
    with pytest.raises(LiveGateError, match="only be built"):
        _FakeLive(account_id="live-1", venue="x", _gate_token=object())
    with pytest.raises(LiveGateError, match="wrong account"):
        build_broker(_bot(), env=env_on, clock=clock, live_factory=_wrong_account)


def test_paper_account_can_never_reach_a_live_broker() -> None:
    from helm.app.gates import GATE_TOKEN

    with pytest.raises(ValueError, match="paper"):
        _bot(account_id="paper-sneaky")  # live bot with paper account: config rejected
    with pytest.raises(ValueError, match="paper-"):
        _bot(mode=Mode.PAPER, account_id="live-1")
    with pytest.raises(LiveGateError, match="PAP-2"):
        _FakeLive(account_id="paper-1", venue="x", _gate_token=GATE_TOKEN)


# ------------------------------------------------------------------ EXE-8
def test_single_writer_lock_across_processes(tmp_path: Path) -> None:
    with AccountLock(tmp_path, "paper-main"):
        code = (
            "from helm.infra.lock import AccountLock; "
            f"AccountLock({str(tmp_path)!r}, 'paper-main').acquire()"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        assert out.returncode != 0 and "already traded by another engine" in out.stderr
        with pytest.raises(AccountLockError):
            AccountLock(tmp_path, "paper-main").acquire()
        AccountLock(tmp_path, "paper-other").acquire()  # other accounts unaffected
    lock = AccountLock(tmp_path, "paper-main")
    lock.acquire()  # released on exit
    lock.release()


# ------------------------------------------------------------------ OPS-2
def test_alert_storm_is_throttled() -> None:
    now = [0]
    am = AlertManager(mode=Mode.PAPER, clock_fn=lambda: now[0])
    for i in range(1000):
        now[0] = i * 10**7  # 1000 events over 10 s
        am.critical("order_rejects", "order rejection burst")
    assert len(am.sent) <= 2 and am.suppressed_total >= 998
    am.flush_suppressed()
    assert "suppressed" in am.sent[-1].message and am.sent[-1].text.startswith("[PAPER]")
    now[0] += 31 * 10**9
    assert am.critical("order_rejects", "again")
    am.info("i", "x")
    assert not am.info("i", "x") and am.warn("w", "x")
    broken = AlertManager(mode=Mode.LIVE, clock_fn=lambda: 0, sinks=[lambda a: 1 / 0])  # type: ignore[arg-type,misc]
    assert broken.critical("k", "sink errors never propagate")
