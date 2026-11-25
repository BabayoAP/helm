"""RiskManager pre-trade rules RISK-1..9 (PRD §10). Tests written before the implementation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from helm.core.config import RiskConfig
from helm.core.enums import EngineState, FeedState, LifecycleStage, Mode, OrderType, Side, Verdict
from helm.core.models import OrderIntent
from helm.risk.manager import RiskManager
from helm.risk.types import FlowState, MarketState, PortfolioState
from tests.factories import SYM, instrument, intent, quote

D = Decimal
NOW = 10**12
ETH = "fake:ETH/USD"


def market(**kw: object) -> MarketState:
    base = MarketState(
        quote=quote("99.95", "100.05", ts=NOW - 10**6),
        feed_state=FeedState.LIVE,
        instrument=instrument(),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def pf(**kw: object) -> PortfolioState:
    base = PortfolioState(
        equity=D(10_000),
        positions={},
        marks={SYM: D(100)},
        multipliers={},
        open_orders=0,
        strategy_exposure=D(0),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def flow(**kw: object) -> FlowState:
    base = FlowState(
        engine_state=EngineState.RUNNING,
        strategy_paused=False,
        orders_last_min_strategy=0,
        orders_last_min_global=0,
        consecutive_rejects=0,
        mode=Mode.PAPER,
        lifecycle=LifecycleStage.PAPER,
        now=NOW,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


RM = RiskManager(RiskConfig())


def decide(
    it: OrderIntent,
    *,
    p: PortfolioState | None = None,
    m: MarketState | None = None,
    f: FlowState | None = None,
    cfg: RiskConfig | None = None,
) -> tuple[Verdict, str, str]:
    rm = RiskManager(cfg) if cfg else RM
    d = rm.evaluate(it, p or pf(), m or market(), f or flow())
    return d.verdict, d.rule_id, d.reason


def test_happy_path_approves_with_hash() -> None:
    d = RM.evaluate(intent(qty="1"), pf(), market(), flow())
    assert d.verdict is Verdict.APPROVE and d.rule_id == "OK" and len(d.inputs_snapshot_hash) == 64
    assert d == RM.evaluate(intent(qty="1"), pf(), market(), flow())  # pure + deterministic


# ------------------------------------------------------------------ RISK-1
def test_risk1_engine_state_gate() -> None:
    long1 = pf(positions={SYM: D(1)})
    exit_order = intent(side=Side.SELL, qty="1")
    assert decide(intent(), f=flow(engine_state=EngineState.PAUSED))[1] == "RISK-1"
    assert (
        decide(exit_order, p=long1, f=flow(engine_state=EngineState.PAUSED))[0] is Verdict.APPROVE
    )
    assert (
        decide(intent(qty="1", reduce_only=True), p=long1, f=flow(engine_state=EngineState.PAUSED))[
            1
        ]
        == "RISK-1"
    )  # wrong side
    assert decide(intent(), f=flow(strategy_paused=True))[1] == "RISK-1"
    assert decide(exit_order, p=long1, f=flow(engine_state=EngineState.HALTED))[1] == "RISK-1"
    flat = intent(side=Side.SELL, qty="1", reduce_only=True)
    assert (
        decide(flat, p=long1, f=flow(engine_state=EngineState.HALTED, is_flatten=True))[0]
        is Verdict.APPROVE
    )
    v, rule, reason = decide(
        intent(qty="1"), p=long1, f=flow(engine_state=EngineState.HALTED, is_flatten=True)
    )
    assert (v, rule) == (Verdict.REJECT, "RISK-1") and "reduce" in reason
    assert (
        decide(intent(side=Side.SELL, qty="1"), p=long1, f=flow(is_flatten=True))[1] == "RISK-1"
    )  # flatten must be reduce_only


# ------------------------------------------------------------------ RISK-2
def test_risk2_instrument_gate() -> None:
    assert decide(intent(), m=market(instrument=None))[2] == "unknown_instrument"
    assert decide(intent(), cfg=RiskConfig(symbol_allowlist=(ETH,)))[2] == "symbol_not_allowed"
    assert decide(intent(qty="1"), cfg=RiskConfig(symbol_allowlist=(SYM,)))[0] is Verdict.APPROVE
    assert (
        decide(intent(), m=market(instrument=instrument(calendar="us_equity_rth")))[2]
        == "market_closed"
    )
    assert decide(intent(qty="0.00015"))[2] == "qty_not_quantized"
    assert (
        decide(intent(qty="0.0001"), m=market(instrument=instrument(min_qty=D("0.001"))))[2]
        == "below_min_qty"
    )
    assert decide(intent(type_=OrderType.LIMIT, limit="100.001"))[2] == "price_not_quantized"
    assert decide(intent(qty="0.01"))[2] == "below_min_notional"  # 0.01 * 100 = $1 < $5
    small_exit = intent(side=Side.SELL, qty="0.01")
    assert (
        decide(small_exit, p=pf(positions={SYM: D("0.01")}))[0] is Verdict.APPROVE
    )  # closing dust allowed
    nomark = pf(marks={})
    assert decide(intent(qty="1"), p=nomark, m=market(quote=None))[2] == "no_reference_price"
    stop = OrderIntent(
        strategy_id="s1",
        symbol=SYM,
        side=Side.SELL,
        type=OrderType.STOP,
        qty=D(1),
        stop_price=D("99.001"),
    )
    assert decide(stop)[2] == "price_not_quantized"


# ------------------------------------------------------------------ RISK-3
def test_risk3_data_freshness() -> None:
    assert decide(intent(qty="1"), m=market(feed_state=FeedState.STALE))[2] == "feed_not_live"
    assert decide(intent(qty="1"), m=market(quote=None))[2] == "no_quote"
    old = quote("99.95", "100.05", ts=NOW - 2001 * 10**6)
    assert decide(intent(qty="1"), m=market(quote=old))[2] == "stale_quote"
    ok = quote("99.95", "100.05", ts=NOW - 2000 * 10**6)
    assert decide(intent(qty="1"), m=market(quote=ok))[0] is Verdict.APPROVE


# ------------------------------------------------------------------ RISK-4
def test_risk4_fat_finger() -> None:
    assert decide(intent(qty="1", type_=OrderType.LIMIT, limit="103.10"))[2] == "fat_finger"
    assert decide(intent(qty="1", type_=OrderType.LIMIT, limit="96.90"))[2] == "fat_finger"
    assert decide(intent(qty="1", type_=OrderType.LIMIT, limit="102.90"))[0] is Verdict.APPROVE
    wide = quote("90", "110", ts=NOW)
    assert decide(intent(qty="1"), m=market(quote=wide))[2] == "fat_finger"
    assert decide(intent(qty="5.1"))[2] == "order_notional_cap"  # 5.1 * 100.05 > $500
    big_exit = intent(side=Side.SELL, qty="9")
    assert decide(big_exit, p=pf(positions={SYM: D(9)}))[0] is Verdict.APPROVE  # exits not capped
    sl = OrderIntent(
        strategy_id="s1",
        symbol=SYM,
        side=Side.BUY,
        type=OrderType.STOP_LIMIT,
        qty=D(1),
        stop_price=D(100),
        limit_price=D(110),
    )
    assert decide(sl)[2] == "fat_finger"


# ------------------------------------------------------------------ RISK-5 / RISK-6
def test_risk5_position_cap() -> None:
    p = pf(equity=D(1000))  # cap = $100 per symbol
    assert decide(intent(qty="1.1"), p=p)[2] == "position_cap"
    assert decide(intent(qty="0.9"), p=p)[0] is Verdict.APPROVE
    over = pf(equity=D(1000), positions={SYM: D(3)})  # already 3x over cap: reducing is fine
    assert decide(intent(side=Side.SELL, qty="1"), p=over)[0] is Verdict.APPROVE
    assert decide(intent(qty="0.1"), p=over)[2] == "position_cap"
    assert decide(intent(qty="0.1"), p=pf(equity=D(0)))[2] == "non_positive_equity"


def test_risk6_leverage_cap() -> None:
    cfg = RiskConfig(max_position_pct_equity=D(100), max_order_notional_usd=D(100_000))
    p = pf(equity=D(1000), positions={ETH: D(10)}, marks={SYM: D(100), ETH: D(150)})  # gross 1500
    assert decide(intent(qty="5.1"), p=p, cfg=cfg)[2] == "leverage_cap"  # 1500 + 510 > 2000
    assert decide(intent(qty="4.9"), p=p, cfg=cfg)[0] is Verdict.APPROVE
    missing = pf(equity=D(1000), positions={ETH: D(1)}, marks={SYM: D(100)})
    assert decide(intent(qty="0.1"), p=missing, cfg=cfg)[2] == "missing_mark"
    lev_over = pf(equity=D(100), positions={SYM: D(5)}, marks={SYM: D(100)})
    assert decide(intent(side=Side.SELL, qty="1"), p=lev_over, cfg=cfg)[0] is Verdict.APPROVE


# ------------------------------------------------------------------ RISK-7
def test_risk7_rate_caps() -> None:
    assert decide(intent(qty="1"), f=flow(orders_last_min_strategy=30))[2] == "strategy_rate_cap"
    assert decide(intent(qty="1"), f=flow(orders_last_min_global=60))[2] == "global_rate_cap"
    assert decide(intent(qty="1"), p=pf(open_orders=20))[2] == "max_open_orders"
    assert decide(intent(qty="1"), f=flow(consecutive_rejects=5))[2] == "consecutive_rejects"


# ------------------------------------------------------------------ RISK-8 / RISK-9
def test_risk8_strategy_allocation() -> None:
    cfg = RiskConfig(
        max_strategy_capital_pct=D(5),
        max_position_pct_equity=D(100),
        max_order_notional_usd=D(10**6),
    )
    assert (
        decide(intent(qty="4"), p=pf(strategy_exposure=D(200)), cfg=cfg)[2] == "strategy_allocation"
    )
    assert decide(intent(qty="2"), p=pf(strategy_exposure=D(200)), cfg=cfg)[0] is Verdict.APPROVE
    # EXE-6: a bot's own allocation is applied when tighter than the global cap...
    tight = pf(strategy_exposure=D(200), strategy_capital_pct=D(3))
    assert decide(intent(qty="2"), p=tight, cfg=cfg)[2] == "strategy_allocation"
    # ...and never loosens it
    loose = pf(strategy_exposure=D(200), strategy_capital_pct=D(50))
    assert decide(intent(qty="4"), p=loose, cfg=cfg)[2] == "strategy_allocation"


def test_risk9_live_small_cap() -> None:
    live_small = flow(mode=Mode.LIVE, lifecycle=LifecycleStage.LIVE_SMALL)
    assert decide(intent(qty="2.1"), f=live_small)[2] == "live_small_cap"  # 2% of 10k = $200
    assert decide(intent(qty="1.9"), f=live_small)[0] is Verdict.APPROVE
    big = pf(equity=D(100_000), marks={SYM: D(100)})
    cfg = RiskConfig(max_order_notional_usd=D(10_000))
    assert (
        decide(intent(qty="5.1"), p=big, f=live_small, cfg=cfg)[2] == "live_small_cap"
    )  # $500 cap
    assert decide(intent(qty="4.9"), p=big, f=live_small, cfg=cfg)[0] is Verdict.APPROVE
    assert (
        decide(intent(qty="2.1"), f=flow(mode=Mode.LIVE, lifecycle=LifecycleStage.LIVE))[0]
        is Verdict.APPROVE
    )


# --------------------------------------------------------------- fail-closed
@pytest.mark.parametrize("broken", ["portfolio", "market", "flow", "intent"])
def test_fail_closed_on_bad_inputs(broken: str) -> None:
    args: dict[str, object] = {
        "intent": intent(qty="1"),
        "portfolio": pf(),
        "market": market(),
        "flow": flow(),
    }
    args[broken] = None
    d = RM.evaluate(args["intent"], args["portfolio"], args["market"], args["flow"])  # type: ignore[arg-type]
    assert (
        d.verdict is Verdict.REJECT
        and d.rule_id == "RISK-ERR"
        and d.reason.startswith("risk_error")
    )


def test_fail_closed_on_exception_inside_rule() -> None:
    weird = pf(marks={SYM: "not-a-decimal"})  # type: ignore[dict-item]
    d = RM.evaluate(
        intent(qty="1"),
        weird,
        market(quote=None, feed_state=FeedState.LIVE),
        flow(is_flatten=False),
    )
    assert d.verdict is Verdict.REJECT


def test_flatten_bypasses_freshness_position_leverage_rate() -> None:
    long_big = pf(equity=D(100), positions={SYM: D(50)}, open_orders=99)
    flat = intent(side=Side.SELL, qty="50", reduce_only=True)
    f = flow(
        engine_state=EngineState.HALTED,
        is_flatten=True,
        orders_last_min_strategy=999,
        consecutive_rejects=99,
    )
    assert (
        decide(flat, p=long_big, m=market(feed_state=FeedState.STALE, quote=None), f=f)[0]
        is Verdict.APPROVE
    )
    over = intent(side=Side.SELL, qty="51", reduce_only=True)
    assert (
        decide(over, p=long_big, m=market(feed_state=FeedState.STALE, quote=None), f=f)[1]
        == "RISK-1"
    )


def test_flatten_without_any_price_still_allowed_reduce_only() -> None:
    f = flow(engine_state=EngineState.HALTED, is_flatten=True)
    p = pf(positions={SYM: D(2)}, marks={})
    flat = intent(side=Side.SELL, qty="2", reduce_only=True)
    assert (
        decide(flat, p=p, m=market(quote=None, feed_state=FeedState.RECONNECTING), f=f)[0]
        is Verdict.APPROVE
    )


def test_live_small_allows_reducing_orders() -> None:
    live_small = flow(mode=Mode.LIVE, lifecycle=LifecycleStage.LIVE_SMALL)
    p = pf(positions={SYM: D(3)}, strategy_exposure=D(300))
    assert decide(intent(side=Side.SELL, qty="1"), p=p, f=live_small)[0] is Verdict.APPROVE


class _Exploding(dict):  # type: ignore[type-arg]
    def items(self) -> object:  # type: ignore[override]
        raise RuntimeError("cannot snapshot")


def test_unhashable_inputs_fail_closed() -> None:
    d = RM.evaluate(intent(qty="1"), pf(positions=_Exploding()), market(), flow())
    assert d.verdict is Verdict.REJECT and d.inputs_snapshot_hash == "unhashable"
