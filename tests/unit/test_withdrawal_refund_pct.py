"""BR-WDR-02: withdrawal refund percentage boundaries.

Day number d = (withdrawal_date - term.start_date).days, so day 0 = start date.
    d 0-7   -> 80%
    d 8-14  -> 60%
    d 15-27 -> 40%
    d >= 28 -> 0%   (day 28 = census date)

The calculator is pure (no ORM), so these tests need no database.
The percentage comes back as a Decimal fraction (0.80 = 80%).
"""

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from academics.calculators import withdrawal_refund_pct

START = date(2026, 9, 7)  # TERM-2026-FALL start_date
TERM = SimpleNamespace(
    term_id="TERM-2026-FALL",
    start_date=START,
    census_date=START + timedelta(days=28),
    end_date=date(2026, 12, 19),
)


def test_BR_WDR_02_census_date_is_day_28():
    assert (TERM.census_date - TERM.start_date).days == 28


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (7, Decimal("0.80")),  # last day of 80% band
        (8, Decimal("0.60")),  # first day of 60% band
        (14, Decimal("0.60")),  # last day of 60% band
        (15, Decimal("0.40")),  # first day of 40% band
        (27, Decimal("0.40")),  # last day of 40% band
        (28, Decimal("0.00")),  # census date: no refund
        (29, Decimal("0.00")),  # after census
    ],
)
def test_BR_WDR_02_refund_pct_boundaries(day, expected):
    withdrawal_date = START + timedelta(days=day)
    result = withdrawal_refund_pct(TERM, withdrawal_date)
    assert isinstance(result, Decimal)
    assert result == expected
