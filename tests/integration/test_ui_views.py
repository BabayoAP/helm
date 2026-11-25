"""UI-3 chart view (bars + trade markers + order lines), UI-4 watchlist in the snapshot,
UI-5 research view (experiments + promotion log), via the authenticated API."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from helm.api.app import create_app
from helm.app.control import EnginePort
from helm.core.clock import SimClock
from helm.core.enums import LifecycleStage, OrderType, Side
from helm.core.models import OrderIntent
from helm.research.experiments.store import ExperimentStore
from helm.research.promotion import PromotionRegistry, Version
from helm.strategy.examples.sma_cross import SmaCross
from helm.tracking.watchlist import Watchlist
from tests.factories import SYM, bar, quote
from tests.integration.test_control_plane import TOKEN, paper_engine

AUTH = {"Authorization": f"Bearer {TOKEN}"}
D = Decimal


def test_chart_watchlist_and_research_endpoints(tmp_path: Path) -> None:
    clock = SimClock(0)
    eng = paper_engine(tmp_path, clock)
    wl = Watchlist([], traded=[SYM])

    async def drive() -> None:
        for i in range(5):
            b = bar(i, 100 + i, 101 + i, 99 + i, 100.5 + i)
            clock.advance_to(max(eng.clock.now(), b.ts_close))
            q = quote(str(100 + i), str(100.1 + i), ts=eng.clock.now())
            wl.update(q)
            await eng.on_quote(q)
            await eng.on_bar(b)
            wl.update(b)
        buy = OrderIntent(
            strategy_id="manual", symbol=SYM, side=Side.BUY, type=OrderType.MARKET, qty=D("0.1")
        )
        await eng.submit_intent(None, buy)
        resting = OrderIntent(
            strategy_id="manual",
            symbol=SYM,
            side=Side.BUY,
            type=OrderType.LIMIT,
            qty=D("0.1"),
            limit_price=D(102),
        )
        await eng.submit_intent(None, resting)
        nxt = bar(5, 105, 106, 104, 105.5)  # fills the market order; the 102 limit keeps resting
        clock.advance_to(nxt.ts_close)
        await eng.on_quote(quote("105", "105.1", ts=eng.clock.now()))  # fresh feed: no stale block
        await eng.on_bar(nxt)
        await eng._drain_broker()  # pyright: ignore[reportPrivateUsage]

    asyncio.run(drive())
    db = tmp_path / "research.sqlite"
    st = ExperimentStore(db)
    st.record(
        run_id="val-1",
        kind="validation",
        strategy=SmaCross(),
        params={"fast": 10},
        data_hash="d",
        data_desc="{}",
        spec_hash="",
        seed=0,
        n_trials=33,
        metrics={"best_is_sharpe": 1.2},
        result_hash="",
        passed=False,
        report={"checks": [{"name": "deflated_sharpe", "passed": False, "detail": "DSR 0.2"}]},
        git="abc",
    )
    st.close()
    reg = PromotionRegistry(db)
    reg._append(  # pyright: ignore[reportPrivateUsage]
        Version("s:S", "c", "p"), LifecycleStage.DRAFT, LifecycleStage.BACKTESTED, {}, "aaron", "ok"
    )
    reg.close()

    port = EnginePort(eng, venue="fake", watchlist=wl, research_db=db)
    c = TestClient(create_app(port, token=TOKEN), base_url="http://127.0.0.1:8765")
    assert c.get(f"/api/chart?symbol={SYM}").status_code == 401
    ch = c.get(f"/api/chart?symbol={SYM}", headers=AUTH).json()
    assert len(ch["bars"]) == 6 and ch["timeframe"] == "1m"
    assert ch["bars"][0]["o"] == "100"
    assert [m["side"] for m in ch["markers"]] == ["buy"]  # the market order filled
    assert [(ln["side"], ln["price"]) for ln in ch["order_lines"]] == [
        ("buy", "102.00")
    ]  # quantized to tick
    assert c.get("/api/chart?symbol=fake:NOPE/USD", headers=AUTH).status_code == 404

    snap = c.get("/api/snapshot", headers=AUTH).json()
    (row,) = snap["watchlist"]
    assert (
        row["symbol"] == SYM and row["traded"] and row["last"] == "104.5"
    )  # watchlist saw bars 0-4

    rs = c.get("/api/research", headers=AUTH).json()
    (exp,) = rs["experiments"]
    assert exp["run_id"] == "val-1" and exp["passed"] is False and exp["sharpe"] == 1.2
    assert exp["failures"] == ["deflated_sharpe: DSR 0.2"]
    (pr,) = rs["promotions"]
    assert (pr["to_stage"], pr["approved_by"]) == ("BACKTESTED", "aaron")
    no_db = EnginePort(eng, venue="fake").research()
    assert no_db.experiments == () and no_db.promotions == ()
