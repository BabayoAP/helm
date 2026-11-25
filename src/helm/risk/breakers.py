"""Runtime circuit breakers RISK-10..13, RISK-15 (PRD §10). Pure functions.

The supervisor gathers `BreakerInputs` on a timer and executes the returned
trips (flatten/halt/pause/cancel). Strategies cannot influence any of this.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from helm.core.config import DailyReset, RiskConfig
from helm.core.enums import FeedState

HUNDRED = Decimal(100)
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


class Action(StrEnum):
    FLATTEN_AND_HALT = "flatten_and_halt"
    CANCEL_AND_HALT = "cancel_and_halt"
    PAUSE = "pause"
    BLOCK_SYMBOL = "block_symbol"  # cancel resting orders for symbol + block new entries
    CLEAR_SYMBOL = "clear_symbol"
    PAUSE_STRATEGY = "pause_strategy"  # RISK-8: one strategy over its own loss cap
    WARN = "warn"


@dataclass(frozen=True)
class Trip:
    rule_id: str
    action: Action
    reason: str
    symbol: str | None = None
    strategy: str | None = None


@dataclass(frozen=True)
class FeedHealth:
    state: FeedState
    last_data_ns: int | None  # last data received (skew-corrected ts_recv)
    healthy_since_ns: int | None  # when it last became healthy


@dataclass(frozen=True)
class BreakerInputs:
    now: int
    equity: Decimal
    start_of_day_equity: Decimal
    high_water_mark: Decimal
    feeds: Mapping[str, FeedHealth]
    blocked_symbols: frozenset[str]
    reconcile_mismatch: str | None  # description, or None when reconciled
    clock_skew_ms: float | None  # None = NTP check failed
    # RISK-8 / EXE-6: per-strategy PnL since the daily reset, and each strategy's cap
    strategy_day_pnl: Mapping[str, Decimal] = field(default_factory=lambda: {})
    strategy_loss_caps: Mapping[str, Decimal] = field(default_factory=lambda: {})
    paused_strategies: frozenset[str] = frozenset()


def day_start_ns(ts_ns: int, reset: DailyReset) -> int:
    """Start of the trading day containing ts_ns for a configured reset time/timezone."""
    tz = ZoneInfo(reset.tz)
    hh, mm = (int(x) for x in reset.time.split(":"))
    local = datetime.fromtimestamp(ts_ns / NS_PER_S, tz=UTC).astimezone(tz)
    start = datetime.combine(local.date(), time(hh, mm), tzinfo=tz)
    if start > local:
        start -= timedelta(days=1)
    return int(start.timestamp()) * NS_PER_S


def _loss_pct(reference: Decimal, equity: Decimal) -> Decimal:
    if reference <= 0:
        return HUNDRED
    return (reference - equity) / reference * HUNDRED


def check_breakers(inp: BreakerInputs, cfg: RiskConfig) -> list[Trip]:
    trips: list[Trip] = []

    # RISK-10 daily loss (+ OPS-2 warnings at 50% / 80% of the limit)
    daily = _loss_pct(inp.start_of_day_equity, inp.equity)
    if daily > cfg.daily_loss_limit_pct:
        act = (
            Action.FLATTEN_AND_HALT
            if cfg.on_daily_loss == "flatten_and_halt"
            else Action.CANCEL_AND_HALT
        )
        trips.append(Trip("RISK-10", act, f"daily loss {daily:.2f}% > {cfg.daily_loss_limit_pct}%"))
    else:
        for frac in (Decimal("0.8"), Decimal("0.5")):
            if daily >= cfg.daily_loss_limit_pct * frac:
                trips.append(
                    Trip("RISK-10", Action.WARN, f"daily loss at {int(frac * 100)}% of limit")
                )
                break

    # RISK-11 drawdown from high-water mark
    dd = _loss_pct(inp.high_water_mark, inp.equity)
    if dd > cfg.max_drawdown_pct:
        trips.append(
            Trip(
                "RISK-11", Action.FLATTEN_AND_HALT, f"drawdown {dd:.2f}% > {cfg.max_drawdown_pct}%"
            )
        )
    else:
        for frac in (Decimal("0.8"), Decimal("0.5")):
            if dd >= cfg.max_drawdown_pct * frac:
                trips.append(
                    Trip("RISK-11", Action.WARN, f"drawdown at {int(frac * 100)}% of limit")
                )
                break

    # RISK-12 per-symbol feed staleness (block) and auto-clear after N healthy seconds
    stale_ns = cfg.stale_feed_ms * NS_PER_MS
    for sym in sorted(inp.feeds):
        h = inp.feeds[sym]
        stale = (
            h.state is not FeedState.LIVE
            or h.last_data_ns is None
            or inp.now - h.last_data_ns > stale_ns
        )
        if stale and sym not in inp.blocked_symbols:
            trips.append(Trip("RISK-12", Action.BLOCK_SYMBOL, f"feed {h.state.value} / stale", sym))
        elif not stale and sym in inp.blocked_symbols:
            healthy_for = inp.now - (
                h.healthy_since_ns if h.healthy_since_ns is not None else inp.now
            )
            if healthy_for >= cfg.feed_healthy_clear_s * NS_PER_S:
                trips.append(Trip("RISK-12", Action.CLEAR_SYMBOL, "feed healthy", sym))

    # RISK-8 per-strategy daily loss cap: pause only that strategy (global caps still apply)
    for strat in sorted(inp.strategy_loss_caps):
        cap = inp.strategy_loss_caps[strat]
        pnl = inp.strategy_day_pnl.get(strat, Decimal(0))
        if pnl <= -cap and strat not in inp.paused_strategies:
            trips.append(
                Trip(
                    "RISK-8",
                    Action.PAUSE_STRATEGY,
                    f"{strat} day PnL {pnl:.2f} hit its loss cap -{cap}",
                    strategy=strat,
                )
            )

    # RISK-13 reconciliation mismatch
    if inp.reconcile_mismatch is not None:
        trips.append(
            Trip("RISK-13", Action.PAUSE, f"reconciliation mismatch: {inp.reconcile_mismatch}")
        )

    # RISK-15 clock skew
    if inp.clock_skew_ms is None:
        trips.append(Trip("RISK-15", Action.WARN, "NTP check failed; feed-lag checks degraded"))
    elif abs(inp.clock_skew_ms) > cfg.clock_skew_pause_ms:
        trips.append(Trip("RISK-15", Action.PAUSE, f"clock skew {inp.clock_skew_ms:.0f} ms"))
    elif abs(inp.clock_skew_ms) > cfg.clock_skew_warn_ms:
        trips.append(Trip("RISK-15", Action.WARN, f"clock skew {inp.clock_skew_ms:.0f} ms"))
    return trips
