"""Decimal helpers for order/accounting boundaries (PRD §4, §8).

Floats are fine for research; anything that touches qty/price/fees in OMS,
brokers, or accounting goes through these helpers.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, InvalidOperation

from helm.core.errors import QuantizationError

ZERO = Decimal(0)
ONE = Decimal(1)


def D(value: Decimal | int | float | str) -> Decimal:
    """Convert to Decimal without binary-float artifacts (floats go through repr)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise QuantizationError(f"not a number: {value!r}") from exc


def _check_step(step: Decimal) -> None:
    if step <= 0:
        raise QuantizationError(f"step must be positive, got {step}")


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Round toward zero to a multiple of `step` (used for qty: never oversize)."""
    _check_step(step)
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (units * step).quantize(step)


def quantize_price(value: Decimal, tick: Decimal, *, side_sign: int = 0) -> Decimal:
    """Snap a price to `tick`.

    side_sign=+1 (buy) rounds down (never pay more than asked),
    side_sign=-1 (sell) rounds up (never sell for less), 0 rounds half-even.
    """
    _check_step(tick)
    rounding = ROUND_HALF_EVEN
    if side_sign > 0:
        rounding = ROUND_DOWN
    elif side_sign < 0:
        rounding = ROUND_UP
    units = (value / tick).to_integral_value(rounding=rounding)
    return (units * tick).quantize(tick)


def is_multiple(value: Decimal, step: Decimal) -> bool:
    _check_step(step)
    return (value % step) == 0


def bps(a: Decimal, b: Decimal) -> Decimal:
    """Relative difference of a vs b in basis points."""
    if b == 0:
        raise QuantizationError("bps reference is zero")
    return (a - b) / b * Decimal(10_000)
