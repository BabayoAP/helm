"""§13 evidence: parity of a real paper run vs a backtest replay of its recorded feed,
and the PAP-4 paper-vs-live divergence report."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from helm.app.engine import Engine
from helm.app.paper import PaperRunner, RunnerConfig
from helm.app.research import instrument_for_symbol
from helm.backtest.friction.models import FrictionConfig
from helm.brokers.sim.broker import SimBroker, SimMode
from helm.cli import main
from helm.core.clock import SimClock
from helm.core.config import RiskConfig
from helm.core.enums import Liquidity, Mode, OrderEventKind, OrderType, Side
from helm.core.events import OrderEvent
from helm.core.models import Fill, Order, OrderIntent
from helm.data.instruments import InstrumentRegistry
from helm.data.recorder import Recorder, replay
from helm.data.synthetic_feed import synthetic_feed
from helm.oms.accounting import Portfolio
from helm.oms.journal import Journal
from helm.research.experiments.store import ExperimentStore, code_hash, params_hash
from helm.research.promotion import Version, gather_evidence
from helm.research.reports import AccountRecord, divergence_report
from helm.strategy.examples.sma_cross import SmaCross

SYM = "fake:BTC/USD"
T0 = 1_700_000_000 * 10**9
PARAMS = {"fast": 3, "slow": 8, "qty": 0.5}
D = Decimal


def _ts(ev: BaseModel) -> int:
    return int(getattr(ev, "ts_recv", None) or getattr(ev, "ts_close", 0))


def _paper_run(tmp: Path) -> tuple[Path, Path]:
    """A paper session exactly like `helm paper --record` would leave behind."""

    async def go() -> Path:
        rec = Recorder(tmp, "session", flush_every=5000)
        async for ev in synthetic_feed(SYM, n_events=10_000, start_ns=T0, seed=8):
            rec.record(ev)
        rec.close()
        events = list(replay(rec.dir))
        clock = SimClock(T0)
        eng = Engine(
            mode=Mode.PAPER,
            account_id="paper-p",
            run_id="paperrun",
            clock=clock,
            broker=SimBroker(
                account_id="paper-p",
                venue="fake",
                clock=clock,
                mode=SimMode.L1,
                friction=FrictionConfig(),
                starting_cash=D(10_000),
            ),
            risk_config=RiskConfig(),
            journal=Journal(tmp / "paper.sqlite", mirror=False),
            instruments=InstrumentRegistry([instrument_for_symbol(SYM)]),
            portfolio=Portfolio(starting_equity=D(10_000)),
            code_fn=lambda: "c",
        )
        eng.add_strategy(SmaCross(), symbols=(SYM,), params=dict(PARAMS))

        async def feed() -> AsyncIterator[BaseModel]:
            for i, ev in enumerate(events):
                clock.advance_to(max(clock.now(), _ts(ev)))
                yield ev
                if i % 200 == 0:
                    await asyncio.sleep(0)

        await PaperRunner(
            eng, feed(), config=RunnerConfig(supervise_every_s=0.001, reconcile_every_s=0.005)
        ).run()
        eng.journal.close()
        return rec.dir

    return asyncio.run(go()), tmp / "paper.sqlite"


def test_parity_of_a_paper_run_counts_as_promotion_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session, journal = _paper_run(tmp_path)
    db = tmp_path / "research.sqlite"
    args = ["research", "parity", "--recording", str(session), "--paper-journal", str(journal)]
    args += ["--paper-account", "paper-p", "--symbol", SYM, "--db", str(db)]
    args += ["--param", "fast=3", "--param", "slow=8", "--param", "qty=0.5"]
    assert main(args) == 0, capsys.readouterr().out
    rep = json.loads(capsys.readouterr().out)
    assert rep["passed"] and rep["paper_orders"] >= 3 and rep["first_mismatch"] is None
    v = Version(
        "helm.strategy.examples.sma_cross:SmaCross", code_hash(SmaCross()), params_hash(PARAMS)
    )
    assert gather_evidence(ExperimentStore(db), v).parity_confirmed  # LIVE_SMALL gate input
    # a different strategy configuration does not reproduce the paper run
    bad = [*args[:-6], "--param", "fast=3", "--param", "slow=8", "--param", "qty=0.3"]
    assert main(bad) == 1
    assert json.loads(capsys.readouterr().out)["first_mismatch"] is not None
    assert main([*args[:3], str(tmp_path / "nothing"), *args[4:]]) == 2


def _order(coid: str, side: Side, ts: int, qty: str = "1") -> Order:
    it = OrderIntent(strategy_id="s", symbol=SYM, side=side, type=OrderType.MARKET, qty=D(qty))
    return Order(client_order_id=coid, account_id="a", intent=it, ts_created=ts, ts_updated=ts)


def _fill(coid: str, side: Side, px: str, ts: int) -> Fill:
    return Fill(
        client_order_id=coid,
        venue="v",
        trade_id=coid,
        symbol=SYM,
        side=side,
        price=D(px),
        qty=D(1),
        fee_ccy="USD",
        liquidity=Liquidity.TAKER,
        ts=ts,
    )


def test_divergence_report_measures_slippage_and_latency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = 10**9
    paper = AccountRecord(
        orders=[
            _order("p1", Side.BUY, 0),
            _order("p2", Side.SELL, 60 * s),
            _order("p3", Side.BUY, 999 * s),
        ],
        fills=[
            _fill("p1", Side.BUY, "100", s // 10),
            _fill("p2", Side.SELL, "101", 60 * s + s // 10),
        ],
    )
    live = AccountRecord(
        orders=[
            _order("l1", Side.BUY, s),
            _order("l2", Side.SELL, 61 * s),
            _order("l9", Side.BUY, 5000 * s),
        ],
        fills=[
            _fill("l1", Side.BUY, "100.10", s + s // 2),
            _fill("l2", Side.SELL, "100.90", 61 * s + s // 2),
        ],
    )
    rep = divergence_report(paper, live, window_s=5)
    assert (rep.matched, rep.unmatched_live, rep.unmatched_paper) == (2, 1, 1)
    # buy 10 bps worse, sell ~9.9 bps worse: positive = live worse than paper
    assert rep.price_delta_bps["n"] == 2 and 9.8 < rep.price_delta_bps["median"] < 10.1
    assert rep.paper_latency_ms["mean"] == 100 and rep.live_latency_ms["mean"] == 500
    assert rep.suggested_extra_slippage_bps == rep.price_delta_bps["median"]
    assert divergence_report(AccountRecord(), AccountRecord()).price_delta_bps == {"n": 0}

    # CLI over journals, recorded for a strategy version (input to the LIVE gate)
    def journal(path: Path, acct: AccountRecord, account: str) -> None:
        with Journal(path, mirror=False) as j:
            for o in acct.orders:
                ev = OrderEvent(
                    kind=OrderEventKind.SUBMITTED,
                    client_order_id=o.client_order_id,
                    state=o.state,
                    order=o,
                    ts=o.ts_created,
                )
                j.append(ev, ts=o.ts_created, run_id="r", account_id=account, key=o.client_order_id)
            for f in acct.fills:
                j.append(f, ts=f.ts, run_id="r", account_id=account, key=f.client_order_id)

    journal(tmp_path / "p.sqlite", paper, "paper-x")
    journal(tmp_path / "l.sqlite", live, "live-x")
    db = tmp_path / "r.sqlite"
    args = ["research", "divergence", "--paper-journal", str(tmp_path / "p.sqlite")]
    args += ["--paper-account", "paper-x", "--live-journal", str(tmp_path / "l.sqlite")]
    args += ["--live-account", "live-x", "--window-s", "5", "--db", str(db)]
    args += ["--strategy", "helm.strategy.examples.sma_cross:SmaCross", "--param", "fast=3"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["matched"] == 2
    v = Version(
        "helm.strategy.examples.sma_cross:SmaCross", code_hash(SmaCross()), params_hash({"fast": 3})
    )
    ev = gather_evidence(ExperimentStore(db), v)
    assert ev.divergence_report and not ev.divergence_confirmed  # still needs a human review
