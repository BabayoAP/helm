"""Alpaca helpers shared by the market-data feed and the broker adapter (decision 0025).

Canonical symbols: equities `alpaca:AAPL/USD`, crypto `alpaca:BTC/USD:crypto`.
Pure functions only; no network, no secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from helm.core.enums import AssetClass
from helm.core.models import Instrument, parse_symbol

VENUE = "alpaca"
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
STREAM_DATA_URL = "wss://stream.data.alpaca.markets"
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC")


@dataclass(frozen=True)
class AlpacaCredentials:
    key_id: str
    secret_key: str

    def __repr__(self) -> str:  # never print secrets (SEC-2)
        return "AlpacaCredentials(<redacted>)"

    def headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key}


def trading_url(environment: str) -> str:
    if environment == "paper":
        return PAPER_TRADING_URL
    if environment == "live":
        return LIVE_TRADING_URL
    raise ValueError(f"alpaca environment must be 'paper' or 'live', got {environment!r}")


def is_crypto(symbol: str) -> bool:
    return parse_symbol(symbol)[3] == "crypto"


def to_venue(symbol: str) -> str:
    venue, base, quote, kind = parse_symbol(symbol)
    if venue != VENUE:
        raise ValueError(f"not an alpaca symbol: {symbol!r}")
    if kind == "crypto":
        return f"{base}/{quote}"
    if kind is not None:
        raise ValueError(f"unsupported alpaca symbol kind {kind!r} in {symbol!r}")
    if quote != "USD":
        raise ValueError(f"alpaca equities are quoted in USD: {symbol!r}")
    return base


def from_venue(venue_symbol: str, asset_class: str = "us_equity") -> str:
    """Alpaca symbol -> canonical. Crypto positions come back without the slash (BTCUSD)."""
    if asset_class == "crypto" or "/" in venue_symbol:
        if "/" in venue_symbol:
            base, quote = venue_symbol.split("/", 1)
        else:
            quote = next(q for q in _CRYPTO_QUOTES if venue_symbol.endswith(q))
            base = venue_symbol[: -len(quote)]
        return f"{VENUE}:{base}/{quote}:crypto"
    return f"{VENUE}:{venue_symbol}/USD"


def instrument_for(symbol: str, asset: dict[str, object] | None = None) -> Instrument:
    """Instrument metadata. `asset` is an optional `/v2/assets/{symbol}` payload."""
    a = asset or {}
    if is_crypto(symbol):
        quote = parse_symbol(symbol)[2]
        return Instrument(
            venue=VENUE,
            symbol=symbol,
            asset_class=AssetClass.CRYPTO,
            tick_size=Decimal(str(a.get("price_increment") or "0.01")),
            lot_size=Decimal(str(a.get("min_trade_increment") or "0.0001")),
            min_qty=Decimal(str(a.get("min_order_size") or "0")),
            min_notional=Decimal(1),
            quote_ccy=quote,
            calendar="24x7",
        )
    return Instrument(
        venue=VENUE,
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        tick_size=Decimal("0.01"),
        lot_size=Decimal(1),  # whole shares (fractional orders are market/day only)
        min_qty=Decimal(1),
        min_notional=Decimal(1),
        quote_ccy="USD",
        calendar="us_equity_rth",
    )


def parse_ts(s: str) -> int:
    """RFC 3339 (any fractional precision, Z or offset) -> UTC integer ns."""
    s = s.strip()
    tz = "+00:00"
    if s.endswith("Z"):
        s = s[:-1]
    elif len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
        s, tz = s[:-6], s[-6:]
    frac = ""
    if "." in s:
        s, frac = s.split(".", 1)
    base = datetime.fromisoformat(s + tz).astimezone(UTC)
    nanos = int((frac + "000000000")[:9]) if frac else 0
    return int(base.timestamp()) * 1_000_000_000 + nanos


def iso(ts_ns: int) -> str:
    sec, nanos = divmod(ts_ns, 1_000_000_000)
    return datetime.fromtimestamp(sec, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def norm_qty(v: object) -> str:
    """Canonical decimal string: '1', '1.0' and '1.00' are the same quantity."""
    d = Decimal(str(v))
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")
