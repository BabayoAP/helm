"""Engine construction + startup recovery (EXE-3, I4, success metric "kill -9").

On start the engine rebuilds orders, fill-dedupe keys, positions and the
kill-switch state from the journal, then asks the broker what really happened
while it was down:
- orders the venue knows: missed fills are fetched and applied (deduped), and
  terminal states adopted;
- PENDING_SUBMIT / UNKNOWN orders the venue does not know: marked REJECTED
  (a stale intent is never resubmitted after a restart);
- anything still unexplained (positions or open orders) => refuse to trade
  unless `adopt=True` (take the venue's state as-is, journaled) or
  `flatten_first=True` (flatten, then start).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from helm.app.engine import Engine
from helm.brokers.base import Broker
from helm.core.clock import Clock
from helm.core.config import RiskConfig
from helm.core.enums import EngineState, Mode, OrderState
from helm.core.errors import HelmError
from helm.core.events import EngineStateChanged, PositionSnapshot
from helm.core.models import Fill, Position
from helm.data.instruments import InstrumentRegistry
from helm.oms.accounting import Portfolio, PositionState
from helm.oms.journal import Journal
from helm.oms.order_manager import OrderManager
from helm.oms.reconciler import reconcile
from helm.risk.killswitch import KillSwitch
from helm.tracking.alerts import AlertManager


class UnexpectedStateError(HelmError):
    """Broker state the journal cannot explain; start with --adopt or --flatten-first."""


@dataclass
class RecoveryReport:
    previous_state: EngineState | None = None
    orders_recovered: int = 0
    fills_from_venue: int = 0
    stale_pending_rejected: int = 0
    states_adopted: int = 0
    unexpected: list[str] = field(default_factory=lambda: [])
    adopted: bool = False
    flattened: bool = False


def rebuild_portfolio(
    journal: Journal, account_id: str, starting_equity: Decimal, multipliers: dict[str, Decimal]
) -> Portfolio:
    pf = Portfolio(starting_equity=starting_equity, multipliers=dict(multipliers))
    for entry in journal.entries(kinds=("Fill", "PositionSnapshot"), account_id=account_id):
        ev = entry.decode()
        if isinstance(ev, Fill):
            pf.apply_fill(ev)
        elif isinstance(ev, PositionSnapshot):  # adoption record (EXE-3 --adopt)
            for p in ev.positions:
                pf.positions[p.symbol] = PositionState(
                    qty=p.qty,
                    cost=p.qty * p.avg_px,
                    realized_pnl=pf.positions.get(p.symbol, PositionState()).realized_pnl,
                )
    return pf


def last_engine_state(journal: Journal, account_id: str) -> EngineState | None:
    state: EngineState | None = None
    for entry in journal.entries(kinds=("EngineStateChanged",), account_id=account_id):
        ev = entry.decode()
        assert isinstance(ev, EngineStateChanged)
        state = ev.current
    return state


def last_run_id(journal: Journal, account_id: str) -> str | None:
    rid: str | None = None
    for entry in journal.entries(account_id=account_id):
        rid = entry.run_id
    return rid


def build_engine(
    *,
    mode: Mode,
    account_id: str,
    run_id: str,
    clock: Clock,
    broker: Broker,
    journal: Journal,
    instruments: InstrumentRegistry,
    risk_config: RiskConfig,
    starting_equity: Decimal,
    code_fn: Callable[[], str],
    alerts: AlertManager | None = None,
) -> tuple[Engine, EngineState | None]:
    """Engine whose OMS, portfolio and kill switch are rebuilt from the journal."""
    run_id = last_run_id(journal, account_id) or run_id
    mults = {i.symbol: i.contract_multiplier for i in instruments.all()}
    om = OrderManager.recover(run_id=run_id, account_id=account_id, journal=journal, clock=clock)
    portfolio = rebuild_portfolio(journal, account_id, starting_equity, mults)
    previous = last_engine_state(journal, account_id)
    ks = (
        KillSwitch.after_restart(previous, code_fn, clock.now)
        if previous is not None
        else KillSwitch(code_fn, clock.now)
    )
    engine = Engine(
        mode=mode,
        account_id=account_id,
        run_id=run_id,
        clock=clock,
        broker=broker,
        risk_config=risk_config,
        journal=journal,
        instruments=instruments,
        portfolio=portfolio,
        killswitch=ks,
        alerts=alerts,
        order_manager=om,
        code_fn=code_fn,
    )
    if previous is not None and ks.state is not previous:
        engine._record_state(
            EngineStateChanged(
                previous=previous, current=ks.state, reason="restart", ts=clock.now()
            )
        )  # pyright: ignore[reportPrivateUsage]
    return engine, previous


async def recover(
    engine: Engine,
    *,
    adopt: bool = False,
    flatten_first: bool = False,
    previous: EngineState | None = None,
) -> RecoveryReport:
    rep = RecoveryReport(previous_state=previous, orders_recovered=len(engine.om.all()))
    om, broker, pf = engine.om, engine.broker, engine.portfolio
    for order in list(om.all()):
        if order.state.is_terminal:
            continue
        venue = await broker.get_order(order.client_order_id)
        if venue is None:
            if order.state in (OrderState.NEW, OrderState.PENDING_SUBMIT, OrderState.UNKNOWN):
                om.mark_rejected(
                    order.client_order_id,
                    "not at venue after restart; stale intent not resubmitted",
                )
                rep.stale_pending_rejected += 1
            else:
                rep.unexpected.append(
                    f"order {order.client_order_id} ({order.state}) unknown to venue"
                )
            continue
        for f in await broker.fills_for_order(order.client_order_id):
            if om.on_fill(f):
                pf.apply_fill(f)
                rep.fills_from_venue += 1
        if om.adopt_state(venue, "restart reconciliation") is not None:
            rep.states_adopted += 1
    venue_positions = await broker.positions()
    marks = {
        p.symbol: (pf.positions[p.symbol].mark or p.avg_px)
        if p.symbol in pf.positions
        else p.avg_px
        for p in venue_positions
    }
    res = reconcile(
        {s: p.qty for s, p in pf.positions.items()},
        venue_positions,
        om.open_orders(),
        await broker.open_orders(),
        marks,
        Decimal(0),  # at startup any difference is unexpected
    )
    rep.unexpected.extend(res.mismatches)
    if rep.unexpected:
        if flatten_first:
            await engine.cancel_all()
            for o in await broker.open_orders():
                if om.find(o.client_order_id) is None:
                    await broker.cancel(o.client_order_id)
            await broker.flatten_all(reduce_only=True)
            rep.flattened = True
            _adopt_positions(engine, await broker.positions(), "flatten-first")
        elif adopt:
            _adopt_positions(engine, venue_positions, "adopt")
            rep.adopted = True
        else:
            raise UnexpectedStateError(
                "refusing to trade; unexplained broker state: " + "; ".join(rep.unexpected)
            )
    return rep


async def resync(engine: Engine) -> int:
    """After a user-stream reconnect (EXE-4): fetch what we may have missed for every
    working order (fills are deduped, so replays are harmless), adopt terminal
    states, then reconcile. Returns the number of new fills applied."""
    applied = 0
    for order in list(engine.om.working_orders()):
        coid = order.client_order_id
        for f in await engine.broker.fills_for_order(coid):
            before = engine.stats.fills
            await engine.on_broker_event(f)
            applied += engine.stats.fills - before
        venue = await engine.broker.get_order(coid)
        if venue is not None and engine.om.get(coid).state is not venue.state:
            engine.om.adopt_state(venue, "resync after reconnect")
    await engine.reconcile()
    return applied


def _adopt_positions(engine: Engine, positions: list[Position], reason: str) -> None:
    snap = PositionSnapshot(
        account_id=engine.account_id, positions=tuple(positions), ts=engine.clock.now()
    )
    engine.journal.append(
        snap, ts=snap.ts, run_id=engine.run_id, account_id=engine.account_id, key=f"adopt:{reason}"
    )
    held = {p.symbol for p in positions}
    for sym, st in engine.portfolio.positions.items():
        if sym not in held:
            st.qty, st.cost = Decimal(0), Decimal(0)
    for p in positions:
        prev = engine.portfolio.positions.get(p.symbol, PositionState())
        engine.portfolio.positions[p.symbol] = PositionState(
            qty=p.qty, cost=p.qty * p.avg_px, realized_pnl=prev.realized_pnl, mark=prev.mark
        )
    engine.alerts.critical(
        f"recovery:{reason}",
        f"startup recovery: venue state {reason}ed: {[(p.symbol, str(p.qty)) for p in positions]}",
    )
