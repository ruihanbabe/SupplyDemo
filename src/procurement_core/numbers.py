"""Exact NUMERIC(18,6) boundary shared by procurement arithmetic."""
from decimal import Decimal, localcontext


def quantity(value: Decimal, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Quantity must be a finite Decimal")
    with localcontext() as ctx:
        ctx.prec = 60
        if abs(value) >= Decimal(1000000000000) or value != value.quantize(Decimal("0.000001")):
            raise ValueError("Quantity exceeds NUMERIC(18,6); implicit rounding is forbidden")
    if value < 0 or (positive and value == 0):
        raise ValueError("Quantity must be positive" if positive else "Quantity must be nonnegative")
    return value
