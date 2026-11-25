"""PRD §10: for random portfolios/intents/configs, no APPROVE results in a post-trade
state that breaches RISK-4/5/6 (unless the order strictly reduces the breach)."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from helm.core.config import RiskConfig
from helm.core.enums import EngineState, FeedState, LifecycleStage, Mode, OrderType, Side, Verdict
from helm.core.models import OrderIntent
from helm.risk.manager import RiskManager
from helm.risk.types import FlowState, MarketState, PortfolioState
from tests.factories import SYM, instrument, quote

ETH = "fake:ETH/USD"
NOW = 10**12
D = Decimal

pct = st.decimals(min_value=D("0.5"), max_value=D(50), places=1)
mids = st.decimals(min_value=D(1), max_value=D(1000), places=2)
qty = st.integers(min_value=1, max_value=200_000).map(lambda n: D(n) * D("0.0001"))
pos = st.integers(min_value=-50_000, max_value=50_000).map(lambda n: D(n) * D("0.0001"))


@settings(max_examples=600, deadline=None)
@given(
    mid=mids,
    eth_mid=mids,
    spread_bp=st.integers(1, 800),
    equity=st.decimals(min_value=D(-100), max_value=D(100_000), places=2),
    pos_sym=pos,
    pos_eth=pos,
    q=qty,
    side=st.sampled_from(list(Side)),
    is_limit=st.booleans(),
    limit_off_bp=st.integers(-600, 600),
    max_pos=pct,
    max_lev=st.decimals(min_value=D("0.5"), max_value=D(5), places=1),
    ff=pct,
    cap=st.decimals(min_value=D(10), max_value=D(10_000), places=0),
)
def test_no_approval_breaches_risk_4_5_6(
    mid: Decimal,
    eth_mid: Decimal,
    spread_bp: int,
    equity: Decimal,
    pos_sym: Decimal,
    pos_eth: Decimal,
    q: Decimal,
    side: Side,
    is_limit: bool,
    limit_off_bp: int,
    max_pos: Decimal,
    max_lev: Decimal,
    ff: Decimal,
    cap: Decimal,
) -> None:
    cfg = RiskConfig(
        max_position_pct_equity=max_pos,
        max_gross_leverage=max_lev,
        fat_finger_pct=ff,
        max_order_notional_usd=cap,
    )
    half = mid * spread_bp / D(20_000)
    bid, ask = (mid - half).quantize(D("0.01")), (mid + half).quantize(D("0.01"))
    if bid <= 0:
        return
    q_ = quote(str(bid), str(ask), ts=NOW)
    limit = (mid * (1 + D(limit_off_bp) / D(10_000))).quantize(D("0.01"))
    it = (
        OrderIntent(
            strategy_id="s",
            symbol=SYM,
            side=side,
            type=OrderType.LIMIT if is_limit else OrderType.MARKET,
            qty=q,
            limit_price=limit if is_limit and limit > 0 else None,
        )
        if (not is_limit or limit > 0)
        else None
    )
    if it is None:
        return
    p = PortfolioState(
        equity=equity,
        positions={SYM: pos_sym, ETH: pos_eth},
        marks={SYM: q_.mid, ETH: eth_mid},
        multipliers={},
        open_orders=0,
        strategy_exposure=D(0),
    )
    m = MarketState(quote=q_, feed_state=FeedState.LIVE, instrument=instrument(min_notional=D(0)))
    f = FlowState(
        engine_state=EngineState.RUNNING,
        strategy_paused=False,
        orders_last_min_strategy=0,
        orders_last_min_global=0,
        consecutive_rejects=0,
        mode=Mode.PAPER,
        lifecycle=LifecycleStage.PAPER,
        now=NOW,
    )
    d = RiskManager(cfg).evaluate(it, p, m, f)
    if d.verdict is not Verdict.APPROVE:
        return
    mid_ = q_.mid
    pre_sym = abs(pos_sym * mid_)
    post_qty = pos_sym + side.sign * q
    post_sym = abs(post_qty * mid_)
    eth_exp = abs(pos_eth * eth_mid)
    reducing = post_sym <= pre_sym
    # RISK-4: price within collar, size within cap (non-reducing orders)
    ref = it.limit_price if it.limit_price is not None else (ask if side is Side.BUY else bid)
    assert abs(ref - mid_) / mid_ * 100 <= ff
    if not reducing:
        assert q * ref <= cap
        # RISK-5 and RISK-6 hold post-trade
        assert equity > 0
        assert post_sym <= max_pos / 100 * equity
        assert (post_sym + eth_exp) <= max_lev * equity or (post_sym + eth_exp) <= pre_sym + eth_exp
