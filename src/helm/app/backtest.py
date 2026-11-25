"""Event-driven backtest runner (BT-2, BT-7): the live engine loop with SimClock + SimBroker."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from helm.app.engine import Engine
from helm.backtest.fills import FillPolicy
from helm.backtest.friction.models import FrictionConfig
from helm.backtest.metrics.metrics import Metrics, compute_metrics
from helm.brokers.sim.broker import SimBroker, SimMode
from helm.core.clock import SimClock
from helm.core.config import RiskConfig
from helm.core.enums import LifecycleStage, Mode
from helm.core.ids import make_run_id
from helm.core.models import Bar, Fill, Instrument, Order, RiskDecision
from helm.data.instruments import InstrumentRegistry
from helm.oms.accounting import Portfolio
from helm.oms.journal import Journal
from helm.strategy.api import ParamValue, Strategy


@dataclass
class BacktestSpec:
    strategy: Strategy
    instruments: Sequence[Instrument]
    params: dict[str, ParamValue] = field(default_factory=lambda: {})
    starting_equity: Decimal = Decimal(10_000)
    friction: FrictionConfig = field(default_factory=FrictionConfig)
    fill_policy: FillPolicy = FillPolicy.CONSERVATIVE
    risk: RiskConfig = field(default_factory=RiskConfig)
    seed: int = 0
    n_trials: int = 1
    periods_per_year: float = 365
    journal_path: str = ":memory:"
    account_id: str = "paper-backtest"

    def fingerprint(self) -> str:
        blob = json.dumps(
            {
                "strategy": f"{type(self.strategy).__module__}:{type(self.strategy).__name__}",
                "id": self.strategy.id,
                "instruments": [i.model_dump(mode="json") for i in self.instruments],
                "params": self.params,
                "equity": str(self.starting_equity),
                "friction": {k: str(v) for k, v in self.friction.__dict__.items()},
                "fill_policy": self.fill_policy.value,
                "risk": self.risk.model_dump(mode="json"),
                "seed": self.seed,
            },
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class BacktestResult:
    run_id: str
    orders: list[Order]
    fills: list[Fill]
    decisions: list[RiskDecision]
    equity_curve: list[tuple[int, Decimal]]
    metrics: Metrics
    stats: dict[str, int]
    spec_hash: str
    data_hash: str

    def order_sequence(self) -> list[tuple[str, str, str, str]]:
        return [
            (o.client_order_id, o.intent.side.value, str(o.intent.qty), o.intent.type.value)
            for o in self.orders
        ]

    @property
    def result_hash(self) -> str:
        """Same data + config + seed => byte-identical hash (I7, BT-7, TEST-5)."""
        h = hashlib.sha256()
        for o in self.orders:
            h.update(o.model_dump_json(exclude={"ts_updated"}).encode())
        for f in self.fills:
            h.update(f.model_dump_json().encode())
        for ts, eq in self.equity_curve:
            h.update(f"{ts}:{eq}".encode())
        h.update(json.dumps(self.metrics.to_dict(), sort_keys=True, default=str).encode())
        return h.hexdigest()


def merge_bars(streams: Iterable[Iterable[Bar]]) -> Iterator[Bar]:
    """Deterministic multi-symbol ordering: by ts_close, then symbol."""
    return heapq.merge(*streams, key=lambda b: (b.ts_close, b.symbol))


def data_hash(bars: Sequence[Bar]) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.symbol}|{b.ts_open}|{b.o}|{b.h}|{b.l}|{b.c}|{b.volume}".encode())
    return h.hexdigest()


async def run_backtest_async(spec: BacktestSpec, bars: Sequence[Bar]) -> BacktestResult:
    spec_hash = spec.fingerprint()
    dh = data_hash(bars)
    run_id = make_run_id(spec_hash + dh)
    clock = SimClock(bars[0].ts_open if bars else 0)
    mults = {i.symbol: i.contract_multiplier for i in spec.instruments}
    friction = FrictionConfig(**{**spec.friction.__dict__, "seed": spec.seed})
    broker = SimBroker(
        account_id=spec.account_id,
        venue=spec.instruments[0].venue,
        clock=clock,
        mode=SimMode.BAR,
        friction=friction,
        fill_policy=spec.fill_policy,
        starting_cash=spec.starting_equity,
        multipliers=mults,
    )
    journal = Journal(spec.journal_path, mirror=spec.journal_path != ":memory:")
    portfolio = Portfolio(starting_equity=spec.starting_equity, multipliers=dict(mults))
    engine = Engine(
        mode=Mode.BACKTEST,
        account_id=spec.account_id,
        run_id=run_id,
        clock=clock,
        broker=broker,
        risk_config=spec.risk,
        journal=journal,
        instruments=InstrumentRegistry(spec.instruments),
        portfolio=portfolio,
        code_fn=lambda: "backtest",
    )
    symbols = tuple(i.symbol for i in spec.instruments)
    engine.add_strategy(
        spec.strategy, symbols=symbols, params=spec.params, lifecycle=LifecycleStage.BACKTESTED
    )
    exposure: list[bool] = []
    fills: list[Fill] = []
    for bar in bars:
        clock.advance_to(bar.ts_close)
        await engine.on_bar(bar)
        exposure.append(any(p.qty != 0 for p in portfolio.positions.values()))
    await engine.stop_strategies()
    for entry in journal.entries(kinds=("Fill",)):
        f = entry.decode()
        assert isinstance(f, Fill)
        fills.append(f)
    metrics = compute_metrics(
        engine.equity_curve,
        fills,
        exposure_flags=exposure,
        fees_paid=portfolio.fees_paid,
        turnover=portfolio.turnover,
        periods_per_year=spec.periods_per_year,
        n_trials=spec.n_trials,
        seed=spec.seed,
    )
    result = BacktestResult(
        run_id=run_id,
        orders=sorted(engine.om.all(), key=lambda o: (o.ts_created, o.client_order_id)),
        fills=fills,
        decisions=list(engine.decisions),
        equity_curve=list(engine.equity_curve),
        metrics=metrics,
        stats=dict(engine.stats.__dict__),
        spec_hash=spec_hash,
        data_hash=dh,
    )
    journal.close()
    return result


def run_backtest(spec: BacktestSpec, bars: Sequence[Bar]) -> BacktestResult:
    return asyncio.run(run_backtest_async(spec, bars))


def write_report(
    result: BacktestResult,
    spec: BacktestSpec,
    out_dir: Path | str,
    *,
    title: str,
    extra_meta: dict[str, Any] | None = None,
    adjusted: bool | None = None,
) -> Path:
    from helm.backtest.tearsheet.html import render  # noqa: PLC0415

    meta: dict[str, Any] = {
        "run_id": result.run_id,
        "result_hash": result.result_hash,
        "spec_hash": result.spec_hash,
        "data_hash": result.data_hash,
        "strategy": f"{type(spec.strategy).__module__}:{type(spec.strategy).__name__}",
        "params": spec.params,
        "friction": "on" if spec.friction.fees or spec.friction.spread else "off",
        "fill_policy": spec.fill_policy.value,
        "seed": spec.seed,
        "risk_rejections": result.stats.get("rejected", 0),
        **(extra_meta or {}),
    }
    warnings = data_warnings(spec.instruments, adjusted)
    if adjusted is not None:
        meta["prices"] = "adjusted (split+dividend)" if adjusted else "raw"
    return render(
        out_dir,
        title=title,
        engine="event_driven",
        metrics=result.metrics.to_dict(),
        curve=[(ts, float(eq)) for ts, eq in result.equity_curve],
        meta=meta,
        warnings=warnings,
    )


def data_warnings(instruments: Sequence[Instrument], adjusted: bool | None) -> list[str]:
    """DATA-9 caveats shown on every equities tearsheet."""
    from helm.core.enums import AssetClass  # noqa: PLC0415

    if not any(i.asset_class is AssetClass.EQUITY for i in instruments):
        return []
    out = [
        "Survivorship bias: this universe was picked today and is static. Stocks that were "
        "delisted, acquired or went bankrupt during the test are missing, which flatters results."
    ]
    if adjusted is False:
        out.append(
            "Raw (unadjusted) prices: splits show up as fake price jumps. Re-run with adjusted "
            "bars (helm data fetch --adjusted) for signals; raw is right only for fill prices."
        )
    elif adjusted is None:
        out.append("Price adjustment unknown: record whether bars are raw or adjusted.")
    return out
