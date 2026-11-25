"""Alert rules (TRK-2): price / change / spread / volume anomaly thresholds, feed and
engine health, PnL and drawdown. Pure evaluation over a context snapshot.

Edge-triggered: a rule fires once when its condition becomes true and re-arms
only after it has been false again. Delivery goes through AlertManager, which
adds per-key dedupe/throttling (no storms) and the Telegram/UI sinks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from helm.core.config import AlertRuleConfig
from helm.core.control import WatchRow
from helm.core.enums import EngineState, Severity
from helm.tracking.alerts import AlertManager


@dataclass(frozen=True)
class RuleContext:
    rows: Mapping[str, WatchRow] = field(default_factory=lambda: {})
    daily_pnl: Decimal = Decimal(0)
    drawdown_pct: Decimal = Decimal(0)
    feeds: Mapping[str, str] = field(default_factory=lambda: {})
    engine_state: EngineState = EngineState.RUNNING


def condition(rule: AlertRuleConfig, ctx: RuleContext) -> tuple[bool, str] | None:
    """(is_true, description), or None when the data needed is not there yet."""
    t = rule.threshold
    if rule.kind == "daily_pnl_below":
        return ctx.daily_pnl < t, f"day PnL {ctx.daily_pnl:+.2f} < {t}"
    if rule.kind == "drawdown_pct_above":
        return ctx.drawdown_pct > t, f"drawdown {ctx.drawdown_pct:.2f}% > {t}%"
    if rule.kind == "engine_not_running":
        return ctx.engine_state is not EngineState.RUNNING, f"engine {ctx.engine_state.value}"
    if rule.kind == "feed_not_live":
        feeds = {k: v for k, v in ctx.feeds.items() if rule.symbol in (None, k)}
        if not feeds:
            return None
        bad = sorted(f"{k}={v}" for k, v in feeds.items() if v != "LIVE")
        return bool(bad), "feeds not live: " + ", ".join(bad)
    row = ctx.rows.get(rule.symbol or "")
    if row is None:
        return None
    if rule.kind in ("price_above", "price_below"):
        if row.last is None:
            return None
        hit = row.last > t if rule.kind == "price_above" else row.last < t
        return hit, f"{row.symbol} last {row.last} {'>' if rule.kind == 'price_above' else '<'} {t}"
    if rule.kind == "change_pct_abs_above":
        if row.change_24h_pct is None:
            return None
        return abs(row.change_24h_pct) > t, f"{row.symbol} 24h {row.change_24h_pct:+.2f}%"
    if rule.kind == "spread_pct_above":
        if row.spread_pct is None:
            return None
        return row.spread_pct > t, f"{row.symbol} spread {row.spread_pct:.3f}% > {t}%"
    # volume_z_above
    if row.volume_z is None:
        return None
    return row.volume_z > float(t), f"{row.symbol} volume z-score {row.volume_z:.1f} > {t}"


class RuleEngine:
    def __init__(self, rules: Sequence[AlertRuleConfig], alerts: AlertManager) -> None:
        names = [r.name for r in rules]
        if len(set(names)) != len(names):
            raise ValueError("alert rule names must be unique")
        self.rules = list(rules)
        self.alerts = alerts
        self._armed: dict[str, bool] = dict.fromkeys(names, True)

    def evaluate(self, ctx: RuleContext) -> list[str]:
        fired: list[str] = []
        for r in self.rules:
            res = condition(r, ctx)
            if res is None:
                continue
            hit, text = res
            if hit and self._armed[r.name]:
                self._armed[r.name] = False
                self.alerts.emit(Severity(r.severity), f"rule:{r.name}", f"[{r.name}] {text}")
                fired.append(r.name)
            elif not hit:
                self._armed[r.name] = True
        return fired
