"""Canonical data contracts (PRD §8, DATA-1).

All models are immutable Pydantic v2 models. Timestamps are UTC integer
nanoseconds. Prices/qty/fees are Decimal. Unknown venue fields are preserved
in `raw` for debugging.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helm.core.enums import (
    AssetClass,
    Liquidity,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    Verdict,
)
from helm.core.money import ZERO


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_inf_nan="strings")

    def fingerprint(self) -> str:
        """Stable SHA-256 of the canonical JSON form."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def parse_symbol(symbol: str) -> tuple[str, str, str, str | None]:
    """Split canonical `venue:BASE/QUOTE[:kind]` into parts."""
    parts = symbol.split(":")
    if len(parts) not in (2, 3) or "/" not in parts[1]:
        raise ValueError(f"symbol must be 'venue:BASE/QUOTE[:kind]', got {symbol!r}")
    base, quote = parts[1].split("/", 1)
    if not (parts[0] and base and quote):
        raise ValueError(f"empty component in symbol {symbol!r}")
    return parts[0], base, quote, parts[2] if len(parts) == 3 else None


class Instrument(Model):
    venue: str
    symbol: str
    asset_class: AssetClass
    tick_size: Decimal = Field(gt=0)
    lot_size: Decimal = Field(gt=0)
    min_qty: Decimal = Field(default=ZERO, ge=0)
    min_notional: Decimal = Field(default=ZERO, ge=0)
    contract_multiplier: Decimal = Field(default=Decimal(1), gt=0)
    quote_ccy: str
    calendar: str = "24x7"

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, v: str) -> str:
        parse_symbol(v)
        return v

    @model_validator(mode="after")
    def _venue_matches(self) -> Instrument:
        if parse_symbol(self.symbol)[0] != self.venue:
            raise ValueError("symbol venue prefix must equal venue")
        return self

    @property
    def base(self) -> str:
        return parse_symbol(self.symbol)[1]


class Bar(Model):
    symbol: str
    timeframe: str
    ts_open: int
    ts_close: int
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741  (PRD field name)
    c: Decimal
    volume: Decimal = Field(ge=0)
    trades: int | None = None
    closed: bool = True

    @model_validator(mode="after")
    def _sanity(self) -> Bar:
        if self.ts_close <= self.ts_open:
            raise ValueError("ts_close must be after ts_open")
        if self.l > min(self.o, self.c) or self.h < max(self.o, self.c) or self.l > self.h:
            raise ValueError("OHLC sanity violated")
        return self


class Quote(Model):
    symbol: str
    bid: Decimal
    bid_sz: Decimal = Field(ge=0)
    ask: Decimal
    ask_sz: Decimal = Field(ge=0)
    ts_event: int
    ts_recv: int

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


class Trade(Model):
    symbol: str
    price: Decimal
    size: Decimal = Field(gt=0)
    side: Side | None = None
    trade_id: str
    ts_event: int
    ts_recv: int


class BookLevel(Model):
    price: Decimal
    size: Decimal = Field(ge=0)


class BookDelta(Model):
    symbol: str
    seq: int
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    ts_event: int
    ts_recv: int


class BookSnapshot(Model):
    symbol: str
    seq: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    ts_event: int
    ts_recv: int


class WalletEvent(Model):
    chain: str
    address: str
    tx_hash: str
    kind: str
    token_in: str | None = None
    token_out: str | None = None
    amount_in: Decimal | None = None
    amount_out: Decimal | None = None
    usd_value: Decimal | None = None
    ts_event: int


class OrderIntent(Model):
    strategy_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal = Field(gt=0)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    tag: str = ""

    @model_validator(mode="after")
    def _prices(self) -> OrderIntent:
        if self.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError("limit orders need limit_price")
        if self.type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError("stop orders need stop_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        return self

    @property
    def intent_id(self) -> str:
        return self.fingerprint()[:16]


class Order(Model):
    client_order_id: str
    account_id: str
    intent: OrderIntent
    state: OrderState = OrderState.NEW
    venue_order_id: str | None = None
    filled_qty: Decimal = ZERO
    avg_px: Decimal = ZERO
    fees: Decimal = ZERO
    ts_created: int
    ts_updated: int

    @property
    def remaining(self) -> Decimal:
        return self.intent.qty - self.filled_qty


class Fill(Model):
    client_order_id: str
    venue_order_id: str | None = None
    venue: str
    trade_id: str
    symbol: str
    side: Side
    price: Decimal = Field(gt=0)
    qty: Decimal = Field(gt=0)
    fee: Decimal = ZERO
    fee_ccy: str
    liquidity: Liquidity = Liquidity.TAKER
    ts: int

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.venue, self.trade_id)


class Position(Model):
    symbol: str
    qty: Decimal = ZERO
    avg_px: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO


class Balance(Model):
    asset: str
    free: Decimal
    locked: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class RiskDecision(Model):
    intent_id: str
    verdict: Verdict
    rule_id: str
    reason: str
    inputs_snapshot_hash: str
    modified_intent: OrderIntent | None = None

    @property
    def approved(self) -> bool:
        return self.verdict is not Verdict.REJECT
