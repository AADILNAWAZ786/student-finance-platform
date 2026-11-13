"""BR-ALC-01..04: AUTO payment allocation plan.

Contract under test (payments.calculators), ARCHITECTURE.md §5:
    plan_allocation(amount, open_lines) -> list[AllocCalc(line_id, amount)]
        open_lines: objects with line_id, due_date, allocation_priority, open_amount
        order: due_date, allocation_priority, line_id; lines with open 0 skipped
        remainder (amount - sum) stays unallocated
        raises ValueError if amount <= 0 or any open_amount < 0
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from payments.calculators import plan_allocation

SPRING_DUE, FALL_DUE = date(2026, 2, 2), date(2026, 9, 14)


def ln(line_id, due, prio, open_amount):
    return NS(line_id=line_id, due_date=due, allocation_priority=prio, open_amount=D(open_amount))


FALL = "INV-STU-000042-2026-000003"
SPRING = "INV-STU-000042-2026-000001"
OPEN = [
    ln(f"{FALL}-L03", FALL_DUE, 30, "5000.00"),  # registration
    ln(f"{FALL}-L01", FALL_DUE, 10, "74000.00"),  # tuition
    ln(f"{SPRING}-L07", SPRING_DUE, 90, "1500.00"),  # older late fee
    ln(f"{FALL}-L06", FALL_DUE, 60, "0.00"),  # fully waived: skipped
    ln(f"{FALL}-L02", FALL_DUE, 20, "12000.00"),  # lab
]


def plan(amount, lines=OPEN):
    return [(a.line_id, a.amount) for a in plan_allocation(D(amount), lines)]


def test_BR_ALC_03_oldest_due_first_then_priority_then_line_id():
    assert plan("100000.00") == [
        (f"{SPRING}-L07", D("1500.00")),
        (f"{FALL}-L01", D("74000.00")),
        (f"{FALL}-L02", D("12000.00")),
        (f"{FALL}-L03", D("5000.00")),
    ]


def test_BR_ALC_04_remainder_stays_unallocated():
    allocated = sum(a for _, a in plan("100000.00"))
    assert allocated == D("92500.00")  # 7,500 left as unallocated credit


def test_BR_ALC_01_never_allocates_more_than_payment():
    result = plan("50000.00")
    assert sum(a for _, a in result) == D("50000.00")
    assert result[-1] == (f"{FALL}-L01", D("48500.00"))  # partial on tuition


def test_BR_ALC_02_never_exceeds_line_open_amount():
    open_by_id = {x.line_id: x.open_amount for x in OPEN}
    for line_id, amount in plan("1000000.00"):
        assert amount <= open_by_id[line_id]


def test_zero_open_lines_are_skipped():
    assert f"{FALL}-L06" not in [lid for lid, _ in plan("1000000.00")]


def test_no_open_lines_allocates_nothing():
    assert plan("5000.00", [ln(f"{FALL}-L01", FALL_DUE, 10, "0.00")]) == []


@pytest.mark.parametrize("bad", ["0.00", "-1.00"])
def test_non_positive_amount_rejected(bad):
    with pytest.raises(ValueError):
        plan_allocation(D(bad), OPEN)


def test_negative_open_amount_rejected():
    with pytest.raises(ValueError):
        plan_allocation(D("100.00"), [ln(f"{FALL}-L01", FALL_DUE, 10, "-0.01")])
