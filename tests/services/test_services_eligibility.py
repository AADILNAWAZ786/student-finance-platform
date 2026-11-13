"""scholarships.services: scholarship types (D-S2, D-S3) and eligibility (BR-SCH-01, D-S7)."""

from datetime import date
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError

from scholarships import services
from scholarships.models import Eligibility, ScholarshipType, ScholarshipTypeProgram

pytestmark = pytest.mark.django_db


def _type(**kw):
    fields = dict(
        name="Test Award",
        category="PROGRAM",
        residency="ALL",
        first_term_only=False,
        program_restricted=False,
        coverage_scope="ELIGIBLE_LINES_ONLY",
        award_basis="FIXED",
        award_value=D("1000.00"),
        max_amount_per_term=D("1000.00"),
        priority=9,
        effective_from=date(2026, 1, 1),
    )
    fields.update(kw)
    return services.create_scholarship_type(**fields)


# --- types --------------------------------------------------------------------------------------


def test_seeded_types_are_exactly_the_documented_seven(scholarship_types):
    t = scholarship_types
    assert list(t) == ["SCT-007", "SCT-001", "SCT-002", "SCT-003", "SCT-004", "SCT-005", "SCT-006"]
    assert (t["SCT-001"].min_gpa, t["SCT-001"].award_value, t["SCT-001"].max_amount_per_term) == (
        D("3.70"),
        D("50.00"),
        D("100000.00"),
    )
    assert (t["SCT-002"].min_gpa, t["SCT-002"].max_gpa) == (D("3.30"), D("3.70"))
    assert t["SCT-007"].coverage_scope == "ALL_FEE_LINES" and t["SCT-007"].priority == 0
    assert all(x.coverage_scope == "ELIGIBLE_LINES_ONLY" for k, x in t.items() if k != "SCT-007")
    assert {x.exclusive_group for x in t.values()} == {"MERIT", "FULL_RIDE", None}
    assert list(ScholarshipTypeProgram.objects.values_list("id", flat=True)) == ["SCT-004-PRG-001"]


@pytest.mark.parametrize(
    ("kw", "field", "message"),
    [
        ({"program_restricted": True}, "program_ids", "Required (a non-empty list)"),
        (
            {"program_restricted": True, "program_ids": []},
            "program_ids",
            "Required (a non-empty list)",
        ),
        ({"program_ids": ["PRG-001"]}, "program_ids", "Must be empty"),
        (
            {"program_restricted": True, "program_ids": ["PRG-001", "PRG-001"]},
            "program_ids",
            "Duplicate",
        ),
        (
            {"program_restricted": True, "program_ids": ["PRG-001", "PRG-404"]},
            "program_ids",
            "Unknown program IDs: PRG-404.",
        ),
        ({"min_gpa": D("3.70"), "max_gpa": D("3.70")}, "max_gpa", "greater than min_gpa"),
        ({"award_basis": "PERCENT", "award_value": D("100.01")}, "award_value", "at most 100"),
        (
            {"effective_from": date(2026, 6, 1), "effective_to": date(2026, 5, 31)},
            "effective_to",
            "on or after effective_from",
        ),
    ],
)
def test_D_S3_type_validation(program, kw, field, message):
    with pytest.raises(ValidationError) as exc:
        _type(**kw)
    assert message in exc.value.message_dict[field][0]
    assert ScholarshipType.objects.count() == 0


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"effective_from": date(2025, 12, 31)}, "effective_from"),
        ({"effective_from": date(2027, 1, 1)}, "effective_from"),
        ({"effective_to": date(2027, 1, 1)}, "effective_to"),
        (
            {"effective_from": date(2025, 1, 1), "effective_to": date(2025, 12, 31)},
            "effective_from",
        ),
    ],
)
def test_effective_dates_must_be_2026_business_dates(kw, field):
    with pytest.raises(ValidationError) as exc:
        _type(**kw)
    assert exc.value.message_dict[field] == [
        "Must be a business date between 2026-01-01 and 2026-12-31."
    ]
    assert ScholarshipType.objects.count() == 0


def test_both_effective_dates_outside_2026_are_both_reported():
    with pytest.raises(ValidationError) as exc:
        _type(effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31))
    assert set(exc.value.message_dict) == {"effective_from", "effective_to"}


def test_effective_window_on_the_2026_boundaries_is_accepted():
    t = _type(effective_from=date(2026, 1, 1), effective_to=date(2026, 12, 31))
    assert (t.effective_from, t.effective_to) == (date(2026, 1, 1), date(2026, 12, 31))


def test_max_gpa_zero_is_accepted_consistently():
    """max_gpa is within the approved 0–4 bounds (inclusive); the DB agrees (no 500)."""
    t = _type(max_gpa=D("0.00"))
    assert ScholarshipType.objects.get(pk=t.pk).max_gpa == D("0.00")
    with pytest.raises(ValidationError) as exc:  # min_gpa 0 with max_gpa 0: min < max fails
        _type(min_gpa=D("0.00"), max_gpa=D("0.00"))
    assert exc.value.message_dict["max_gpa"] == ["max_gpa must be greater than min_gpa."]


def test_D_S3_program_list_is_stored(program, make_program):
    other = make_program(code="BSC-X")
    t = _type(program_restricted=True, program_ids=[other.pk, program.pk])
    assert sorted(t.type_programs.values_list("program_id", flat=True)) == ["PRG-001", "PRG-002"]


@pytest.mark.parametrize(
    ("kw", "fields"),
    [
        ({"category": "MERIT", "exclusive_group": "FULL_RIDE", "priority": 0}, {"category"}),
        ({"category": "FULL_RIDE", "exclusive_group": "MERIT", "priority": 0}, {"exclusive_group"}),
        ({"category": "FULL_RIDE", "exclusive_group": "FULL_RIDE", "priority": 1}, {"priority"}),
    ],
)
def test_D_S2_all_fee_lines_shape(kw, fields):
    with pytest.raises(ValidationError) as exc:
        _type(coverage_scope="ALL_FEE_LINES", **kw)
    assert set(exc.value.message_dict) == fields


def test_D_S2_only_one_all_fee_lines_type(scholarship_types):
    with pytest.raises(ValidationError) as exc:
        _type(
            coverage_scope="ALL_FEE_LINES",
            category="FULL_RIDE",
            exclusive_group="FULL_RIDE",
            priority=0,
        )
    assert exc.value.message_dict["coverage_scope"] == [
        "Only one ALL_FEE_LINES type may exist; it is SCT-007."
    ]


def test_only_the_all_fee_lines_race_is_mapped_to_a_400(scholarship_types, monkeypatch):
    """The IntegrityError handler must not relabel other database errors."""
    from django.db import IntegrityError

    def fail(self, *a, **k):
        raise IntegrityError("CHECK constraint failed: scholtype_award_value_gt_0")

    monkeypatch.setattr(ScholarshipType, "save", fail)
    with pytest.raises(IntegrityError, match="award_value"):
        _type()  # a defect surfaces as itself, not as "Only one ALL_FEE_LINES type"

    def race(self, *a, **k):  # a concurrent second full-ride type slipped past the check
        raise IntegrityError(
            "UNIQUE constraint failed: scholarships_scholarshiptype.coverage_scope"
        )

    monkeypatch.setattr(ScholarshipType, "save", race)
    monkeypatch.setattr(
        ScholarshipType.objects, "filter", lambda **kw: ScholarshipType.objects.none()
    )
    with pytest.raises(ValidationError) as exc:
        _type(
            coverage_scope="ALL_FEE_LINES",
            category="FULL_RIDE",
            exclusive_group="FULL_RIDE",
            priority=0,
        )
    assert exc.value.message_dict == {"coverage_scope": ["Only one ALL_FEE_LINES type may exist."]}


def test_D_S2_full_ride_is_recognised_by_scope_not_by_id():
    t = _type(
        coverage_scope="ALL_FEE_LINES",
        category="FULL_RIDE",
        exclusive_group="FULL_RIDE",
        priority=0,
    )
    assert t.scholarship_type_id == "SCT-001" and services.is_full_ride(t)


# --- eligibility (BR-SCH-01, D-S7) ----------------------------------------------------------------


def test_every_type_is_evaluated_and_ineligible_outcomes_are_stored(
    scholarship_types, invoice, evaluate
):
    rows = evaluate(invoice.student)
    assert {k: (e.is_eligible, e.reason_codes) for k, e in rows.items()} == {
        "SCT-007": (False, ["GPA_BELOW_MIN", "NEED_BELOW_MIN"]),
        "SCT-001": (True, ["ALL_CRITERIA_MET"]),
        "SCT-002": (False, ["GPA_AT_OR_ABOVE_MAX"]),
        "SCT-003": (False, ["NEED_BELOW_MIN"]),
        "SCT-004": (True, ["ALL_CRITERIA_MET"]),
        "SCT-005": (False, ["RESIDENCY_MISMATCH"]),
        "SCT-006": (False, ["NEED_BELOW_MIN", "NOT_FIRST_TERM"]),
    }
    assert Eligibility.objects.count() == 7
    assert [e.eligibility_id for e in rows.values()] == [f"ELG-2026-{n:06d}" for n in range(1, 8)]


def test_D_S7_evaluated_on_is_always_the_term_invoice_date(
    scholarship_types, invoice, evaluate, fall
):
    rows = evaluate(invoice.student)
    assert {e.evaluated_on for e in rows.values()} == {fall.invoice_date} == {date(2026, 8, 17)}


def test_eligible_fee_base_by_scope_when_an_invoice_exists(scholarship_types, invoice, evaluate):
    rows = evaluate(invoice.student)
    assert rows["SCT-001"].eligible_fee_base == D("150000.00")  # TUITION + LAB
    assert rows["SCT-007"].eligible_fee_base == D("164000.00")  # all FEE lines


def test_without_an_invoice_the_base_is_empty(scholarship_types, student, evaluate):
    assert {e.eligible_fee_base for e in evaluate(student).values()} == {None}


def test_snapshot_and_re_evaluation_appends(scholarship_types, student, evaluate):
    first = evaluate(student)["SCT-001"]
    second = evaluate(student)["SCT-001"]
    assert first.pk != second.pk and Eligibility.objects.count() == 14
    assert second.inputs_snapshot == {
        "gpa": "3.72",
        "need_index": "0.35",
        "residency": "DOMESTIC",
        "program": "PRG-001",
        "admit_term": "TERM-2026-SPRING",
        "term": "TERM-2026-FALL",
        "first_term": False,
    }
    first.refresh_from_db()
    assert first.reason_codes == ["ALL_CRITERIA_MET"]  # earlier rows are never rewritten


def test_full_ride_first_term_international_profiles(
    scholarship_types, make_student, evaluate, terms
):
    s = make_student(
        gpa=D("3.90"),
        need_index=D("0.90"),
        residency="INTERNATIONAL",
        admit_term=terms["TERM-2026-FALL"],
    )
    rows = evaluate(s)
    eligible = sorted(k for k, e in rows.items() if e.is_eligible)
    assert eligible == ["SCT-001", "SCT-003", "SCT-004", "SCT-005", "SCT-006", "SCT-007"]


def test_non_billable_term_is_422(scholarship_types, student, terms):
    from core.exceptions import BusinessRuleViolation

    with pytest.raises(BusinessRuleViolation) as exc:
        services.evaluate_eligibility(student=student, term=terms["TERM-2025-FALL"])
    assert exc.value.code == "TERM_NOT_BILLABLE"
    assert Eligibility.objects.count() == 0
