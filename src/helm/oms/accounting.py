"""Accounting engine shared by backtest, paper and live (BT-8).

Average-cost positions with flips, realized/unrealized PnL, fees, funding,
gross exposure and leverage. Margin-style cash: cash = equity_0 + realized -
fees - funding, so equity = cash + unrealized holds for spot and linear
derivatives alike. Single quote currency per portfolio (decision 0012).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from helm.core.models import Fill, Position
from helm.core.money import ZERO

# partial-close cost is rounded to 1e-12 so every ledger value stays exact
# within Decimal's 28-digit context (no silent precision loss in sums)
COST_Q = Decimal("1e-12")


@dataclass
class PositionState:
    """`cost` is the signed cost basis of the open quantity (qty * avg price).

    Tracking cost (not avg price) keeps the accounting identity exact: any
    rounding in a partial close moves between realized and unrealized and
    cancels in equity.
    """

    qty: Decimal = ZERO
    cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    mark: Decimal | None = None

    @property
    def avg_px(self) -> Decimal:
        return self.cost / self.qty if self.qty != 0 else ZERO


@dataclass
class Portfolio:
    starting_equity: Decimal
    multipliers: dict[str, Decimal] = field(default_factory=lambda: {})
    positions: dict[str, PositionState] = field(default_factory=lambda: {})
    fees_paid: Decimal = ZERO
    funding_paid: Decimal = ZERO
    trade_count: int = 0
    turnover: Decimal = ZERO

    def _mult(self, symbol: str) -> Decimal:
        return self.multipliers.get(symbol, Decimal(1))

    def apply_fill(self, fill: Fill) -> Decimal:
        """Apply a fill; returns the realized PnL it produced (before fees)."""
        p = self.positions.setdefault(fill.symbol, PositionState())
        mult = self._mult(fill.symbol)
        signed = fill.qty * fill.side.sign
        self.fees_paid += fill.fee
        self.trade_count += 1
        self.turnover += fill.qty * fill.price * mult
        if p.mark is None:
            p.mark = fill.price
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            p.qty += signed
            p.cost += signed * fill.price
            return ZERO
        closing = min(abs(signed), abs(p.qty))
        direction = Decimal(1) if p.qty > 0 else Decimal(-1)
        removed = (
            p.cost if closing == abs(p.qty) else (p.cost * closing / abs(p.qty)).quantize(COST_Q)
        )
        realized = (direction * fill.price * closing - removed) * mult
        p.realized_pnl += realized
        p.cost -= removed
        p.qty += signed
        if p.qty == 0:
            p.cost = ZERO
        elif (p.qty > 0) != (direction > 0):  # flipped through zero: new lot at fill price
            p.cost = p.qty * fill.price
        return realized

    def mark(self, symbol: str, price: Decimal) -> None:
        if price <= 0:
            raise ValueError(f"mark price must be positive, got {price}")
        self.positions.setdefault(symbol, PositionState()).mark = price

    def apply_funding(self, symbol: str, rate: Decimal) -> Decimal:
        """Perp funding: longs pay rate * notional when rate > 0. Returns amount paid."""
        p = self.positions.get(symbol)
        if p is None or p.qty == 0 or p.mark is None:
            return ZERO
        paid = p.qty * p.mark * self._mult(symbol) * rate
        self.funding_paid += paid
        return paid

    def unrealized_for(self, symbol: str) -> Decimal:
        p = self.positions.get(symbol)
        if p is None or p.qty == 0 or p.mark is None:
            return ZERO
        return (p.qty * p.mark - p.cost) * self._mult(symbol)

    def unrealized(self) -> Decimal:
        return sum((self.unrealized_for(s) for s in self.positions), ZERO)

    def realized(self) -> Decimal:
        return sum((p.realized_pnl for p in self.positions.values()), ZERO)

    def cash(self) -> Decimal:
        return self.starting_equity + self.realized() - self.fees_paid - self.funding_paid

    def equity(self) -> Decimal:
        return self.cash() + self.unrealized()

    def exposure(self, symbol: str) -> Decimal:
        """Signed notional at mark."""
        p = self.positions.get(symbol)
        if p is None or p.qty == 0 or p.mark is None:
            return ZERO
        return p.qty * p.mark * self._mult(symbol)

    def gross_exposure(self) -> Decimal:
        return sum((abs(self.exposure(s)) for s in self.positions), ZERO)

    def leverage(self) -> Decimal:
        eq = self.equity()
        return self.gross_exposure() / eq if eq > 0 else Decimal("Infinity")

    def position(self, symbol: str) -> Position:
        p = self.positions.get(symbol)
        if p is None:
            return Position(symbol=symbol)
        return Position(
            symbol=symbol,
            qty=p.qty,
            avg_px=p.avg_px,
            realized_pnl=p.realized_pnl,
            unrealized_pnl=self.unrealized_for(symbol),
        )

    def snapshot(self) -> tuple[Position, ...]:
        return tuple(self.position(s) for s in sorted(self.positions) if self.positions[s].qty != 0)
