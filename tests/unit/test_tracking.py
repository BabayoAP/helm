"""TRK-1 watchlist + TRK-2 alert rules (edge-triggered, throttled, no storms)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from helm.app.paper import PaperRunner, RunnerConfig
from helm.app.services import tracking_service
from helm.core.config import AlertRuleConfig
from helm.core.control import WatchRow
from helm.core.enums import EngineState, Mode, Severity
from helm.tracking.alerts import AlertManager
from helm.tracking.rules import RuleContext, RuleEngine, condition
from helm.tracking.watchlist import DAY_NS, Watchlist
from tests.factories import MIN, SYM, bar, quote
from tests.integration.test_control_plane import paper_engine

D = Decimal
OTHER = "fake:ETH/USD"


def test_watchlist_change_spread_and_volume_z() -> None:
    wl = Watchlist([OTHER], traded=[SYM], z_window=10)
    assert wl.symbols == [OTHER, SYM]
    wl.update(quote("99.9", "100.1", ts=1, symbol=SYM))
    r = wl.row(SYM)
    assert (
        r.last == D(100) and r.spread_pct is not None and abs(r.spread_pct - D("0.2")) < D("1e-9")
    )
    assert r.traded and r.change_24h_pct is None and r.volume_z is None
    # one day of hourly bars at 100, then a jump to 110 on 10x volume
    n = 25
    for i in range(n):
        b = bar(i * 60, 100, 100, 100, 100, v=10 + (i % 3))
        wl.update(b)
    wl.update(bar(n * 60, 100, 110, 100, 110, v=150))
    r = wl.row(SYM)
    assert r.last == D(110)
    assert r.change_24h_pct is not None and D(9) < r.change_24h_pct <= D(10)
    assert r.volume_z is not None and r.volume_z > 50
    wl.update(bar(0, 1, 1, 1, 1, symbol="fake:NOT/WATCHED"))  # ignored
    assert len(wl.rows()) == 2 and wl.row(OTHER).last is None
    assert DAY_NS == 1440 * MIN


def rule(kind: str, threshold: str = "0", symbol: str | None = SYM, **kw: str) -> AlertRuleConfig:
    return AlertRuleConfig.model_validate(
        {
            "name": f"{kind}-{threshold}",
            "kind": kind,
            "threshold": threshold,
            "symbol": symbol,
            **kw,
        }
    )


def test_each_rule_kind() -> None:
    row = WatchRow(symbol=SYM, last=D(105), change_24h_pct=D(-6), spread_pct=D("0.4"), volume_z=4.2)
    ctx = RuleContext(
        rows={SYM: row},
        daily_pnl=D(-120),
        drawdown_pct=D(7),
        feeds={SYM: "LIVE", OTHER: "STALE"},
        engine_state=EngineState.PAUSED,
    )
    hits = {
        ("price_above", "100"): True,
        ("price_above", "110"): False,
        ("price_below", "110"): True,
        ("change_pct_abs_above", "5"): True,
        ("spread_pct_above", "0.5"): False,
        ("volume_z_above", "3"): True,
        ("daily_pnl_below", "-100"): True,
        ("drawdown_pct_above", "8"): False,
    }
    for (kind, th), want in hits.items():
        res = condition(rule(kind, th), ctx)
        assert res is not None and res[0] is want, (kind, th, res)
    assert condition(rule("feed_not_live", symbol=None), ctx) == (
        True,
        "feeds not live: fake:ETH/USD=STALE",
    )
    assert condition(rule("feed_not_live", symbol=SYM), ctx) == (False, "feeds not live: ")
    assert condition(rule("engine_not_running", symbol=None), ctx) == (True, "engine PAUSED")
    # missing data => no decision (never a false alarm, never a false all-clear)
    empty = RuleContext(rows={SYM: WatchRow(symbol=SYM)})
    for kind in ("price_above", "change_pct_abs_above", "spread_pct_above", "volume_z_above"):
        assert condition(rule(kind), empty) is None
    assert condition(rule("price_above", symbol="fake:NONE/USD"), empty) is None
    assert condition(rule("feed_not_live", symbol=None), empty) is None
    with pytest.raises(ValidationError, match="needs a symbol"):
        rule("price_above", symbol=None)


def test_rules_are_edge_triggered_and_storms_are_bounded() -> None:
    now = [0]
    am = AlertManager(mode=Mode.PAPER, clock_fn=lambda: now[0])
    eng = RuleEngine([rule("price_above", "100", severity="CRITICAL")], am)
    up = RuleContext(rows={SYM: WatchRow(symbol=SYM, last=D(101))})
    down = RuleContext(rows={SYM: WatchRow(symbol=SYM, last=D(99))})
    assert eng.evaluate(up) == ["price_above-100"]
    for _ in range(1000):  # condition stays true: no repeats
        assert eng.evaluate(up) == []
    assert len(am.sent) == 1 and am.sent[0].severity is Severity.CRITICAL
    # flapping 1000 times inside the throttle window: AlertManager coalesces
    for _ in range(1000):
        eng.evaluate(down)
        eng.evaluate(up)
    assert len(am.sent) == 1 and am.suppressed_total == 1000
    with pytest.raises(ValueError, match="unique"):
        RuleEngine([rule("price_above"), rule("price_above")], am)


def test_runner_keeps_watch_only_symbols_away_from_the_engine(tmp_path: Path) -> None:
    async def run() -> None:
        eng = paper_engine(tmp_path)
        wl = Watchlist([OTHER], traded=[SYM])
        ts = eng.clock.now()

        async def feed() -> AsyncIterator[BaseModel]:
            yield quote(ts=ts, symbol=SYM)
            yield quote("2000", "2001", ts=ts, symbol=OTHER)

        r = PaperRunner(
            eng,
            feed(),
            config=RunnerConfig(supervise_every_s=60, reconcile_every_s=60, flatten_realert_s=60),
            on_event=wl.update,
            engine_symbols=frozenset([SYM]),
        )
        await r.run()
        assert set(eng.feeds) == {SYM}  # no stale-feed breaker for watch-only symbols
        assert wl.row(OTHER).last == D("2000.5") and r.events == 2

        # tracking service evaluates rules against watchlist + engine state
        am_before = len(eng.alerts.sent)
        svc = tracking_service(
            eng,
            wl,
            RuleEngine([rule("price_above", "1000", symbol=OTHER)], eng.alerts),
            every_s=0.001,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(svc(stop))
        await asyncio.sleep(0.02)
        stop.set()
        await task
        new = eng.alerts.sent[am_before:]
        assert [a.key for a in new] == ["rule:price_above-1000"]

    asyncio.run(run())
