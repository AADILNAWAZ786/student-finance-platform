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
  Withdrawal  ENROLLED → WITHDRAWN, term start ≤ withdrawal_date ≤ term end, on or after
              enrolled_on (BR-WDR-02; DATABASE_DESIGN Enrollment SV)
  Cancellation ENROLLED → CANCELLED, enrolled_on ≤ cancellation_date < term start (BR-WDR-01)

Withdrawals and cancellations (Phase 5, decision D-W1): these services are the entry points
and orchestrate the whole operation in one transaction. They change the enrollment, then
call the exit handlers registered in ``academics.exits`` (billing registers one; it reaches
scholarships and payments through billing's registry), because academics may not import the
apps above it. A repeated request finds a terminal status: 409 ILLEGAL_TRANSITION.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from academics.calculators import withdrawal_refund_pct
from academics.calendar import CENSUS_OFFSET_DAYS
from academics.exits import run_exit_handlers
from academics.lifecycle import CANCELLED, COMPLETED, ENROLLED, ENROLLMENT_TRANSITIONS, WITHDRAWN
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


# --- withdrawals and cancellations (Phase 5: BR-WDR-01..03) ----------------------------------


@dataclass
class ExitResult:
    """What a withdrawal or cancellation did. ``handlers`` holds each exit handler's result
    (billing's: the invoice, its release plan and the refund quote)."""

    enrollment: Enrollment
    kind: str  # WITHDRAWAL or CANCELLATION
    effective_date: date
    refund_pct: Decimal | None  # BR-WDR-02 schedule; None for a cancellation
    handlers: dict = field(default_factory=dict)

    @property
    def financial(self) -> dict | None:
        return self.handlers.get("billing")


def _locked_enrollment(enrollment) -> Enrollment:
    return (
        Enrollment.objects.select_for_update()
        .select_related("term", "student")
        .get(pk=enrollment.pk)
    )


def _exit(enrollment: Enrollment, *, target, kind, on_date, refund_pct, date_field, actor):
    previous = enrollment.status
    enrollment.status = target
    setattr(enrollment, date_field, on_date)
    enrollment.save(update_fields=["status", date_field])
    _event(
        enrollment,
        from_status=previous,
        to_status=target,
        event_date=on_date,
        reason=kind,
        actor=actor,
    )
    handlers = run_exit_handlers(
        kind=kind, enrollment=enrollment, on_date=on_date, refund_pct=refund_pct, actor=actor
    )
    return ExitResult(enrollment, kind, on_date, refund_pct, handlers)


@transaction.atomic
def withdraw_enrollment(enrollment: Enrollment, *, withdrawal_date: date, actor: str) -> ExitResult:
    """ENROLLED → WITHDRAWN on ``withdrawal_date`` (on or after the term start, BR-WDR-02),
    then the invoice adjustments, scholarship reversals and payment releases (BR-WDR-03),
    all in this transaction."""
    enrollment = _locked_enrollment(enrollment)
    check_transition(
        ENROLLMENT_TRANSITIONS,
        enrollment.status,
        WITHDRAWN,
        entity="Enrollment",
        record_id=enrollment.enrollment_id,
    )
    term = enrollment.term
    problems = []
    if withdrawal_date < term.start_date:
        problems.append(
            f"Must be on or after the term start ({term.start_date}); before the start, "
            "cancel the enrollment instead."
        )
    elif withdrawal_date > term.end_date:
        problems.append(f"Must be on or before the term end ({term.end_date}).")
    if withdrawal_date < enrollment.enrolled_on:
        problems.append(f"Must be on or after enrolled_on ({enrollment.enrolled_on}).")
    if problems:
        raise ValidationError({"withdrawal_date": problems})
    return _exit(
        enrollment,
        target=WITHDRAWN,
        kind="WITHDRAWAL",
        on_date=withdrawal_date,
        refund_pct=withdrawal_refund_pct(term, withdrawal_date),
        date_field="withdrawal_date",
        actor=actor,
    )


@transaction.atomic
def cancel_enrollment(enrollment: Enrollment, *, cancellation_date: date, actor: str) -> ExitResult:
    """ENROLLED → CANCELLED on ``cancellation_date`` (before the term start, BR-WDR-01), then
    the invoice adjustments, releases and ISSUED → CANCELLED, all in this transaction."""
    enrollment = _locked_enrollment(enrollment)
    check_transition(
        ENROLLMENT_TRANSITIONS,
        enrollment.status,
        CANCELLED,
        entity="Enrollment",
        record_id=enrollment.enrollment_id,
    )
    term = enrollment.term
    problems = []
    if cancellation_date >= term.start_date:
        problems.append(
            f"Must be before the term start ({term.start_date}); from the start on, withdraw "
            "the enrollment instead."
        )
    if cancellation_date < enrollment.enrolled_on:
        problems.append(f"Must be on or after enrolled_on ({enrollment.enrolled_on}).")
    if problems:
        raise ValidationError({"cancellation_date": problems})
    return _exit(
        enrollment,
        target=CANCELLED,
        kind="CANCELLATION",
        on_date=cancellation_date,
        refund_pct=None,
        date_field="cancellation_date",
        actor=actor,
    )
