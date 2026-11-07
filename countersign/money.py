"""Money helpers: Decimal only, never float."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

CENT = Decimal("0.01")


class MoneyError(ValueError):
    pass


def parse_amount(value) -> Decimal:
    """Parse an amount and keep it exact. Rejects floats, NaN, negative numbers and sub-cent precision."""
    if isinstance(value, float):
        raise MoneyError("float amounts are not accepted; pass a string like '12.50'")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise MoneyError(f"not an amount: {value!r}") from exc
    if not d.is_finite():
        raise MoneyError(f"not a finite amount: {value!r}")
    if d.as_tuple().exponent < -2:  # type: ignore[operator]
        raise MoneyError(f"more than 2 decimals: {value!r}")
    if d <= 0:
        raise MoneyError(f"amount must be positive: {value!r}")
    return d.quantize(CENT)


def fmt(d: Decimal) -> str:
    return str(d.quantize(CENT))
