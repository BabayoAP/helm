"""BT-8 accounting (written before the implementation, PRD §0.6).

Identity: equity_T = equity_0 + sum(realized) + unrealized - sum(fees) (+ funding),
cross-checked against an independent cash-flow computation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helm.core.enums import Liquidity, Side
from helm.core.models import Fill
from helm.oms.accounting import Portfolio

SYMS = ["fake:BTC/USD", "fake:ETH/USD"]
prices = st.decimals(min_value=Decimal("0.5"), max_value=Decimal("5000"), places=2)
qtys = st.decimals(min_value=Decimal("0.001"), max_value=Decimal("50"), places=3)


def _fill(sym: str, side: Side, qty: Decimal, px: Decimal, fee: Decimal, n: int) -> Fill:
    return Fill(
        client_order_id=f"c{n}",
        venue="fake",
        trade_id=f"t{n}",
        symbol=sym,
        side=side,
        price=px,
        qty=qty,
        fee=fee,
        fee_ccy="USD",
        liquidity=Liquidity.TAKER,
        ts=n,
    )


@settings(max_examples=300, deadline=None)
@given(
    trades=st.lists(
        st.tuples(
            st.sampled_from(SYMS),
            st.sampled_from(list(Side)),
            qtys,
            prices,
            st.decimals(min_value=0, max_value=5, places=4),
        ),
        min_size=1,
        max_size=40,
    ),
    marks=st.tuples(prices, prices),
)
def test_equity_identity_and_cashflow_crosscheck(
    trades: list[tuple[str, Side, Decimal, Decimal, Decimal]], marks: tuple[Decimal, Decimal]
) -> None:
    eq0 = Decimal(100_000)
    pf = Portfolio(starting_equity=eq0)
    cash_flow = eq0
    for n, (sym, side, qty, px, fee) in enumerate(trades):
        pf.apply_fill(_fill(sym, side, qty, px, fee, n))
        cash_flow -= side.sign * qty * px + fee
    for sym, m in zip(SYMS, marks, strict=True):
        pf.mark(sym, m)
    realized = sum((p.realized_pnl for p in pf.positions.values()), Decimal(0))
    unrealized = pf.unrealized()
    assert pf.equity() == eq0 + realized + unrealized - pf.fees_paid
    holdings = sum(
        (pf.positions[s].qty * m for s, m in zip(SYMS, marks, strict=True) if s in pf.positions),
        Decimal(0),
    )
    assert pf.equity() == cash_flow + holdings


def test_avg_price_partial_close_and_flip() -> None:
    pf = Portfolio(starting_equity=Decimal(1000))
    s = SYMS[0]
    pf.apply_fill(_fill(s, Side.BUY, Decimal(1), Decimal(100), Decimal("0.1"), 1))
    pf.apply_fill(_fill(s, Side.BUY, Decimal(1), Decimal(110), Decimal("0.1"), 2))
    p = pf.positions[s]
    assert (p.qty, p.avg_px) == (Decimal(2), Decimal(105))
    pf.apply_fill(_fill(s, Side.SELL, Decimal("0.5"), Decimal(120), Decimal(0), 3))
    assert pf.positions[s].realized_pnl == Decimal("7.5") and pf.positions[s].avg_px == Decimal(105)
    pf.apply_fill(
        _fill(s, Side.SELL, Decimal(3), Decimal(100), Decimal(0), 4)
    )  # close 1.5, open short 1.5
    p = pf.positions[s]
    assert p.qty == Decimal("-1.5") and p.avg_px == Decimal(100)
    assert p.realized_pnl == Decimal("7.5") + Decimal("-7.5")
    pf.mark(s, Decimal(90))
    assert pf.unrealized() == Decimal(15)
    assert pf.gross_exposure() == Decimal(135)
    assert pf.fees_paid == Decimal("0.2")
    assert pf.trade_count == 4


def test_funding_and_leverage_and_snapshot() -> None:
    pf = Portfolio(starting_equity=Decimal(1000))
    s = SYMS[0]
    pf.apply_fill(_fill(s, Side.BUY, Decimal(10), Decimal(100), Decimal(0), 1))
    pf.mark(s, Decimal(100))
    assert pf.leverage() == Decimal(1)
    pf.apply_funding(s, rate=Decimal("0.0001"))  # longs pay positive funding
    assert pf.funding_paid == Decimal("0.1") and pf.equity() == Decimal("999.9")
    snap = pf.position(s)
    assert snap.qty == 10 and pf.position("x:Y/Z").qty == 0
    with pytest.raises(ValueError):
        pf.mark(s, Decimal(0))


def test_contract_multiplier() -> None:
    pf = Portfolio(starting_equity=Decimal(1000), multipliers={SYMS[0]: Decimal(10)})
    pf.apply_fill(_fill(SYMS[0], Side.BUY, Decimal(1), Decimal(100), Decimal(0), 1))
    pf.mark(SYMS[0], Decimal(101))
    assert pf.unrealized() == Decimal(10) and pf.gross_exposure() == Decimal(1010)
