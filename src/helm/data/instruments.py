"""Instrument metadata service (DATA-8).

Cached tick/lot/min-notional per symbol, refreshed from a loader (e.g. CCXT
`load_markets`) on a TTL and persisted to JSON so the engine can start offline.
Unknown instrument => callers must reject.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from helm.core.enums import AssetClass
from helm.core.errors import HelmError
from helm.core.models import Instrument, OrderIntent
from helm.core.money import quantize_down, quantize_price

Loader = Callable[[], Awaitable[Iterable[Instrument]]]


class UnknownInstrumentError(HelmError):
    pass


class InstrumentRegistry:
    def __init__(
        self,
        instruments: Iterable[Instrument] = (),
        *,
        loader: Loader | None = None,
        ttl_ns: int = 3600 * 10**9,
        cache_path: Path | str | None = None,
    ) -> None:
        self._by_symbol: dict[str, Instrument] = {i.symbol: i for i in instruments}
        self._loader = loader
        self._ttl = ttl_ns
        self._loaded_at: int | None = None
        self._cache = Path(cache_path) if cache_path else None
        if self._cache and self._cache.exists() and not self._by_symbol:
            self._by_symbol = {
                i["symbol"]: Instrument.model_validate(i)
                for i in json.loads(self._cache.read_text())
            }

    def __contains__(self, symbol: str) -> bool:
        return symbol in self._by_symbol

    def get(self, symbol: str) -> Instrument:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise UnknownInstrumentError(f"unknown instrument {symbol!r}") from None

    def find(self, symbol: str) -> Instrument | None:
        return self._by_symbol.get(symbol)

    def all(self) -> list[Instrument]:
        return [self._by_symbol[k] for k in sorted(self._by_symbol)]

    def upsert(self, inst: Instrument) -> None:
        self._by_symbol[inst.symbol] = inst

    async def refresh(self, now_ns: int, *, force: bool = False) -> bool:
        if self._loader is None:
            return False
        if not force and self._loaded_at is not None and now_ns - self._loaded_at < self._ttl:
            return False
        fresh = {i.symbol: i for i in await self._loader()}
        self._by_symbol.update(fresh)
        self._loaded_at = now_ns
        if self._cache:
            self._cache.parent.mkdir(parents=True, exist_ok=True)
            self._cache.write_text(
                json.dumps([i.model_dump(mode="json") for i in self.all()], indent=1)
            )
        return True

    def conform(self, intent: OrderIntent) -> OrderIntent:
        """Quantize qty (down to lot) and prices (to tick, in the safe direction)."""
        inst = self.get(intent.symbol)
        qty = quantize_down(intent.qty, inst.lot_size)
        upd: dict[str, Any] = {"qty": qty}
        if intent.limit_price is not None:
            upd["limit_price"] = quantize_price(
                intent.limit_price, inst.tick_size, side_sign=intent.side.sign
            )
        if intent.stop_price is not None:
            upd["stop_price"] = quantize_price(intent.stop_price, inst.tick_size)
        return intent.model_copy(update=upd)


def instrument_from_ccxt(venue: str, market: dict[str, Any]) -> Instrument | None:
    """Map a CCXT unified market to an Instrument (precision mode TICK_SIZE assumed)."""
    if not market.get("active", True):
        return None
    kind = None if market.get("spot") else market.get("type")
    symbol = f"{venue}:{market['base']}/{market['quote']}" + (f":{kind}" if kind else "")
    precision = market.get("precision") or {}
    limits = market.get("limits") or {}

    def dec(v: Any, default: str) -> Decimal:
        return Decimal(str(v)) if v not in (None, 0) else Decimal(default)

    return Instrument(
        venue=venue,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO,
        tick_size=dec(precision.get("price"), "0.00000001"),
        lot_size=dec(precision.get("amount"), "0.00000001"),
        min_qty=dec((limits.get("amount") or {}).get("min"), "0"),
        min_notional=dec((limits.get("cost") or {}).get("min"), "0"),
        contract_multiplier=dec(market.get("contractSize"), "1"),
        quote_ccy=str(market["quote"]),
    )
