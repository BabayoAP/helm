"""Order state machine + order manager (PRD §6.5, I4). Written before the implementation."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helm.core.clock import SimClock
from helm.core.enums import Liquidity, OrderEventKind, OrderState, Side
from helm.core.errors import IllegalTransitionError
from helm.core.events import OrderEvent
from helm.core.models import Fill, Order
from helm.oms.journal import Journal
from helm.oms.order_manager import OrderManager
from helm.oms.state_machine import LEGAL, is_stale, transition
from tests.factories import SYM, intent

S = OrderState


@pytest.mark.parametrize(
    ("cur", "new"),
    [
        (S.NEW, S.PENDING_SUBMIT),
        (S.PENDING_SUBMIT, S.ACCEPTED),
        (S.PENDING_SUBMIT, S.UNKNOWN),
        (S.PENDING_SUBMIT, S.FILLED),
        (S.ACCEPTED, S.PARTIALLY_FILLED),
        (S.PARTIALLY_FILLED, S.PARTIALLY_FILLED),
        (S.PARTIALLY_FILLED, S.FILLED),
        (S.ACCEPTED, S.CANCELED),
        (S.ACCEPTED, S.EXPIRED),
        (S.UNKNOWN, S.ACCEPTED),
        (S.UNKNOWN, S.PENDING_SUBMIT),  # confirmed not at venue -> resubmit same id
        (S.NEW, S.REJECTED),
    ],
)
def test_legal_transitions(cur: OrderState, new: OrderState) -> None:
    assert transition(cur, new) is new


@pytest.mark.parametrize(
    ("cur", "new"),
    [
        (S.FILLED, S.CANCELED),
        (S.CANCELED, S.FILLED),
        (S.REJECTED, S.ACCEPTED),
        (S.NEW, S.FILLED),
        (S.EXPIRED, S.PARTIALLY_FILLED),
        (S.ACCEPTED, S.NEW),
    ],
)
def test_illegal_transitions_raise(cur: OrderState, new: OrderState) -> None:
    with pytest.raises(IllegalTransitionError):
        transition(cur, new)


def test_stale_events_detected() -> None:
    assert is_stale(S.FILLED, S.ACCEPTED)
    assert is_stale(S.PARTIALLY_FILLED, S.ACCEPTED)
    assert is_stale(S.ACCEPTED, S.ACCEPTED)
    assert not is_stale(S.ACCEPTED, S.FILLED)
    assert not is_stale(S.PARTIALLY_FILLED, S.PARTIALLY_FILLED)


@given(st.lists(st.sampled_from(list(OrderState)), max_size=12))
def test_state_machine_never_leaves_terminal(seq: list[OrderState]) -> None:
    """TEST-2: any accepted sequence of transitions never moves out of a terminal state."""
    cur = S.NEW
    for nxt in seq:
        if is_stale(cur, nxt):
            continue
        try:
            new = transition(cur, nxt)
        except IllegalTransitionError:
            assert nxt not in LEGAL[cur]
            continue
        assert not cur.is_terminal
        cur = new


def _fill(coid: str, tid: str, qty: str, px: str = "100") -> Fill:
    return Fill(
        client_order_id=coid,
        venue="fake",
        trade_id=tid,
        symbol=SYM,
        side=Side.BUY,
        price=Decimal(px),
        qty=Decimal(qty),
        fee=Decimal("0.01"),
        fee_ccy="USD",
        liquidity=Liquidity.TAKER,
        ts=1,
    )


def _ev(order: Order, state: OrderState) -> OrderEvent:
    kind = {
        S.ACCEPTED: OrderEventKind.ACCEPTED,
        S.CANCELED: OrderEventKind.CANCELED,
        S.REJECTED: OrderEventKind.REJECTED,
    }[state]
    return OrderEvent(
        kind=kind,
        client_order_id=order.client_order_id,
        state=state,
        order=order.model_copy(update={"state": state}),
        ts=2,
    )


def test_order_manager_lifecycle_dedupe_and_recovery(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    om = OrderManager(run_id="r1", account_id="paper", journal=j, clock=SimClock(5))
    o = om.create(intent(qty="1"))
    assert o.client_order_id == "r1-s1-0" and o.state is S.NEW
    assert om.create(intent(qty="2")).client_order_id == "r1-s1-1"
    o = om.mark_pending(o.client_order_id)
    om.apply_event(_ev(o, S.ACCEPTED))
    om.apply_event(_ev(o, S.ACCEPTED))  # duplicate: stale, counted, not applied twice
    assert om.stale_events == 1
    assert om.on_fill(_fill(o.client_order_id, "t1", "0.4"))
    assert not om.on_fill(_fill(o.client_order_id, "t1", "0.4"))  # duplicate fill (venue, trade_id)
    got = om.get(o.client_order_id)
    assert got.state is S.PARTIALLY_FILLED and got.filled_qty == Decimal("0.4")
    assert om.on_fill(_fill(o.client_order_id, "t2", "0.6", px="110"))
    got = om.get(o.client_order_id)
    assert got.state is S.FILLED and got.avg_px == Decimal("106") and got.fees == Decimal("0.02")
    with pytest.raises(IllegalTransitionError):
        om.apply_event(_ev(got, S.CANCELED))
    assert om.illegal_events == 1
    second = om.mark_pending("r1-s1-1")
    om.mark_unknown(second.client_order_id, "timeout")
    assert [x.client_order_id for x in om.unknown_orders()] == ["r1-s1-1"]
    assert [x.client_order_id for x in om.open_orders()] == []
    with pytest.raises(KeyError):
        om.get("nope")
    assert not om.on_fill(
        _fill("not-ours", "t9", "1")
    )  # unknown order fills are reported, not applied
    assert om.orphan_fills == 1
    j.close()

    # crash + restart: rebuild from the journal
    j2 = Journal(tmp_path / "j.sqlite")
    om2 = OrderManager.recover(run_id="r1", account_id="paper", journal=j2, clock=SimClock(9))
    assert om2.get("r1-s1-0").state is S.FILLED and om2.get("r1-s1-0").filled_qty == 1
    assert om2.get("r1-s1-1").state is S.UNKNOWN
    assert not om2.on_fill(_fill("r1-s1-0", "t2", "0.6"))  # dedupe set survives restart
    assert om2.create(intent()).client_order_id == "r1-s1-2"  # sequence continues: no id reuse
    j2.close()


def test_reject_and_cancel_paths(tmp_path: Path) -> None:
    with Journal(tmp_path / "j.sqlite", mirror=False) as j:
        om = OrderManager(run_id="r", account_id="a", journal=j, clock=SimClock())
        a = om.mark_pending(om.create(intent()).client_order_id)
        om.mark_rejected(a.client_order_id, "venue said no")
        assert om.get(a.client_order_id).state is S.REJECTED
        b = om.mark_pending(om.create(intent()).client_order_id)
        om.apply_event(_ev(b, S.ACCEPTED))
        assert [o.client_order_id for o in om.open_orders()] == [b.client_order_id]
        om.apply_event(_ev(b, S.CANCELED))
        assert om.open_orders() == []
        c = om.create(intent())
        om.reject_new(c.client_order_id, "risk")
        assert om.get(c.client_order_id).state is S.REJECTED
