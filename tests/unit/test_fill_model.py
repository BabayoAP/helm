"""BT-3 bar fill model, table-driven with hand-computed expectations (tests first)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from helm.backtest.fills import BarFillModel, FillPolicy
from helm.core.enums import OrderType, Side
from helm.core.models import OrderIntent
from tests.factories import SYM, bar

D = Decimal
# next bar: open 100, high 105, low 95, close 102
NEXT = bar(1, 100, 105, 95, 102)
GAP_DOWN = bar(1, 90, 92, 88, 91)
GAP_UP = bar(1, 110, 112, 108, 111)


def it(
    side: Side, type_: OrderType, limit: str | None = None, stop: str | None = None
) -> OrderIntent:
    return OrderIntent(
        strategy_id="s",
        symbol=SYM,
        side=side,
        type=type_,
        qty=D(1),
        limit_price=D(limit) if limit else None,
        stop_price=D(stop) if stop else None,
    )


CASES = [
    # (name, intent, bar, policy, expected fill price or None)
    (
        "mkt buy at next open + slip",
        it(Side.BUY, OrderType.MARKET),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("100.1"),
    ),
    (
        "mkt sell at next open - slip",
        it(Side.SELL, OrderType.MARKET),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("99.9"),
    ),
    (
        "lmt buy touched (low == limit) conservative: no fill",
        it(Side.BUY, OrderType.LIMIT, "95"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        None,
    ),
    (
        "lmt buy touched optimistic: fill at limit",
        it(Side.BUY, OrderType.LIMIT, "95"),
        NEXT,
        FillPolicy.OPTIMISTIC,
        D("95"),
    ),
    (
        "lmt buy traded through: fill at limit",
        it(Side.BUY, OrderType.LIMIT, "96"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("96"),
    ),
    (
        "lmt buy above open: fills at open (better)",
        it(Side.BUY, OrderType.LIMIT, "101"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("100"),
    ),
    (
        "lmt sell traded through",
        it(Side.SELL, OrderType.LIMIT, "104"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("104"),
    ),
    (
        "lmt sell touched high conservative: no fill",
        it(Side.SELL, OrderType.LIMIT, "105"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        None,
    ),
    (
        "lmt sell below open: fills at open",
        it(Side.SELL, OrderType.LIMIT, "99"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("100"),
    ),
    (
        "lmt buy not reached",
        it(Side.BUY, OrderType.LIMIT, "90"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        None,
    ),
    (
        "stop sell triggered intrabar: at stop - slip",
        it(Side.SELL, OrderType.STOP, stop="97"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("96.903"),
    ),
    (
        "stop sell gap through: worse of stop/open",
        it(Side.SELL, OrderType.STOP, stop="97"),
        GAP_DOWN,
        FillPolicy.CONSERVATIVE,
        D("89.91"),
    ),
    (
        "stop buy gap up: worse of stop/open",
        it(Side.BUY, OrderType.STOP, stop="104"),
        GAP_UP,
        FillPolicy.CONSERVATIVE,
        D("110.11"),
    ),
    (
        "stop buy not triggered",
        it(Side.BUY, OrderType.STOP, stop="106"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        None,
    ),
    (
        "stop-limit buy triggered then limit fill",
        it(Side.BUY, OrderType.STOP_LIMIT, limit="104", stop="103"),
        NEXT,
        FillPolicy.CONSERVATIVE,
        D("104"),
    ),
    (
        "stop-limit buy optimistic fills at trigger",
        it(Side.BUY, OrderType.STOP_LIMIT, limit="104", stop="103"),
        NEXT,
        FillPolicy.OPTIMISTIC,
        D("103"),
    ),
    (
        "stop-limit sell gapped past limit: no fill",
        it(Side.SELL, OrderType.STOP_LIMIT, limit="95", stop="97"),
        GAP_DOWN,
        FillPolicy.CONSERVATIVE,
        None,
    ),
]


@pytest.mark.parametrize(
    ("name", "intent", "nb", "policy", "expected"), CASES, ids=[c[0] for c in CASES]
)
def test_bar_fill_table(
    name: str, intent: OrderIntent, nb: object, policy: FillPolicy, expected: Decimal | None
) -> None:
    model = BarFillModel(slippage_bps=D(10), policy=policy)  # 10 bp = 0.1 on 100
    got = model.match(intent, nb)  # type: ignore[arg-type]
    assert (got.price if got else None) == expected, name


def test_same_bar_entry_and_exit_resolved_pessimistically() -> None:
    """A bracket with both stop-loss and take-profit inside the same bar: the stop wins."""
    model = BarFillModel(slippage_bps=D(0))
    stop = it(Side.SELL, OrderType.STOP, stop="96")
    take = it(Side.SELL, OrderType.LIMIT, "104")
    winner = model.resolve_oco([take, stop], NEXT)
    assert winner is not None and winner[0] is stop and winner[1].price == D("96")


def test_market_fill_is_maker_taker_labelled() -> None:
    m = BarFillModel(slippage_bps=D(0))
    mk = m.match(it(Side.BUY, OrderType.MARKET), NEXT)
    lm = m.match(it(Side.BUY, OrderType.LIMIT, "96"), NEXT)
    assert mk is not None and mk.taker and lm is not None and not lm.taker
