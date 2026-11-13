"""Scholarship services: the only code that writes scholarship types, eligibility
evaluations, awards, allocations and scholarship events. Each write is one atomic
transaction (ARCHITECTURE §8.4).

Three money limits are kept apart (approved Phase 3 decisions):

1. **Per-term cap** (BR-SCH-03, D-S5), at award time, on *awards*: ``award_amount`` with
   ``already_awarded`` = cumulative awarded_amount of the student's non-revoked awards of
   the same type in the same term.
2. **Award-stage reservation guardrail** (not BR-SCH-04), at award time: the invoice's
   reservations plus the new award must fit its eligible fee base. An award reserves its
   awarded_amount while OFFERED/ACCEPTED, its net applied amount once APPLIED or
   PARTIALLY_REVERSED, nothing once REVOKED. It caps the award "at eligible fees" (§9.3
   step 5) so that awards that could never be applied are not created.
3. **BR-SCH-04**, at apply time, on *actual allocations*: Σ net APPLY/REVERSE allocations on
   the invoice after the apply ≤ ``eligible_fee_base``. Apply also keeps every line within
   its own remaining room (net + adjustments − scholarship already on it; D-S6, BR-ALC-02)
   and Σ allocations of the award ≤ awarded_amount. Application is all-or-nothing (D-S8).

BR-ALC-06: apply asks billing's payment-release hook to free payment money on the lines it
covers. No payment allocations exist in Phase 3, so the hook must receive only zeros; a
non-zero release raises and rolls the whole application back.
"""

from __future__ import annotations

from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from billing import services as billing
from billing.models import Invoice
from core.exceptions import domain_error
from core.ids import next_id, scholarship_type_program_id
from core.lifecycle import check_transition
from core.money import ZERO, dsum, format_money, round_money
from scholarships import calculators as calc
from scholarships import policy
from scholarships.lifecycle import (
    ACCEPTED,
    APPLIED,
    OFFERED,
    PARTIALLY_REVERSED,
    REVOKED,
    SCHOLARSHIP_TRANSITIONS,
    UNAPPLIED,
)
from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
    ScholarshipTypeProgram,
)

EXCEEDS = "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"


# --- helpers -----------------------------------------------------------------------------------


def check_business_date(field: str, value: date) -> None:
    if not policy.BUSINESS_DATE_MIN <= value <= policy.BUSINESS_DATE_MAX:
        raise ValidationError(
            {
                field: [
                    f"Must be a business date between {policy.BUSINESS_DATE_MIN} and "
                    f"{policy.BUSINESS_DATE_MAX}."
                ]
            }
        )


def with_program_ids(scholarship_type: ScholarshipType) -> ScholarshipType:
    """Attach the bridge rows as ``program_ids`` (what ``calculators.evaluate`` reads)."""
    scholarship_type.program_ids = frozenset(
        scholarship_type.type_programs.values_list("program_id", flat=True)
    )
    return scholarship_type


def is_full_ride(scholarship_type) -> bool:
    """Full-ride is recognised by its coverage scope, never by its ID (D-S2)."""
    return scholarship_type.coverage_scope == policy.ALL_FEE_LINES


def invoice_allocations(invoice) -> list[ScholarshipAllocation]:
    """Allocation provider registered with billing (``billing.allocations``)."""
    return list(
        ScholarshipAllocation.objects.filter(invoice_line__invoice_id=invoice.pk).order_by(
            "allocation_id"
        )
    )


def _event(scholarship, *, from_status, to_status, event_date, reason, actor, source_ref=None):
    return ScholarshipEvent.objects.create(
        event_id=next_id("status_event", entity="SCH", year=event_date.year),
        scholarship=scholarship,
        from_status=from_status,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
        source_ref=source_ref,
    )


def _latest_invoice_date(invoice) -> date:
    """The latest business date already recorded on the invoice (events, lines, status)."""
    dates = [invoice.issue_date, invoice.collection_status_as_of]
    last_event = invoice.events.order_by("-event_date").values_list("event_date", flat=True).first()
    last_line = invoice.lines.order_by("-posted_date").values_list("posted_date", flat=True).first()
    return max(d for d in [*dates, last_event, last_line] if d is not None)


def _invoice_not_issued(invoice):
    return domain_error(
        "ILLEGAL_TRANSITION",
        f"Invoice {invoice.invoice_id} is {invoice.lifecycle_status}; scholarships apply only "
        "to ISSUED invoices.",
        details={
            "entity": "Invoice",
            "record_id": invoice.invoice_id,
            "status": invoice.lifecycle_status,
            "required_status": "ISSUED",
        },
    )


# --- scholarship types ---------------------------------------------------------------------------


@transaction.atomic
def create_scholarship_type(
    *,
    name,
    category,
    residency,
    first_term_only,
    program_restricted,
    coverage_scope,
    award_basis,
    award_value,
    max_amount_per_term,
    priority,
    effective_from,
    exclusive_group=None,
    min_gpa=None,
    max_gpa=None,
    min_need_index=None,
    effective_to=None,
    program_ids=(),
) -> ScholarshipType:
    """Create an immutable type and its program bridge rows (D-S2, D-S3)."""
    from academics.models import Program

    program_ids = list(program_ids or ())
    exclusive_group = exclusive_group or None
    errors: dict[str, list[str]] = {}

    # D-S3: the program list
    if program_restricted and not program_ids:
        errors["program_ids"] = ["Required (a non-empty list) when program_restricted is true."]
    elif not program_restricted and program_ids:
        errors["program_ids"] = ["Must be empty when program_restricted is false."]
    else:
        duplicates = sorted({p for p in program_ids if program_ids.count(p) > 1})
        unknown = sorted(
            set(program_ids)
            - set(Program.objects.filter(pk__in=program_ids).values_list("pk", flat=True))
        )
        if duplicates:
            errors["program_ids"] = [f"Duplicate program IDs: {', '.join(duplicates)}."]
        elif unknown:
            errors["program_ids"] = [f"Unknown program IDs: {', '.join(unknown)}."]

    # D-S2: the single ALL_FEE_LINES (full-ride) type
    if coverage_scope == policy.ALL_FEE_LINES:
        if category != policy.FULL_RIDE:
            errors.setdefault("category", []).append("ALL_FEE_LINES requires category FULL_RIDE.")
        if exclusive_group != policy.FULL_RIDE:
            errors.setdefault("exclusive_group", []).append(
                "ALL_FEE_LINES requires exclusive_group FULL_RIDE."
            )
        if priority != policy.FULL_RIDE_PRIORITY:
            errors.setdefault("priority", []).append("ALL_FEE_LINES requires priority 0.")
        existing = ScholarshipType.objects.filter(coverage_scope=policy.ALL_FEE_LINES).first()
        if existing:
            errors.setdefault("coverage_scope", []).append(
                f"Only one ALL_FEE_LINES type may exist; it is {existing.scholarship_type_id}."
            )

    # single-row ranges (also DB CHECKs), reported as 400 with the field
    if min_gpa is not None and max_gpa is not None and min_gpa >= max_gpa:
        errors.setdefault("max_gpa", []).append("max_gpa must be greater than min_gpa.")
    if award_basis == policy.PERCENT and award_value > 100:
        errors.setdefault("award_value", []).append("A PERCENT award_value must be at most 100.")
    # effective window: 2026 business dates (ARCHITECTURE D1), field-level 400
    for field, value in (("effective_from", effective_from), ("effective_to", effective_to)):
        if value is None:
            continue
        try:
            check_business_date(field, value)
        except ValidationError as exc:
            errors.setdefault(field, []).extend(exc.message_dict[field])
    if effective_to is not None and effective_to < effective_from and "effective_to" not in errors:
        errors.setdefault("effective_to", []).append(
            "effective_to must be on or after effective_from."
        )
    if errors:
        raise ValidationError(errors)

    stype = ScholarshipType(
        scholarship_type_id=next_id("scholarship_type"),
        name=name,
        category=category,
        exclusive_group=exclusive_group,
        min_gpa=min_gpa,
        max_gpa=max_gpa,
        min_need_index=min_need_index,
        residency=residency,
        first_term_only=first_term_only,
        program_restricted=program_restricted,
        coverage_scope=coverage_scope,
        award_basis=award_basis,
        award_value=round_money(award_value),
        max_amount_per_term=round_money(max_amount_per_term),
        priority=priority,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    stype.full_clean(validate_unique=False, validate_constraints=False)
    try:
        with transaction.atomic():
            stype.save(force_insert=True)
    except IntegrityError as exc:
        # Only the race this guards (a concurrent second ALL_FEE_LINES type, caught by the
        # partial unique index on coverage_scope) becomes a 400; any other integrity error
        # is a defect and is re-raised unchanged, never relabelled.
        if coverage_scope == policy.ALL_FEE_LINES and "coverage_scope" in str(exc):
            raise ValidationError(
                {"coverage_scope": ["Only one ALL_FEE_LINES type may exist."]}
            ) from exc
        raise
    for program_id in sorted(program_ids):
        ScholarshipTypeProgram.objects.create(
            id=scholarship_type_program_id(stype.scholarship_type_id, program_id),
            scholarship_type=stype,
            program_id=program_id,
        )
    return stype


# --- eligibility (BR-SCH-01) ----------------------------------------------------------------------


def current_invoice(student, term) -> Invoice | None:
    """The student's non-VOID invoice for the term (BR-INV-01: at most one)."""
    return (
        Invoice.objects.filter(student=student, term=term)
        .exclude(lifecycle_status="VOID")
        .order_by("invoice_id")
        .first()
    )


@transaction.atomic
def evaluate_eligibility(*, student, term) -> list[Eligibility]:
    """Evaluate the student against **every** scholarship type for the term and store one
    Eligibility row per type, eligible or not (BR-SCH-01).

    ``evaluated_on`` is always ``term.invoice_date`` (D-S7); clients cannot choose it.
    ``eligible_fee_base`` is filled when the student has a non-VOID invoice for the term.
    """
    if not term.is_billable:
        raise domain_error(
            "TERM_NOT_BILLABLE",
            f"{term.term_id} is not a billable term.",
            details={"term_id": term.term_id},
        )
    evaluated_on = term.invoice_date
    invoice = current_invoice(student, term)
    lines = list(invoice.lines.all()) if invoice else None
    rows = []
    for stype in ScholarshipType.objects.prefetch_related("type_programs").order_by(
        "priority", "scholarship_type_id"
    ):
        stype.program_ids = frozenset(tp.program_id for tp in stype.type_programs.all())
        result = calc.evaluate(student, term, stype)
        rows.append(
            Eligibility.objects.create(
                eligibility_id=next_id("eligibility", year=evaluated_on.year),
                student=student,
                scholarship_type=stype,
                term=term,
                evaluated_on=evaluated_on,
                is_eligible=result.is_eligible,
                reason_codes=list(result.reason_codes),
                inputs_snapshot=result.inputs_snapshot,
                eligible_fee_base=calc.eligible_fee_base(stype, lines)
                if lines is not None
                else None,
            )
        )
    return rows


# --- awards (BR-SCH-02/03/05/08) ------------------------------------------------------------------


def _not_eligible(message, **details):
    return domain_error("SCHOLARSHIP_NOT_ELIGIBLE", message, rule_id="BR-SCH-02", details=details)


def _group_conflict(stype, conflicting, rule_id):
    return domain_error(
        "SCHOLARSHIP_GROUP_CONFLICT",
        f"{stype.scholarship_type_id} conflicts with existing award(s) "
        f"{', '.join(s.scholarship_id for s in conflicting)} for the same student and term.",
        rule_id=rule_id,
        details={
            "scholarship_type_id": stype.scholarship_type_id,
            "exclusive_group": stype.exclusive_group,
            "conflicting_scholarship_ids": [s.scholarship_id for s in conflicting],
        },
    )


def applied_amount(scholarship) -> object:
    """Σ net allocations (APPLY − REVERSE) of one award."""
    return dsum(scholarship.allocations.values_list("amount", flat=True))


def reserved_on_invoice(invoice, *, exclude_pk=None) -> object:
    """Award-stage reservation guardrail (see the module docstring): what the invoice's
    non-revoked awards already hold against its eligible fee base."""
    total = ZERO
    for award in Scholarship.objects.filter(invoice=invoice).exclude(status=REVOKED):
        if award.pk == exclude_pk:
            continue
        total += award.awarded_amount if award.status in UNAPPLIED else applied_amount(award)
    return round_money(total)


@transaction.atomic
def award_scholarship(
    *, eligibility, invoice, requested_amount=None, award_date: date | None = None, actor: str
) -> Scholarship:
    """Create an award (OFFERED) from a positive, current eligibility (BR-SCH-02)."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    eligibility = Eligibility.objects.select_related("scholarship_type", "student", "term").get(
        pk=eligibility.pk
    )
    stype, student, term = eligibility.scholarship_type, eligibility.student, eligibility.term

    # BR-SCH-02: a stored, positive, current, unused evaluation
    if not eligibility.is_eligible:
        raise _not_eligible(
            f"{eligibility.eligibility_id} is not a positive eligibility.",
            eligibility_id=eligibility.eligibility_id,
            reason_codes=list(eligibility.reason_codes),
        )
    latest = (
        Eligibility.objects.filter(student=student, scholarship_type=stype, term=term)
        .order_by("-eligibility_id")
        .first()
    )
    if latest.pk != eligibility.pk:
        raise _not_eligible(
            f"{eligibility.eligibility_id} is superseded by the later evaluation "
            f"{latest.eligibility_id}.",
            eligibility_id=eligibility.eligibility_id,
            reason="SUPERSEDED",
            latest_eligibility_id=latest.eligibility_id,
        )
    used = Scholarship.objects.filter(eligibility=eligibility).first()
    if used:
        raise _not_eligible(
            f"{eligibility.eligibility_id} was already used by {used.scholarship_id}.",
            eligibility_id=eligibility.eligibility_id,
            reason="ELIGIBILITY_ALREADY_USED",
            existing_scholarship_id=used.scholarship_id,
        )

    # the target invoice
    if invoice.student_id != student.pk or invoice.term_id != term.pk:
        raise ValidationError(
            {"invoice_id": [f"{invoice.invoice_id} is not {student.pk}'s invoice for {term.pk}."]}
        )
    if invoice.lifecycle_status != "ISSUED":
        raise _invoice_not_issued(invoice)

    # BR-SCH-05 / BR-SCH-08: exclusive groups and full-ride exclusivity (both directions)
    active = list(
        Scholarship.objects.filter(student=student, term=term)
        .exclude(status=REVOKED)
        .select_related("scholarship_type")
        .order_by("scholarship_id")
    )
    if active and is_full_ride(stype):
        raise _group_conflict(stype, active, "BR-SCH-08")
    full_rides = [s for s in active if is_full_ride(s.scholarship_type)]
    if full_rides:
        raise _group_conflict(stype, full_rides, "BR-SCH-08")
    if stype.exclusive_group:
        same_group = [
            s for s in active if s.scholarship_type.exclusive_group == stype.exclusive_group
        ]
        if same_group:
            raise _group_conflict(stype, same_group, "BR-SCH-05")

    # amounts: (1) per-term cap on awards; (2) reservation guardrail on the invoice
    lines = list(invoice.lines.all())
    base = calc.eligible_fee_base(stype, lines)
    already_awarded = dsum(
        s.awarded_amount for s in active if s.scholarship_type_id == stype.scholarship_type_id
    )
    computed = calc.award_amount(stype, base, already_awarded)
    reserved = reserved_on_invoice(invoice)
    available = round_money(max(ZERO, base - reserved))
    maximum = min(computed, available)
    details = {
        "scholarship_type_id": stype.scholarship_type_id,
        "eligible_fee_base": format_money(base),
        "already_awarded_this_term": format_money(already_awarded),
        "computed_award": format_money(computed),
        "reserved_on_invoice": format_money(reserved),
        "maximum_award": format_money(maximum),
    }
    if requested_amount is not None:
        if requested_amount <= 0:
            raise ValidationError({"requested_amount": ["Must be greater than 0."]})
        if requested_amount > maximum:
            raise domain_error(
                EXCEEDS,
                f"Requested {format_money(requested_amount)} exceeds the maximum award "
                f"{format_money(maximum)}.",
                rule_id="BR-SCH-03" if requested_amount > computed else None,
                details={**details, "requested_amount": format_money(requested_amount)},
            )
        amount = round_money(requested_amount)
    else:
        amount = maximum
    if amount <= 0:
        raise domain_error(
            EXCEEDS,
            f"No award is possible: the maximum award for {stype.scholarship_type_id} is 0.00.",
            rule_id="BR-SCH-03" if computed <= 0 else None,
            details=details,
        )

    award_date = award_date or max(eligibility.evaluated_on, invoice.issue_date)
    check_business_date("award_date", award_date)
    if award_date < eligibility.evaluated_on:
        raise ValidationError(
            {
                "award_date": [
                    f"award_date must be on or after {eligibility.evaluated_on} (evaluated_on)."
                ]
            }
        )

    scholarship = Scholarship(
        scholarship_id=next_id("scholarship", year=award_date.year),
        student=student,
        scholarship_type=stype,
        term=term,
        eligibility=eligibility,
        invoice=invoice,
        awarded_amount=amount,
        award_date=award_date,
        status=OFFERED,
    )
    try:
        with transaction.atomic():
            scholarship.save(force_insert=True)
    except IntegrityError as exc:  # the eligibility was used concurrently
        raise _not_eligible(
            f"{eligibility.eligibility_id} was already used.",
            eligibility_id=eligibility.eligibility_id,
            reason="ELIGIBILITY_ALREADY_USED",
        ) from exc
    check_transition(SCHOLARSHIP_TRANSITIONS, None, OFFERED, entity="Scholarship")
    _event(
        scholarship,
        from_status=None,
        to_status=OFFERED,
        event_date=award_date,
        reason="AWARDED",
        actor=actor,
        source_ref=eligibility.eligibility_id,
    )
    return scholarship


# --- application (BR-SCH-04/06/08, BR-ALC-02/06) --------------------------------------------------


def _adjustments_by_line(lines) -> dict:
    out: dict = {}
    for line in lines:
        if line.line_type == "ADJUSTMENT":
            out[line.adjusts_line_id] = out.get(line.adjusts_line_id, ZERO) + line.net_amount
    return out


@transaction.atomic
def apply_scholarship(scholarship, *, allocated_on: date | None = None, actor: str) -> Scholarship:
    """Allocate the whole award to its coverable lines (all-or-nothing, D-S8)."""
    scholarship = (
        Scholarship.objects.select_for_update()
        .select_related("scholarship_type")
        .get(pk=scholarship.pk)
    )
    status = scholarship.status
    if status != OFFERED:  # OFFERED → ACCEPTED → APPLIED happens automatically (D-S13)
        check_transition(
            SCHOLARSHIP_TRANSITIONS,
            status,
            APPLIED,
            entity="Scholarship",
            record_id=scholarship.scholarship_id,
        )
    invoice = Invoice.objects.select_for_update().get(pk=scholarship.invoice_id)
    if invoice.lifecycle_status != "ISSUED":
        raise _invoice_not_issued(invoice)

    earliest = max(
        scholarship.award_date,
        _latest_invoice_date(invoice),
        scholarship.events.order_by("-event_date").values_list("event_date", flat=True).first()
        or scholarship.award_date,
    )
    allocated_on = allocated_on or earliest
    check_business_date("allocated_on", allocated_on)
    if allocated_on < earliest:
        raise ValidationError(
            {
                "allocated_on": [
                    f"allocated_on must be on or after {earliest}, the award date or the "
                    "invoice's latest posting or status date."
                ]
            }
        )

    stype = scholarship.scholarship_type
    lines = list(invoice.lines.all())
    existing = invoice_allocations(invoice)
    _, payment_allocations = billing.allocations_for(invoice)
    amount = round_money(scholarship.awarded_amount - applied_amount(scholarship))

    # BR-SCH-04, on actual allocations: Σ net applied on the invoice ≤ eligible fee base
    base = calc.eligible_fee_base(stype, lines)
    applied_on_invoice = dsum(a.amount for a in existing)
    if applied_on_invoice + amount > base:
        raise domain_error(
            EXCEEDS,
            f"Applying {format_money(amount)} would bring scholarship applied to "
            f"{invoice.invoice_id} to {format_money(applied_on_invoice + amount)}, above its "
            f"eligible fee base {format_money(base)}.",
            rule_id="BR-SCH-04",
            details={
                "scholarship_id": scholarship.scholarship_id,
                "invoice_id": invoice.invoice_id,
                "eligible_fee_base": format_money(base),
                "already_applied": format_money(applied_on_invoice),
                "amount": format_money(amount),
            },
        )
    # each line's remaining room (net + adjustments − scholarship already on it)
    try:
        plan = calc.plan_application(
            stype,
            lines,
            amount,
            adjustments=_adjustments_by_line(lines),
            scholarship_allocated=calc.net_by_line(existing),
            payment_allocated=calc.net_by_line(payment_allocations),
        )
    except calc.InsufficientRoom as exc:
        raise domain_error(
            EXCEEDS,
            f"The coverable lines of {invoice.invoice_id} have {format_money(exc.room)} of room "
            f"left; {format_money(amount)} cannot be applied.",
            rule_id="BR-ALC-02",
            details={
                "scholarship_id": scholarship.scholarship_id,
                "invoice_id": invoice.invoice_id,
                "amount": format_money(amount),
                "remaining_room": format_money(exc.room),
            },
        ) from exc

    # BR-ALC-06: make room by releasing payment allocations first (payments' LIFO handler)
    billing.release_payments_for_scholarship(
        invoice=invoice,
        releases={p.line_id: p.payment_to_release for p in plan},
        on_date=allocated_on,
        actor=actor,
        source_ref=scholarship.scholarship_id,
    )
    for p in plan:
        allocation = ScholarshipAllocation(
            allocation_id=next_id("scholarship_allocation", parent=scholarship.scholarship_id),
            scholarship=scholarship,
            invoice_line_id=p.line_id,
            allocation_type=policy.APPLY,
            amount=p.amount,
            reverses=None,
            reason=policy.AWARD,
            allocated_on=allocated_on,
        )
        allocation.save(force_insert=True)
        billing.post_scholarship_ledger(allocation, student_id=scholarship.student_id)

    if status == OFFERED:
        check_transition(SCHOLARSHIP_TRANSITIONS, OFFERED, ACCEPTED, entity="Scholarship")
        _event(
            scholarship,
            from_status=OFFERED,
            to_status=ACCEPTED,
            event_date=allocated_on,
            reason="ACCEPTED_ON_APPLY",
            actor=actor,
        )
    _event(
        scholarship,
        from_status=ACCEPTED,
        to_status=APPLIED,
        event_date=allocated_on,
        reason="APPLIED",
        actor=actor,
        source_ref=invoice.invoice_id,
    )
    scholarship.status = APPLIED
    scholarship.save(update_fields=["status"])
    billing.recalculate_collection_status(
        invoice,
        as_of=allocated_on,
        reason="SCHOLARSHIP_APPLIED",
        actor=actor,
        source_ref=scholarship.scholarship_id,
    )
    return scholarship


# --- reversal (BR-SCH-07) -------------------------------------------------------------------------


@transaction.atomic
def reverse_allocation(allocation, *, amount, reason: str, on_date: date, actor: str):
    """Append a REVERSE row for ``amount`` (> 0) of an APPLY allocation, its
    SCHOLARSHIP_REVERSAL ledger entry, the first-reversal status change and the invoice
    collection recalculation. No endpoint. (Withdrawals and cancellations use
    ``reverse_for_adjustment``, which shares the row writing but not the date rule or the
    recalculation.)
    """
    allocation = ScholarshipAllocation.objects.select_related(
        "scholarship", "invoice_line__invoice"
    ).get(pk=allocation.pk)
    if allocation.allocation_type != policy.APPLY:
        raise ValidationError({"allocation": ["Only an APPLY allocation can be reversed."]})
    if reason not in policy.REVERSAL_REASONS:
        raise ValidationError({"reason": [f"Must be one of {', '.join(policy.REVERSAL_REASONS)}."]})
    if amount <= 0:
        raise ValidationError({"amount": ["Must be greater than 0."]})
    reversed_so_far = -dsum(allocation.reversals.values_list("amount", flat=True))
    if reversed_so_far + amount > allocation.amount:
        raise ValidationError(
            {
                "amount": [
                    f"Reversals of {allocation.allocation_id} would total "
                    f"{format_money(reversed_so_far + amount)}, above its "
                    f"{format_money(allocation.amount)}."
                ]
            }
        )
    scholarship = Scholarship.objects.select_for_update().get(pk=allocation.scholarship_id)
    if scholarship.status not in (APPLIED, PARTIALLY_REVERSED):
        check_transition(
            SCHOLARSHIP_TRANSITIONS,
            scholarship.status,
            PARTIALLY_REVERSED,
            entity="Scholarship",
            record_id=scholarship.scholarship_id,
        )
    invoice = allocation.invoice_line.invoice
    check_business_date("on_date", on_date)
    earliest = max(allocation.allocated_on, _latest_invoice_date(invoice))
    if on_date < earliest:
        raise ValidationError({"on_date": [f"on_date must be on or after {earliest}."]})

    reversal = _append_reversal(
        allocation, scholarship, amount=amount, reason=reason, on_date=on_date, actor=actor
    )
    billing.recalculate_collection_status(
        invoice,
        as_of=on_date,
        reason="SCHOLARSHIP_REVERSED",
        actor=actor,
        source_ref=reversal.allocation_id,
    )
    return reversal


def _append_reversal(allocation, scholarship, *, amount, reason, on_date, actor):
    """Write one REVERSE row, its SCHOLARSHIP_REVERSAL ledger entry and, on the award's first
    reversal, APPLIED → PARTIALLY_REVERSED with its event (D-S16). The caller validates."""
    reversal = ScholarshipAllocation(
        allocation_id=next_id("scholarship_allocation", parent=scholarship.scholarship_id),
        scholarship=scholarship,
        invoice_line=allocation.invoice_line,
        allocation_type=policy.REVERSE,
        amount=-round_money(amount),
        reverses=allocation,
        reason=reason,
        allocated_on=on_date,
    )
    reversal.save(force_insert=True)
    billing.post_scholarship_ledger(reversal, student_id=scholarship.student_id)
    if scholarship.status == APPLIED:  # later reversals keep PARTIALLY_REVERSED (D-S16)
        check_transition(SCHOLARSHIP_TRANSITIONS, APPLIED, PARTIALLY_REVERSED, entity="Scholarship")
        _event(
            scholarship,
            from_status=APPLIED,
            to_status=PARTIALLY_REVERSED,
            event_date=on_date,
            reason=reason,
            actor=actor,
            source_ref=reversal.allocation_id,
        )
        scholarship.status = PARTIALLY_REVERSED
        scholarship.save(update_fields=["status"])
    return reversal


class ScholarshipReversalFailed(RuntimeError):
    """A withdrawal or cancellation must reverse more scholarship than the line holds.

    Billing computes R from the same allocations, so this is an integrity problem; it is
    raised inside the caller's transaction, which rolls back completely.
    """


REVERSAL_DATE_FIELD = {
    policy.WITHDRAWAL: "withdrawal_date",
    policy.CANCELLATION: "cancellation_date",
}


@transaction.atomic
def reverse_for_adjustment(
    *, invoice_line_id: str, amount, reason: str, on_date: date, actor: str, source_ref=None
) -> list:
    """BR-WDR-03 step 1 / BR-SCH-07 (registered with billing as the scholarship_reversal
    adjustment handler): reverse ``amount`` of the scholarship on one line, split across its
    APPLY allocations in proportion to what each still holds, largest-remainder rounding,
    ties by allocation_id (decision D-W4). Returns the REVERSE rows.

    Dates: a business date, never before the allocation it reverses (decision D-W8: no
    invoice-wide "latest date" rule). The invoice's collection status is left to billing,
    which recalculates it once after the whole withdrawal or cancellation.
    """
    if reason not in policy.REVERSAL_REASONS:
        raise ValidationError({"reason": [f"Must be one of {', '.join(policy.REVERSAL_REASONS)}."]})
    amount = round_money(amount)
    if amount <= 0:
        raise ValidationError({"amount": ["Must be greater than 0."]})
    field = REVERSAL_DATE_FIELD[reason]
    check_business_date(field, on_date)
    rows = list(
        ScholarshipAllocation.objects.select_related("invoice_line").filter(
            invoice_line_id=invoice_line_id
        )
    )
    reversed_by = {}
    for r in rows:
        if r.allocation_type == policy.REVERSE:
            reversed_by[r.reverses_id] = reversed_by.get(r.reverses_id, ZERO) - r.amount
    remaining = {
        r.allocation_id: r.amount - reversed_by.get(r.allocation_id, ZERO)
        for r in rows
        if r.allocation_type == policy.APPLY
    }
    try:
        shares = calc.split_reversal(remaining, amount)
    except ValueError as exc:
        raise ScholarshipReversalFailed(
            f"{source_ref}: {invoice_line_id} holds {dsum(remaining.values())} of scholarship, "
            f"cannot reverse {amount}"
        ) from exc
    by_id = {r.allocation_id: r for r in rows}
    for allocation_id in sorted(shares):  # validate every row before writing any
        original = by_id[allocation_id]
        if on_date < original.allocated_on:
            raise ValidationError(
                {
                    field: [
                        f"Must be on or after {original.allocated_on.isoformat()}, the date of "
                        f"scholarship allocation {allocation_id}, which it would reverse."
                    ]
                }
            )
    scholarships = {
        s.pk: s
        for s in Scholarship.objects.select_for_update().filter(
            pk__in={by_id[a].scholarship_id for a in shares}
        )
    }
    for s in scholarships.values():
        if s.status not in (APPLIED, PARTIALLY_REVERSED):
            check_transition(
                SCHOLARSHIP_TRANSITIONS,
                s.status,
                PARTIALLY_REVERSED,
                entity="Scholarship",
                record_id=s.scholarship_id,
            )
    return [
        _append_reversal(
            by_id[allocation_id],
            scholarships[by_id[allocation_id].scholarship_id],
            amount=shares[allocation_id],
            reason=reason,
            on_date=on_date,
            actor=actor,
        )
        for allocation_id in sorted(shares)
    ]
