"""Shared Phase 3 fixtures. Everything goes through services or the seed command.

The default ``student`` (tests/conftest.py) is DOMESTIC, GPA 3.72, need 0.35, admitted in
TERM-2026-SPRING, in PRG-001 (a STEM lab program). On TERM-2026-FALL that student is
eligible for SCT-001 (merit 50%) and SCT-004 (STEM) and for nothing else.

The FALL invoice from tests/billing_fixtures.py (16 credits): TUITION 144,000, LAB 6,000,
REGISTRATION 5,000, TECHNOLOGY 4,000, LIBRARY 2,000, STUDENT_ACTIVITY 3,000 = 164,000.
Eligible fee base: 150,000 for standard types, 164,000 for the full-ride type.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command

from scholarships import services as scholarships
from scholarships.models import ScholarshipType

STANDARD_BASE = Decimal("150000.00")
FULL_RIDE_BASE = Decimal("164000.00")


@pytest.fixture
def scholarship_types(program, terms):
    call_command("seed_scholarship_types", verbosity=0)
    return {t.scholarship_type_id: t for t in ScholarshipType.objects.all()}


@pytest.fixture
def fall(terms):
    return terms["TERM-2026-FALL"]


@pytest.fixture
def evaluate(fall):
    def _evaluate(student, term=None):
        rows = scholarships.evaluate_eligibility(student=student, term=term or fall)
        return {e.scholarship_type_id: e for e in rows}

    return _evaluate


@pytest.fixture
def student_with_invoice(make_student, fee_setup, enroll_fall):
    """``student_with_invoice(gpa="3.90", need_index="0.90")`` → (student, FALL invoice)."""
    from billing import services as billing

    def _make(enrolled_on=date(2026, 8, 10), credits=16, **overrides):
        for k in ("gpa", "need_index"):
            if k in overrides:
                overrides[k] = Decimal(overrides[k])
        s = make_student(**overrides)
        inv = billing.generate_invoice(
            enrollment=enroll_fall(s, credits=credits, enrolled_on=enrolled_on), actor="test"
        )
        return s, inv

    return _make


@pytest.fixture
def merit_award(scholarship_types, invoice, evaluate):
    """SCT-001 (50% of 150,000 = 75,000) awarded to STU-000001 on the FALL invoice."""
    elig = evaluate(invoice.student)["SCT-001"]
    return scholarships.award_scholarship(eligibility=elig, invoice=invoice, actor="test")
