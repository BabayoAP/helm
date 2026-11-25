"""The engine loop: one code path for backtest, paper and live (I1).

Only the Broker, Clock and DataFeed differ between modes. Market data enters via
`on_bar` / `on_quote` / `on_trade`; broker updates via `on_broker_event`.
Every strategy intent is journaled, quantized, risk-checked (fail-closed, I2),
journaled again with its decision, and only then turned into an order whose
state is journaled before it is submitted (I4).
"""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from pydantic import ValidationError

from helm.brokers.base import Broker
from helm.core.calendar import is_open
from helm.core.clock import NS_PER_S, Clock
from helm.core.config import RiskConfig
from helm.core.enums import (
    ControlAction,
    EngineState,
    FeedState,
    LifecycleStage,
    Mode,
    OrderState,
    OrderType,
    Side,
)
from helm.core.errors import AmbiguousSubmitError, BrokerError, IllegalTransitionError
from helm.core.events import (
    ConfigChanged,
    ControlCommand,
    EngineStateChanged,
    FeedStatus,
    IntentEvent,
    OrderEvent,
)
from helm.core.models import Bar, Fill, Instrument, Order, OrderIntent, Quote, RiskDecision, Trade
from helm.data.instruments import InstrumentRegistry
from helm.oms.accounting import Portfolio
from helm.oms.journal import Journal
from helm.oms.order_manager import OrderManager
from helm.oms.reconciler import ReconcileResult, reconcile
from helm.risk.breakers import Action, BreakerInputs, FeedHealth, Trip, check_breakers, day_start_ns
from helm.risk.killswitch import KillSwitch, ResumeDeniedError
from helm.risk.manager import RiskManager
from helm.risk.types import FlowState, MarketState, PortfolioState
from helm.strategy.api import ParamValue, Strategy
from helm.strategy.context import BarHistory, EngineContext
from helm.tracking.alerts import AlertManager

NS_PER_MIN = 60 * NS_PER_S


@dataclass
class StrategySlot:
    strategy: Strategy
    ctx: EngineContext
    lifecycle: LifecycleStage
    symbols: tuple[str, ...]
    paused: bool = False
    consecutive_rejects: int = 0
    order_times: deque[int] = field(default_factory=lambda: deque(maxlen=1000))
    positions: dict[str, Decimal] = field(default_factory=lambda: {})


@dataclass
class EngineStats:
    intents: int = 0
    approved: int = 0
    rejected: int = 0
    submitted: int = 0
    fills: int = 0
    strategy_errors: int = 0


class Engine:
    def __init__(
        self,
        *,
        mode: Mode,
        account_id: str,
        run_id: str,
        clock: Clock,
        broker: Broker,
        risk_config: RiskConfig,
        journal: Journal,
        instruments: InstrumentRegistry,
        portfolio: Portfolio,
        killswitch: KillSwitch | None = None,
        alerts: AlertManager | None = None,
        order_manager: OrderManager | None = None,
        code_fn: Callable[[], str] | None = None,
    ) -> None:
        self.mode = mode
        self.account_id = account_id
        self.run_id = run_id
        self.clock = clock
        self.broker = broker
        self.risk_config = risk_config
        self.risk = RiskManager(risk_config)
        self.journal = journal
        self.instruments = instruments
        self.portfolio = portfolio
        self.alerts = alerts or AlertManager(mode=mode, clock_fn=clock.now)
        self.om = order_manager or OrderManager(
            run_id=run_id, account_id=account_id, journal=journal, clock=clock
        )
        self._code_fn = code_fn or (lambda: secrets.token_hex(4))
        self.ks = killswitch or KillSwitch(self._code_fn, clock.now)
        self.history = BarHistory()
        self.quotes: dict[str, Quote] = {}
        self.feeds: dict[str, FeedHealth] = {}
        self.blocked: set[str] = set()
        self.slots: list[StrategySlot] = []
        self._slot_by_strategy: dict[str, StrategySlot] = {}
        self.decisions: list[RiskDecision] = []
        self.equity_curve: list[tuple[int, Decimal]] = []
        self.stats = EngineStats()
        self.global_order_times: deque[int] = deque(maxlen=5000)
        self.reconcile_result: ReconcileResult | None = None
        self.clock_skew_ms: float | None = 0.0
        self._sod_equity = portfolio.equity()
        self._sod_start: int | None = None
        self._hwm = portfolio.equity()
        self.last_resume_code: str | None = None
        self.flatten_failures = 0
        self.bar_mode_synthetic_quotes = True
        self._real_quote_symbols: set[str] = set()

    # ------------------------------------------------------------- utilities
    def _journal(self, event: Any, key: str = "") -> None:
        self.journal.append(
            event, ts=self.clock.now(), run_id=self.run_id, account_id=self.account_id, key=key
        )

    def add_strategy(
        self,
        strategy: Strategy,
        *,
        symbols: Iterable[str],
        params: Mapping[str, ParamValue] | None = None,
        lifecycle: LifecycleStage = LifecycleStage.DRAFT,
        log: Any = None,
    ) -> StrategySlot:
        slot_positions: dict[str, Decimal] = {}
        ctx = EngineContext(
            strategy.id,
            self.clock,
            self.history,
            params=params,
            position_fn=self.portfolio.position,
            equity_fn=self.portfolio.equity,
            quote_fn=self.quotes.get,
            log=log,
        )
        slot = StrategySlot(strategy, ctx, lifecycle, tuple(symbols), positions=slot_positions)
        self.slots.append(slot)
        self._slot_by_strategy[strategy.id] = slot
        self._call(slot, "on_start", ctx)
        return slot

    def _call(self, slot: StrategySlot, method: str, *args: Any) -> list[OrderIntent]:
        """Invoke a strategy callback; a crashing strategy is paused, never crashes the engine."""
        try:
            out = getattr(slot.strategy, method)(*args)
        except Exception as exc:
            self.stats.strategy_errors += 1
            slot.paused = True
            self.alerts.critical(
                f"strategy_error:{slot.strategy.id}",
                f"strategy {slot.strategy.id} raised {type(exc).__name__} in {method}: {exc}; "
                "paused",
            )
            return []
        return list(out or [])

    # ----------------------------------------------------------- market data
    async def on_bar(self, bar: Bar) -> None:
        """A *closed* bar at clock.now() >= bar.ts_close (I3)."""
        if not bar.closed:
            return
        sim_on_bar = getattr(self.broker, "on_bar", None)
        if sim_on_bar is not None:
            sim_on_bar(bar)  # match orders placed before this bar against it
        await self._drain_broker()
        self.portfolio.mark(bar.symbol, bar.c)
        if self.bar_mode_synthetic_quotes and bar.symbol not in self._real_quote_symbols:
            # bar-mode backtests have no quotes: a zero-spread quote at the close stands in
            # (spread is charged by the friction model) so RISK-3/4 see fresh data
            self.quotes[bar.symbol] = Quote(
                symbol=bar.symbol,
                bid=bar.c,
                bid_sz=bar.volume,
                ask=bar.c,
                ask_sz=bar.volume,
                ts_event=bar.ts_close,
                ts_recv=bar.ts_close,
            )
            self.feeds.setdefault(
                bar.symbol, FeedHealth(FeedState.LIVE, bar.ts_close, bar.ts_close)
            )
            self.feeds[bar.symbol] = FeedHealth(
                FeedState.LIVE, bar.ts_close, self.feeds[bar.symbol].healthy_since_ns
            )
        self.history.add(bar)
        await self._apply_funding()
        await self.supervise()
        for slot in self.slots:
            if bar.symbol in slot.symbols:
                await self._process(slot, self._call(slot, "on_bar", bar, slot.ctx))
        self.equity_curve.append((bar.ts_close, self.portfolio.equity()))

    async def on_quote(self, q: Quote) -> None:
        self._real_quote_symbols.add(q.symbol)
        self.quotes[q.symbol] = q
        prev = self.feeds.get(q.symbol)
        healthy_since = (
            prev.healthy_since_ns if prev and prev.healthy_since_ns is not None else q.ts_recv
        )
        state = prev.state if prev else FeedState.LIVE
        self.feeds[q.symbol] = FeedHealth(state, q.ts_recv, healthy_since)
        sim_on_quote = getattr(self.broker, "on_quote", None)
        if sim_on_quote is not None:
            sim_on_quote(q)
        self.portfolio.mark(q.symbol, q.mid)
        await self._drain_broker()
        for slot in self.slots:
            if q.symbol in slot.symbols:
                await self._process(slot, self._call(slot, "on_quote", q, slot.ctx))

    async def on_trade(self, t: Trade) -> None:
        sim_on_trade = getattr(self.broker, "on_trade", None)
        if sim_on_trade is not None:
            sim_on_trade(t)
        await self._drain_broker()

    async def on_feed_status(self, fs: FeedStatus) -> None:
        targets = [fs.symbol] if fs.symbol else list(self.feeds)
        for sym in targets:
            prev = self.feeds.get(sym)
            healthy_since = (
                (prev.healthy_since_ns if prev and prev.state is FeedState.LIVE else fs.ts)
                if fs.state is FeedState.LIVE
                else None
            )
            self.feeds[sym] = FeedHealth(
                fs.state, prev.last_data_ns if prev else None, healthy_since
            )
        if fs.state is not FeedState.LIVE:
            self.alerts.critical(
                f"feed:{fs.venue}", f"feed {fs.venue} {fs.state.value}: {fs.reason}"
            )
        await self.supervise()

    # -------------------------------------------------------- broker updates
    async def _drain_broker(self) -> None:
        drain = getattr(self.broker, "drain", None)
        if drain is None:
            return
        while True:
            events = cast(list[OrderEvent | Fill], drain())
            if not events:
                return
            for ev in events:
                await self.on_broker_event(ev)

    async def on_broker_event(self, ev: OrderEvent | Fill) -> None:
        try:
            if isinstance(ev, Fill):
                await self._on_fill(ev)
            elif self.om.find(ev.client_order_id) is not None:
                updated = self.om.apply_event(ev)
                if updated is not None and updated.state is OrderState.REJECTED:
                    slot = self._slot_by_strategy.get(updated.intent.strategy_id)
                    if slot is not None:
                        self._count_reject(slot, f"venue reject: {ev.reason}")
        except IllegalTransitionError as exc:
            self.alerts.critical(
                f"illegal:{getattr(ev, 'client_order_id', '?')}", f"illegal order transition: {exc}"
            )
            await self.pause("illegal order transition")

    async def _on_fill(self, fill: Fill) -> None:
        if not self.om.on_fill(fill):
            if self.om.find(fill.client_order_id) is None:
                self.alerts.critical(
                    "orphan_fill", f"fill for unknown order {fill.client_order_id}"
                )
            return
        self.stats.fills += 1
        self.portfolio.apply_fill(fill)
        order = self.om.get(fill.client_order_id)
        slot = self._slot_by_strategy.get(order.intent.strategy_id)
        if slot is None:
            return
        slot.positions[fill.symbol] = (
            slot.positions.get(fill.symbol, Decimal(0)) + fill.qty * fill.side.sign
        )
        await self._process(slot, self._call(slot, "on_fill", fill, slot.ctx))

    # ------------------------------------------------------- intent pipeline
    def _strategy_exposure(self, slot: StrategySlot) -> Decimal:
        total = Decimal(0)
        for sym, qty in slot.positions.items():
            q = self.quotes.get(sym)
            mark = q.mid if q is not None else None
            if mark is None:
                p = self.portfolio.positions.get(sym)
                mark = p.mark if p is not None else None
            if mark is not None:
                total += abs(qty * mark * self.portfolio.multipliers.get(sym, Decimal(1)))
        return total

    def portfolio_state(self, slot: StrategySlot | None) -> PortfolioState:
        marks: dict[str, Decimal] = {}
        for sym, p in self.portfolio.positions.items():
            q = self.quotes.get(sym)
            if q is not None:
                marks[sym] = q.mid
            elif p.mark is not None:
                marks[sym] = p.mark
        for sym, q in self.quotes.items():
            marks.setdefault(sym, q.mid)
        return PortfolioState(
            equity=self.portfolio.equity(),
            positions={s: p.qty for s, p in self.portfolio.positions.items() if p.qty != 0},
            marks=marks,
            multipliers=dict(self.portfolio.multipliers),
            open_orders=len(self.om.working_orders()),
            strategy_exposure=self._strategy_exposure(slot) if slot else Decimal(0),
        )

    def _market_state(self, symbol: str, inst: Instrument | None) -> MarketState:
        health = self.feeds.get(symbol)
        state = health.state if health is not None else FeedState.RECONNECTING
        if symbol in self.blocked:
            state = FeedState.STALE
        return MarketState(quote=self.quotes.get(symbol), feed_state=state, instrument=inst)

    def _flow_state(self, slot: StrategySlot | None, *, is_flatten: bool = False) -> FlowState:
        now = self.clock.now()
        window = now - NS_PER_MIN
        strat_n = sum(1 for t in slot.order_times if t > window) if slot else 0
        global_n = sum(1 for t in self.global_order_times if t > window)
        return FlowState(
            engine_state=self.ks.state,
            strategy_paused=slot.paused if slot else False,
            orders_last_min_strategy=strat_n,
            orders_last_min_global=global_n,
            consecutive_rejects=slot.consecutive_rejects if slot else 0,
            mode=self.mode,
            lifecycle=slot.lifecycle if slot else LifecycleStage.LIVE,
            now=now,
            is_flatten=is_flatten,
        )

    def _count_reject(self, slot: StrategySlot, reason: str) -> None:
        slot.consecutive_rejects += 1
        if slot.consecutive_rejects >= self.risk_config.max_consecutive_rejects and not slot.paused:
            slot.paused = True
            self.alerts.critical(
                f"rejects:{slot.strategy.id}",
                f"{slot.strategy.id}: {slot.consecutive_rejects} consecutive rejects ({reason}); "
                "strategy paused (RISK-7)",
            )

    async def _process(
        self, slot: StrategySlot | None, intents: list[OrderIntent], *, is_flatten: bool = False
    ) -> None:
        for raw in intents:
            await self.submit_intent(slot, raw, is_flatten=is_flatten)

    async def submit_intent(
        self, slot: StrategySlot | None, raw: OrderIntent, *, is_flatten: bool = False
    ) -> Order | None:
        self.stats.intents += 1
        self._journal(IntentEvent(intent=raw, ts=self.clock.now()), key=raw.strategy_id)
        inst = self.instruments.find(raw.symbol)
        intent = raw
        if inst is not None:
            try:
                intent = self.instruments.conform(raw)  # quantize BEFORE the risk check
            except (ValidationError, ArithmeticError):
                intent = raw  # e.g. qty rounds to zero: risk rejects the unquantized intent
        decision = self.risk.evaluate(
            intent,
            self.portfolio_state(slot),
            self._market_state(intent.symbol, inst),
            self._flow_state(slot, is_flatten=is_flatten),
        )
        self._journal(decision, key=intent.strategy_id)
        self.decisions.append(decision)
        if not decision.approved:
            self.stats.rejected += 1
            if decision.rule_id == "RISK-ERR":
                self.alerts.critical("risk_error", f"risk error: {decision.reason}")
            if slot is not None:
                self._count_reject(slot, f"{decision.rule_id} {decision.reason}")
            return None
        self.stats.approved += 1
        if slot is not None:
            slot.consecutive_rejects = 0
            slot.order_times.append(self.clock.now())
        self.global_order_times.append(self.clock.now())
        order = self.om.create(intent)
        order = self.om.mark_pending(order.client_order_id)  # journaled before the side effect
        await self._send(order)
        return self.om.get(order.client_order_id)

    async def _send(self, order: Order) -> None:
        try:
            await self.broker.submit(order)
            self.stats.submitted += 1
        except AmbiguousSubmitError as exc:
            self.om.mark_unknown(order.client_order_id, str(exc))
            await self.resolve_unknown()
        except BrokerError as exc:
            self.om.mark_rejected(order.client_order_id, str(exc))
            slot = self._slot_by_strategy.get(order.intent.strategy_id)
            if slot is not None:
                self._count_reject(slot, str(exc))
        await self._drain_broker()

    async def resolve_unknown(self) -> None:
        """UNKNOWN orders: look up by client_order_id; resubmit (same id) only if absent."""
        for order in self.om.unknown_orders():
            try:
                venue = await self.broker.get_order(order.client_order_id)
            except BrokerError as exc:
                self.alerts.warn(f"unknown:{order.client_order_id}", f"lookup failed: {exc}")
                continue
            if venue is not None:
                self.om.adopt_state(venue, "resolved by lookup")
            else:
                resubmit = self.om.mark_pending(order.client_order_id)
                await self._send(resubmit)

    # -------------------------------------------------------------- funding
    async def _apply_funding(self) -> None:
        events = getattr(self.broker, "funding_events", None)
        if not events:
            return
        while events:
            sym, rate = events.pop(0)
            self.portfolio.apply_funding(sym, rate)

    # ------------------------------------------------------------ supervisor
    def _roll_day(self, now: int) -> None:
        start = day_start_ns(now, self.risk_config.daily_reset)
        if self._sod_start != start:
            self._sod_start = start
            self._sod_equity = self.portfolio.equity()

    async def supervise(self) -> list[Trip]:
        now = self.clock.now()
        self._roll_day(now)
        equity = self.portfolio.equity()
        self._hwm = max(self._hwm, equity)
        rec = self.reconcile_result
        trips = check_breakers(
            BreakerInputs(
                now=now,
                equity=equity,
                start_of_day_equity=self._sod_equity,
                high_water_mark=self._hwm,
                feeds=self._feeds_in_session(now),
                blocked_symbols=frozenset(self.blocked),
                reconcile_mismatch=rec.describe() if rec is not None and not rec.ok else None,
                clock_skew_ms=self.clock_skew_ms,
            ),
            self.risk_config,
        )
        for trip in trips:
            await self._execute_trip(trip)
        return trips

    def _feeds_in_session(self, now: int) -> dict[str, FeedHealth]:
        """Staleness only matters while the market is open (EXE-9): a closed equity
        market is quiet, not stale. Unknown instruments are always checked."""
        out: dict[str, FeedHealth] = {}
        for sym, h in self.feeds.items():
            inst = self.instruments.find(sym)
            if inst is None or is_open(inst.calendar, now):
                out[sym] = h
        return out

    async def _execute_trip(self, trip: Trip) -> None:
        a = trip.action
        if a is Action.WARN:
            self.alerts.warn(f"{trip.rule_id}:{trip.reason[:24]}", f"{trip.rule_id}: {trip.reason}")
        elif a is Action.PAUSE:
            if self.ks.state is EngineState.RUNNING:
                self.alerts.critical(trip.rule_id, f"{trip.rule_id}: {trip.reason}; engine PAUSED")
            await self.pause(f"{trip.rule_id}: {trip.reason}")
        elif a is Action.BLOCK_SYMBOL and trip.symbol is not None:
            self.blocked.add(trip.symbol)
            self.alerts.critical(
                f"stale:{trip.symbol}",
                f"{trip.rule_id}: {trip.symbol} {trip.reason}; entries blocked",
            )
            for o in self.om.working_orders():
                if o.intent.symbol == trip.symbol:
                    await self._cancel(o.client_order_id)
        elif a is Action.CLEAR_SYMBOL and trip.symbol is not None:
            self.blocked.discard(trip.symbol)
            self.alerts.info(
                f"stale:{trip.symbol}", f"{trip.symbol} feed healthy; entries unblocked"
            )
        elif (
            a in (Action.FLATTEN_AND_HALT, Action.CANCEL_AND_HALT)
            and self.ks.state is not EngineState.HALTED
        ):
            self.alerts.critical(trip.rule_id, f"{trip.rule_id}: {trip.reason}; {a.value}")
            await self.halt(f"{trip.rule_id}: {trip.reason}", flatten=a is Action.FLATTEN_AND_HALT)

    # --------------------------------------------------------------- views
    @property
    def start_of_day_equity(self) -> Decimal:
        return self._sod_equity

    @property
    def daily_pnl(self) -> Decimal:
        return self.portfolio.equity() - self._sod_equity

    # --------------------------------------------------------------- control
    def _record_state(self, ev: EngineStateChanged | None) -> None:
        if ev is not None:
            self._journal(ev, key="engine")

    async def pause(self, reason: str) -> None:
        self._record_state(self.ks.pause(reason))

    async def halt(self, reason: str, *, flatten: bool = True) -> bool:
        ev, code = self.ks.halt(reason)
        self._record_state(ev)
        self.last_resume_code = code
        await self.cancel_all()
        flat = await self.flatten() if flatten else True
        self.alerts.critical(
            "halt", f"HALTED ({reason}); resume code issued to authorized channels"
        )
        return flat

    async def _cancel(self, coid: str) -> None:
        try:
            await self.broker.cancel(coid)
        except BrokerError as exc:
            self.alerts.warn(f"cancel:{coid}", f"cancel failed: {exc}")
        await self._drain_broker()

    async def cancel_all(self) -> None:
        for o in list(self.om.working_orders()):
            await self._cancel(o.client_order_id)

    async def flatten(self, *, max_retries: int = 3) -> bool:
        """Cancel all, reduce-only close every position, verify flat via the broker (§10)."""
        await self.cancel_all()
        for _attempt in range(max_retries + 1):
            positions = [p for p in await self.broker.positions() if p.qty != 0]
            if not positions:
                return True
            for p in positions:
                intent = OrderIntent(
                    strategy_id="flatten",
                    symbol=p.symbol,
                    side=Side.SELL if p.qty > 0 else Side.BUY,
                    type=OrderType.MARKET,
                    qty=abs(p.qty),
                    reduce_only=True,
                    tag="flatten",
                )
                await self.submit_intent(None, intent, is_flatten=True)
            await self._drain_broker()
        still = [p for p in await self.broker.positions() if p.qty != 0]
        if still:
            self.flatten_failures += 1
            self.alerts.critical(
                "flatten_failed",
                f"FLATTEN FAILED: still holding {[(p.symbol, str(p.qty)) for p in still]}",
            )
            return False
        return True

    async def control(self, cmd: ControlCommand) -> str:
        """Control plane entry (API/Telegram/CLI). Journaled with who/when/where (SEC-7)."""
        self._journal(cmd, key=f"control:{cmd.source}")
        targets = [s for s in self.slots if cmd.target in (None, s.strategy.id)]
        if cmd.action is ControlAction.KILL:
            await self.halt(f"kill from {cmd.source}", flatten=True)
            return "halted"
        if cmd.action is ControlAction.FLATTEN:
            await self.pause(f"flatten from {cmd.source}")
            return "flat" if await self.flatten() else "flatten_failed"
        if cmd.action is ControlAction.PAUSE:
            if cmd.target is None:
                await self.pause(f"pause from {cmd.source}")
            for s in targets:
                s.paused = True
            return "paused"
        if cmd.action is ControlAction.STOP:
            for s in targets:
                s.paused = True
            for o in list(self.om.working_orders()):
                if cmd.target in (None, o.intent.strategy_id):
                    await self._cancel(o.client_order_id)
            return "stopped"
        if cmd.action is ControlAction.START:
            for s in targets:
                s.paused = False
                s.consecutive_rejects = 0
            return "started"
        # RESUME
        try:
            self._record_state(self.ks.resume(cmd.code, f"resume from {cmd.source}"))
        except ResumeDeniedError as exc:
            self.alerts.warn("resume_denied", str(exc))
            return "denied"
        for s in targets:
            s.paused = False
        return "running"

    async def reconcile(self) -> ReconcileResult:
        marks = {s: q.mid for s, q in self.quotes.items()}
        res = reconcile(
            {s: p.qty for s, p in self.portfolio.positions.items()},
            await self.broker.positions(),
            self.om.open_orders(),
            await self.broker.open_orders(),
            marks,
            self.risk_config.reconcile_tolerance_pct,
        )
        self.reconcile_result = res
        if not res.ok:
            await self.supervise()
        return res

    def update_risk_config(self, new: RiskConfig, source: str = "cli") -> list[str]:
        """EXE-7: tighten at runtime; any loosening is refused (needs a restart)."""
        looser = self.risk_config.tightening_violations(new)
        self._journal(
            ConfigChanged(
                section="risk",
                before=self.risk_config.model_dump_json(),
                after=new.model_dump_json(),
                source=source,
                accepted=not looser,
                ts=self.clock.now(),
            ),
            key="config",
        )
        if looser:
            self.alerts.warn("risk_loosen_denied", f"runtime loosening refused for {looser}")
            return looser
        self.risk_config = new
        self.risk = RiskManager(new)
        return []

    async def stop_strategies(self) -> None:
        for slot in self.slots:
            self._call(slot, "on_stop", slot.ctx)
