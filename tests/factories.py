"""Shared test builders."""

from __future__ import annotations

from decimal import Decimal

from helm.core.enums import AssetClass, OrderType, Side
from helm.core.models import Bar, Instrument, Order, OrderIntent, Quote

NS = 1_000_000_000
MIN = 60 * NS
SYM = "fake:BTC/USD"


def instrument(symbol: str = SYM, **kw: object) -> Instrument:
    venue = symbol.split(":", maxsplit=1)[0]
    base: dict[str, object] = {
        "venue": venue,
        "symbol": symbol,
        "asset_class": AssetClass.CRYPTO,
        "tick_size": Decimal("0.01"),
        "lot_size": Decimal("0.0001"),
        "min_qty": Decimal("0.0001"),
        "min_notional": Decimal("5"),
        "quote_ccy": "USD",
    }
    base.update(kw)
    return Instrument.model_validate(base)


def intent(
    side: Side = Side.BUY,
    qty: str = "0.01",
    type_: OrderType = OrderType.MARKET,
    limit: str | None = None,
    symbol: str = SYM,
    strategy_id: str = "s1",
    reduce_only: bool = False,
) -> OrderIntent:
    return OrderIntent(
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        type=type_,
        qty=Decimal(qty),
        limit_price=Decimal(limit) if limit else None,
        reduce_only=reduce_only,
    )


def order(
    coid: str = "r1-s1-0", it: OrderIntent | None = None, account: str = "fake-acct"
) -> Order:
    return Order(
        client_order_id=coid, account_id=account, intent=it or intent(), ts_created=0, ts_updated=0
    )


def quote(bid: str = "100", ask: str = "100.1", ts: int = 0, symbol: str = SYM) -> Quote:
    return Quote(
        symbol=symbol,
        bid=Decimal(bid),
        bid_sz=Decimal(1),
        ask=Decimal(ask),
        ask_sz=Decimal(1),
        ts_event=ts,
        ts_recv=ts,
    )


def bar(i: int, o: float, h: float, l: float, c: float, v: float = 10, symbol: str = SYM) -> Bar:  # noqa: E741
    return Bar(
        symbol=symbol,
        timeframe="1m",
        ts_open=i * MIN,
        ts_close=(i + 1) * MIN,
        o=Decimal(str(o)),
        h=Decimal(str(h)),
        l=Decimal(str(l)),
        c=Decimal(str(c)),
        volume=Decimal(str(v)),
    )
