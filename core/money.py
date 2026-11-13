"""Decimal money helpers (ARCHITECTURE.md §2, §8.3).

Rules:
  * money is always ``Decimal``, 2 decimal places, INR, rounded ROUND_HALF_UP;
  * floats (and bools) are rejected everywhere, never silently converted;
  * sums are computed in Python ``Decimal`` (``dsum``), never by SQLite, which stores
    decimals as REAL.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_DIGITS = 12
MAX_ABS = Decimal("9999999999.99")  # Decimal(12, 2)


class MoneyError(ValueError):
    """A value is not acceptable as money."""


def to_decimal(value: object) -> Decimal:
    """Exact conversion of Decimal / int / numeric str to Decimal. Floats are refused."""
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyError(f"{type(value).__name__} is not allowed for money; use str or Decimal")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise MoneyError(f"{value!r} is not a number") from None
    else:
        raise MoneyError(f"{type(value).__name__} is not allowed for money")
    if not result.is_finite():
        raise MoneyError(f"{value!r} is not a finite number")
    return result


def round_money(value: object) -> Decimal:
    """Round to cents with ROUND_HALF_UP (e.g. 0.025 -> 0.03, -0.025 -> -0.03)."""
    result = to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    _check_range(result, value)
    return result + 0  # normalises -0.00 to 0.00


def parse_money(value: object, *, allow_negative: bool = False) -> Decimal:
    """Strict parse for input: at most 2 decimal places, no rounding, within Decimal(12,2)."""
    d = to_decimal(value)
    if d.as_tuple().exponent < -2:  # strict, like the API field: "1.500" is rejected too
        raise MoneyError(f"{value!r} has more than 2 decimal places")
    if not allow_negative and d < 0:
        raise MoneyError(f"{value!r} must not be negative")
    result = d.quantize(CENT)
    _check_range(result, value)
    return result + 0


def apply_rate(amount: object, rate: object) -> Decimal:
    """``amount × rate`` rounded HALF_UP to cents (late fee 2%, withdrawal 60%, …)."""
    return round_money(to_decimal(amount) * to_decimal(rate))


def dsum(values: Iterable[object]) -> Decimal:
    """Exact Decimal sum, quantized to cents. Floats in the input are refused."""
    total = Decimal(0)
    for v in values:
        total += to_decimal(v)
    return round_money(total)


def format_money(value: object) -> str:
    """Canonical string form for APIs and exports: ``"1234.50"``, no exponent, no ``-0.00``."""
    return f"{round_money(value):.2f}"


def _check_range(d: Decimal, original: object) -> None:
    if abs(d) > MAX_ABS:
        raise MoneyError(f"{original!r} exceeds the money range ±{MAX_ABS}")
