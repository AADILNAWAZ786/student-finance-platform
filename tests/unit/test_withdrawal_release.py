"""BR-WDR-03 / BR-SCH-07 / BR-ALC-02: release when an adjustment is posted on a line.

Scholarship is reversed first (proportional share, or more if it would still exceed
the reduced line); payments are deallocated after that, newest allocation first.

Contract under test (billing.calculators), ARCHITECTURE.md §5:
    release_for_adjustment(*, line_net, adjustment, scholarship_allocated,
                           payment_allocations) -> ReleasePlan
        payment_allocations: objects with allocation_id, allocated_on, amount (net remaining)
        ReleasePlan.scholarship_reversal: Decimal (>= 0, amount to reverse)
        ReleasePlan.deallocations: list[(allocation_id, Decimal)] in LIFO order
        raises ValueError if adjustment > 0 or |adjustment| > line_net
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from billing.calculators import release_for_adjustment


def alloc(aid, day, amount):
    return NS(allocation_id=aid, allocated_on=date(2026, 9, day), amount=D(amount))


A1 = alloc("ALLOC-PAY-2026-000101-01", 10, "60000.00")  # older (guardian)
A2 = alloc("ALLOC-PAY-2026-000102-01", 12, "14000.00")  # newer (sponsor)


def release(line_net, adjustment, scholarship, pays):
    p = release_for_adjustment(
        line_net=D(line_net),
        adjustment=D(adjustment),
        scholarship_allocated=D(scholarship),
        payment_allocations=pays,
    )
    return p.scholarship_reversal, [(a, amt) for a, amt in p.deallocations]


def test_scholarship_first_then_payments_lifo():
    # ARCHITECTURE §7, line L01: 160,000 tuition, 60% credit on day 10
    rev, deallocs = release("160000.00", "-96000.00", "86000.00", [A1, A2])
    assert rev == D("51600.00")
    assert deallocs == [(A2.allocation_id, D("14000.00")), (A1.allocation_id, D("30400.00"))]
    # line ends exactly covered: (86,000-51,600) + (74,000-44,400) = 64,000
    assert (D("86000.00") - rev) + (D("74000.00") - sum(a for _, a in deallocs)) == D("64000.00")


def test_payments_untouched_when_scholarship_reversal_removes_excess():
    rev, deallocs = release("160000.00", "-96000.00", "86000.00", [])
    assert rev == D("51600.00")
    assert deallocs == []  # student now owes 64,000 - 34,400 = 29,600


def test_full_ride_reversal_matches_adjustment_exactly():
    rev, deallocs = release("160000.00", "-96000.00", "160000.00", [])
    assert rev == D("96000.00")
    assert deallocs == []


def test_cancellation_releases_everything_on_the_line():
    pay = alloc("ALLOC-PAY-2026-000200-02", 1, "6000.00")
    rev, deallocs = release("12000.00", "-12000.00", "6000.00", [pay])
    assert rev == D("6000.00")
    assert deallocs == [(pay.allocation_id, D("6000.00"))]


def test_rounded_proportional_share_never_leaves_scholarship_above_reduced_line():
    # Deterministic sweep over small cent values: after step 1 the remaining
    # scholarship never exceeds the reduced line (S - R <= N'), with no payments.
    cents = [D(c) / 100 for c in (1, 2, 3, 7, 33, 99, 100, 101, 333, 1001, 3333)]
    for n in cents:
        for s in (x for x in cents if x <= n):
            for a in (x for x in cents if x <= n):
                rev, deallocs = release(str(n), str(-a), str(s), [])
                assert s - rev <= n - a
                assert rev >= 0 and deallocs == []


def test_proportional_share_uses_round_half_up():
    # S=0.05, N=0.10, A=-0.05: 0.05 * 0.05 / 0.10 = 0.025 -> 0.03 under HALF_UP
    rev, _ = release("0.10", "-0.05", "0.05", [])
    assert rev == D("0.03")


def test_payment_only_line_deallocates_newest_first():
    # 12,000 lab line fully paid by two allocations; 60% credit frees 7,200
    old = alloc("ALLOC-PAY-2026-000103-01", 5, "8000.00")
    new = alloc("ALLOC-PAY-2026-000104-01", 11, "4000.00")
    rev, deallocs = release("12000.00", "-7200.00", "0.00", [old, new])
    assert rev == D("0.00")
    assert deallocs == [(new.allocation_id, D("4000.00")), (old.allocation_id, D("3200.00"))]


def test_same_date_tiebreak_by_allocation_id_descending():
    x = alloc("ALLOC-PAY-2026-000300-01", 15, "1000.00")
    y = alloc("ALLOC-PAY-2026-000300-02", 15, "1000.00")
    _, deallocs = release("2000.00", "-1500.00", "0.00", [x, y])
    assert deallocs == [(y.allocation_id, D("1000.00")), (x.allocation_id, D("500.00"))]


def test_zero_adjustment_releases_nothing():
    rev, deallocs = release("160000.00", "0.00", "86000.00", [A1, A2])
    assert rev == D("0.00") and deallocs == []


@pytest.mark.parametrize("bad_adjustment", ["1.00", "-160000.01"])
def test_invalid_adjustment_rejected(bad_adjustment):
    with pytest.raises(ValueError):
        release_for_adjustment(
            line_net=D("160000.00"),
            adjustment=D(bad_adjustment),
            scholarship_allocated=D("0"),
            payment_allocations=[],
        )
