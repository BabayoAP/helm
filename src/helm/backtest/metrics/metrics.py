"""Performance metrics (BT-6). Floats are fine here (research layer, PRD §4).

Sharpe/Sortino are computed on daily (UTC) equity returns and annualized with
`periods_per_year` (365 for crypto, 252 for equities). PSR and the deflated
Sharpe ratio follow Bailey & Lopez de Prado (2012, 2014).
"""

from __future__ import annotations

import itertools
import math
import random
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from statistics import NormalDist
from typing import Any

from helm.core.models import Fill

NS_PER_DAY = 86_400 * 10**9
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    entry_ts: int
    exit_ts: int
    qty: float
    pnl: float  # net of fees
    ret: float  # pnl / entry notional


def daily_equity(curve: Sequence[tuple[int, Decimal]]) -> list[tuple[int, float]]:
    """Last equity per UTC day, keyed by the day's start (ns)."""
    out: dict[int, float] = {}
    for ts, eq in curve:
        out[ts // NS_PER_DAY * NS_PER_DAY] = float(eq)
    return sorted(out.items())


def returns(values: Sequence[float]) -> list[float]:
    return [b / a - 1 for a, b in itertools.pairwise(values) if a != 0]


def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x) if x else 0.0


def _std(x: Sequence[float]) -> float:
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def sharpe(rets: Sequence[float], periods_per_year: float = 365) -> float:
    sd = _std(rets)
    return _mean(rets) / sd * math.sqrt(periods_per_year) if sd > 0 else 0.0


def sortino(rets: Sequence[float], periods_per_year: float = 365) -> float:
    if len(rets) < 2:
        return 0.0
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / (len(rets) - 1))
    return _mean(rets) / downside * math.sqrt(periods_per_year) if downside > 0 else 0.0


def max_drawdown(curve: Sequence[tuple[int, float]]) -> tuple[float, int]:
    """(max depth as a fraction, longest time under water in ns)."""
    peak = -math.inf
    peak_ts = curve[0][0] if curve else 0
    depth = 0.0
    longest = 0
    for ts, eq in curve:
        if eq >= peak:
            peak, peak_ts = eq, ts
        else:
            depth = max(depth, (peak - eq) / peak if peak > 0 else 0.0)
            longest = max(longest, ts - peak_ts)
    return depth, longest


def cagr(curve: Sequence[tuple[int, float]]) -> float:
    if len(curve) < 2 or curve[0][1] <= 0:
        return 0.0
    years = (curve[-1][0] - curve[0][0]) / (365.25 * NS_PER_DAY)
    if years <= 0:
        return 0.0
    ratio = curve[-1][1] / curve[0][1]
    return ratio ** (1 / years) - 1 if ratio > 0 else -1.0


def moments(x: Sequence[float]) -> tuple[float, float]:
    """(skewness, kurtosis) — kurtosis is non-excess (normal = 3)."""
    n = len(x)
    sd = _std(x)
    if n < 3 or sd == 0:
        return 0.0, 3.0
    m = _mean(x)
    skew = sum(((v - m) / sd) ** 3 for v in x) / n
    kurt = sum(((v - m) / sd) ** 4 for v in x) / n
    return skew, kurt


def probabilistic_sharpe(
    sr: float, n: int, skew: float, kurt: float, sr_benchmark: float = 0.0
) -> float:
    """PSR: probability the true (per-period) Sharpe exceeds the benchmark."""
    if n < 2:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """E[max SR] over n independent trials under the null (SR* in the DSR)."""
    if n_trials <= 1 or sr_variance <= 0:
        return 0.0
    a = _N.inv_cdf(1 - 1 / n_trials)
    b = _N.inv_cdf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe(
    sr: float, n: int, skew: float, kurt: float, n_trials: int, sr_variance: float | None = None
) -> float:
    """DSR = PSR evaluated against the expected max Sharpe of `n_trials` (per-period units)."""
    if sr_variance is None:
        sr_variance = (1 - skew * sr + (kurt - 1) / 4 * sr**2) / max(n - 1, 1)
    return probabilistic_sharpe(sr, n, skew, kurt, expected_max_sharpe(n_trials, sr_variance))


def round_trips(fills: Sequence[Fill]) -> list[RoundTrip]:
    """FIFO-match fills into round trips per symbol; fees allocated pro rata."""
    lots: dict[str, deque[list[float]]] = {}  # [signed qty, price, fee_per_unit, ts]
    out: list[RoundTrip] = []
    for f in sorted(fills, key=lambda x: x.ts):
        q = float(f.qty) * f.side.sign
        px = float(f.price)
        fee_unit = float(f.fee) / float(f.qty)
        book = lots.setdefault(f.symbol, deque())
        while q != 0 and book and (book[0][0] > 0) != (q > 0):
            lot = book[0]
            take = min(abs(q), abs(lot[0]))
            direction = 1.0 if lot[0] > 0 else -1.0
            pnl = (px - lot[1]) * take * direction - (lot[2] + fee_unit) * take
            out.append(RoundTrip(f.symbol, int(lot[3]), f.ts, take, pnl, pnl / (lot[1] * take)))
            lot[0] -= take * direction
            q += take * direction
            if abs(lot[0]) < 1e-12:
                book.popleft()
        if abs(q) > 1e-12:
            book.append([q, px, fee_unit, float(f.ts)])
    return out


def block_bootstrap(
    rets: Sequence[float], *, sims: int = 1000, block: int = 5, seed: int = 0
) -> dict[str, float]:
    """Monte Carlo of trade returns by circular block bootstrap: final-return percentiles."""
    if not rets:
        return {"p05": 0.0, "p50": 0.0, "p95": 0.0, "p_loss": 0.0}
    rng = random.Random(seed)
    n = len(rets)
    finals: list[float] = []
    for _ in range(sims):
        path: list[float] = []
        while len(path) < n:
            start = rng.randrange(n)
            path.extend(rets[(start + i) % n] for i in range(block))
        eq = 1.0
        for r in path[:n]:
            eq *= 1 + r
        finals.append(eq - 1)
    finals.sort()

    def pct(p: float) -> float:
        return finals[min(len(finals) - 1, int(p * len(finals)))]

    return {
        "p05": pct(0.05),
        "p50": pct(0.5),
        "p95": pct(0.95),
        "p_loss": sum(1 for x in finals if x < 0) / sims,
    }


@dataclass(frozen=True)
class Metrics:
    start_equity: float
    end_equity: float
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_days: float
    profit_factor: float
    expectancy: float
    win_rate: float
    exposure: float
    turnover: float
    trade_count: int
    fills: int
    fees_paid: float
    psr: float
    deflated_sharpe: float
    n_trials: int
    monte_carlo: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (round(v, 10) if isinstance(v, float) else v) for k, v in d.items()}


def compute_metrics(
    curve: Sequence[tuple[int, Decimal]],
    fills: Sequence[Fill],
    *,
    exposure_flags: Sequence[bool] = (),
    fees_paid: Decimal = Decimal(0),
    turnover: Decimal = Decimal(0),
    periods_per_year: float = 365,
    n_trials: int = 1,
    seed: int = 0,
) -> Metrics:
    daily = daily_equity(curve)
    eq = [v for _, v in daily]
    rets = returns(eq)
    depth, dur = max_drawdown(daily)
    c = cagr(daily)
    trips = round_trips(fills)
    wins = [t.pnl for t in trips if t.pnl > 0]
    losses = [-t.pnl for t in trips if t.pnl < 0]
    sr_period = sharpe(rets, 1)
    skew, kurt = moments(rets)
    start = float(curve[0][1]) if curve else 0.0
    end = float(curve[-1][1]) if curve else 0.0
    avg_eq = _mean([float(v) for _, v in curve]) if curve else 0.0
    return Metrics(
        start_equity=start,
        end_equity=end,
        total_return=end / start - 1 if start else 0.0,
        cagr=c,
        sharpe=sharpe(rets, periods_per_year),
        sortino=sortino(rets, periods_per_year),
        calmar=c / depth if depth > 0 else 0.0,
        max_drawdown=depth,
        max_drawdown_days=dur / NS_PER_DAY,
        profit_factor=(sum(wins) / sum(losses)) if losses else (math.inf if wins else 0.0),
        expectancy=_mean([t.pnl for t in trips]),
        win_rate=len(wins) / len(trips) if trips else 0.0,
        exposure=sum(exposure_flags) / len(exposure_flags) if exposure_flags else 0.0,
        turnover=float(turnover) / avg_eq if avg_eq else 0.0,
        trade_count=len(trips),
        fills=len(fills),
        fees_paid=float(fees_paid),
        psr=probabilistic_sharpe(sr_period, len(rets), skew, kurt),
        deflated_sharpe=deflated_sharpe(sr_period, len(rets), skew, kurt, n_trials),
        n_trials=n_trials,
        monte_carlo=block_bootstrap([t.ret for t in trips], seed=seed),
    )
