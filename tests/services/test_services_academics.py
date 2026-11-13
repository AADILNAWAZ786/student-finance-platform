"""academics.services: program/term creation rules, enrollment rules, completion lifecycle."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError

from academics import services
from academics.lifecycle import ENROLLMENT_TRANSITIONS
from academics.models import Enrollment, EnrollmentEvent
from core.exceptions import BusinessRuleViolation, StateConflict

pytestmark = pytest.mark.django_db


def test_program_ids_sequential_and_rules(make_program):
    a, b = make_program(), make_program()
    assert (a.program_id, b.program_id) == ("PRG-001", "PRG-002")
    with pytest.raises(ValidationError):
        make_program(min_credits=20, max_credits=12)
    with pytest.raises(ValidationError) as exc:
        make_program(code=a.code)
    assert "code" in exc.value.message_dict


def test_term_id_derived_and_census_rule():
    start = date(2027, 1, 25)
    t = services.create_term(
        year=2027,
        season="SPRING",
        is_billable=False,
        invoice_date=start - timedelta(21),
        due_date=start + timedelta(7),
        start_date=start,
        census_date=start + timedelta(28),
        end_date=start + timedelta(103),
    )
    assert t.term_id == "TERM-2027-SPRING"
    with pytest.raises(ValidationError) as exc:
        services.create_term(
            year=2027,
            season="FALL",
            is_billable=False,
            invoice_date=date(2027, 8, 16),
            due_date=date(2027, 9, 13),
            start_date=date(2027, 9, 6),
            census_date=date(2027, 10, 5),
            end_date=date(2027, 12, 18),
        )
    assert "census_date" in exc.value.message_dict


def test_duplicate_term_rejected(terms):
    fall = terms["TERM-2026-FALL"]
    with pytest.raises(ValidationError) as exc:
        services.create_term(
            year=2026,
            season="FALL",
            is_billable=True,
            invoice_date=fall.invoice_date,
            due_date=fall.due_date,
            start_date=fall.start_date,
            census_date=fall.census_date,
            end_date=fall.end_date,
        )
    assert "already exists" in str(exc.value)


def _enroll(student, term, **kw):
    args = dict(credits=16, enrolled_on=date(2026, 8, 20), actor="t")
    args.update(kw)
    return services.enroll(student=student, term=term, **args)


def test_enroll_creates_enrollment_and_event(student, terms):
    e = _enroll(student, terms["TERM-2026-FALL"])
    assert e.enrollment_id == "ENR-2026-000001"
    assert (e.status, e.program, e.credits) == ("ENROLLED", student.program, 16)
    ev = e.events.get()
    assert (ev.event_id, ev.from_status, ev.to_status, ev.event_date) == (
        "EVT-ENR-2026-0000001",
        None,
        "ENROLLED",
        date(2026, 8, 20),
    )


def test_all_three_billable_terms(student, terms):
    ids = [
        _enroll(student, terms[t], enrolled_on=terms[t].start_date).enrollment_id
        for t in ("TERM-2026-SPRING", "TERM-2026-SUMMER", "TERM-2026-FALL")
    ]
    assert ids == ["ENR-2026-000001", "ENR-2026-000002", "ENR-2026-000003"]


def test_non_billable_term_422(student, terms):
    with pytest.raises(BusinessRuleViolation) as exc:
        _enroll(student, terms["TERM-2025-FALL"], enrolled_on=date(2025, 9, 1))
    assert exc.value.code == "TERM_NOT_BILLABLE" and exc.value.status_code == 422


@pytest.mark.parametrize(("credits", "ok"), [(11, False), (12, True), (20, True), (21, False)])
def test_credits_within_program_range(student, terms, credits, ok):
    if ok:
        assert _enroll(student, terms["TERM-2026-FALL"], credits=credits).credits == credits
    else:
        with pytest.raises(BusinessRuleViolation) as exc:
            _enroll(student, terms["TERM-2026-FALL"], credits=credits)
        assert exc.value.code == "CREDITS_OUT_OF_RANGE"
        assert exc.value.details["min_credits"] == 12


def test_duplicate_enrollment_409(student, terms):
    first = _enroll(student, terms["TERM-2026-FALL"])
    with pytest.raises(StateConflict) as exc:
        _enroll(student, terms["TERM-2026-FALL"])
    assert exc.value.code == "ENROLLMENT_EXISTS"
    assert exc.value.details["existing_enrollment_id"] == first.enrollment_id
    assert Enrollment.objects.count() == 1 and EnrollmentEvent.objects.count() == 1


@pytest.mark.parametrize(
    ("enrolled_on", "ok"),
    [
        (date(2026, 9, 14), True),  # start + 7 (last allowed day)
        (date(2026, 9, 15), False),  # start + 8
    ],
)
def test_add_drop_window(student, terms, enrolled_on, ok):
    if ok:
        _enroll(student, terms["TERM-2026-FALL"], enrolled_on=enrolled_on)
    else:
        with pytest.raises(ValidationError) as exc:
            _enroll(student, terms["TERM-2026-FALL"], enrolled_on=enrolled_on)
        assert "enrolled_on" in exc.value.message_dict


def test_cannot_enroll_before_admit_term(make_student, terms):
    s = make_student(admit_term=terms["TERM-2026-FALL"])
    with pytest.raises(ValidationError) as exc:
        _enroll(s, terms["TERM-2026-SPRING"], enrolled_on=date(2026, 1, 20))
    assert "term" in exc.value.message_dict


def test_inactive_program_cannot_enroll(student, terms):
    student.program.is_active = False
    student.program.save()
    with pytest.raises(ValidationError):
        _enroll(student, terms["TERM-2026-FALL"])


def test_complete_only_after_term_end(student, terms):
    e = _enroll(student, terms["TERM-2026-FALL"])
    with pytest.raises(ValidationError):
        services.complete_enrollment(e, on_date=date(2026, 12, 19), actor="t")
    e = services.complete_enrollment(e, on_date=date(2026, 12, 20), actor="t")
    assert e.status == "COMPLETED"
    assert list(e.events.order_by("event_id").values_list("from_status", "to_status")) == [
        (None, "ENROLLED"),
        ("ENROLLED", "COMPLETED"),
    ]
    with pytest.raises(StateConflict):  # COMPLETED is terminal
        services.complete_enrollment(e, on_date=date(2026, 12, 21), actor="t")


def test_transition_table_matches_design():
    assert {k: set(v) for k, v in ENROLLMENT_TRANSITIONS.items()} == {
        None: {"ENROLLED"},
        "ENROLLED": {"WITHDRAWN", "CANCELLED", "COMPLETED"},
        "WITHDRAWN": set(),
        "CANCELLED": set(),
        "COMPLETED": set(),
    }
