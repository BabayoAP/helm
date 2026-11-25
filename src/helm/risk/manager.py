"""RiskManager: pure, fail-closed pre-trade gate (PRD §10, I2).

`evaluate(intent, portfolio, market, flow) -> RiskDecision` is a pure function
of its inputs and config. Rules run in order (RISK-1 .. RISK-9); the first
rejection wins. Any exception or missing input => REJECT("risk_error...").
Flatten intents bypass RISK-3/5/6/7 but never the reduce-only requirement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from helm.core.calendar import is_open
from helm.core.config import RiskConfig
from helm.core.enums import EngineState, FeedState, LifecycleStage, Mode, Side, Verdict
from helm.core.models import OrderIntent, RiskDecision
from helm.core.money import ZERO, is_multiple
from helm.risk.types import FlowState, MarketState, PortfolioState

HUNDRED = Decimal(100)
NS_PER_MS = 1_000_000


class _Reject(Exception):
    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(reason)
        self.rule_id = rule_id
        self.reason = reason


@dataclass(frozen=True)
class _Derived:
    """Values computed once and shared by the rules."""

    pos: Decimal
    mid: Decimal | None
    ref_px: Decimal | None
    mult: Decimal
    pre_exp: Decimal  # |position notional| in this symbol, at mid
    post_exp: Decimal
    closes_only: bool  # opposite side and qty <= |position|  (a true exit)


def _hash_inputs(*parts: Any) -> str:
    def enc(o: Any) -> Any:
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        if hasattr(o, "__dataclass_fields__"):
            return {k: enc(v) for k, v in asdict(o).items()}
        if isinstance(o, dict):
            return {str(k): enc(v) for k, v in o.items()}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
        if isinstance(o, Decimal):
            return str(o)
        return o

    blob = json.dumps([enc(p) for p in parts], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class RiskManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._rules: list[
            Callable[[OrderIntent, PortfolioState, MarketState, FlowState, _Derived], None]
        ] = [
            self._risk1_state,
            self._risk2_instrument,
            self._risk3_freshness,
            self._risk4_fat_finger,
            self._risk5_position,
            self._risk6_leverage,
            self._risk7_rates,
            self._risk8_allocation,
            self._risk9_live_small,
        ]

    # ------------------------------------------------------------- entrypoint
    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: PortfolioState,
        market: MarketState,
        flow: FlowState,
    ) -> RiskDecision:
        snap = "unhashable"
        intent_id = getattr(intent, "intent_id", "unknown")
        try:
            snap = _hash_inputs(intent, portfolio, market, flow, self.config)
            if intent is None or portfolio is None or market is None or flow is None:  # pyright: ignore[reportUnnecessaryComparison]
                raise ValueError("missing risk input")
            derived = self._derive(intent, portfolio, market)
            for rule in self._rules:
                rule(intent, portfolio, market, flow, derived)
        except _Reject as rej:
            return RiskDecision(
                intent_id=intent_id,
                verdict=Verdict.REJECT,
                rule_id=rej.rule_id,
                reason=rej.reason,
                inputs_snapshot_hash=snap,
            )
        except Exception as exc:
            return RiskDecision(
                intent_id=intent_id,
                verdict=Verdict.REJECT,
                rule_id="RISK-ERR",
                reason=f"risk_error: {type(exc).__name__}: {exc}",
                inputs_snapshot_hash=snap,
            )
        return RiskDecision(
            intent_id=intent_id,
            verdict=Verdict.APPROVE,
            rule_id="OK",
            reason="approved",
            inputs_snapshot_hash=snap,
        )

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _derive(intent: OrderIntent, p: PortfolioState, m: MarketState) -> _Derived:
        pos = Decimal(p.positions.get(intent.symbol, ZERO))
        q = m.quote
        mid = q.mid if q is not None else p.marks.get(intent.symbol)
        if mid is not None:
            mid = Decimal(mid)
        if intent.limit_price is not None:
            ref: Decimal | None = intent.limit_price
        elif q is not None:
            ref = q.ask if intent.side is Side.BUY else q.bid
        else:
            ref = mid
        mult = Decimal(p.multipliers.get(intent.symbol, Decimal(1)))
        signed = intent.qty * intent.side.sign
        px = mid if mid is not None else ZERO
        closes_only = pos != 0 and (pos > 0) != (signed > 0) and intent.qty <= abs(pos)
        return _Derived(
            pos=pos,
            mid=mid,
            ref_px=ref,
            mult=mult,
            pre_exp=abs(pos * px * mult),
            post_exp=abs((pos + signed) * px * mult),
            closes_only=closes_only,
        )

    # ------------------------------------------------------------------ rules
    def _risk1_state(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten:
            if not (it.reduce_only and d.closes_only):
                raise _Reject(
                    "RISK-1", "flatten orders must be reduce-only and reduce the position"
                )
            return
        if it.reduce_only and not d.closes_only:
            raise _Reject("RISK-1", "reduce_only order would not reduce the position")
        if f.engine_state is EngineState.HALTED:
            raise _Reject("RISK-1", "engine HALTED: only flatten allowed")
        if (f.engine_state is EngineState.PAUSED or f.strategy_paused) and not d.closes_only:
            raise _Reject("RISK-1", "paused: only exits allowed")

    def _risk2_instrument(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        inst = m.instrument
        if inst is None or inst.symbol != it.symbol:
            raise _Reject("RISK-2", "unknown_instrument")
        allow = self.config.symbol_allowlist
        if allow and it.symbol not in allow:
            raise _Reject("RISK-2", "symbol_not_allowed")
        if not is_open(inst.calendar, f.now):
            raise _Reject("RISK-2", "market_closed")
        if not is_multiple(it.qty, inst.lot_size):
            raise _Reject("RISK-2", "qty_not_quantized")
        if it.qty < inst.min_qty:
            raise _Reject("RISK-2", "below_min_qty")
        for px in (it.limit_price, it.stop_price):
            if px is not None and not is_multiple(px, inst.tick_size):
                raise _Reject("RISK-2", "price_not_quantized")
        if d.ref_px is None:
            if f.is_flatten:
                return  # flatten must not be blocked by missing data; reduce-only still enforced
            raise _Reject("RISK-2", "no_reference_price")
        if not d.closes_only and it.qty * d.ref_px * d.mult < inst.min_notional:
            raise _Reject("RISK-2", "below_min_notional")

    def _risk3_freshness(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten:
            return
        if m.feed_state is not FeedState.LIVE:
            raise _Reject("RISK-3", "feed_not_live")
        if m.quote is None:
            raise _Reject("RISK-3", "no_quote")
        if f.now - m.quote.ts_recv > self.config.stale_feed_ms * NS_PER_MS:
            raise _Reject("RISK-3", "stale_quote")

    def _risk4_fat_finger(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if d.mid is None or d.ref_px is None:
            return  # only reachable for flatten (RISK-2/3 reject otherwise)
        limit_pct = self.config.fat_finger_pct
        if abs(d.ref_px - d.mid) / d.mid * HUNDRED > limit_pct:
            raise _Reject("RISK-4", "fat_finger")
        if not d.closes_only and it.qty * d.ref_px * d.mult > self.config.max_order_notional_usd:
            raise _Reject("RISK-4", "order_notional_cap")

    def _risk5_position(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten or d.post_exp <= d.pre_exp:
            return
        if p.equity <= 0:
            raise _Reject("RISK-5", "non_positive_equity")
        if d.post_exp > self.config.max_position_pct_equity / HUNDRED * p.equity:
            raise _Reject("RISK-5", "position_cap")

    def _risk6_leverage(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten or d.post_exp <= d.pre_exp:
            return
        others = ZERO
        for sym, qty in p.positions.items():
            if sym == it.symbol or qty == 0:
                continue
            mark = p.marks.get(sym)
            if mark is None:
                raise _Reject("RISK-6", "missing_mark")
            others += abs(
                Decimal(qty) * Decimal(mark) * Decimal(p.multipliers.get(sym, Decimal(1)))
            )
        if others + d.post_exp > self.config.max_gross_leverage * p.equity:
            raise _Reject("RISK-6", "leverage_cap")

    def _risk7_rates(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten:
            return
        c = self.config
        if f.orders_last_min_strategy >= c.max_orders_per_min:
            raise _Reject("RISK-7", "strategy_rate_cap")
        if f.orders_last_min_global >= c.max_orders_per_min_global:
            raise _Reject("RISK-7", "global_rate_cap")
        if p.open_orders >= c.max_open_orders:
            raise _Reject("RISK-7", "max_open_orders")
        if f.consecutive_rejects >= c.max_consecutive_rejects:
            raise _Reject("RISK-7", "consecutive_rejects")

    def _strategy_post_exposure(self, p: PortfolioState, d: _Derived) -> Decimal:
        return p.strategy_exposure - d.pre_exp + d.post_exp

    def _risk8_allocation(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten or d.post_exp <= d.pre_exp:
            return
        cap = self.config.max_strategy_capital_pct / HUNDRED * p.equity
        if self._strategy_post_exposure(p, d) > cap:
            raise _Reject("RISK-8", "strategy_allocation")

    def _risk9_live_small(
        self, it: OrderIntent, p: PortfolioState, m: MarketState, f: FlowState, d: _Derived
    ) -> None:
        if f.is_flatten or f.mode is not Mode.LIVE or f.lifecycle is not LifecycleStage.LIVE_SMALL:
            return
        if d.post_exp <= d.pre_exp:
            return
        cap = min(
            self.config.live_small_capital_pct / HUNDRED * p.equity,
            self.config.live_small_max_exposure_usd,
        )
        if self._strategy_post_exposure(p, d) > cap:
            raise _Reject("RISK-9", "live_small_cap")
