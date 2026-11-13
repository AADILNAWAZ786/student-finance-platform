"""BR-INV-08: invoice lifecycle status (separate from collection status).

Contract under test (billing.lifecycle), ARCHITECTURE.md §5:
    lifecycle_transition_allowed(current, target) -> bool
        current is None for creation
    can_void(amounts) -> bool    # only when no scholarship and no payment is applied
"""

from decimal import Decimal as D

import pytest

from billing.calculators import InvoiceAmounts
from billing.lifecycle import can_void, lifecycle_transition_allowed


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (None, "ISSUED", True),
        ("ISSUED", "CANCELLED", True),
        ("ISSUED", "VOID", True),
        (None, "CANCELLED", False),
        (None, "VOID", False),
        ("CANCELLED", "ISSUED", False),
        ("CANCELLED", "VOID", False),
        ("VOID", "ISSUED", False),
        ("VOID", "CANCELLED", False),
        ("ISSUED", "ISSUED", False),
    ],
)
def test_BR_INV_08_lifecycle_transitions(current, target, allowed):
    assert lifecycle_transition_allowed(current, target) is allowed


def test_collection_statuses_are_not_lifecycle_states():
    for collection in (
        "UNPAID",
        "PARTIALLY_PAID",
        "OVERDUE",
        "PAID",
        "SCHOLARSHIP_COVERED",
        "ADJUSTED_TO_ZERO",
    ):
        assert lifecycle_transition_allowed("ISSUED", collection) is False


def _a(scholarship="0", paid="0"):
    return InvoiceAmounts(
        original_charges=D("183000.00"),
        adjustments=D("0"),
        scholarship_applied=D(scholarship),
        amount_paid=D(paid),
    )


def test_can_void_with_no_allocations():
    assert can_void(_a()) is True


def test_cannot_void_with_payment_applied():
    assert can_void(_a(paid="0.01")) is False


def test_cannot_void_with_scholarship_applied():
    assert can_void(_a(scholarship="86000.00")) is False
