"""BT-4: L1 replay backtester with a queue-position model for passive orders.

Replays a recorded feed deterministically; resting limits fill on prints through
the limit (always) and on prints AT the limit only after the estimated queue
ahead of them has traded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from helm.app.backtest import BacktestSpec, run_replay
from helm.backtest.friction.models import FrictionConfig
from helm.brokers.sim.broker import SimBroker, SimMode
from helm.cli import main
from helm.core.clock import SimClock
from helm.core.config import RiskConfig
from helm.core.enums import OrderType
from helm.core.models import Fill, Order, Quote, Trade
from helm.data.recorder import Recorder, replay
from helm.data.synthetic_feed import synthetic_feed
from tests.factories import SYM, instrument, intent
from tests.replay.test_parity import QuoteMaker

D = Decimal
T0 = 1_700_000_000 * 10**9


def q(bid: str, bsz: str, ask: str = "100.10", asz: str = "5", ts: int = 1) -> Quote:
    return Quote(
        symbol=SYM, bid=D(bid), bid_sz=D(bsz), ask=D(ask), ask_sz=D(asz), ts_event=ts, ts_recv=ts
    )


def tr(px: str, size: str, ts: int = 2, tid: str = "t") -> Trade:
    return Trade(symbol=SYM, price=D(px), size=D(size), trade_id=tid, ts_event=ts, ts_recv=ts)


def broker(queue: bool) -> SimBroker:
    return SimBroker(
        account_id="paper-q",
        venue="fake",
        clock=SimClock(1),
        mode=SimMode.L1,
        friction=FrictionConfig.off(),
        queue_model=queue,
    )


def rest(b: SimBroker, limit: str, qty: str = "2", coid: str = "c0") -> None:
    order = Order(
        client_order_id=coid,
        account_id="paper-q",
        intent=intent(qty=qty, type_=OrderType.LIMIT, limit=limit),
        ts_created=1,
        ts_updated=1,
    )
    asyncio.run(b.submit(order))


def filled(b: SimBroker) -> list[Decimal]:
    return [e.qty for e in b.drain() if isinstance(e, Fill)]


def test_joining_the_touch_waits_for_the_queue_ahead() -> None:
    b = broker(queue=True)
    b.on_quote(q("100", "5"))
    rest(b, "100")  # joins behind 5 displayed at 100
    assert b._queue_ahead["c0"] == D(5)  # pyright: ignore[reportPrivateUsage]
    filled(b)
    b.on_trade(tr("100", "3"))  # 3 of the 5 ahead trade
    assert filled(b) == []
    b.on_quote(q("100", "1"))  # displayed size drops to 1: cancels ahead of us
    b.on_trade(tr("100", "2"))  # 1 ahead + 1 for us
    assert filled(b) == [D(1)]
    b.on_trade(tr("100", "5"))
    assert filled(b) == [D(1)]  # the rest of our 2


def test_no_queue_model_never_fills_at_the_limit_only_through_it() -> None:
    b = broker(queue=False)
    b.on_quote(q("100", "5"))
    rest(b, "100")
    filled(b)
    b.on_trade(tr("100", "50"))
    assert filled(b) == []  # conservative paper model: at-the-limit prints don't fill
    b.on_trade(tr("99.99", "1"))
    assert filled(b) == [D(1)]  # through the limit: fills in both models


def test_improving_the_touch_is_first_and_behind_the_touch_waits_for_it() -> None:
    b = broker(queue=True)
    b.on_quote(q("100", "5"))
    rest(b, "100.05", qty="1", coid="better")  # improves the bid: nobody ahead
    rest(b, "99.90", coid="behind")  # rests behind the touch: queue unknown
    filled(b)
    qa = b._queue_ahead  # pyright: ignore[reportPrivateUsage]
    assert qa["better"] == 0 and qa["behind"] is None
    b.on_trade(tr("100.05", "1"))
    assert filled(b) == [D(1)]
    b.on_trade(tr("99.90", "10"))
    assert filled(b) == []  # level never was the touch: no fill on a stray print
    b.on_quote(q("99.90", "4"))  # touch moves down to our level
    assert qa["behind"] == D(4)


def _record(tmp: Path) -> tuple[Path, Sequence[BaseModel]]:
    async def go() -> Path:
        rec = Recorder(tmp, "feed", flush_every=4000)
        async for ev in synthetic_feed(SYM, n_events=8000, start_ns=T0, seed=5):
            rec.record(ev)
        rec.close()
        return rec.dir

    d = asyncio.run(go())
    return d, list(replay(d))


def _spec() -> BacktestSpec:
    return BacktestSpec(
        strategy=QuoteMaker(),
        instruments=[instrument()],
        risk=RiskConfig(max_position_pct_equity=D(50), max_order_notional_usd=D(5_000)),
        friction=FrictionConfig(seed=4),
    )


def test_replay_is_deterministic_and_queue_model_changes_fills(tmp_path: Path) -> None:
    _d, events = _record(tmp_path)
    a = run_replay(_spec(), events)
    b = run_replay(_spec(), events)
    assert a.result_hash == b.result_hash and a.data_hash == b.data_hash  # I7
    assert len(a.fills) > 0
    off = run_replay(_spec(), events, queue_model=False)
    assert off.run_id != a.run_id
    # with the queue model, prints at the limit can fill once the queue is gone, so
    # passive orders fill at least as often as with through-only matching
    assert len(a.fills) >= len(off.fills)
    assert a.result_hash != off.result_hash  # the queue model changes which fills happen


def test_backtest_replay_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    d, _ = _record(tmp_path)
    args = ["backtest", "--replay", str(d), "--out", str(tmp_path / "rep"), "--no-record"]
    args += ["--strategy", "helm.strategy.examples.sma_cross:SmaCross", "--param", "qty=0.5"]
    args += ["--param", "fast=3", "--param", "slow=8"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert '"events": ' in out and "tearsheet" in out
    assert main(["backtest", "--replay", str(tmp_path / "empty"), "--no-record"]) == 2
