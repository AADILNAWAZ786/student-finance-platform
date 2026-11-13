"""Scholarship models and the extended LedgerEntry: DB constraints (DATABASE_DESIGN §3).

Rows are inserted directly with the ORM to prove the database itself rejects single-row
violations. The cross-row money rules are deliberately not DB constraints (the
SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES anomaly must be representable): see the last test.
"""

from datetime import date
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from billing.models import LedgerEntry
from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
    ScholarshipTypeProgram,
)

pytestmark = pytest.mark.django_db


def _rejects(obj):
    with pytest.raises(IntegrityError), transaction.atomic():
        obj.save(force_insert=True)
        connection.check_constraints()


def _type(**kw):
    fields = dict(
        scholarship_type_id="SCT-900",
        name="T",
        category="MERIT",
        residency="ALL",
        first_term_only=False,
        program_restricted=False,
        coverage_scope="ELIGIBLE_LINES_ONLY",
        award_basis="PERCENT",
        award_value=D("50"),
        max_amount_per_term=D("1000.00"),
        priority=5,
        effective_from=date(2026, 1, 1),
    )
    fields.update(kw)
    return ScholarshipType(**fields)


def test_type_valid_row_saves():
    _type().save(force_insert=True)


@pytest.mark.parametrize(
    "kw",
    [
        {"category": "SPORTS"},
        {"residency": "LOCAL"},
        {"coverage_scope": "SOME"},
        {"award_basis": "TIERED"},
        {"award_value": D("0")},
        {"award_value": D("100.01")},  # PERCENT ≤ 100
        {"max_amount_per_term": D("0")},
        {"min_gpa": D("4.01")},
        {"max_gpa": D("-0.01")},
        {"max_gpa": D("4.01")},
        {"min_gpa": D("3.70"), "max_gpa": D("3.70")},
        {"min_need_index": D("1.01")},
        {"effective_from": date(2026, 6, 1), "effective_to": date(2026, 5, 31)},
        {"name": ""},
        {"coverage_scope": "ALL_FEE_LINES"},  # D-S2: needs FULL_RIDE category, group, priority 0
        {
            "coverage_scope": "ALL_FEE_LINES",
            "category": "FULL_RIDE",
            "exclusive_group": "FULL_RIDE",
        },
        {"coverage_scope": "ALL_FEE_LINES", "category": "FULL_RIDE", "priority": 0},
    ],
)
def test_type_check_constraints(kw):
    _rejects(_type(**kw))


@pytest.mark.parametrize("max_gpa", [D("0.00"), D("4.00")])
def test_max_gpa_bounds_are_inclusive_0_to_4(max_gpa):
    """Matches the serializer's 0–4 bounds, so a valid request can never cause a DB error."""
    _type(max_gpa=max_gpa).save(force_insert=True)
    assert ScholarshipType.objects.get(pk="SCT-900").max_gpa == max_gpa


def test_fixed_award_above_100_is_fine():
    _type(award_basis="FIXED", award_value=D("40000.00")).save(force_insert=True)


def test_D_S2_single_all_fee_lines_type():
    full = dict(
        coverage_scope="ALL_FEE_LINES",
        category="FULL_RIDE",
        exclusive_group="FULL_RIDE",
        priority=0,
    )
    _type(**full).save(force_insert=True)
    _rejects(_type(scholarship_type_id="SCT-901", **full))


def test_type_program_bridge_unique_and_protected(program):
    t = _type(program_restricted=True)
    t.save(force_insert=True)
    ScholarshipTypeProgram.objects.create(id="SCT-900-PRG-001", scholarship_type=t, program=program)
    _rejects(ScholarshipTypeProgram(id="SCT-900-PRG-002", scholarship_type=t, program=program))
    with pytest.raises(ProtectedError):
        program.delete()


@pytest.fixture
def merit_rows(merit_award, invoice):
    return merit_award, invoice.lines.get(fee_type="TUITION")


def _elig(student, stype, term, **kw):
    fields = dict(
        eligibility_id="ELG-2026-000900",
        student=student,
        scholarship_type=stype,
        term=term,
        evaluated_on=date(2026, 8, 17),
        is_eligible=True,
        reason_codes=["ALL_CRITERIA_MET"],
        inputs_snapshot={},
    )
    fields.update(kw)
    return Eligibility(**fields)


def test_eligibility_has_no_unique_and_base_is_non_negative(scholarship_types, student, fall):
    t = scholarship_types["SCT-001"]
    _elig(student, t, fall).save(force_insert=True)
    _elig(student, t, fall, eligibility_id="ELG-2026-000901").save(force_insert=True)  # appends
    _rejects(
        _elig(student, t, fall, eligibility_id="ELG-2026-000902", eligible_fee_base=D("-0.01"))
    )


@pytest.mark.parametrize("kw", [{"awarded_amount": D("0")}, {"status": "PAID"}])
def test_scholarship_check_constraints(merit_award, kw):
    s = Scholarship.objects.get(pk=merit_award.pk)
    s.pk, s.eligibility_id = "SCH-2026-000900", None
    for k, v in kw.items():
        setattr(s, k, v)
    from scholarships.services import evaluate_eligibility

    s.eligibility = evaluate_eligibility(student=s.student, term=s.term)[1]
    _rejects(s)


def test_scholarship_eligibility_is_unique(merit_award):
    s = Scholarship.objects.get(pk=merit_award.pk)
    s.pk = "SCH-2026-000901"
    _rejects(s)


def _alloc(award, line, **kw):
    fields = dict(
        allocation_id=f"SCHA-{award.pk}-90",
        scholarship=award,
        invoice_line=line,
        allocation_type="APPLY",
        amount=D("10.00"),
        reason="AWARD",
        allocated_on=date(2026, 8, 17),
    )
    fields.update(kw)
    return ScholarshipAllocation(**fields)


def test_allocation_valid_apply_and_reverse(merit_rows):
    award, line = merit_rows
    a = _alloc(award, line)
    a.save(force_insert=True)
    _alloc(
        award,
        line,
        allocation_id=f"SCHA-{award.pk}-91",
        allocation_type="REVERSE",
        amount=D("-5"),
        reverses=a,
        reason="WITHDRAWAL",
    ).save(force_insert=True)


@pytest.mark.parametrize(
    "kw",
    [
        {"amount": D("0")},
        {"amount": D("-1")},  # APPLY > 0
        {"allocation_type": "REVERSE", "amount": D("-1"), "reason": "WITHDRAWAL"},  # needs reverses
        {"reason": "WITHDRAWAL"},  # APPLY ↔ AWARD
        {"allocation_type": "MOVE"},
    ],
)
def test_allocation_constraints(merit_rows, kw):
    award, line = merit_rows
    _rejects(_alloc(award, line, **kw))


def test_reverse_needs_negative_amount_and_reversal_reason(merit_rows):
    award, line = merit_rows
    a = _alloc(award, line)
    a.save(force_insert=True)
    reverse = {
        "allocation_type": "REVERSE",
        "reverses": a,
        "amount": D("-1"),
        "reason": "WITHDRAWAL",
    }
    for bad in ({"amount": D("1")}, {"reason": "AWARD"}):
        _rejects(_alloc(award, line, allocation_id=f"SCHA-{award.pk}-92", **{**reverse, **bad}))


def test_apply_with_reverses_is_rejected(merit_rows):
    award, line = merit_rows
    a = _alloc(award, line)
    a.save(force_insert=True)
    _rejects(_alloc(award, line, allocation_id=f"SCHA-{award.pk}-93", reverses=a))


def test_scholarship_event_and_protection(merit_award):
    assert ScholarshipEvent.objects.filter(scholarship=merit_award).count() == 1
    with pytest.raises(ProtectedError):
        merit_award.delete()


# --- LedgerEntry with scholarship sources ---------------------------------------------------------


@pytest.fixture
def applied(merit_award):
    from scholarships import services

    services.apply_scholarship(merit_award, actor="t")
    return merit_award.allocations.get()


def _entry(**kw):
    fields = dict(
        entry_id="LED-2026-0099990", student_id="STU-000001", entry_date=date(2026, 8, 17)
    )
    fields.update(kw)
    return LedgerEntry(**fields)


def test_ledger_scholarship_entry_rules(applied, merit_rows):
    _, line = merit_rows
    # a second entry for the same allocation: unique source
    _rejects(
        _entry(entry_type="SCHOLARSHIP_CREDIT", amount=D("-1"), scholarship_allocation=applied)
    )
    rev = _alloc(
        applied.scholarship,
        line,
        allocation_id=f"SCHA-{applied.scholarship_id}-94",
        allocation_type="REVERSE",
        amount=D("-1"),
        reverses=applied,
        reason="WITHDRAWAL",
    )
    rev.save(force_insert=True)
    for kw in (
        {"entry_type": "SCHOLARSHIP_REVERSAL", "amount": D("-1")},  # reversal is a debit
        {"entry_type": "SCHOLARSHIP_CREDIT", "amount": D("1")},  # credit is negative
        {"entry_type": "CHARGE", "amount": D("1")},  # a line type needs an invoice_line source
        {
            "entry_type": "SCHOLARSHIP_REVERSAL",
            "amount": D("1"),
            "invoice_line": line,
        },  # two sources
    ):
        _rejects(_entry(scholarship_allocation=rev, **kw))
    _entry(entry_type="SCHOLARSHIP_REVERSAL", amount=D("1"), scholarship_allocation=rev).save(
        force_insert=True
    )


def test_over_cap_award_is_representable_for_reconciliation(scholarship_types, invoice, evaluate):
    """No DB constraint enforces BR-SCH-04 or the per-term cap (they are SV + REC)."""
    elig = evaluate(invoice.student)["SCT-001"]
    Scholarship.objects.create(
        scholarship_id="SCH-2026-000999",
        student=invoice.student,
        scholarship_type=elig.scholarship_type,
        term=invoice.term,
        eligibility=elig,
        invoice=invoice,
        awarded_amount=D("999999.00"),
        award_date=date(2026, 8, 17),
        status="OFFERED",
    )
