"""Friction models (BT-5): fee tiers, spread, square-root impact, funding, latency.

Every component can be toggled; `scale` multiplies all costs so tests can prove
PnL falls monotonically as friction rises.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from helm.core.enums import Liquidity
from helm.core.models import Bar, OrderIntent
from helm.core.timeframes import timeframe_ns

BPS = Decimal(10_000)
NS_PER_DAY = 86_400 * 10**9


@dataclass(frozen=True)
class FeeTier:
    min_30d_volume: Decimal
    maker_bps: Decimal
    taker_bps: Decimal


@dataclass(frozen=True)
class FeeSchedule:
    tiers: tuple[FeeTier, ...] = (FeeTier(Decimal(0), Decimal(10), Decimal(10)),)

    def rate_bps(self, liquidity: Liquidity, volume_30d: Decimal) -> Decimal:
        tier = self.tiers[0]
        for t in sorted(self.tiers, key=lambda x: x.min_30d_volume):
            if volume_30d >= t.min_30d_volume:
                tier = t
        return tier.maker_bps if liquidity is Liquidity.MAKER else tier.taker_bps


@dataclass
class FrictionConfig:
    fees: bool = True
    spread: bool = True
    impact: bool = True
    funding: bool = True
    latency: bool = True
    scale: Decimal = Decimal(1)
    fee_schedule: FeeSchedule = field(default_factory=FeeSchedule)
    fixed_slippage_bps: Decimal = Decimal(1)
    half_spread_bps: Decimal = Decimal(2)  # modelled when no quotes are available (bar mode)
    impact_k: Decimal = Decimal("0.5")
    vol_window: int = 60
    adv_window_days: int = 20
    funding_rate_8h: Decimal = Decimal(0)  # constant rate per 8 h (perps); 0 for spot
    latency_ms: tuple[float, float] = (50.0, 250.0)
    seed: int = 0

    @classmethod
    def off(cls) -> FrictionConfig:
        return cls(
            fees=False,
            spread=False,
            impact=False,
            funding=False,
            latency=False,
            fixed_slippage_bps=Decimal(0),
        )

    def scaled(self, k: Decimal) -> FrictionConfig:
        return FrictionConfig(**{**self.__dict__, "scale": k})


class ImpactModel:
    """slip(Q) = k * sigma * sqrt(Q / ADV) (BT-5), sigma = trailing daily vol.

    Updated only with bars strictly before the fill bar (no lookahead).
    """

    def __init__(self, k: Decimal, vol_window: int, adv_window_days: int) -> None:
        self.k = float(k)
        self.vol_window = vol_window
        self.adv_days = adv_window_days
        self._closes: dict[str, deque[float]] = {}
        self._vols: dict[str, deque[float]] = {}
        self._tf_ns: dict[str, int] = {}

    def update(self, bar: Bar) -> None:
        self._tf_ns[bar.symbol] = timeframe_ns(bar.timeframe)
        bars_per_day = max(1, NS_PER_DAY // self._tf_ns[bar.symbol])
        c = self._closes.setdefault(bar.symbol, deque(maxlen=self.vol_window + 1))
        v = self._vols.setdefault(bar.symbol, deque(maxlen=bars_per_day * self.adv_days))
        c.append(float(bar.c))
        v.append(float(bar.volume))

    def slip_bps(self, symbol: str, qty: Decimal) -> Decimal:
        closes = self._closes.get(symbol)
        vols = self._vols.get(symbol)
        if not closes or len(closes) < 3 or not vols:
            return Decimal(0)
        rets = [
            math.log(b / a)
            for a, b in zip(list(closes)[:-1], list(closes)[1:], strict=True)
            if a > 0 and b > 0
        ]
        if len(rets) < 2:
            return Decimal(0)
        mean = sum(rets) / len(rets)
        sigma_bar = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        bars_per_day = max(1, NS_PER_DAY // self._tf_ns[symbol])
        sigma_day = sigma_bar * math.sqrt(bars_per_day)
        adv = sum(vols) / len(vols) * bars_per_day
        if adv <= 0:
            return Decimal(0)
        slip = self.k * sigma_day * math.sqrt(float(qty) / adv)
        return Decimal(repr(round(slip * 10_000, 6)))


class LatencyModel:
    """Seeded signal->arrival latency, uniform in [lo, hi] ms (default 50-250 ms)."""

    def __init__(self, lo_ms: float, hi_ms: float, seed: int) -> None:
        self.lo, self.hi = lo_ms, hi_ms
        self._rng = random.Random(seed)

    def sample_ns(self) -> int:
        return int(self._rng.uniform(self.lo, self.hi) * 1_000_000)


class Friction:
    """Bundle used by SimBroker: fees, slippage (spread + fixed + impact), latency, funding."""

    def __init__(self, cfg: FrictionConfig) -> None:
        self.cfg = cfg
        self.impact = ImpactModel(cfg.impact_k, cfg.vol_window, cfg.adv_window_days)
        self.latency = LatencyModel(*cfg.latency_ms, seed=cfg.seed)
        self.volume_30d = Decimal(0)

    def fee(self, notional: Decimal, liquidity: Liquidity) -> Decimal:
        if not self.cfg.fees:
            return Decimal(0)
        rate = self.cfg.fee_schedule.rate_bps(liquidity, self.volume_30d)
        return (notional * rate / BPS * self.cfg.scale).quantize(Decimal("0.00000001"))

    def taker_slippage_bps(self, intent: OrderIntent, _bar: Bar | None = None) -> Decimal:
        bps = self.cfg.fixed_slippage_bps
        if self.cfg.spread:
            bps += self.cfg.half_spread_bps
        if self.cfg.impact:
            bps += self.impact.slip_bps(intent.symbol, intent.qty)
        return bps * self.cfg.scale

    def latency_ns(self) -> int:
        return self.latency.sample_ns() if self.cfg.latency else 0

    def funding_rate(self) -> Decimal:
        return self.cfg.funding_rate_8h * self.cfg.scale if self.cfg.funding else Decimal(0)
