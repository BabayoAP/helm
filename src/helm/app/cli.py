"""`helm backtest` and `helm sweep` (BT-1, BT-2, BT-6, RES-6)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def _params(pairs: list[str]) -> dict[str, float | int | str | bool]:
    out: dict[str, float | int | str | bool] = {}
    for p in pairs:
        k, _, v = p.partition("=")
        try:
            out[k] = int(v)
        except ValueError:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def _ts(s: str) -> int:
    dt = datetime.fromisoformat(s)
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()) * 10**9


def _instrument(symbol: str, tick: str, lot: str, min_notional: str):  # type: ignore[no-untyped-def]
    from helm.core.enums import AssetClass
    from helm.core.models import Instrument, parse_symbol

    venue = parse_symbol(symbol)[0]
    return Instrument(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        tick_size=Decimal(tick),
        lot_size=Decimal(lot),
        min_notional=Decimal(min_notional),
        quote_ccy=parse_symbol(symbol)[2],
    )


def cmd_backtest(a: argparse.Namespace) -> int:
    from helm.app.backtest import BacktestSpec, run_backtest, write_report
    from helm.backtest.friction.models import FrictionConfig
    from helm.core.enums import AssetClass
    from helm.data.parquet_store import ParquetStore, SeriesKey
    from helm.data.synthetic import frame_to_bars, gbm_frame
    from helm.infra.config import load_config
    from helm.strategy.loader import load_strategy

    inst = _instrument(a.symbol, a.tick, a.lot, a.min_notional)
    if a.synthetic:
        bars = list(
            frame_to_bars(
                gbm_frame(a.synthetic, start_ns=_ts(a.start), timeframe=a.timeframe, seed=a.seed),
                a.symbol,
                a.timeframe,
            )
        )
    else:
        key = SeriesKey(AssetClass(a.asset_class), a.symbol, "bars", a.timeframe)
        bars = list(
            ParquetStore(a.data_dir).iter_bars(key, _ts(a.start), _ts(a.end) if a.end else None)
        )
    if not bars:
        print("no bars in range: run `helm data import|fetch` first, or use --synthetic N")
        return 2
    spec = BacktestSpec(
        strategy=load_strategy(a.strategy),
        instruments=[inst],
        params=_params(a.param),
        starting_equity=Decimal(a.equity),
        friction=FrictionConfig() if not a.no_friction else FrictionConfig.off(),
        seed=a.seed,
    )
    if a.risk:
        spec.risk = load_config(risk_path=a.risk).risk
    res = run_backtest(spec, bars)
    out = Path(a.out) / f"{spec.strategy.id}-{res.run_id}"
    path = write_report(res, spec, out, title=f"{spec.strategy.id} · {a.symbol} · {a.timeframe}")
    print(
        json.dumps(
            {
                "tearsheet": str(path),
                "result_hash": res.result_hash,
                **{k: v for k, v in res.metrics.to_dict().items() if k != "monte_carlo"},
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_sweep(a: argparse.Namespace) -> int:
    from helm.backtest.vectorized.sweep import sma_cross_sweep
    from helm.core.enums import AssetClass
    from helm.data.parquet_store import ParquetStore, SeriesKey
    from helm.data.synthetic import gbm_frame

    if a.synthetic:
        df = gbm_frame(a.synthetic, start_ns=0, seed=a.seed)
    else:
        df = ParquetStore(a.data_dir).read_bars(
            SeriesKey(AssetClass.CRYPTO, a.symbol, "bars", a.timeframe)
        )
    grid = [(f, s) for f in a.fast for s in a.slow]
    rows = sorted(sma_cross_sweep(df, grid, cost_bps=a.cost_bps), key=lambda r: -r.sharpe)
    print("VECTORIZED SCREENING ONLY: results are not promotion evidence (PRD 9.4)")
    for r in rows[: a.top]:
        print(json.dumps(r.__dict__))
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    bt = sub.add_parser("backtest", help="event-driven backtest -> tearsheet (BT-2, BT-6)")
    bt.add_argument("--strategy", default="helm.strategy.examples.sma_cross:SmaCross")
    bt.add_argument("--symbol", default="fake:BTC/USD")
    bt.add_argument("--timeframe", default="1m")
    bt.add_argument("--asset-class", default="crypto")
    bt.add_argument("--start", default="2024-01-01")
    bt.add_argument("--end", default=None)
    bt.add_argument("--param", action="append", default=[], help="key=value (repeatable)")
    bt.add_argument("--equity", default="10000")
    bt.add_argument("--tick", default="0.01")
    bt.add_argument("--lot", default="0.0001")
    bt.add_argument("--min-notional", default="5")
    bt.add_argument("--risk", default=None, help="risk TOML (default: PRD defaults)")
    bt.add_argument("--no-friction", action="store_true")
    bt.add_argument("--seed", type=int, default=0)
    bt.add_argument(
        "--synthetic", type=int, default=0, help="use N seeded synthetic bars instead of the lake"
    )
    bt.add_argument("--data-dir", default="data")
    bt.add_argument("--out", default="reports")
    bt.set_defaults(handler=cmd_backtest)

    sw = sub.add_parser("sweep", help="vectorized SMA-cross screening sweep (BT-1)")
    sw.add_argument("--symbol", default="fake:BTC/USD")
    sw.add_argument("--timeframe", default="1m")
    sw.add_argument("--fast", type=int, nargs="+", default=[5, 10, 20, 30])
    sw.add_argument("--slow", type=int, nargs="+", default=[50, 100, 200])
    sw.add_argument("--cost-bps", type=float, default=10.0)
    sw.add_argument("--top", type=int, default=10)
    sw.add_argument("--synthetic", type=int, default=0)
    sw.add_argument("--seed", type=int, default=0)
    sw.add_argument("--data-dir", default="data")
    sw.set_defaults(handler=cmd_sweep)
