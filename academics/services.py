"""Academics services: the only code that creates Programs, Terms and Enrollments or changes
enrollment status. Every write is one atomic transaction (ARCHITECTURE §8.4).

Rules enforced here (DATABASE_DESIGN.md §3 SV rules):
  Program     credits 6 ≤ min ≤ max ≤ 24 (also DB CHECK)
  Term        term_id = TERM-{year}-{season}; invoice ≤ due; start < census < end;
              census = start + 28 days; (year, season) unique
  Enrollment  billable term (422 TERM_NOT_BILLABLE); program = student's program, active;
              credits within the program range (422 CREDITS_OUT_OF_RANGE);
              one per student and term (409 ENROLLMENT_EXISTS);
              enrolled_on ≤ term start + 7; student's admit term ≤ enrolled term
"""

from __future__ import annotations

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from academics.calendar import CENSUS_OFFSET_DAYS
from academics.lifecycle import COMPLETED, ENROLLED, ENROLLMENT_TRANSITIONS
from academics.models import Enrollment, EnrollmentEvent, Program, Term
from core.exceptions import domain_error
from core.ids import next_id, term_id
from core.lifecycle import check_transition

ADD_DROP_DAYS = 7  # enrolled_on ≤ start + 7


@transaction.atomic
def create_program(
    *, code, name, level, department, is_stem, has_lab, min_credits, max_credits, is_active=True
) -> Program:
    errors = {}
    if not 6 <= min_credits <= max_credits <= 24:
        errors["max_credits"] = ["Credits must satisfy 6 ≤ min_credits ≤ max_credits ≤ 24."]
    if Program.objects.filter(code=code).exists():
        errors["code"] = [f"Program code {code!r} already exists."]
    if errors:
        raise ValidationError(errors)
    program = Program(
        program_id=next_id("program"),
        code=code,
        name=name,
        level=level,
        department=department,
        is_stem=is_stem,
        has_lab=has_lab,
        min_credits=min_credits,
        max_credits=max_credits,
        is_active=is_active,
    )
    program.full_clean(validate_unique=False, validate_constraints=False)
    program.save(force_insert=True)
    return program


@transaction.atomic
def create_term(
    *,
    year,
    season,
    is_billable,
    invoice_date,
    due_date,
    start_date,
    census_date,
    end_date,
    late_fee_grace_days=14,
) -> Term:
    errors: dict[str, list[str]] = {}
    if invoice_date > due_date:
        errors.setdefault("due_date", []).append("due_date must be on or after invoice_date.")
    if not start_date < census_date < end_date:
        errors.setdefault("census_date", []).append(
            "Dates must satisfy start_date < census_date < end_date."
        )
    if census_date != start_date + timedelta(days=CENSUS_OFFSET_DAYS):
        errors.setdefault("census_date", []).append(
            f"census_date must be start_date + {CENSUS_OFFSET_DAYS} days "
            f"({start_date + timedelta(days=CENSUS_OFFSET_DAYS)})."
        )
    tid = term_id(year, season)
    if Term.objects.filter(year=year, season=season).exists():
        errors.setdefault("season", []).append(f"Term {tid} already exists.")
    if errors:
        raise ValidationError(errors)
    term = Term(
        term_id=tid,
        year=year,
        season=season,
        is_billable=is_billable,
        invoice_date=invoice_date,
        due_date=due_date,
        start_date=start_date,
        census_date=census_date,
        end_date=end_date,
        late_fee_grace_days=late_fee_grace_days,
    )
    term.full_clean(validate_unique=False, validate_constraints=False)
    term.save(force_insert=True)
    return term


def _event(
    enrollment: Enrollment,
    *,
    from_status,
    to_status,
    event_date: date,
    reason: str,
    actor: str,
    source_ref: str | None = None,
) -> EnrollmentEvent:
    return EnrollmentEvent.objects.create(
        event_id=next_id("status_event", entity="ENR", year=event_date.year),
        enrollment=enrollment,
        from_status=from_status,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
        source_ref=source_ref,
    )


@transaction.atomic
def enroll(*, student, term: Term, credits: int, enrolled_on: date, actor: str) -> Enrollment:
    """Create an ENROLLED enrollment and its first EnrollmentEvent."""
    from students.models import Student  # students ↔ academics reference each other

    student = (
        Student.objects.select_for_update()
        .select_related("program", "admit_term")
        .get(pk=student.pk)
    )
    program = student.program

    if not term.is_billable:
        raise domain_error(
            "TERM_NOT_BILLABLE",
            f"{term.term_id} is not a billable term.",
            details={"term_id": term.term_id},
        )
    errors: dict[str, list[str]] = {}
    if not program.is_active:
        errors["student"] = [f"The student's program {program.program_id} is not active."]
    if student.admit_term.start_date > term.start_date:
        errors["term"] = [
            f"{term.term_id} starts before the student's admit term {student.admit_term.term_id}."
        ]
    last_day = term.start_date + timedelta(days=ADD_DROP_DAYS)
    if enrolled_on > last_day:
        errors["enrolled_on"] = [
            f"enrolled_on must be on or before {last_day} (term start + {ADD_DROP_DAYS} days)."
        ]
    if errors:
        raise ValidationError(errors)
    if not program.min_credits <= credits <= program.max_credits:
        raise domain_error(
            "CREDITS_OUT_OF_RANGE",
            f"Credits must be between {program.min_credits} and {program.max_credits} "
            f"for {program.program_id}.",
            details={
                "credits": credits,
                "min_credits": program.min_credits,
                "max_credits": program.max_credits,
                "program_id": program.program_id,
            },
        )
    existing = Enrollment.objects.filter(student=student, term=term).first()
    if existing:
        raise _enrollment_exists(student.pk, term.term_id, existing.enrollment_id)

    enrollment = Enrollment(
        enrollment_id=next_id("enrollment", year=term.year),
        student=student,
        program=program,
        term=term,
        credits=credits,
        status=ENROLLED,
        enrolled_on=enrolled_on,
    )
    try:
        with transaction.atomic():
            enrollment.save(force_insert=True)
    except IntegrityError as exc:  # concurrent insert of the same (student, term)
        if "enrollment_unique_student_term" in str(exc) or "UNIQUE" in str(exc):
            raise _enrollment_exists(student.pk, term.term_id, None) from exc
        raise
    check_transition(
        ENROLLMENT_TRANSITIONS,
        None,
        ENROLLED,
        entity="Enrollment",
        record_id=enrollment.enrollment_id,
    )
    _event(
        enrollment,
        from_status=None,
        to_status=ENROLLED,
        event_date=enrolled_on,
        reason="ENROLLED",
        actor=actor,
    )
    return enrollment


def _enrollment_exists(student_id, term_id_, existing_id):
    return domain_error(
        "ENROLLMENT_EXISTS",
        f"{student_id} is already enrolled in {term_id_}.",
        details={
            "student_id": student_id,
            "term_id": term_id_,
            "existing_enrollment_id": existing_id,
        },
    )


@transaction.atomic
def complete_enrollment(enrollment: Enrollment, *, on_date: date, actor: str) -> Enrollment:
    """ENROLLED -> COMPLETED, allowed only after the term has ended."""
    enrollment = Enrollment.objects.select_for_update().select_related("term").get(pk=enrollment.pk)
    check_transition(
        ENROLLMENT_TRANSITIONS,
        enrollment.status,
        COMPLETED,
        entity="Enrollment",
        record_id=enrollment.enrollment_id,
    )
    if on_date <= enrollment.term.end_date:
        raise ValidationError(
            {
                "on_date": [
                    "An enrollment can complete only after the term ends "
                    f"({enrollment.term.end_date})."
                ]
            }
        )
    previous = enrollment.status
    enrollment.status = COMPLETED
    enrollment.save(update_fields=["status"])
    _event(
        enrollment,
        from_status=previous,
        to_status=COMPLETED,
        event_date=on_date,
        reason="TERM_ENDED",
        actor=actor,
    )
    return enrollment
