"""Student and StudentStatusEvent: fields, FKs and DB constraints (DATABASE_DESIGN §3)."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from students.models import Student, StudentStatusEvent

pytestmark = pytest.mark.django_db


def _student(program, term, **kw):
    fields = dict(
        student_id="STU-900001",
        first_name="A",
        last_name="B",
        email="stu900001@example.edu",
        date_of_birth=date(2006, 1, 1),
        residency="DOMESTIC",
        program=program,
        admit_term=term,
        gpa=Decimal("3.00"),
        need_index=Decimal("0.50"),
    )
    fields.update(kw)
    return Student(**fields)


def _rejects(obj):
    with pytest.raises(IntegrityError), transaction.atomic():
        obj.save(force_insert=True)


def test_valid_row_and_relationships(program, terms):
    s = _student(program, terms["TERM-2025-FALL"])
    s.save(force_insert=True)
    s.refresh_from_db()
    assert s.status == "ACTIVE"  # documented default
    assert s.gpa == Decimal("3.00") and isinstance(s.gpa, Decimal)
    assert list(program.students.all()) == [s]
    assert list(terms["TERM-2025-FALL"].admitted_students.all()) == [s]


@pytest.mark.parametrize("value", ["0.00", "4.00", "2.67"])
def test_decimal_boundaries_round_trip_exactly(program, terms, value):
    s = _student(
        program, terms["TERM-2026-SPRING"], gpa=Decimal(value), need_index=Decimal(value) / 4
    )
    s.save(force_insert=True)
    s.refresh_from_db()
    assert s.gpa == Decimal(value)


@pytest.mark.parametrize(
    "kw",
    [
        {"gpa": Decimal("4.01")},
        {"gpa": Decimal("-0.01")},
        {"need_index": Decimal("1.01")},
        {"need_index": Decimal("-0.01")},
        {"residency": "RESIDENT"},
        {"status": "SUSPENDED"},
        {"first_name": ""},
        {"last_name": ""},
        {"email": ""},
    ],
)
def test_check_constraints(program, terms, kw):
    _rejects(_student(program, terms["TERM-2026-SPRING"], **kw))


def test_email_unique(program, terms):
    _student(program, terms["TERM-2026-SPRING"]).save(force_insert=True)
    _rejects(_student(program, terms["TERM-2026-SPRING"], student_id="STU-900002"))


def test_id_format_and_decimal_places_validated_by_full_clean(program, terms):
    with pytest.raises(ValidationError) as exc:
        _student(
            program, terms["TERM-2026-SPRING"], student_id="STU-1", gpa=Decimal("3.333")
        ).full_clean()
    assert {"student_id", "gpa"} <= set(exc.value.message_dict)


def test_protect_on_program_term_and_events(student):
    assert student.status_events.count() == 1
    for obj in (student.program, student.admit_term, student):
        with pytest.raises(ProtectedError):
            obj.delete()


def test_status_event_id_format_validated(student):
    ev = StudentStatusEvent(
        event_id="EVT-XYZ-2026-1",
        student=student,
        to_status="ACTIVE",
        event_date=date(2026, 1, 5),
        reason="CREATED",
        actor="t",
    )
    with pytest.raises(ValidationError):
        ev.full_clean()
