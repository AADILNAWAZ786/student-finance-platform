"""Shared Phase 4 fixtures. Everything goes through services, as the API does.

The FALL invoice from tests/billing_fixtures.py (STU-000001, 16 credits, issued 2026-08-17,
due 2026-09-14): L01 TUITION 144,000, L02 LAB 6,000, L03 REGISTRATION 5,000,
L04 TECHNOLOGY 4,000, L05 LIBRARY 2,000, L06 STUDENT_ACTIVITY 3,000 = 164,000.
"""

import itertools
from datetime import date
from decimal import Decimal

import pytest

from billing.models import InvoiceLine
from payments import services as payments

ISSUE = date(2026, 8, 17)
_refs = itertools.count(1)


@pytest.fixture
def record(invoice):
    """``record("60000.00", payer="GUARDIAN", auto=True)`` → Payment (result on ``.result``)."""

    def _record(
        amount,
        *,
        student=None,
        on=ISSUE,
        payer="SELF",
        method="UPI",
        auto=False,
        ref=None,
        actor="test",
    ):
        payment, result = payments.record_payment(
            student=student or invoice.student,
            amount=Decimal(amount),
            received_date=on,
            method=method,
            payer_type=payer,
            external_reference=ref or f"UTR-{next(_refs):06d}",
            auto_allocate=auto,
            actor=actor,
        )
        payment.result = result
        return payment

    return _record


def line(invoice, n: int) -> InvoiceLine:
    return InvoiceLine.objects.select_related("invoice").get(pk=f"{invoice.pk}-L{n:02d}")


@pytest.fixture
def manual(invoice):
    """``manual(payment, (1, "100.00"), (2, "50.00"), on=date(...))`` → AllocationResult."""

    def _manual(payment, *pairs, on=ISSUE, inv=None):
        target = inv or invoice
        return payments.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(line(target, n), Decimal(a)) for n, a in pairs],
            allocated_on=on,
            actor="test",
        )

    return _manual


@pytest.fixture
def request_refund():
    def _request(payment, amount, *, on=date(2026, 8, 21)):
        return payments.request_refund(
            payment=payment, amount=Decimal(amount), requested_date=on, actor="test"
        )

    return _request
