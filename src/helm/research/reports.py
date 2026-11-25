"""Promotion evidence reports from account journals (§13, PAP-4).

- Parity (TEST-4 on real data): replay a feed recorded during a paper run through
  the backtester and compare with what paper actually did. Passing parity says
  "the backtest you validated is the strategy that is trading".
- Divergence (PAP-4): match live orders to the paper shadow's orders and measure
  fill-price and latency deltas, to recalibrate friction before scaling up.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from helm.core.events import OrderEvent
from helm.core.models import Fill, Order
from helm.oms.journal import Journal

NS = 1_000_000_000


@dataclass
class AccountRecord:
    orders: list[Order] = field(default_factory=lambda: [])  # first-seen snapshot, created order
    fills: list[Fill] = field(default_factory=lambda: [])


def load_account(journal: Path | str, account_id: str) -> AccountRecord:
    rec = AccountRecord()
    seen: set[str] = set()
    with Journal(journal, mirror=False) as j:
        for e in j.entries(kinds=("OrderEvent", "Fill"), account_id=account_id):
            ev = e.decode()
            if isinstance(ev, OrderEvent) and ev.client_order_id not in seen:
                seen.add(ev.client_order_id)
                rec.orders.append(ev.order)
            elif isinstance(ev, Fill):
                rec.fills.append(ev)
    rec.orders.sort(key=lambda o: (o.ts_created, o.client_order_id))
    return rec


def _sig(o: Order) -> tuple[str, str, str, str, str, str]:
    it = o.intent
    return (
        it.strategy_id,
        it.symbol,
        it.side.value,
        str(it.qty),
        it.type.value,
        str(it.limit_price),
    )


def _realized(fills: Sequence[Fill]) -> Decimal:
    """Cash-flow PnL of fills (sell proceeds - buy cost - fees): equal positions => comparable."""
    out = Decimal(0)
    for f in fills:
        out += -f.side.sign * f.price * f.qty - f.fee
    return out


def _net_qty(fills: Sequence[Fill]) -> dict[str, Decimal]:
    q: dict[str, Decimal] = {}
    for f in fills:
        q[f.symbol] = q.get(f.symbol, Decimal(0)) + f.side.sign * f.qty
    return q


@dataclass
class ParityReport:
    paper_orders: int
    replay_orders: int
    matching_prefix: int
    first_mismatch: dict[str, Any] | None
    same_positions: bool
    pnl_delta_bp: float
    tolerance_bp: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parity_report(
    paper: AccountRecord,
    replay_orders: Sequence[Order],
    replay_fills: Sequence[Fill],
    *,
    equity: Decimal,
    tolerance_bp: Decimal,
) -> ParityReport:
    ps, rs = [_sig(o) for o in paper.orders], [_sig(o) for o in replay_orders]
    n = 0
    while n < min(len(ps), len(rs)) and ps[n] == rs[n]:
        n += 1
    mismatch = None
    if n < max(len(ps), len(rs)):
        mismatch = {
            "index": n,
            "paper": list(ps[n]) if n < len(ps) else None,
            "replay": list(rs[n]) if n < len(rs) else None,
        }
    same_pos = _net_qty(paper.fills) == _net_qty(replay_fills)
    delta = abs(_realized(paper.fills) - _realized(replay_fills))
    bp = float(delta / equity * 10_000) if equity > 0 else 0.0
    passed = mismatch is None and same_pos and bp <= float(tolerance_bp)
    return ParityReport(len(ps), len(rs), n, mismatch, same_pos, bp, float(tolerance_bp), passed)


@dataclass
class DivergenceReport:
    matched: int
    unmatched_live: int
    unmatched_paper: int
    price_delta_bps: dict[str, float]  # live minus paper, signed so positive = live worse
    paper_latency_ms: dict[str, float]
    live_latency_ms: dict[str, float]
    suggested_extra_slippage_bps: float
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stats(xs: Sequence[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    return {
        "n": len(s),
        "mean": statistics.fmean(s),
        "median": statistics.median(s),
        "p90": s[min(len(s) - 1, int(0.9 * len(s)))],
    }


def _fill_summary(o: Order, fills: Sequence[Fill]) -> tuple[Decimal, int] | None:
    mine = [f for f in fills if f.client_order_id == o.client_order_id]
    if not mine:
        return None
    qty = sum((f.qty for f in mine), Decimal(0))
    vwap = sum((f.price * f.qty for f in mine), Decimal(0)) / qty
    return vwap, min(f.ts for f in mine) - o.ts_created


def divergence_report(
    paper: AccountRecord, live: AccountRecord, *, window_s: float = 10.0
) -> DivergenceReport:
    """Greedy time-ordered matching of live orders to paper orders with the same
    strategy, symbol, side and quantity created within `window_s` of each other."""
    free = list(paper.orders)
    rows: list[dict[str, Any]] = []
    deltas: list[float] = []
    p_lat: list[float] = []
    l_lat: list[float] = []
    unmatched_live = 0
    for lo in live.orders:
        key = _sig(lo)[:4]
        cand = [
            p
            for p in free
            if _sig(p)[:4] == key and abs(p.ts_created - lo.ts_created) <= window_s * NS
        ]
        if not cand:
            unmatched_live += 1
            continue
        po = min(cand, key=lambda p: abs(p.ts_created - lo.ts_created))
        free.remove(po)
        pf, lf = _fill_summary(po, paper.fills), _fill_summary(lo, live.fills)
        row: dict[str, Any] = {"live": lo.client_order_id, "paper": po.client_order_id}
        if pf and lf:
            sign = lo.intent.side.sign
            d = float(sign * (lf[0] - pf[0]) / pf[0] * 10_000)
            deltas.append(d)
            p_lat.append(pf[1] / 1e6)
            l_lat.append(lf[1] / 1e6)
            row |= {"paper_px": str(pf[0]), "live_px": str(lf[0]), "delta_bps": round(d, 3)}
        rows.append(row)
    pd = _stats(deltas)
    return DivergenceReport(
        matched=len(rows),
        unmatched_live=unmatched_live,
        unmatched_paper=len(free),
        price_delta_bps=pd,
        paper_latency_ms=_stats(p_lat),
        live_latency_ms=_stats(l_lat),
        suggested_extra_slippage_bps=max(0.0, pd.get("median", 0.0)),
        rows=rows,
    )
