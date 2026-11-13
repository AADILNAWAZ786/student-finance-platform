"""Billing services: the only code that writes fee schedules, invoices, invoice lines,
invoice events and ledger entries. Each write is one atomic transaction
(ARCHITECTURE §8.4); batch services use one transaction per invoice, as the map says.

Rules enforced here (SV rules of DATABASE_DESIGN.md §3 and ARCHITECTURE §6.1):
  FeeSchedule   billable term; residency mapping (D3); TUITION PER_CREDIT; flags and
                priority from fee_type (D5); V1 once per key; a new version supersedes the
                latest one and starts later (422 FEE_SCHEDULE_VERSION_INVALID)
  Invoice       ENROLLED enrollment only (409 ILLEGAL_TRANSITION, D11); one non-VOID invoice
                per enrollment (409 DUPLICATE_INVOICE, BR-INV-01); versions in force on the
                term's invoice_date (BR-FEE-01; 422 FEE_SCHEDULE_NOT_FOUND for a missing
                required fee type, D2); waivers (BR-WAV-01, 400); issue/due dates (BR-INV-03);
                total = Σ FEE net (BR-INV-02); collection status recalculated (BR-INV-07)
  Void          lifecycle ISSUED; current scholarship_applied = amount_paid = 0
                (409 INVOICE_HAS_ALLOCATIONS, BR-INV-08); explicit void_date (D15)
  Late fees     BR-LATE-01, explicit as_of_date (D8)
  Ledger        exactly one entry per invoice line (BR-LED-01), via ``post_ledger`` only
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from billing import allocations as allocation_registry
from billing import calculators as calc
from billing import policy
from billing.lifecycle import ISSUED, LIFECYCLE_TRANSITIONS, VOID, can_void
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.exceptions import domain_error
from core.ids import fee_schedule_id, next_id
from core.lifecycle import check_transition
from core.money import ZERO, dsum, format_money, round_money

ENTRY_TYPE_FOR_LINE = {"FEE": "CHARGE", "LATE_FEE": "LATE_FEE", "ADJUSTMENT": "ADJUSTMENT"}
MAX_FEE_VERSION = 99  # the ID format allows V1..V99


# --- shared helpers ----------------------------------------------------------------------------


def check_business_date(field: str, value: date) -> None:
    """Every business date falls in 2026 (ARCHITECTURE D1). 400 otherwise."""
    if not policy.BUSINESS_DATE_MIN <= value <= policy.BUSINESS_DATE_MAX:
        raise ValidationError(
            {
                field: [
                    f"Must be a business date between {policy.BUSINESS_DATE_MIN} and "
                    f"{policy.BUSINESS_DATE_MAX}."
                ]
            }
        )


def allocations_for(invoice) -> tuple[list, list]:
    """(scholarship allocations, payment allocations) on the invoice's lines.

    Read from the allocation registry (``billing.allocations``, decision D-S1): the
    scholarships app registers its provider (Phase 3) and payments will register theirs
    (Phase 4). Billing never imports either app. Every caller (amounts, open amounts, void,
    late fees, collection status, statement, balance) goes through this one function.
    """
    return allocation_registry.allocations("scholarship", invoice), allocation_registry.allocations(
        "payment", invoice
    )


def _dated_on_or_before(rows, as_of: date | None) -> list:
    rows = list(rows)
    if as_of is None:
        return rows
    return [r for r in rows if getattr(r, "allocated_on", None) is None or r.allocated_on <= as_of]


def current_amounts(invoice, lines=None, *, as_of: date | None = None) -> calc.InvoiceAmounts:
    """BR-INV-04 amounts from the stored rows (lines and allocations dated after ``as_of``
    excluded)."""
    lines = list(invoice.lines.all()) if lines is None else list(lines)
    if as_of is not None:
        lines = [line for line in lines if line.posted_date <= as_of]
    scholarship, payments = allocations_for(invoice)
    return calc.invoice_amounts(
        lines, _dated_on_or_before(scholarship, as_of), _dated_on_or_before(payments, as_of)
    )


def _event(
    invoice, *, dimension, from_status, to_status, event_date, reason, actor, source_ref=None
):
    return InvoiceEvent.objects.create(
        event_id=next_id("status_event", entity="INV", year=event_date.year),
        invoice=invoice,
        dimension=dimension,
        from_status=from_status,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
        source_ref=source_ref,
    )


def post_ledger(line: InvoiceLine) -> LedgerEntry:
    """The one place ledger entries are written (BR-LED-01 posting table).

    FEE → CHARGE (+net, may be 0), LATE_FEE → LATE_FEE (+net), ADJUSTMENT → ADJUSTMENT
    (net, already negative). ``entry_date`` = the line's posted_date.
    """
    entry_type = ENTRY_TYPE_FOR_LINE[line.line_type]
    entry = LedgerEntry(
        entry_id=next_id("ledger_entry", year=line.posted_date.year),
        student_id=line.invoice.student_id,
        entry_date=line.posted_date,
        entry_type=entry_type,
        amount=calc.ledger_amount(entry_type, abs(line.net_amount)),
        invoice_line=line,
        memo=line.description[:120],
    )
    entry.save(force_insert=True)
    return entry


def _set_collection_status(invoice, *, as_of: date, reason: str, actor: str, source_ref=None):
    """BR-INV-07: derive the collection status at ``as_of``, write an event if it changed,
    and record ``collection_status_as_of``."""
    amounts = current_amounts(invoice)
    new = calc.collection_status(amounts, due_date=invoice.due_date, as_of=as_of)
    old = invoice.collection_status
    if new != old:
        _event(
            invoice,
            dimension="COLLECTION",
            from_status=old,
            to_status=new,
            event_date=as_of,
            reason=reason,
            actor=actor,
            source_ref=source_ref,
        )
    invoice.collection_status = new
    invoice.collection_status_as_of = as_of
    invoice.save(update_fields=["collection_status", "collection_status_as_of"])
    return old, new


def recalculate_collection_status(
    invoice, *, as_of: date, reason: str, actor: str, source_ref=None
):
    """Public BR-INV-07 hook for higher apps (scholarships now, payments in Phase 4).

    Derives the status at ``max(as_of, invoice.collection_status_as_of)`` (a status is
    never moved back in time) and writes a COLLECTION event only when it changes.
    Must run inside the caller's transaction.
    """
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    effective = max(as_of, invoice.collection_status_as_of)
    return _set_collection_status(
        invoice, as_of=effective, reason=reason, actor=actor, source_ref=source_ref
    )


SCHOLARSHIP_ENTRY_TYPE = {"APPLY": "SCHOLARSHIP_CREDIT", "REVERSE": "SCHOLARSHIP_REVERSAL"}


def post_scholarship_ledger(allocation, *, student_id: str) -> LedgerEntry:
    """BR-LED-01 for a scholarship allocation (given as a plain object; billing does not
    import scholarships). APPLY → SCHOLARSHIP_CREDIT (−amount); REVERSE (amount < 0) →
    SCHOLARSHIP_REVERSAL (+|amount|). ``entry_date`` = ``allocated_on``.
    """
    entry_type = SCHOLARSHIP_ENTRY_TYPE[allocation.allocation_type]
    entry = LedgerEntry(
        entry_id=next_id("ledger_entry", year=allocation.allocated_on.year),
        student_id=student_id,
        entry_date=allocation.allocated_on,
        entry_type=entry_type,
        amount=calc.ledger_amount(entry_type, abs(allocation.amount)),
        invoice_line=None,
        scholarship_allocation_id=allocation.pk,
        memo=f"{allocation.allocation_type} {allocation.pk} on {allocation.invoice_line_id}"[:120],
    )
    entry.save(force_insert=True)
    return entry


def release_payments_for_scholarship(*, invoice, releases, on_date: date, actor: str, source_ref):
    """BR-ALC-06 hook: free payment money on lines a scholarship now covers.

    Delegates to the handler payments registers (Phase 4). With no handler, any non-zero
    release raises ``PaymentReleaseUnavailable`` inside the caller's transaction.
    """
    allocation_registry.release_payments(
        invoice=invoice, releases=releases, on_date=on_date, actor=actor, source_ref=source_ref
    )


# --- fee schedules (BR-FEE-01..04) ---------------------------------------------------------------


def chain_of(schedule: FeeSchedule):
    """Every version with the same program, term, fee type and residency, by version."""
    return FeeSchedule.objects.filter(
        program_id=schedule.program_id,
        term_id=schedule.term_id,
        fee_type=schedule.fee_type,
        residency=schedule.residency,
    ).order_by("version")


def _version_invalid(message, **details):
    return domain_error(
        "FEE_SCHEDULE_VERSION_INVALID", message, rule_id="BR-FEE-03", details=details
    )


@transaction.atomic
def create_fee_schedule(
    *, program, term, fee_type, residency, charge_basis, amount, effective_from
) -> FeeSchedule:
    """Create version 1 of a fee schedule. Later prices are new versions (BR-FEE-03)."""
    errors: dict[str, list[str]] = {}
    if not term.is_billable:
        errors["term"] = [f"{term.term_id} is not a billable term."]
    allowed = policy.ALLOWED_RESIDENCIES[fee_type]
    if residency not in allowed:
        errors["residency"] = [
            f"{fee_type} fee schedules must use residency {' or '.join(allowed)}."
        ]
    if fee_type == policy.TUITION and charge_basis != "PER_CREDIT":
        errors["charge_basis"] = ["TUITION must be charged PER_CREDIT."]
    if amount < 0:
        errors["amount"] = ["Must not be negative."]
    if errors:
        raise ValidationError(errors)
    check_business_date("effective_from", effective_from)

    existing = (
        FeeSchedule.objects.filter(
            program=program, term=term, fee_type=fee_type, residency=residency
        )
        .order_by("-version")
        .first()
    )
    if existing:
        raise _version_invalid(
            f"A fee schedule already exists for {program.program_id}, {term.term_id}, "
            f"{fee_type}, {residency}. Create a new version of {existing.fee_schedule_id} instead.",
            latest_fee_schedule_id=existing.fee_schedule_id,
        )
    flags = policy.fee_flags(fee_type)
    schedule = FeeSchedule(
        fee_schedule_id=fee_schedule_id(program.program_id, term.term_id, fee_type, residency, 1),
        program=program,
        term=term,
        fee_type=fee_type,
        residency=residency,
        charge_basis=charge_basis,
        amount=round_money(amount),
        is_scholarship_eligible=flags.is_scholarship_eligible,
        is_refundable=flags.is_refundable,
        is_waivable=flags.is_waivable,
        allocation_priority=flags.allocation_priority,
        version=1,
        effective_from=effective_from,
        supersedes=None,
    )
    return _insert_schedule(schedule)


def _insert_schedule(schedule: FeeSchedule) -> FeeSchedule:
    try:
        with transaction.atomic():
            schedule.save(force_insert=True)
    except IntegrityError as exc:  # a concurrent create/new-version of the same key
        raise _version_invalid(
            f"{schedule.fee_schedule_id} could not be created: the version chain changed.",
            fee_schedule_id=schedule.fee_schedule_id,
        ) from exc
    return schedule


@transaction.atomic
def new_fee_schedule_version(base: FeeSchedule, *, amount, effective_from) -> FeeSchedule:
    """Insert version n+1 superseding ``base`` (BR-FEE-03). ``base`` is never changed.

    ``base`` must be the latest version, and the new ``effective_from`` must be later
    than ``base.effective_from``; otherwise 422 FEE_SCHEDULE_VERSION_INVALID.
    """
    if amount < 0:
        raise ValidationError({"amount": ["Must not be negative."]})
    check_business_date("effective_from", effective_from)
    base = FeeSchedule.objects.select_for_update().get(pk=base.pk)
    latest = chain_of(base).last()
    if latest.pk != base.pk:
        raise _version_invalid(
            f"{base.fee_schedule_id} is not the latest version; the latest is "
            f"{latest.fee_schedule_id}.",
            fee_schedule_id=base.fee_schedule_id,
            latest_fee_schedule_id=latest.fee_schedule_id,
        )
    if effective_from <= base.effective_from:
        raise _version_invalid(
            f"effective_from must be later than {base.effective_from} "
            f"(the effective_from of {base.fee_schedule_id}).",
            fee_schedule_id=base.fee_schedule_id,
            predecessor_effective_from=base.effective_from.isoformat(),
            effective_from=effective_from.isoformat(),
        )
    if base.version >= MAX_FEE_VERSION:
        raise _version_invalid(
            f"{base.fee_schedule_id} is version {MAX_FEE_VERSION}, the last one the ID "
            "format allows.",
            fee_schedule_id=base.fee_schedule_id,
        )
    version = base.version + 1
    schedule = FeeSchedule(
        fee_schedule_id=fee_schedule_id(
            base.program_id, base.term_id, base.fee_type, base.residency, version
        ),
        program_id=base.program_id,
        term_id=base.term_id,
        fee_type=base.fee_type,
        residency=base.residency,
        charge_basis=base.charge_basis,
        amount=round_money(amount),
        is_scholarship_eligible=base.is_scholarship_eligible,
        is_refundable=base.is_refundable,
        is_waivable=base.is_waivable,
        allocation_priority=base.allocation_priority,
        version=version,
        effective_from=effective_from,
        supersedes=base,
    )
    return _insert_schedule(schedule)


@dataclass(frozen=True)
class ResolvedFees:
    rows: tuple
    missing: tuple[str, ...]


def resolve_fees(enrollment, on_date: date) -> ResolvedFees:
    """The version in force on ``on_date`` for every fee type that applies to the
    enrollment (BR-FEE-01, BR-FEE-04), and the required types (D2) with none in force."""
    student = enrollment.student
    has_lab = enrollment.program.has_lab
    groups = defaultdict(list)
    for row in FeeSchedule.objects.filter(program=enrollment.program, term=enrollment.term):
        groups[(row.fee_type, row.residency)].append(row)
    required = set(policy.required_fee_types(has_lab=has_lab, residency=student.residency))
    rows, missing = [], []
    for fee_type in policy.applicable_fee_types(has_lab=has_lab, residency=student.residency):
        residency = (
            student.residency
            if fee_type == policy.TUITION
            else policy.ALLOWED_RESIDENCIES[fee_type][0]
        )
        try:
            rows.append(calc.resolve_fee_version(groups.get((fee_type, residency), ()), on_date))
        except LookupError:
            if fee_type in required:
                missing.append(fee_type)
    return ResolvedFees(rows=tuple(rows), missing=tuple(missing))


# --- invoices ------------------------------------------------------------------------------------


@transaction.atomic
def generate_invoice(*, enrollment, waivers=(), actor: str) -> Invoice:
    """Issue the invoice for an enrollment (ARCHITECTURE §8.4 ``generate_invoice``)."""
    from academics.models import Enrollment

    enrollment = (
        Enrollment.objects.select_for_update()
        .select_related("student", "program", "term")
        .get(pk=enrollment.pk)
    )
    if enrollment.status != Enrollment.Status.ENROLLED:
        raise domain_error(
            "ILLEGAL_TRANSITION",
            f"Enrollment {enrollment.enrollment_id} is {enrollment.status}; an invoice can be "
            "generated only for an ENROLLED enrollment.",
            details={
                "entity": "Enrollment",
                "record_id": enrollment.enrollment_id,
                "status": enrollment.status,
                "required_status": "ENROLLED",
            },
        )
    existing = Invoice.objects.filter(enrollment=enrollment).exclude(lifecycle_status=VOID).first()
    if existing:
        raise domain_error(
            "DUPLICATE_INVOICE",
            f"Enrollment {enrollment.enrollment_id} already has invoice {existing.invoice_id}.",
            rule_id="BR-INV-01",
            details={
                "enrollment_id": enrollment.enrollment_id,
                "existing_invoice_id": existing.invoice_id,
            },
        )

    term, student = enrollment.term, enrollment.student
    pricing_date = term.invoice_date  # BR-FEE-01: versions in force on the term's invoice_date
    resolved = resolve_fees(enrollment, pricing_date)
    if resolved.missing:
        raise domain_error(
            "FEE_SCHEDULE_NOT_FOUND",
            f"No fee schedule version is in force on {pricing_date} for "
            f"{', '.join(resolved.missing)} ({enrollment.program_id}, {term.term_id}, "
            f"{student.residency}).",
            rule_id="BR-FEE-01",
            details={
                "enrollment_id": enrollment.enrollment_id,
                "program_id": enrollment.program_id,
                "term_id": term.term_id,
                "residency": student.residency,
                "on_date": pricing_date.isoformat(),
                "missing_fee_types": list(resolved.missing),
            },
        )
    priced = calc.price_lines(enrollment, resolved.rows)
    try:
        priced = calc.apply_waivers(priced, waivers)
    except calc.WaiverError as exc:
        raise ValidationError({"waivers": exc.errors}) from exc

    issue_date = max(term.invoice_date, enrollment.enrolled_on)  # BR-INV-03
    due_date = term.due_date
    if issue_date > due_date:
        raise ValidationError(
            {"enrollment_id": [f"The issue date {issue_date} is after the due date {due_date}."]}
        )
    check_business_date("issue_date", issue_date)

    invoice = Invoice(
        invoice_id=next_id("invoice", parent=student.student_id, year=issue_date.year),
        student=student,
        enrollment=enrollment,
        term=term,
        issue_date=issue_date,
        due_date=due_date,
        total_amount=dsum(p.net_amount for p in priced),  # BR-INV-02
        lifecycle_status=ISSUED,
        collection_status="UNPAID",
        collection_status_as_of=issue_date,
        currency=policy.CURRENCY,
    )
    invoice.save(force_insert=True)
    for p in priced:
        line = InvoiceLine(
            line_id=next_id("invoice_line", parent=invoice.invoice_id),
            invoice=invoice,
            line_type="FEE",
            fee_schedule_id=p.fee_schedule_id,
            fee_type=p.fee_type,
            description=p.description,
            quantity=p.quantity,
            unit_amount=p.unit_amount,
            gross_amount=p.gross_amount,
            waiver_amount=p.waiver_amount,
            waiver_reason=p.waiver_reason,
            net_amount=p.net_amount,
            is_scholarship_eligible=p.is_scholarship_eligible,
            is_refundable=p.is_refundable,
            is_waivable=p.is_waivable,
            allocation_priority=p.allocation_priority,
            posted_date=issue_date,
        )
        line.save(force_insert=True)
        post_ledger(line)
    _event(
        invoice,
        dimension="LIFECYCLE",
        from_status=None,
        to_status=ISSUED,
        event_date=issue_date,
        reason="CREATED",
        actor=actor,
        source_ref=enrollment.enrollment_id,
    )
    _event(
        invoice,
        dimension="COLLECTION",
        from_status=None,
        to_status="UNPAID",
        event_date=issue_date,
        reason="CREATED",
        actor=actor,
        source_ref=enrollment.enrollment_id,
    )
    # a fully zero invoice (e.g. every charge priced at 0) is ADJUSTED_TO_ZERO at once
    _set_collection_status(invoice, as_of=issue_date, reason="RECALCULATED", actor=actor)
    return invoice


def _latest_event_date(invoice) -> date | None:
    last = invoice.events.order_by("-event_date", "-event_id").first()
    return last.event_date if last else None


@transaction.atomic
def void_invoice(invoice: Invoice, *, void_date: date, actor: str) -> Invoice:
    """ISSUED → VOID (BR-INV-08). Posts a VOID adjustment of minus the remaining net of
    every FEE/LATE_FEE line, its ADJUSTMENT ledger entries and the InvoiceEvents."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    check_transition(
        LIFECYCLE_TRANSITIONS,
        invoice.lifecycle_status,
        VOID,
        entity="Invoice",
        record_id=invoice.invoice_id,
        rule_id="BR-INV-08",
    )
    lines = list(invoice.lines.all())
    amounts = current_amounts(invoice, lines)
    if not can_void(amounts):
        raise domain_error(
            "INVOICE_HAS_ALLOCATIONS",
            f"{invoice.invoice_id} has scholarship or payment applied; it cannot be voided.",
            rule_id="BR-INV-08",
            details={
                "invoice_id": invoice.invoice_id,
                "scholarship_applied": format_money(amounts.scholarship_applied),
                "amount_paid": format_money(amounts.amount_paid),
            },
        )
    check_business_date("void_date", void_date)
    earliest = max(
        [invoice.issue_date, invoice.collection_status_as_of]
        + [line.posted_date for line in lines]
        + [d for d in [_latest_event_date(invoice)] if d]
    )
    if void_date < earliest:
        detail = (
            f"void_date must be on or after the invoice issue date ({invoice.issue_date})."
            if earliest == invoice.issue_date
            else f"void_date must be on or after {earliest}, the invoice's latest posting "
            "or status date."
        )
        raise ValidationError({"void_date": [detail]})

    for adj in calc.void_adjustments(lines):
        line = InvoiceLine(
            line_id=next_id("invoice_line", parent=invoice.invoice_id),
            invoice=invoice,
            line_type="ADJUSTMENT",
            fee_schedule=None,
            fee_type=adj.fee_type,
            description=f"Void of {adj.adjusts_line_id}",
            quantity=1,
            unit_amount=adj.amount,
            gross_amount=adj.amount,
            waiver_amount=ZERO,
            waiver_reason=None,
            net_amount=adj.amount,
            adjusts_line_id=adj.adjusts_line_id,
            adjustment_reason="VOID",
            is_scholarship_eligible=False,
            is_refundable=False,
            is_waivable=False,
            allocation_priority=adj.allocation_priority,
            posted_date=void_date,
        )
        line.save(force_insert=True)
        post_ledger(line)
    _event(
        invoice,
        dimension="LIFECYCLE",
        from_status=invoice.lifecycle_status,
        to_status=VOID,
        event_date=void_date,
        reason="VOIDED",
        actor=actor,
    )
    invoice.lifecycle_status = VOID
    invoice.save(update_fields=["lifecycle_status"])
    _set_collection_status(invoice, as_of=void_date, reason="INVOICE_VOIDED", actor=actor)
    return invoice


# --- late fees (BR-LATE-01) ----------------------------------------------------------------------


@dataclass(frozen=True)
class LateFeeResult:
    invoice_id: str
    outstanding: object
    late_fee: object
    posted_date: date
    line_id: str | None


def late_fee_candidates(as_of: date):
    """ISSUED invoices with no late fee yet whose grace period ended before ``as_of``."""
    invoices = (
        Invoice.objects.filter(lifecycle_status=ISSUED)
        .exclude(lines__line_type="LATE_FEE")
        .select_related("term")
        .order_by("invoice_id")
    )
    for invoice in invoices:
        posted = calc.late_fee_date(invoice.due_date, invoice.term.late_fee_grace_days)
        if posted <= as_of:
            yield invoice, posted


def _assess_one(invoice: Invoice, posted: date, *, dry_run: bool, actor: str):
    grace = invoice.term.late_fee_grace_days
    cutoff = posted - timedelta(days=1)  # the last grace day: due_date + grace_days
    outstanding = current_amounts(invoice, as_of=cutoff).outstanding
    rules = policy.LateFeePolicy(grace_days=grace)
    fee = calc.late_fee(outstanding, invoice.due_date, posted, rules)
    if fee <= 0:
        return None
    line_id = None
    if not dry_run:
        line = InvoiceLine(
            line_id=next_id("invoice_line", parent=invoice.invoice_id),
            invoice=invoice,
            line_type="LATE_FEE",
            fee_schedule=None,
            fee_type=policy.LATE_FEE,
            description=f"Late fee: 2% of {format_money(outstanding)} outstanding",
            quantity=1,
            unit_amount=fee,
            gross_amount=fee,
            waiver_amount=ZERO,
            waiver_reason=None,
            net_amount=fee,
            is_scholarship_eligible=False,
            is_refundable=False,
            is_waivable=False,
            allocation_priority=policy.LATE_FEE_PRIORITY,
            posted_date=posted,
        )
        line.save(force_insert=True)
        post_ledger(line)
        _set_collection_status(
            invoice,
            as_of=max(posted, invoice.collection_status_as_of),
            reason="LATE_FEE_POSTED",
            actor=actor,
            source_ref=line.line_id,
        )
        line_id = line.line_id
    return LateFeeResult(invoice.invoice_id, outstanding, fee, posted, line_id)


def assess_late_fees(*, as_of: date, dry_run: bool, actor: str) -> dict:
    """Post at most one late fee per ISSUED invoice (one transaction per invoice).

    The fee is dated the day after ``due_date + grace_days`` and computed on what was
    outstanding at the end of that grace day, so the result does not depend on when the
    batch runs. ``dry_run`` reports the same result and writes nothing.
    """
    check_business_date("as_of_date", as_of)
    results, examined = [], 0
    for invoice, posted in list(late_fee_candidates(as_of)):
        examined += 1
        with transaction.atomic():
            locked = Invoice.objects.select_for_update().select_related("term").get(pk=invoice.pk)
            if (
                locked.lifecycle_status != ISSUED
                or locked.lines.filter(line_type="LATE_FEE").exists()
            ):
                continue
            result = _assess_one(locked, posted, dry_run=dry_run, actor=actor)
            if dry_run:
                transaction.set_rollback(True)  # belt and braces: nothing is written
        if result:
            results.append(result)
    return {
        "as_of_date": as_of,
        "dry_run": dry_run,
        "examined": examined,
        "assessed_count": len(results),
        "total_late_fees": dsum(r.late_fee for r in results),
        "assessed": results,
    }


# --- collection status refresh (BR-INV-07) -------------------------------------------------------


def refresh_collection_status(*, as_of: date, actor: str) -> dict:
    """Re-derive BR-INV-06 at ``as_of`` for every invoice that is not VOID.

    Invoices whose status is already derived for a later date are skipped (a status is
    never moved back in time). Each invoice is its own transaction.
    """
    check_business_date("as_of_date", as_of)
    examined, skipped, changes = 0, 0, []
    for invoice_id in (
        Invoice.objects.exclude(lifecycle_status=VOID)
        .order_by("invoice_id")
        .values_list("invoice_id", flat=True)
    ):
        with transaction.atomic():
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
            if invoice.collection_status_as_of > as_of:
                skipped += 1
                continue
            examined += 1
            target = calc.collection_status(
                current_amounts(invoice), due_date=invoice.due_date, as_of=as_of
            )
            reason = "DUE_DATE_PASSED" if target == "OVERDUE" else "RECALCULATED"
            old, new = _set_collection_status(invoice, as_of=as_of, reason=reason, actor=actor)
            if old != new:
                changes.append({"invoice_id": invoice_id, "from_status": old, "to_status": new})
    return {
        "as_of_date": as_of,
        "examined": examined,
        "changed": len(changes),
        "unchanged": examined - len(changes),
        "skipped_later_as_of": skipped,
        "changes": changes,
    }


# --- student account views (read-only) -----------------------------------------------------------


def student_balance(student, as_of: date | None) -> dict:
    """BR-BAL-01 ledger balance, its breakdown, and the BR-BAL-02 identity check.

    Payment and refund components are 0 until Phase 4 adds their rows.
    """
    entries = list(student.ledger_entries.all())
    included = [e for e in entries if as_of is None or e.entry_date <= as_of]
    by_type = defaultdict(list)
    for e in included:
        by_type[e.entry_type].append(e.amount)
    balance = calc.ledger_balance(entries, as_of)
    outstanding = dsum(
        current_amounts(inv, as_of=as_of).outstanding
        for inv in student.invoices.prefetch_related("lines")
        if as_of is None or inv.issue_date <= as_of
    )
    unallocated_credit = ZERO  # Σ payments − Σ net payment allocations − Σ PROCESSED refunds
    expected = round_money(outstanding - unallocated_credit)
    return {
        "student_id": student.student_id,
        "as_of": as_of,
        "ledger_balance": balance,
        "breakdown": {
            "charges": dsum(by_type["CHARGE"]),
            "late_fees": dsum(by_type["LATE_FEE"]),
            "adjustments": dsum(by_type["ADJUSTMENT"]),
            "scholarship_credits": dsum(by_type["SCHOLARSHIP_CREDIT"]),
            "scholarship_reversals": dsum(by_type["SCHOLARSHIP_REVERSAL"]),
            "payments": ZERO,
            "payments_by_payer": {p: ZERO for p in calc.PAYER_TYPES},
            "refunds": ZERO,
        },
        "invoice_outstanding": outstanding,
        "unallocated_credit": unallocated_credit,
        "identity": {
            "rule_id": "BR-BAL-02",
            "holds": balance == expected,
            "ledger_balance": balance,
            "invoice_outstanding_minus_unallocated_credit": expected,
        },
    }
