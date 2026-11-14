"""Pure academics calculators (ARCHITECTURE.md §5). No ORM, no clock, no I/O.

Frozen contract:
    withdrawal_refund_pct(term, withdrawal_date) -> Decimal      BR-WDR-02
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

# BR-WDR-02: (last day of the band, refund fraction). Day d = (withdrawal_date − start).days,
# so day 0 = start date; every range is inclusive and they do not overlap. Day 28 is the
# census date: from day 28 on, nothing is refunded.
WITHDRAWAL_SCHEDULE: tuple[tuple[int, Decimal], ...] = (
    (7, Decimal("0.80")),
    (14, Decimal("0.60")),
    (27, Decimal("0.40")),
)
NO_REFUND = Decimal("0.00")


def withdrawal_day(term, withdrawal_date: date) -> int:
    """Day number of a withdrawal: 0 on the term's start date."""
    return (withdrawal_date - term.start_date).days


def withdrawal_refund_pct(term, withdrawal_date: date) -> Decimal:
    """BR-WDR-02 refund fraction for refundable lines (0.80 = 80%).

    Raises ``ValueError`` before the start date: that is a cancellation (BR-WDR-01).
    """
    day = withdrawal_day(term, withdrawal_date)
    if day < 0:
        raise ValueError(
            f"{withdrawal_date} is before the term start {term.start_date}: a cancellation"
        )
    for last_day, pct in WITHDRAWAL_SCHEDULE:
        if day <= last_day:
            return pct
    return NO_REFUND
