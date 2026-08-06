"""Money handling.

Every amount in this system is an integer number of paise. Never a float, and never
a rupee value with a decimal point.

Floats cannot represent 0.10 exactly, so a float rupee total drifts as sessions,
extensions and surcharges accumulate. A café running fifteen units for twelve hours
does enough arithmetic for that drift to reach the counter, and "every rupee
downstream traces back to this" stops being true. Integers cannot drift.

Rounding is half-up rather than banker's rounding: a customer shown ₹62.50 expects to
pay ₹63, and Python's default round() would give 62. Rounding happens once, at the end
of a calculation, never on intermediate values.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Amounts are integer paise. 100 paise = ₹1.
Paise = int

PAISE_PER_RUPEE = 100
MINUTES_PER_HOUR = 60


def rupees(amount: float | int | str) -> Paise:
    """Convert a rupee amount to paise.

    Accepts a string or number. Decimal is used rather than float arithmetic so that
    rupees("0.10") is exactly 10 paise instead of 10.000000000000002.
    """
    quantised = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantised * PAISE_PER_RUPEE)


def to_rupees(amount: Paise) -> Decimal:
    """Convert paise back to a rupee Decimal, for display and reporting only."""
    return (Decimal(amount) / PAISE_PER_RUPEE).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def format_rupees(amount: Paise) -> str:
    """Render paise as ``₹1,234.50`` for the dashboard and receipts."""
    return f"₹{to_rupees(amount):,.2f}"


def prorate_hourly(hourly_rate: Paise, minutes: int) -> Paise:
    """Charge ``minutes`` at an hourly rate: rate ÷ 60 × minutes.

    This is the doc's base billing rule. The multiplication happens before the division
    so that no precision is lost part-way through: a ₹120/hr rate for 7 minutes is
    120_00 × 7 ÷ 60 = 1400 paise exactly, where dividing first would round ₹2 per hour
    away on every session.

    Negative durations are treated as zero. A clock adjustment should never produce a
    credit note.
    """
    if minutes <= 0:
        return 0

    return _divide_half_up(hourly_rate * minutes, MINUTES_PER_HOUR)


def _divide_half_up(numerator: int, denominator: int) -> Paise:
    """Integer division rounding halves away from zero, keeping it exact throughout."""
    quotient = Decimal(numerator) / Decimal(denominator)
    return int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
