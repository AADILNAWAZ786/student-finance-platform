"""Student services: the only code that creates students or changes their status.

Rules enforced here (DATABASE_DESIGN.md §3, students):
  * student_id allocated from the STU sequence; immutable
  * email unique, on an allowed synthetic domain (settings.STUDENT_EMAIL_DOMAINS)
  * age 16–60 on the admit term's start date
  * program must be active
  * status changes follow STUDENT_TRANSITIONS and always write a StudentStatusEvent
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from academics.models import Program, Term
from core.ids import next_id
from core.lifecycle import check_transition
from students.calculators import age_on
from students.lifecycle import ACTIVE, STUDENT_TRANSITIONS
from students.models import Student, StudentStatusEvent

MIN_AGE, MAX_AGE = 16, 60
CONTACT_FIELDS = ("first_name", "last_name", "email")


def _normalise_email(email: str) -> str:
    local, _, domain = email.strip().rpartition("@")
    return f"{local}@{domain.lower()}"


def _email_errors(email: str, *, exclude_pk: str | None = None) -> list[str]:
    errors = []
    domain = email.rpartition("@")[2]
    if domain not in settings.STUDENT_EMAIL_DOMAINS:
        errors.append(
            f"Email must use one of the synthetic domains "
            f"{', '.join(settings.STUDENT_EMAIL_DOMAINS)}."
        )
    clash = Student.objects.filter(email__iexact=email)
    if exclude_pk:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        errors.append("A student with this email already exists.")
    return errors


def _status_event(
    student: Student,
    *,
    from_status,
    to_status,
    event_date: date,
    reason: str,
    actor: str,
    source_ref: str | None = None,
) -> StudentStatusEvent:
    return StudentStatusEvent.objects.create(
        event_id=next_id("status_event", entity="STU", year=event_date.year),
        student=student,
        from_status=from_status,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
        source_ref=source_ref,
    )


@transaction.atomic
def create_student(
    *,
    first_name: str,
    last_name: str,
    email: str,
    date_of_birth: date,
    residency: str,
    program: Program,
    admit_term: Term,
    gpa: Decimal,
    need_index: Decimal,
    event_date: date,
    actor: str,
) -> Student:
    email = _normalise_email(email)
    errors: dict[str, list[str]] = {}
    if email_errors := _email_errors(email):
        errors["email"] = email_errors
    age = age_on(date_of_birth, admit_term.start_date)
    if not MIN_AGE <= age <= MAX_AGE:
        errors["date_of_birth"] = [
            f"Student must be {MIN_AGE}–{MAX_AGE} years old on the admit term start "
            f"({admit_term.start_date}); this date of birth gives {age}."
        ]
    if not program.is_active:
        errors["program"] = [f"Program {program.program_id} is not active."]
    if errors:
        raise ValidationError(errors)

    student = Student(
        student_id=next_id("student"),
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=email,
        date_of_birth=date_of_birth,
        residency=residency,
        program=program,
        admit_term=admit_term,
        gpa=gpa,
        need_index=need_index,
        status=ACTIVE,
    )
    student.full_clean(validate_unique=False, validate_constraints=False)
    student.save(force_insert=True)
    check_transition(
        STUDENT_TRANSITIONS, None, ACTIVE, entity="Student", record_id=student.student_id
    )
    _status_event(
        student,
        from_status=None,
        to_status=ACTIVE,
        event_date=event_date,
        reason="CREATED",
        actor=actor,
    )
    return student


@transaction.atomic
def update_contact(student: Student, **changes: str) -> Student:
    """PATCH: contact fields only (first_name, last_name, email)."""
    illegal = sorted(set(changes) - set(CONTACT_FIELDS))
    if illegal:
        raise ValidationError({f: ["Only contact fields can be changed."] for f in illegal})
    student = Student.objects.select_for_update().get(pk=student.pk)
    if "email" in changes:
        changes["email"] = _normalise_email(changes["email"])
        if email_errors := _email_errors(changes["email"], exclude_pk=student.pk):
            raise ValidationError({"email": email_errors})
    for field, value in changes.items():
        setattr(student, field, value.strip())
    student.full_clean(validate_unique=False, validate_constraints=False)
    student.save(update_fields=list(changes))
    return student


@transaction.atomic
def change_status(
    student: Student, *, to_status: str, event_date: date, reason: str, actor: str
) -> tuple[Student, StudentStatusEvent]:
    student = Student.objects.select_for_update().get(pk=student.pk)
    check_transition(
        STUDENT_TRANSITIONS,
        student.status,
        to_status,
        entity="Student",
        record_id=student.student_id,
    )
    last = student.status_events.order_by("-event_date", "-event_id").first()
    if last and event_date < last.event_date:
        raise ValidationError(
            {
                "event_date": [
                    f"event_date must be on or after the previous status event ({last.event_date})."
                ]
            }
        )
    previous = student.status
    student.status = to_status
    student.save(update_fields=["status"])
    event = _status_event(
        student,
        from_status=previous,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
    )
    return student, event
