"""`helm data ...` subcommands (DATA-2, DATA-3)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from helm.core.enums import AssetClass
from helm.data.parquet_store import ParquetStore, SeriesKey
from helm.data.validators import gap_report


def _key(a: argparse.Namespace) -> SeriesKey:
    """DATA-9: adjusted (split/dividend) bars live in their own series, never mixed with raw."""
    datatype = "bars_adj" if getattr(a, "adjusted", False) else "bars"
    return SeriesKey(AssetClass(a.asset_class), a.symbol, datatype, a.timeframe)


def _ts(s: str) -> int:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 10**9


def cmd_import(a: argparse.Namespace) -> int:
    from helm.data.importer import import_bars

    rep = import_bars(a.path, ParquetStore(a.data_dir), _key(a))
    print(json.dumps(rep.to_dict(), indent=2, default=str))
    return 0


def cmd_gaps(a: argparse.Namespace) -> int:
    store = ParquetStore(a.data_dir)
    ts = store.scan_bars(_key(a)).select("ts_open").collect()["ts_open"]
    gaps = gap_report(ts, a.timeframe)
    print(
        json.dumps(
            {
                "bars": ts.len(),
                "gaps": len(gaps),
                "missing_bars": sum(g.missing_bars for g in gaps),
                "first": [g.__dict__ for g in gaps[:20]],
            },
            indent=2,
        )
    )
    return 0


def cmd_fetch(a: argparse.Namespace) -> int:
    """Public market data only: the exchange client is created without credentials."""
    import ccxt.async_support as ccxt_async  # type: ignore[import-untyped]

    from helm.data.connectors.ccxt_history import fetch_history
    from helm.infra.ratelimit import RateGovernor

    async def run() -> int:
        venue = a.symbol.split(":")[0]
        if venue == "alpaca":  # market data needs keys even for the free feed (decision 0023)
            from helm.data.connectors.alpaca import AlpacaOhlcvClient
            from helm.data.connectors.alpaca_common import AlpacaCredentials, is_crypto
            from helm.infra.secrets import EnvSecretStore

            store = EnvSecretStore()
            client = AlpacaOhlcvClient(
                AlpacaCredentials(
                    store.get(f"{a.credentials}_key_id"), store.get(f"{a.credentials}_secret_key")
                ),
                crypto=is_crypto(a.symbol),
                feed=a.feed,
                adjustment="all" if a.adjusted else "raw",
            )
        else:
            if a.adjusted:
                print("--adjusted applies to equities (alpaca) only", file=sys.stderr)
                return 2
            client = getattr(ccxt_async, venue)({"enableRateLimit": False})
        try:
            gov = RateGovernor.for_window(a.requests_per_s, 1.0)
            res = await fetch_history(
                client,
                ParquetStore(a.data_dir),
                _key(a),
                start_ns=_ts(a.start),
                end_ns=_ts(a.end),
                governor=gov,
                now_ns=int(datetime.now(UTC).timestamp()) * 10**9,
            )
            print(
                json.dumps(
                    {
                        "pages": res.pages,
                        "rows_written": res.rows_written,
                        "last_ts_open": res.last_ts_open,
                    }
                )
            )
        finally:
            await client.close()
        return 0

    return asyncio.run(run())


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    data = sub.add_parser("data", help="historical data tools")
    dsub = data.add_subparsers(dest="data_command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--symbol", required=True, help="canonical venue:BASE/QUOTE[:kind]")
        p.add_argument("--timeframe", default="1m")
        p.add_argument("--asset-class", default="crypto", choices=[c.value for c in AssetClass])
        p.add_argument("--data-dir", default="data")

    imp = dsub.add_parser("import", help="import CSV/Parquet bars into the lake (DATA-2)")
    imp.add_argument("path")
    common(imp)
    imp.add_argument("--adjusted", action="store_true", help="the file holds adjusted bars")
    imp.set_defaults(handler=cmd_import)

    gaps = dsub.add_parser("gaps", help="gap report for a series")
    common(gaps)
    gaps.set_defaults(handler=cmd_gaps)

    fetch = dsub.add_parser(
        "fetch", help="fetch history from a CCXT venue or Alpaca (DATA-3, DATA-9)"
    )
    common(fetch)
    fetch.add_argument(
        "--adjusted", action="store_true", help="split+dividend adjusted equities (bars_adj)"
    )
    fetch.add_argument("--feed", default="iex", help="alpaca stock feed: iex (free) or sip")
    fetch.add_argument("--credentials", default="alpaca_paper", help="alpaca secret name prefix")
    fetch.add_argument("--start", required=True, help="ISO date, UTC")
    fetch.add_argument("--end", required=True, help="ISO date, UTC")
    fetch.add_argument("--requests-per-s", type=int, default=5)
    fetch.set_defaults(handler=cmd_fetch)
