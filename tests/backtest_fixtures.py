"""Shared deterministic backtest fixtures (synthetic, seeded)."""

from __future__ import annotations

from decimal import Decimal

from helm.app.backtest import BacktestSpec
from helm.backtest.friction.models import FrictionConfig
from helm.core.models import Bar
from helm.data.synthetic import frame_to_bars, gbm_frame
from helm.strategy.api import Strategy
from helm.strategy.examples.breakout import Breakout
from helm.strategy.examples.rsi_reversion import RsiReversion
from helm.strategy.examples.sma_cross import SmaCross
from tests.factories import SYM, instrument

T0 = 1_704_067_200 * 10**9

EXAMPLES: dict[str, tuple[type[Strategy], dict[str, float | int | str | bool]]] = {
    "sma_cross": (SmaCross, {"fast": 10, "slow": 30, "qty": "0.2"}),  # type: ignore[dict-item]
    "rsi_reversion": (RsiReversion, {"period": 14, "entry": 30, "exit": 55, "qty": "0.2"}),  # type: ignore[dict-item]
    "breakout": (Breakout, {"entry_n": 20, "exit_n": 10, "qty": "0.2"}),  # type: ignore[dict-item]
}


def bars(n: int = 4000, seed: int = 7) -> list[Bar]:
    return list(frame_to_bars(gbm_frame(n, start_ns=T0, seed=seed, sigma=0.8), SYM, "1m"))


def spec(name: str, friction: FrictionConfig | None = None, seed: int = 0) -> BacktestSpec:
    cls, params = EXAMPLES[name]
    return BacktestSpec(
        strategy=cls(),
        instruments=[instrument()],
        params=dict(params),
        starting_equity=Decimal(10_000),
        friction=friction or FrictionConfig(),
        seed=seed,
    )
