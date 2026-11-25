"""Inputs to the pure risk function (PRD §10). Built by the engine; never by strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from helm.core.enums import EngineState, FeedState, LifecycleStage, Mode
from helm.core.models import Instrument, Quote


@dataclass(frozen=True)
class PortfolioState:
    equity: Decimal
    positions: Mapping[str, Decimal]  # signed qty per symbol
    marks: Mapping[str, Decimal]  # valuation price (mid) per symbol
    multipliers: Mapping[str, Decimal]
    open_orders: int
    strategy_exposure: Decimal  # gross notional attributed to the intent's strategy
    strategy_capital_pct: Decimal | None = None  # EXE-6 per-strategy allocation (% equity)


@dataclass(frozen=True)
class MarketState:
    quote: Quote | None  # last L1 quote for the intent's symbol
    feed_state: FeedState
    instrument: Instrument | None


@dataclass(frozen=True)
class FlowState:
    engine_state: EngineState
    strategy_paused: bool
    orders_last_min_strategy: int
    orders_last_min_global: int
    consecutive_rejects: int
    mode: Mode
    lifecycle: LifecycleStage
    now: int
    is_flatten: bool = False
