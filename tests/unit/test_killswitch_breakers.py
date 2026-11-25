"""Kill switch (PRD §10, EXE-2) and circuit breakers RISK-10..15."""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from helm.core.config import DailyReset, RiskConfig
from helm.core.enums import EngineState, FeedState
from helm.risk.breakers import Action, BreakerInputs, FeedHealth, check_breakers, day_start_ns
from helm.risk.killswitch import KillSwitch, ResumeDeniedError

D = Decimal
S = EngineState
NS = 10**9


def _ks(state: EngineState = S.RUNNING) -> KillSwitch:
    codes = (f"code{i}" for i in itertools.count())
    return KillSwitch(lambda: next(codes), lambda: 7, state)


def test_kill_switch_latch_and_one_time_code() -> None:
    ks = _ks()
    assert ks.can_open_risk
    ev = ks.pause("manual")
    assert ev is not None and ev.current is S.PAUSED and not ks.can_open_risk
    assert ks.pause("again") is None  # idempotent
    assert ks.resume() is not None and ks.state is S.RUNNING  # PAUSED -> RUNNING needs no code
    ev, code = ks.halt("kill")
    assert ev is not None and ev.current is S.HALTED and code == "code0"
    assert ks.pause("x") is None and ks.state is S.HALTED  # pause can't unlatch
    with pytest.raises(ResumeDeniedError):
        ks.resume()
    with pytest.raises(ResumeDeniedError):
        ks.resume("wrong")
    code2 = ks.issue_code()  # re-issue invalidates the old one
    with pytest.raises(ResumeDeniedError):
        ks.resume(code)
    assert ks.resume(code2) is not None and ks.state is S.RUNNING
    ev2, code3 = ks.halt("again")
    assert ev2 is not None
    ks.resume(code3)
    ks.halt("x")
    with pytest.raises(ResumeDeniedError):
        ks.resume(code3)  # single use


def test_restart_policy() -> None:
    assert KillSwitch.after_restart(S.HALTED, lambda: "c", lambda: 0).state is S.HALTED
    assert KillSwitch.after_restart(S.RUNNING, lambda: "c", lambda: 0).state is S.PAUSED
    assert KillSwitch.after_restart(None, lambda: "c", lambda: 0).state is S.PAUSED
    ks = KillSwitch.after_restart(S.HALTED, lambda: "c", lambda: 0)
    with pytest.raises(ResumeDeniedError):
        ks.resume("c")  # no code issued yet after restart
    ks.resume(ks.issue_code())


def _inp(**kw: object) -> BreakerInputs:
    base: dict[str, object] = {
        "now": 100 * NS,
        "equity": D(10_000),
        "start_of_day_equity": D(10_000),
        "high_water_mark": D(10_000),
        "feeds": {},
        "blocked_symbols": frozenset(),
        "reconcile_mismatch": None,
        "clock_skew_ms": 1.0,
    }
    base.update(kw)
    return BreakerInputs(**base)  # type: ignore[arg-type]


def _acts(inp: BreakerInputs, cfg: RiskConfig | None = None) -> list[tuple[str, Action]]:
    return [(t.rule_id, t.action) for t in check_breakers(inp, cfg or RiskConfig())]


def test_daily_loss_and_drawdown() -> None:
    assert _acts(_inp()) == []
    assert _acts(_inp(equity=D(9_840))) == [("RISK-10", Action.WARN)]  # 1.6% >= 50% of 3%
    assert _acts(_inp(equity=D(9_750))) == [("RISK-10", Action.WARN)]  # 2.5% >= 80%
    assert _acts(_inp(equity=D(9_699))) == [("RISK-10", Action.FLATTEN_AND_HALT)]
    cancel = RiskConfig(on_daily_loss="cancel_and_halt")
    assert _acts(_inp(equity=D(9_699)), cancel) == [("RISK-10", Action.CANCEL_AND_HALT)]
    hwm = _inp(high_water_mark=D(12_000), start_of_day_equity=D(10_000), equity=D(10_700))
    assert _acts(hwm) == [("RISK-11", Action.FLATTEN_AND_HALT)]  # 1300 / 12000 = 10.83% > 10%
    assert _acts(
        _inp(high_water_mark=D(12_000), equity=D(10_900), start_of_day_equity=D(10_900))
    ) == [("RISK-11", Action.WARN)]
    assert _acts(
        _inp(high_water_mark=D(12_000), equity=D(11_300), start_of_day_equity=D(11_300))
    ) == [("RISK-11", Action.WARN)]
    assert _acts(_inp(start_of_day_equity=D(0), high_water_mark=D(10_000)))[0] == (
        "RISK-10",
        Action.FLATTEN_AND_HALT,
    )


def test_per_strategy_loss_cap_pauses_only_that_strategy() -> None:
    caps = {"a": D(100), "b": D(100)}
    inp = _inp(strategy_day_pnl={"a": D(-100), "b": D(-40)}, strategy_loss_caps=caps)
    trips = check_breakers(inp, RiskConfig())
    assert [(t.rule_id, t.action, t.strategy) for t in trips] == [
        ("RISK-8", Action.PAUSE_STRATEGY, "a")
    ]
    already = _inp(
        strategy_day_pnl={"a": D(-500)}, strategy_loss_caps=caps, paused_strategies=frozenset("a")
    )
    assert check_breakers(already, RiskConfig()) == []  # no repeat while paused
    assert check_breakers(_inp(strategy_loss_caps=caps), RiskConfig()) == []  # no PnL yet


def test_feed_staleness_block_and_clear() -> None:
    now = 100 * NS
    fresh = FeedHealth(FeedState.LIVE, now - 100 * 10**6, now - 20 * NS)
    stale = FeedHealth(FeedState.LIVE, now - 3 * NS, now - 50 * NS)
    down = FeedHealth(FeedState.RECONNECTING, None, None)
    recovering = FeedHealth(FeedState.LIVE, now, now - 2 * NS)
    feeds = {"a": fresh, "b": stale, "c": down, "d": recovering, "e": fresh}
    trips = check_breakers(
        _inp(feeds=feeds, blocked_symbols=frozenset({"c", "d", "e"})), RiskConfig()
    )
    assert [(t.action, t.symbol) for t in trips] == [
        (Action.BLOCK_SYMBOL, "b"),
        (Action.CLEAR_SYMBOL, "e"),
    ]
    unknown_since = FeedHealth(FeedState.LIVE, now, None)
    assert (
        _acts(_inp(feeds={"x": unknown_since}, blocked_symbols=frozenset({"x"}))) == []
    )  # healthy_for = 0 < 10 s


def test_reconcile_and_clock_skew() -> None:
    assert _acts(_inp(reconcile_mismatch="qty")) == [("RISK-13", Action.PAUSE)]
    assert _acts(_inp(clock_skew_ms=None)) == [("RISK-15", Action.WARN)]
    assert _acts(_inp(clock_skew_ms=300.0)) == [("RISK-15", Action.WARN)]
    assert _acts(_inp(clock_skew_ms=-1500.0)) == [("RISK-15", Action.PAUSE)]


def test_day_start() -> None:
    utc = DailyReset()
    ts = 1_704_110_400 * NS  # 2024-01-01 12:00 UTC
    assert day_start_ns(ts, utc) == 1_704_067_200 * NS
    ny = DailyReset(tz="America/New_York", time="09:30")
    early = 1_704_117_600 * NS  # 2024-01-01 14:00 UTC = 09:00 NY -> previous day's 09:30
    assert day_start_ns(early, ny) == (1_704_117_600 - 30 * 60 - 86_400 + 3600) * NS
