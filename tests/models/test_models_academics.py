"""Program, Term, Enrollment: fields, PKs, FKs and DB constraints (DATABASE_DESIGN §3).

Rows are inserted directly with the ORM here (bypassing services) to prove the database
itself rejects single-row violations.
"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError

from academics.models import Enrollment, EnrollmentEvent, Program, Term

pytestmark = pytest.mark.django_db


def _program(**kw):
    fields = dict(
        program_id="PRG-900",
        code="TST-1",
        name="Test",
        level="UG",
        department="X",
        is_stem=False,
        has_lab=False,
        min_credits=12,
        max_credits=20,
    )
    fields.update(kw)
    return Program(**fields)


def _term(**kw):
    start = date(2027, 1, 25)
    fields = dict(
        term_id="TERM-2027-SPRING",
        year=2027,
        season="SPRING",
        is_billable=False,
        invoice_date=start - timedelta(21),
        due_date=start + timedelta(7),
        start_date=start,
        census_date=start + timedelta(28),
        end_date=start + timedelta(103),
    )
    fields.update(kw)
    return Term(**fields)


def _rejects(obj):
    with pytest.raises(IntegrityError), transaction.atomic():
        obj.save(force_insert=True)


# --- Program -----------------------------------------------------------------------


def test_program_valid_row_saves():
    _program().save(force_insert=True)
    assert Program.objects.get(pk="PRG-900").is_active is True  # documented default


@pytest.mark.parametrize(
    "kw",
    [
        {"min_credits": 5},
        {"max_credits": 25},
        {"min_credits": 21, "max_credits": 20},
        {"level": "PHD"},
        {"code": ""},
        {"name": ""},
        {"department": ""},
    ],
)
def test_program_check_constraints(kw):
    _rejects(_program(**kw))


def test_program_code_unique():
    _program().save(force_insert=True)
    _rejects(_program(program_id="PRG-901"))


def test_program_id_format_validated_by_full_clean():
    with pytest.raises(ValidationError) as exc:
        _program(program_id="P-1").full_clean()
    assert "program_id" in exc.value.message_dict


# --- Term --------------------------------------------------------------------------


def test_term_valid_row_saves_with_default_grace():
    _term().save(force_insert=True)
    assert Term.objects.get(pk="TERM-2027-SPRING").late_fee_grace_days == 14


@pytest.mark.parametrize(
    "kw",
    [
        {"invoice_date": date(2027, 2, 2), "due_date": date(2027, 2, 1)},  # invoice > due
        {"census_date": date(2027, 1, 25)},  # census = start
        {"census_date": date(2027, 5, 8)},  # census = end
        {"season": "WINTER"},
    ],
)
def test_term_check_constraints(kw):
    _rejects(_term(**kw))


def test_term_year_season_unique(terms):
    _rejects(
        _term(
            term_id="TERM-2026-FALL",
            year=2026,
            season="FALL",
            start_date=date(2026, 9, 7),
            census_date=date(2026, 10, 5),
            end_date=date(2026, 12, 19),
            invoice_date=date(2026, 8, 17),
            due_date=date(2026, 9, 14),
        )
    )


# --- Enrollment --------------------------------------------------------------------


def _enrollment(student, term, **kw):
    fields = dict(
        enrollment_id="ENR-2026-900001",
        student=student,
        program=student.program,
        term=term,
        credits=16,
        enrolled_on=date(2026, 8, 20),
    )
    fields.update(kw)
    return Enrollment(**fields)


def test_enrollment_relationships(student, terms):
    e = _enrollment(student, terms["TERM-2026-FALL"])
    e.save(force_insert=True)
    assert e.status == "ENROLLED"  # default
    assert list(student.enrollments.all()) == [e]
    assert list(student.program.enrollments.all()) == [e]
    assert list(terms["TERM-2026-FALL"].enrollments.all()) == [e]


def test_enrollment_unique_student_term(student, terms):
    _enrollment(student, terms["TERM-2026-FALL"]).save(force_insert=True)
    _rejects(_enrollment(student, terms["TERM-2026-FALL"], enrollment_id="ENR-2026-900002"))


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "WITHDRAWN"},  # date missing
        {"withdrawal_date": date(2026, 9, 10)},  # date without status
        {"status": "CANCELLED"},
        {"cancellation_date": date(2026, 9, 1)},
        {
            "status": "WITHDRAWN",
            "withdrawal_date": date(2026, 9, 10),
            "cancellation_date": date(2026, 9, 1),
        },
        {"status": "PAUSED"},
        {"credits": 0},
    ],
)
def test_enrollment_check_constraints(student, terms, kw):
    _rejects(_enrollment(student, terms["TERM-2026-FALL"], **kw))


def test_enrollment_terminal_states_with_their_dates_are_valid(student, terms):
    _enrollment(
        student, terms["TERM-2026-FALL"], status="WITHDRAWN", withdrawal_date=date(2026, 9, 17)
    ).save(force_insert=True)
    _enrollment(
        student,
        terms["TERM-2026-SPRING"],
        enrollment_id="ENR-2026-900002",
        status="CANCELLED",
        cancellation_date=date(2026, 1, 10),
    ).save(force_insert=True)
    assert Enrollment.objects.count() == 2


def test_protect_prevents_deleting_referenced_rows(student, terms):
    e = _enrollment(student, terms["TERM-2026-FALL"])
    e.save(force_insert=True)
    EnrollmentEvent.objects.create(
        event_id="EVT-ENR-2026-9000001",
        enrollment=e,
        to_status="ENROLLED",
        event_date=date(2026, 8, 20),
        reason="ENROLLED",
        actor="test",
    )
    for obj in (student.program, terms["TERM-2026-FALL"], e):
        with pytest.raises(ProtectedError):
            obj.delete()
