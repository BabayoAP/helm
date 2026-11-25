"""Research glue (RES-2/3/4): data sources, the event-driven evaluator for the
validation suite, experiment recording and reproducible reruns.

Promotion evidence always comes from the event-driven engine (I1): the evaluator
here runs the same backtester as `helm backtest` for every parameter set and fold.
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from helm.app.backtest import BacktestResult, BacktestSpec, run_backtest
from helm.backtest.friction.models import FrictionConfig
from helm.core.config import RiskConfig
from helm.core.enums import AssetClass
from helm.core.models import Bar, Instrument, parse_symbol
from helm.core.timeframes import timeframe_ns
from helm.research.experiments.store import ExperimentStore, RunRecord, code_hash
from helm.research.promotion import AccountStats
from helm.research.validation.suite import EvalResult, Evaluator
from helm.strategy.loader import load_strategy
from helm.strategy.params import ParamValue

NS_PER_YEAR = 365 * 86_400 * 10**9


def ts_ns(s: str) -> int:
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()) * 10**9


@dataclass(frozen=True)
class DataSource:
    """Where bars come from, serialisable so a run can be reproduced (RES-2)."""

    symbol: str
    timeframe: str = "1m"
    synthetic: int = 0  # N seeded synthetic bars instead of the lake
    seed: int = 0
    start: str = "2024-01-01"
    end: str | None = None
    asset_class: str = "crypto"
    data_dir: str = "data"
    adjusted: bool = False  # DATA-9: equities split/dividend-adjusted series

    def describe(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def parse(cls, desc: str) -> DataSource:
        return cls(**json.loads(desc))

    def load(self) -> list[Bar]:
        if self.synthetic:
            from helm.data.synthetic import frame_to_bars, gbm_frame  # noqa: PLC0415

            frame = gbm_frame(
                self.synthetic, start_ns=ts_ns(self.start), timeframe=self.timeframe, seed=self.seed
            )
            return list(frame_to_bars(frame, self.symbol, self.timeframe))
        from helm.data.parquet_store import ParquetStore, SeriesKey  # noqa: PLC0415

        datatype = "bars_adj" if self.adjusted else "bars"
        key = SeriesKey(AssetClass(self.asset_class), self.symbol, datatype, self.timeframe)
        end = ts_ns(self.end) if self.end else None
        return list(ParquetStore(self.data_dir).iter_bars(key, ts_ns(self.start), end))


def instrument_for_symbol(
    symbol: str, *, tick: str = "0.01", lot: str = "0.0001", min_notional: str = "5"
) -> Instrument:
    venue, _base, quote, _kind = parse_symbol(symbol)
    if venue == "alpaca":
        from helm.data.connectors.alpaca_common import instrument_for  # noqa: PLC0415

        return instrument_for(symbol)
    return Instrument(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        tick_size=Decimal(tick),
        lot_size=Decimal(lot),
        min_notional=Decimal(min_notional),
        quote_ccy=quote,
    )


@dataclass
class RunSettings:
    """Everything besides strategy params and data that shapes a backtest."""

    strategy: str
    instrument: Instrument
    starting_equity: Decimal = Decimal(10_000)
    friction: FrictionConfig = field(default_factory=FrictionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    seed: int = 0
    return_period: str = "1h"  # returns sampled at this period for Sharpe / DSR

    def spec(self, params: Mapping[str, ParamValue], n_trials: int = 1) -> BacktestSpec:
        return BacktestSpec(
            strategy=load_strategy(self.strategy),
            instruments=[self.instrument],
            params=dict(params),
            starting_equity=self.starting_equity,
            friction=self.friction,
            risk=self.risk,
            seed=self.seed,
            n_trials=n_trials,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "instrument": self.instrument.model_dump(mode="json"),
            "starting_equity": str(self.starting_equity),
            "friction": self.friction.to_dict(),
            "risk": self.risk.model_dump(mode="json"),
            "seed": self.seed,
            "return_period": self.return_period,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RunSettings:
        return cls(
            strategy=d["strategy"],
            instrument=Instrument.model_validate(d["instrument"]),
            starting_equity=Decimal(d["starting_equity"]),
            friction=FrictionConfig.from_dict(d["friction"]),
            risk=RiskConfig.model_validate(d["risk"]),
            seed=int(d["seed"]),
            return_period=d["return_period"],
        )


def period_returns(curve: Sequence[tuple[int, Decimal]], period: str) -> list[float]:
    """Equity sampled at the last point of each period, as simple returns."""
    step = timeframe_ns(period)
    last: dict[int, float] = {}
    for ts, eq in curve:
        last[ts // step] = float(eq)
    vals = [last[k] for k in sorted(last)]
    return [b / a - 1 for a, b in itertools.pairwise(vals) if a > 0]


def eval_from_result(res: BacktestResult, period: str) -> EvalResult:
    rets = period_returns(res.equity_curve, period)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    per_year = NS_PER_YEAR / timeframe_ns(period)
    sharpe = statistics.fmean(rets) / sd * math.sqrt(per_year) if sd > 0 else 0.0
    return EvalResult(
        sharpe=sharpe,
        trades=res.metrics.trade_count,
        returns=tuple(rets),
        total_return=res.metrics.total_return,
    )


def backtest_evaluator(settings: RunSettings) -> Evaluator[Bar]:
    """Event-driven evaluator for the validation suite: a fresh strategy per call."""

    def evaluate(params: Mapping[str, ParamValue], bars: Sequence[Bar]) -> EvalResult:
        if not bars:
            return EvalResult(sharpe=0.0, trades=0, returns=())
        res = run_backtest(settings.spec(params), list(bars))
        return eval_from_result(res, settings.return_period)

    return evaluate


def record_backtest(
    store: ExperimentStore,
    settings: RunSettings,
    source: DataSource,
    spec: BacktestSpec,
    res: BacktestResult,
    *,
    kind: str = "backtest",
) -> RunRecord:
    return store.record(
        run_id=res.run_id,
        kind=kind,
        strategy=spec.strategy,
        params=spec.params,
        data_hash=res.data_hash,
        data_desc=source.describe(),
        spec_hash=res.spec_hash,
        seed=spec.seed,
        n_trials=spec.n_trials,
        metrics={k: v for k, v in res.metrics.to_dict().items() if k != "monte_carlo"},
        result_hash=res.result_hash,
        report={"settings": settings.to_dict()},
    )


@dataclass
class RerunOutcome:
    reproduced: bool
    original_hash: str
    new_hash: str
    code_changed: bool
    data_changed: bool


def rerun(store: ExperimentStore, run_id: str) -> RerunOutcome:
    """RES-2: rerun a recorded backtest from its record; the result hash must match."""
    rec = store.get(run_id)
    if rec is None:
        raise KeyError(f"no experiment {run_id!r}")
    if "settings" not in rec.report:
        raise ValueError(f"run {run_id} is a {rec.kind} record without backtest settings")
    settings = RunSettings.from_dict(rec.report["settings"])
    source = DataSource.parse(rec.data_desc)
    spec = settings.spec(rec.params, n_trials=rec.n_trials)
    res = run_backtest(spec, source.load())
    return RerunOutcome(
        reproduced=res.result_hash == rec.result_hash,
        original_hash=rec.result_hash,
        new_hash=res.result_hash,
        code_changed=code_hash(spec.strategy) != rec.code_hash,
        data_changed=res.data_hash != rec.data_hash,
    )


def account_stats(journal_path: str, account_id: str) -> AccountStats:
    """Paper / live-small evidence for promotion gates, read from an account journal."""
    from helm.backtest.metrics.metrics import round_trips  # noqa: PLC0415
    from helm.core.enums import EngineState, Verdict  # noqa: PLC0415
    from helm.core.events import EngineStateChanged  # noqa: PLC0415
    from helm.core.models import Fill, RiskDecision  # noqa: PLC0415
    from helm.oms.journal import Journal  # noqa: PLC0415

    st = AccountStats()
    fills: list[Fill] = []
    first: int | None = None
    last: int | None = None
    state: EngineState | None = None
    with Journal(journal_path, mirror=False) as j:
        for e in j.entries(account_id=account_id):
            first = e.ts if first is None else first
            last = e.ts
            if e.kind == "Fill":
                f = e.decode()
                assert isinstance(f, Fill)
                fills.append(f)
            elif e.kind == "EngineStateChanged":
                ev = e.decode()
                assert isinstance(ev, EngineStateChanged)
                state = ev.current
                st.halts += ev.current is EngineState.HALTED
            elif e.kind == "RiskDecision":
                d = e.decode()
                assert isinstance(d, RiskDecision)
                st.risk_rejections += d.verdict is Verdict.REJECT
    st.fills = len(fills)
    st.round_trips = len(round_trips(fills))
    st.days = (last - first) / 86_400e9 if first is not None and last is not None else 0.0
    st.currently_halted = state is EngineState.HALTED
    return st
