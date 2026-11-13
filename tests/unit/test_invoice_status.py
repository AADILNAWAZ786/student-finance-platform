"""BR-INV-04..06 and BR-PAY-03: invoice amounts and collection status.

Pure calculator tests (no database). Lifecycle status (ISSUED/CANCELLED/VOID) is
separate and tested in test_invoice_lifecycle.py.

Contract under test (billing.calculators), ARCHITECTURE.md §5:
    InvoiceAmounts(original_charges, adjustments, scholarship_applied, amount_paid,
                   payments_by_payer={})
        .net_charges  = original_charges + adjustments
        .outstanding  = net_charges - scholarship_applied - amount_paid
        .is_settled   = outstanding == 0
    invoice_amounts(lines, scholarship_allocations, payment_allocations) -> InvoiceAmounts
    collection_status(amounts, *, due_date, as_of) -> str
        raises ValueError when outstanding, scholarship_applied or amount_paid < 0
"""

from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from billing.calculators import InvoiceAmounts, collection_status, invoice_amounts

DUE = date(2026, 9, 14)  # TERM-2026-FALL due_date
BEFORE_DUE = DUE - timedelta(days=3)
AFTER_DUE = DUE + timedelta(days=1)


def amounts(original, adjustments="0", scholarship="0", paid="0"):
    return InvoiceAmounts(
        original_charges=D(original),
        adjustments=D(adjustments),
        scholarship_applied=D(scholarship),
        amount_paid=D(paid),
    )


def status(a, as_of=BEFORE_DUE):
    return collection_status(a, due_date=DUE, as_of=as_of)


# --- outstanding > 0 ---------------------------------------------------------


def test_unpaid_nothing_applied():
    assert status(amounts("200000.00")) == "UNPAID"


def test_partial_scholarship_without_payment_is_unpaid_not_paid():
    a = amounts("200000.00", scholarship="50000.00")
    assert status(a) == "UNPAID"
    assert a.amount_paid == D("0")
    assert a.outstanding == D("150000.00")


def test_partially_paid():
    assert status(amounts("200000.00", paid="80000.00")) == "PARTIALLY_PAID"


def test_on_due_date_is_not_overdue():
    assert status(amounts("200000.00"), as_of=DUE) == "UNPAID"


def test_day_after_due_date_unpaid_becomes_overdue():
    assert status(amounts("200000.00"), as_of=AFTER_DUE) == "OVERDUE"


def test_overdue_even_with_partial_payment():
    a = amounts("200000.00", paid="80000.00")
    assert status(a, as_of=AFTER_DUE) == "OVERDUE"
    assert a.amount_paid == D("80000.00")  # payment still visible


# --- outstanding = 0 ---------------------------------------------------------


def test_paid_by_payment_only():
    assert status(amounts("200000.00", paid="200000.00")) == "PAID"


def test_scholarship_plus_payment_is_paid():
    assert status(amounts("200000.00", scholarship="120000.00", paid="80000.00")) == "PAID"


def test_full_ride_with_no_payment_is_scholarship_covered():
    a = amounts("200000.00", scholarship="200000.00")
    assert status(a) == "SCHOLARSHIP_COVERED"
    assert a.is_settled and a.amount_paid == D("0")


def test_scholarship_covered_is_never_overdue():
    a = amounts("200000.00", scholarship="200000.00")
    assert status(a, as_of=AFTER_DUE) == "SCHOLARSHIP_COVERED"


def test_withdrawal_adjustment_then_scholarship_covers_rest():
    # 60% credit (-96,000) on 160,000 tuition; full-ride reversed by the same
    # amount still covers everything that remains
    a = amounts("200000.00", adjustments="-96000.00", scholarship="104000.00")
    assert a.net_charges == D("104000.00")
    assert status(a) == "SCHOLARSHIP_COVERED"


def test_withdrawal_adjustment_then_payment_covers_rest_is_paid():
    a = amounts("200000.00", adjustments="-96000.00", paid="104000.00")
    assert status(a) == "PAID"


def test_zero_from_adjustments_alone_is_adjusted_to_zero_not_scholarship_covered():
    a = amounts("50000.00", adjustments="-50000.00")
    assert a.is_settled
    assert status(a) == "ADJUSTED_TO_ZERO"


def test_void_invoice_amounts_give_adjusted_to_zero():
    # BR-INV-08: voiding posts -net adjustments for every line
    a = amounts("183000.00", adjustments="-183000.00")
    assert status(a, as_of=AFTER_DUE) == "ADJUSTED_TO_ZERO"


def test_adjustments_are_not_counted_as_payment_or_scholarship():
    a = amounts("200000.00", adjustments="-96000.00")
    assert a.amount_paid == D("0")
    assert a.scholarship_applied == D("0")
    assert a.outstanding == D("104000.00")
    assert status(a) == "UNPAID"


def test_cancelled_enrollment_registration_still_owed_stays_collectable():
    # everything adjusted away except a 5,000 registration fee: still owed
    a = amounts("200000.00", adjustments="-195000.00")
    assert status(a) == "UNPAID"
    assert status(a, as_of=AFTER_DUE) == "OVERDUE"


def test_cancelled_enrollment_registration_covered_by_full_ride():
    a = amounts("200000.00", adjustments="-195000.00", scholarship="5000.00")
    assert status(a) == "SCHOLARSHIP_COVERED"


# --- invalid amounts ---------------------------------------------------------


def test_negative_outstanding_raises():
    with pytest.raises(ValueError):
        status(amounts("100000.00", scholarship="60000.00", paid="50000.00"))


def test_negative_amount_paid_raises():
    with pytest.raises(ValueError):
        status(amounts("100000.00", paid="-1.00"))


def test_negative_scholarship_applied_raises():
    with pytest.raises(ValueError):
        status(amounts("100000.00", scholarship="-1.00"))


# --- aggregation from source rows (BR-INV-04, BR-PAY-03) ---------------------

LINES = [
    NS(line_type="FEE", net_amount=D("180000.00")),  # tuition
    NS(line_type="FEE", net_amount=D("5000.00")),  # registration
    NS(line_type="LATE_FEE", net_amount=D("1500.00")),
    NS(line_type="ADJUSTMENT", net_amount=D("-40000.00")),  # withdrawal credit
]


def test_aggregation_keeps_adjustments_separate():
    a = invoice_amounts(LINES, [], [])
    assert a.original_charges == D("186500.00")  # FEE + LATE_FEE
    assert a.adjustments == D("-40000.00")
    assert a.net_charges == D("146500.00")


def test_guardian_and_sponsor_payments_count_as_paid():
    allocs = [
        NS(amount=D("50000.00"), payer_type="SELF"),
        NS(amount=D("60000.00"), payer_type="GUARDIAN"),
        NS(amount=D("36500.00"), payer_type="SPONSOR"),
    ]
    a = invoice_amounts(LINES, [], allocs)
    assert a.amount_paid == D("146500.00")
    assert a.payments_by_payer == {
        "SELF": D("50000.00"),
        "GUARDIAN": D("60000.00"),
        "SPONSOR": D("36500.00"),
    }
    assert collection_status(a, due_date=DUE, as_of=BEFORE_DUE) == "PAID"


def test_sponsor_paid_invoice_is_paid_not_scholarship_covered():
    a = invoice_amounts(LINES, [], [NS(amount=D("146500.00"), payer_type="SPONSOR")])
    assert a.scholarship_applied == D("0")
    assert collection_status(a, due_date=DUE, as_of=BEFORE_DUE) == "PAID"


def test_deallocations_and_reversals_are_subtracted():
    sch = [NS(amount=D("100000.00")), NS(amount=D("-20000.00"))]  # APPLY, REVERSE
    pay = [
        NS(amount=D("30000.00"), payer_type="SELF"),
        NS(amount=D("-10000.00"), payer_type="SELF"),
    ]  # ALLOCATE, DEALLOCATE
    a = invoice_amounts(LINES, sch, pay)
    assert a.scholarship_applied == D("80000.00")
    assert a.amount_paid == D("20000.00")
    assert a.outstanding == D("46500.00")
