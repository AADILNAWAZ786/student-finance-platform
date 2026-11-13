"""Shared billing fixtures (Phase 2). Import the ones a test module needs, e.g.

    from tests.billing_fixtures import fee_setup, fall_enrollment, invoice  # noqa: F401

Everything is created through services, as the API does. Prices follow the documented
pricing rule (DATABASE_DESIGN §3): tuition per credit by residency (international ×1.6),
flat fees otherwise. FALL invoice date 2026-08-17, due 2026-09-14, grace 14 days.
"""

from datetime import date
from decimal import Decimal

import pytest

from billing import services as billing
from billing.models import FeeSchedule

V1_FROM = date(2026, 1, 1)

# (fee_type, residency, charge_basis, amount)
FALL_PRICES = [
    ("TUITION", "DOMESTIC", "PER_CREDIT", "9000.00"),
    ("TUITION", "INTERNATIONAL", "PER_CREDIT", "14400.00"),
    ("LAB", "ALL", "FLAT", "6000.00"),
    ("REGISTRATION", "ALL", "FLAT", "5000.00"),
    ("TECHNOLOGY", "ALL", "FLAT", "4000.00"),
    ("LIBRARY", "ALL", "FLAT", "2000.00"),
    ("STUDENT_ACTIVITY", "ALL", "FLAT", "3000.00"),
    ("INTERNATIONAL_SERVICE", "INTERNATIONAL", "FLAT", "8000.00"),
]
# a domestic 16-credit student in a lab program: 144,000 + 6,000 + 5,000 + 4,000 + 2,000 + 3,000
DOMESTIC_16_TOTAL = Decimal("164000.00")


def make_fees(program, term, prices=FALL_PRICES, effective_from=V1_FROM):
    return {
        (fee_type, residency): billing.create_fee_schedule(
            program=program,
            term=term,
            fee_type=fee_type,
            residency=residency,
            charge_basis=basis,
            amount=Decimal(amount),
            effective_from=effective_from,
        )
        for fee_type, residency, basis, amount in prices
    }


@pytest.fixture
def fee_setup(program, terms):
    """V1 FALL fee schedules for PRG-001, keyed by (fee_type, residency)."""
    return make_fees(program, terms["TERM-2026-FALL"])


@pytest.fixture
def enroll_fall(terms):
    from academics import services

    def _enroll(student, *, credits=16, enrolled_on=date(2026, 8, 10)):
        return services.enroll(
            student=student,
            term=terms["TERM-2026-FALL"],
            credits=credits,
            enrolled_on=enrolled_on,
            actor="test",
        )

    return _enroll


@pytest.fixture
def fall_enrollment(student, enroll_fall):
    return enroll_fall(student)


@pytest.fixture
def invoice(fee_setup, fall_enrollment):
    return billing.generate_invoice(enrollment=fall_enrollment, actor="test")


@pytest.fixture
def finance_api(api_client_for):
    return api_client_for("platform_admin")


def schedule_count():
    return FeeSchedule.objects.count()
