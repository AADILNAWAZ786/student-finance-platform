"""Payments services: the only code that writes payments, payment allocations, refunds and
refund events (and, through billing, their PAYMENT / REFUND ledger entries). Each public
write is one atomic transaction (ARCHITECTURE §8.4). SQLite runs every transaction as
``BEGIN IMMEDIATE`` (§8.2), so all the reads a check depends on happen after the write lock
is taken: two concurrent writers are serialized, never interleaved.

Rules enforced here (DATABASE_DESIGN.md §3 payments SV rules, ARCHITECTURE §6.3–6.4):
  Payment      amount > 0 and external_reference unique (BR-PAY-01, 409 DUPLICATE_PAYMENT);
               received_date a 2026 business date on or after the student's first invoice
               issue date (400, decision D-P9); one PAYMENT ledger entry (BR-PAY-02)
  Allocation   FEE / LATE_FEE lines only; same student (422 ALLOCATION_STUDENT_MISMATCH);
               Σ ≤ payment available (BR-ALC-01); ≤ line open (BR-ALC-02); AUTO order
               (BR-ALC-03); remainder stays unallocated credit (BR-ALC-04, D-P13); never
               edited, released by DEALLOCATE rows (BR-ALC-05); scholarship precedence
               (BR-ALC-06); at most 99 rows per payment (409 ALLOCATION_LIMIT_REACHED,
               D-P13a), checked before anything is written
  Refund       caps BR-REF-01/02 at request, approval and processing (D-P7, D-P8) with
               pending refunds reserving their amount; lifecycle BR-REF-03; payer copied
               from the source payment (BR-REF-04); REFUND ledger entry only on PROCESSED;
               ``calculation`` is the request-time snapshot and is never rewritten
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from academics.calculators import withdrawal_refund_pct
from billing import calculators as billing_calc
from billing import services as billing
from billing.models import Invoice, InvoiceLine, LedgerEntry
from core.exceptions import domain_error
from core.ids import next_id
from core.lifecycle import check_transition
from core.money import ZERO, dsum, format_money, round_money
from payments import calculators as calc
from payments import policy
from payments.lifecycle import REFUND_TRANSITIONS
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent

check_business_date = billing.check_business_date
# D-W6: a WITHDRAWAL / CANCELLATION refund names an enrollment that has had that exit
REFUND_ENROLLMENT_STATUS = {policy.WITHDRAWAL: "WITHDRAWN", policy.CANCELLATION: "CANCELLED"}


class PaymentReleaseFailed(RuntimeError):
    """BR-ALC-06: a line's payment allocations hold less than a scholarship needs released.

    The scholarship's plan is computed from the same allocations, so this is an integrity
    problem; it is raised inside the caller's transaction, which rolls back completely.
    """


# --- providers registered with billing (decision D-S1 / D-P4) -------------------------------------


def invoice_allocations(invoice) -> list[PaymentAllocation]:
    """Payment allocations on ``invoice``'s lines (``payer_type`` read from the payment)."""
    return list(
        PaymentAllocation.objects.filter(invoice_line__invoice_id=invoice.pk)
        .select_related("payment")
        .order_by("allocation_id")
    )


def student_unallocated_credit(student, as_of: date | None = None) -> Decimal:
    """BR-BAL-02 unallocated credit from the payment rows (not from the ledger)."""
    return calc.unallocated_credit(
        Payment.objects.filter(student_id=student.pk),
        PaymentAllocation.objects.filter(payment__student_id=student.pk),
        Refund.objects.filter(student_id=student.pk),
        as_of,
    )


def student_refunds(student) -> list[Refund]:
    return list(Refund.objects.filter(student_id=student.pk).order_by("refund_id"))


# --- payments (BR-PAY-01..03) ---------------------------------------------------------------------


def _duplicate(external_reference: str, existing_id: str | None):
    return domain_error(
        "DUPLICATE_PAYMENT",
        f"external_reference {external_reference!r} is already recorded"
        + (f" as {existing_id}." if existing_id else "."),
        rule_id="BR-PAY-01",
        details={"external_reference": external_reference, "existing_payment_id": existing_id},
    )


def first_invoice_issue_date(student) -> date | None:
    return (
        Invoice.objects.filter(student_id=student.pk)
        .order_by("issue_date")
        .values_list("issue_date", flat=True)
        .first()
    )


@dataclass
class AllocationResult:
    """What an allocation call did. ``outcome`` is ALLOCATED, NO_OPEN_LINES or
    NO_FUNDS_AVAILABLE; the last two write nothing and are not errors (D-P13)."""

    payment: Payment
    mode: str
    outcome: str
    allocated_on: date
    available_before: Decimal
    allocations: list

    @property
    def allocated(self) -> Decimal:
        return dsum(a.amount for a in self.allocations)

    @property
    def unallocated_after(self) -> Decimal:
        return round_money(self.available_before - self.allocated)


@transaction.atomic
def record_payment(
    *,
    student,
    amount,
    received_date: date,
    method: str,
    payer_type: str,
    external_reference: str,
    payer_name: str | None = None,
    auto_allocate: bool = False,
    actor: str,
) -> tuple[Payment, AllocationResult | None]:
    """Record a payment and its PAYMENT ledger entry, then (optionally) AUTO-allocate it in
    the same transaction (ARCHITECTURE §8.4)."""
    errors: dict[str, list[str]] = {}
    if amount <= 0:
        errors["amount"] = ["Must be greater than 0."]
    if method not in policy.METHODS:
        errors["method"] = [f"Must be one of {', '.join(policy.METHODS)}."]
    if payer_type not in policy.PAYER_TYPES:
        errors["payer_type"] = [f"Must be one of {', '.join(policy.PAYER_TYPES)}."]
    if not external_reference:
        errors["external_reference"] = ["Required."]
    if errors:
        raise ValidationError(errors)
    check_business_date("received_date", received_date)

    existing = Payment.objects.filter(external_reference=external_reference).first()
    if existing:  # BR-PAY-01: resubmitting is idempotent and answered with 409
        raise _duplicate(external_reference, existing.payment_id)
    first_issue = first_invoice_issue_date(student)
    if first_issue is None:  # D-P9: a DATABASE_DESIGN SV rule, not BR-PAY-01
        raise ValidationError(
            {
                "received_date": [
                    f"Student {student.pk} has no invoice; a payment cannot be received before "
                    "billing."
                ]
            }
        )
    if received_date < first_issue:
        raise ValidationError(
            {
                "received_date": [
                    "Must be on or after the student's first invoice issue date "
                    f"({first_issue.isoformat()})."
                ]
            }
        )

    payment = Payment(
        payment_id=next_id("payment", year=received_date.year),
        student_id=student.pk,
        amount=round_money(amount),
        currency=policy.CURRENCY,
        received_date=received_date,
        method=method,
        payer_type=payer_type,
        payer_name=payer_name or None,
        external_reference=external_reference,
        status=policy.POSTED,
    )
    try:
        with transaction.atomic():
            payment.save(force_insert=True)
    except IntegrityError as exc:  # the DB unique is the last line of defence
        if "external_reference" not in str(exc):
            raise
        raise _duplicate(external_reference, None) from exc
    billing.post_payment_ledger(payment)  # BR-PAY-02: credited in full when received
    result = None
    if auto_allocate:
        result = _allocate_auto(payment, allocated_on=received_date, actor=actor)
    return payment, result


# --- allocation (BR-ALC-01..05) -------------------------------------------------------------------


@dataclass(frozen=True)
class OpenLine:
    line_id: str
    invoice: Invoice
    due_date: date
    allocation_priority: int
    open_amount: Decimal


def available(payment: Payment) -> Decimal:
    """``amount − Σ net allocations − Σ refunds not REJECTED`` for one payment."""
    return calc.payment_available(
        payment.amount, list(payment.allocations.all()), list(payment.refunds.all())
    )


def _open_amounts(invoice) -> dict:
    """Current BR-ALC-02 open amount of every FEE / LATE_FEE line of ``invoice``."""
    scholarship, payments = billing.allocations_for(invoice)
    return billing_calc.line_open_amounts(list(invoice.lines.all()), scholarship, payments)


def _student_open_lines(student, *, allocated_on: date) -> list[OpenLine]:
    """FEE / LATE_FEE lines posted on or before ``allocated_on`` on the student's ISSUED or
    CANCELLED invoices (never VOID, D-P12), with their current open amount."""
    out = []
    invoices = Invoice.objects.filter(
        student_id=student.pk, lifecycle_status__in=policy.AUTO_INVOICE_LIFECYCLES
    ).order_by("invoice_id")
    for invoice in invoices:
        opens = _open_amounts(invoice)
        for line in invoice.lines.all():
            if line.line_type in policy.ALLOCATABLE_LINE_TYPES and line.posted_date <= allocated_on:
                out.append(
                    OpenLine(
                        line_id=line.line_id,
                        invoice=invoice,
                        due_date=invoice.due_date,
                        allocation_priority=line.allocation_priority,
                        open_amount=opens[line.line_id],
                    )
                )
    return out


def _check_allocation_date(payment: Payment, allocated_on: date, field: str = "allocated_on"):
    check_business_date(field, allocated_on)
    if allocated_on < payment.received_date:
        raise ValidationError(
            {
                field: [
                    "Must be on or after the payment's received_date "
                    f"({payment.received_date.isoformat()})."
                ]
            }
        )


def check_capacity(new_rows: Counter | dict) -> None:
    """D-P13a: ``ALLOC-{payment_id}-NN`` allows 99 rows per payment, deallocations included.

    ``new_rows`` = {payment_id: rows this transaction is about to write}. Counts every
    existing PaymentAllocation row (ALLOCATE and DEALLOCATE) and raises before anything is
    written, so the ID formatter can never overflow halfway through a transaction.
    """
    limit = policy.MAX_ALLOCATIONS_PER_PAYMENT
    for payment_id in sorted(new_rows):
        planned = new_rows[payment_id]
        if planned <= 0:
            continue
        existing = PaymentAllocation.objects.filter(payment_id=payment_id).count()
        if existing + planned > limit:
            raise domain_error(
                "ALLOCATION_LIMIT_REACHED",
                f"{payment_id} already has {existing} allocation rows; {planned} more would "
                f"exceed the {limit} its allocation ID format allows.",
                details={
                    "payment_id": payment_id,
                    "existing_rows": existing,
                    "requested_rows": planned,
                    "limit": limit,
                },
            )


def _write_allocation(
    payment, line_id: str, amount: Decimal, *, rule: str, allocated_on: date, reverses=None
):
    deallocate = reverses is not None
    row = PaymentAllocation(
        allocation_id=next_id("payment_allocation", parent=payment.payment_id),
        payment=payment,
        invoice_line_id=line_id,
        allocation_type=policy.DEALLOCATE if deallocate else policy.ALLOCATE,
        amount=-round_money(amount) if deallocate else round_money(amount),
        reverses=reverses,
        rule_applied=rule,
        allocated_on=allocated_on,
    )
    row.save(force_insert=True)
    return row


def _refresh_invoices(invoices, *, as_of: date, reason: str, actor: str, source_ref: str):
    """BR-INV-07 for every invoice whose lines changed (one COLLECTION event if it moved)."""
    for invoice in sorted({inv.pk: inv for inv in invoices}.values(), key=lambda i: i.pk):
        billing.recalculate_collection_status(
            invoice, as_of=as_of, reason=reason, actor=actor, source_ref=source_ref
        )


def _result(payment, mode, outcome, allocated_on, available_before, rows):
    return AllocationResult(
        payment=payment,
        mode=mode,
        outcome=outcome,
        allocated_on=allocated_on,
        available_before=available_before,
        allocations=rows,
    )


def _allocate_auto(payment: Payment, *, allocated_on: date, actor: str) -> AllocationResult:
    """BR-ALC-03/04 (D-P13): allocate everything available, oldest due date first. No open
    lines → nothing written, the money stays unallocated credit (NO_OPEN_LINES). Nothing
    available → nothing written (NO_FUNDS_AVAILABLE). Neither is an error."""
    _check_allocation_date(payment, allocated_on)
    funds = available(payment)
    if funds <= 0:
        return _result(payment, "AUTO", policy.NO_FUNDS_AVAILABLE, allocated_on, funds, [])
    lines = [x for x in _student_open_lines(payment.student, allocated_on=allocated_on)]
    lines = [x for x in lines if x.open_amount > 0]
    if not lines:
        return _result(payment, "AUTO", policy.NO_OPEN_LINES, allocated_on, funds, [])
    plan = calc.plan_allocation(funds, lines)
    check_capacity({payment.payment_id: len(plan)})
    invoice_of = {x.line_id: x.invoice for x in lines}
    rows = [
        _write_allocation(
            payment, p.line_id, p.amount, rule=policy.AUTO_OLDEST_FIRST, allocated_on=allocated_on
        )
        for p in plan
    ]
    _refresh_invoices(
        [invoice_of[p.line_id] for p in plan],
        as_of=allocated_on,
        reason="PAYMENT_ALLOCATED",
        actor=actor,
        source_ref=payment.payment_id,
    )
    return _result(payment, "AUTO", policy.ALLOCATED, allocated_on, funds, rows)


def _allocate_manual(payment: Payment, items, *, allocated_on: date, actor: str):
    """MANUAL: ``items`` = [(InvoiceLine, amount)], all-or-nothing."""
    _check_allocation_date(payment, allocated_on)
    problems = []
    if not items:
        problems.append("At least one line is required.")
    if len(items) > policy.MAX_ALLOCATIONS_PER_PAYMENT:
        problems.append(f"At most {policy.MAX_ALLOCATIONS_PER_PAYMENT} lines per request.")
    counts = Counter(line.line_id for line, _ in items)
    for line_id in sorted(k for k, n in counts.items() if n > 1):
        problems.append(f"{line_id} appears more than once.")
    for line, amount in items:
        if amount <= 0:
            problems.append(f"{line.line_id}: amount must be greater than 0.")
        if line.line_type not in policy.ALLOCATABLE_LINE_TYPES:
            problems.append(
                f"{line.line_id} is an {line.line_type} line; payments are allocated only to "
                "FEE and LATE_FEE lines."
            )
    if problems:
        raise ValidationError({"lines": problems})

    lines = [line for line, _ in items]
    foreign = sorted(
        line.line_id for line in lines if line.invoice.student_id != payment.student_id
    )
    if foreign:
        raise domain_error(
            "ALLOCATION_STUDENT_MISMATCH",
            f"{payment.payment_id} belongs to {payment.student_id}; {', '.join(foreign)} "
            "belong(s) to another student.",
            details={
                "payment_id": payment.payment_id,
                "student_id": payment.student_id,
                "invoice_line_ids": foreign,
            },
        )
    late = [line for line in lines if line.posted_date > allocated_on]
    if late:
        latest = max(line.posted_date for line in late)
        raise ValidationError(
            {
                "allocated_on": [
                    f"Must be on or after {latest.isoformat()}, the posted date of "
                    f"{', '.join(sorted(line.line_id for line in late))}."
                ]
            }
        )

    total = dsum(amount for _, amount in items)
    funds = available(payment)
    if total > funds:  # BR-ALC-01
        raise domain_error(
            "ALLOCATION_EXCEEDS_PAYMENT",
            f"Allocating {format_money(total)} exceeds the {format_money(funds)} available on "
            f"{payment.payment_id}.",
            rule_id="BR-ALC-01",
            details={
                "payment_id": payment.payment_id,
                "requested": format_money(total),
                "available": format_money(funds),
            },
        )
    opens: dict = {}
    for invoice in {line.invoice.pk: line.invoice for line in lines}.values():
        opens.update(_open_amounts(invoice))
    for line, amount in items:  # BR-ALC-02
        if amount > opens[line.line_id]:
            raise domain_error(
                "ALLOCATION_EXCEEDS_LINE_OPEN",
                f"Allocating {format_money(amount)} to {line.line_id} exceeds its open amount "
                f"{format_money(opens[line.line_id])}.",
                rule_id="BR-ALC-02",
                details={
                    "payment_id": payment.payment_id,
                    "invoice_line_id": line.line_id,
                    "amount": format_money(amount),
                    "line_open": format_money(opens[line.line_id]),
                },
            )
    check_capacity({payment.payment_id: len(items)})
    rows = [
        _write_allocation(
            payment, line.line_id, amount, rule=policy.MANUAL, allocated_on=allocated_on
        )
        for line, amount in items
    ]
    _refresh_invoices(
        [line.invoice for line in lines],
        as_of=allocated_on,
        reason="PAYMENT_ALLOCATED",
        actor=actor,
        source_ref=payment.payment_id,
    )
    return _result(payment, "MANUAL", policy.ALLOCATED, allocated_on, funds, rows)


@transaction.atomic
def allocate_payment(
    payment: Payment, *, mode: str, lines=None, allocated_on: date | None = None, actor: str
) -> AllocationResult:
    """``mode="AUTO"`` (``allocated_on`` defaults to the payment's received_date, D-P6) or
    ``mode="MANUAL"`` with ``lines=[(InvoiceLine, amount)]`` and a required ``allocated_on``.
    """
    payment = Payment.objects.select_related("student").get(pk=payment.pk)
    if mode == "AUTO":
        if lines:
            raise ValidationError({"lines": ["Must be omitted when mode is AUTO."]})
        return _allocate_auto(
            payment, allocated_on=allocated_on or payment.received_date, actor=actor
        )
    if mode == "MANUAL":
        if allocated_on is None:
            raise ValidationError({"allocated_on": ["Required when mode is MANUAL."]})
        items = [
            (InvoiceLine.objects.select_related("invoice").get(pk=line.pk), round_money(amount))
            for line, amount in (lines or [])
        ]
        return _allocate_manual(payment, items, allocated_on=allocated_on, actor=actor)
    raise ValidationError({"mode": ["Must be AUTO or MANUAL."]})


# --- releases (BR-ALC-05 / BR-ALC-06; Phase 5 adds WITHDRAWAL_ / CANCELLATION_RELEASE) ----------


def _write_releases(plan, *, rule: str, on_date: date, field: str) -> list[PaymentAllocation]:
    """DEALLOCATE rows for ``plan`` = [ReleaseCalc(allocation_id, amount > 0)]. Validates
    every row, then the 99-row capacity of every payment touched, before writing anything."""
    if rule not in policy.RELEASE_RULES:
        raise ValidationError(
            {"rule_applied": [f"Must be one of {', '.join(policy.RELEASE_RULES)}."]}
        )
    check_business_date(field, on_date)
    originals = {
        a.pk: a
        for a in PaymentAllocation.objects.select_related("payment").filter(
            pk__in=[p.allocation_id for p in plan]
        )
    }
    released_so_far = {
        pk: -dsum(PaymentAllocation.objects.filter(reverses_id=pk).values_list("amount", flat=True))
        for pk in originals
    }
    pending: Counter = Counter()
    for p in plan:
        original = originals[p.allocation_id]
        if original.allocation_type != policy.ALLOCATE:
            raise ValidationError({"allocation": ["Only an ALLOCATE row can be released."]})
        if p.amount <= 0:
            raise ValidationError({"amount": ["Must be greater than 0."]})
        released_so_far[original.pk] += p.amount
        if released_so_far[original.pk] > original.amount:  # Σ deallocations ≤ original
            raise ValidationError(
                {
                    "amount": [
                        f"Releases of {original.pk} would total "
                        f"{format_money(released_so_far[original.pk])}, above its "
                        f"{format_money(original.amount)}."
                    ]
                }
            )
        if on_date < original.allocated_on:
            raise ValidationError(
                {
                    field: [
                        f"Must be on or after {original.allocated_on.isoformat()}, the date of "
                        f"payment allocation {original.pk}, which it would release."
                    ]
                }
            )
        pending[original.payment_id] += 1
    check_capacity(pending)
    return [
        _write_allocation(
            originals[p.allocation_id].payment,
            originals[p.allocation_id].invoice_line_id,
            p.amount,
            rule=rule,
            allocated_on=on_date,
            reverses=originals[p.allocation_id],
        )
        for p in plan
    ]


@transaction.atomic
def deallocate(
    allocation, *, amount, rule_applied: str, on_date: date, actor: str, source_ref=None
) -> PaymentAllocation:
    """Release ``amount`` (> 0) of one ALLOCATE row (BR-ALC-05): append a DEALLOCATE row and
    refresh the invoice's collection status. No endpoint (D-P10); BR-ALC-06 uses the same
    building block and Phase 5 calls this for WITHDRAWAL_ / CANCELLATION_RELEASE."""
    plan = [calc.ReleaseCalc(allocation.pk, round_money(amount))]
    (row,) = _write_releases(plan, rule=rule_applied, on_date=on_date, field="on_date")
    invoice = InvoiceLine.objects.select_related("invoice").get(pk=row.invoice_line_id).invoice
    _refresh_invoices(
        [invoice],
        as_of=on_date,
        reason="PAYMENT_DEALLOCATED",
        actor=actor,
        source_ref=source_ref or row.allocation_id,
    )
    return row


def release_for_scholarship(*, invoice, releases, on_date: date, actor: str, source_ref) -> list:
    """BR-ALC-06 handler (registered with billing): free ``releases[line_id]`` of payment
    money on each line, last-in-first-out across the line's payments, as DEALLOCATE rows with
    ``rule_applied = SCHOLARSHIP_PRECEDENCE``. Freed money becomes unallocated credit; it is
    not re-allocated (D-P11).

    Runs inside ``apply_scholarship``'s transaction and leaves the invoice's collection status
    to it (the status is recalculated once, after the scholarship allocations are written).
    """
    plan = []
    for line_id in sorted(releases):
        amount = round_money(releases[line_id])
        if amount <= 0:
            continue
        rows = list(PaymentAllocation.objects.filter(invoice_line_id=line_id))
        try:
            plan += calc.plan_release_lifo(rows, amount)
        except calc.InsufficientAllocations as exc:
            raise PaymentReleaseFailed(
                f"{source_ref} needs {exc.needed} of payment money released on {line_id}, "
                f"but its payment allocations hold {exc.held}"
            ) from exc
    if not plan:
        return []
    return _write_releases(
        plan, rule=policy.SCHOLARSHIP_PRECEDENCE, on_date=on_date, field="allocated_on"
    )


def release_for_adjustment(
    *, deallocations, rule: str, on_date: date, date_field: str, actor: str, source_ref=None
) -> list:
    """BR-WDR-03 step 2 (registered with billing as the payment_release adjustment handler):
    write DEALLOCATE rows for ``deallocations`` = [(allocation_id, amount > 0)], the LIFO
    plan billing computed, with ``rule`` WITHDRAWAL_RELEASE or CANCELLATION_RELEASE.

    Every row is validated (Σ releases ≤ original, date ≥ the allocation's date) and the
    99-row capacity of every payment touched is checked before anything is written. Freed
    money becomes unallocated credit (refundable, BR-REF). The collection status is left to
    billing, which recalculates it once after the whole withdrawal or cancellation.
    """
    if rule not in (policy.WITHDRAWAL_RELEASE, policy.CANCELLATION_RELEASE):
        raise ValidationError(
            {"rule_applied": ["Must be WITHDRAWAL_RELEASE or CANCELLATION_RELEASE."]}
        )
    plan = [
        calc.ReleaseCalc(allocation_id, round_money(amount))
        for allocation_id, amount in deallocations
    ]
    if not plan:
        return []
    return _write_releases(plan, rule=rule, on_date=on_date, field=date_field)


# --- refunds (BR-REF-01..04) ----------------------------------------------------------------------


def refund_caps(payment: Payment, *, exclude_refund_id: str | None = None) -> calc.RefundCaps:
    """BR-REF-01/02 caps for a refund from ``payment``, from rows read now (inside the
    caller's transaction). ``exclude_refund_id`` is the refund being approved or processed."""
    student_id = payment.student_id
    student_refunds = list(Refund.objects.filter(student_id=student_id))
    ledger = billing_calc.ledger_balance(LedgerEntry.objects.filter(student_id=student_id), None)
    return calc.refund_caps(
        payment_amounts=list(
            Payment.objects.filter(student_id=student_id).values_list("amount", flat=True)
        ),
        student_refunds=student_refunds,
        ledger_balance=ledger,
        source_amount=payment.amount,
        source_allocations=list(PaymentAllocation.objects.filter(payment_id=payment.pk)),
        source_refunds=[r for r in student_refunds if r.payment_id == payment.pk],
        exclude_refund_id=exclude_refund_id,
    )


def _check_caps(amount: Decimal, caps: calc.RefundCaps, payment, refund_id=None) -> None:
    details = {
        **({"refund_id": refund_id} if refund_id else {}),
        "payment_id": payment.payment_id,
        "amount": format_money(amount),
        **{k: format_money(v) for k, v in caps.as_dict().items()},
    }
    if amount > caps.net_paid:
        raise domain_error(
            "REFUND_EXCEEDS_NET_PAID",
            f"Refund {format_money(amount)} exceeds the student's net paid "
            f"{format_money(caps.net_paid)} (payments less refunds not rejected).",
            rule_id="BR-REF-01",
            details=details,
        )
    if amount > caps.credit_available:
        raise domain_error(
            "REFUND_EXCEEDS_CREDIT_BALANCE",
            f"Refund {format_money(amount)} exceeds the credit available for refunds "
            f"{format_money(caps.credit_available)} (credit balance "
            f"{format_money(caps.credit_balance)} less {format_money(caps.pending_refunds)} "
            "reserved by pending refunds).",
            rule_id="BR-REF-02",
            details=details,
        )
    if amount > caps.payment_available:
        raise domain_error(
            "REFUND_EXCEEDS_PAYMENT_AVAILABLE",
            f"Refund {format_money(amount)} exceeds the {format_money(caps.payment_available)} "
            f"available on {payment.payment_id}.",
            rule_id="BR-REF-02",
            details=details,
        )


def _snapshot(caps: calc.RefundCaps, schedule_pct) -> dict:
    """Request-time audit snapshot (DATABASE_DESIGN Refund.calculation): the four design keys
    plus the reservation figures that explain ``credit_balance``. Money as strings."""
    return {
        "net_paid": format_money(caps.net_paid),
        "credit_balance": format_money(caps.credit_balance),
        "payment_available": format_money(caps.payment_available),
        "schedule_pct": None if schedule_pct is None else str(schedule_pct),
        "pending_refunds": format_money(caps.pending_refunds),
        "credit_available": format_money(caps.credit_available),
        "max_refundable": format_money(caps.max_refundable),
    }


def _refund_event(refund, *, from_status, to_status, event_date, reason, actor, source_ref=None):
    return RefundEvent.objects.create(
        event_id=next_id("status_event", entity="REF", year=event_date.year),
        refund=refund,
        from_status=from_status,
        to_status=to_status,
        event_date=event_date,
        reason=reason,
        actor=actor,
        source_ref=source_ref,
    )


def _check_status_date(refund: Refund, field: str, value: date) -> None:
    """A lifecycle date is a 2026 business date, on or after the refund's latest date."""
    check_business_date(field, value)
    last_event = refund.events.order_by("-event_date").values_list("event_date", flat=True).first()
    earliest = max(d for d in (refund.requested_date, last_event) if d is not None)
    if value < earliest:
        raise ValidationError(
            {
                field: [
                    f"Must be on or after {earliest.isoformat()}, the refund's latest status date."
                ]
            }
        )


@transaction.atomic
def request_refund(
    *,
    payment,
    amount,
    requested_date: date,
    reason: str = policy.OVERPAYMENT,
    enrollment=None,
    schedule_pct=None,
    actor: str,
) -> Refund:
    """∅ → REQUESTED. The caps are checked now, with every other pending refund reserving its
    amount, and saved as the request-time ``calculation`` snapshot (never rewritten)."""
    payment = Payment.objects.get(pk=payment.pk)
    errors: dict[str, list[str]] = {}
    if amount <= 0:
        errors["amount"] = ["Must be greater than 0."]
    if reason not in policy.REFUND_REASONS:
        errors["reason"] = [f"Must be one of {', '.join(policy.REFUND_REASONS)}."]
    elif reason in policy.ENROLLMENT_REFUND_REASONS and enrollment is None:
        errors["enrollment"] = [f"Required when reason is {reason}."]
    elif reason not in policy.ENROLLMENT_REFUND_REASONS and enrollment is not None:
        errors["enrollment"] = [f"Must be omitted when reason is {reason}."]
    if enrollment is not None and enrollment.student_id != payment.student_id:
        errors["enrollment"] = [f"{enrollment.pk} belongs to another student."]
    elif enrollment is not None and reason in REFUND_ENROLLMENT_STATUS:
        needed = REFUND_ENROLLMENT_STATUS[reason]
        if enrollment.status != needed:  # D-W6: the refund follows that exit
            errors["enrollment"] = [
                f"{enrollment.pk} is {enrollment.status}; a {reason} refund needs a {needed} "
                "enrollment."
            ]
    if errors:
        raise ValidationError(errors)
    if reason == policy.WITHDRAWAL and schedule_pct is None:  # audit: the BR-WDR-02 schedule
        schedule_pct = withdrawal_refund_pct(enrollment.term, enrollment.withdrawal_date)
    check_business_date("requested_date", requested_date)
    if requested_date < payment.received_date:
        raise ValidationError(
            {
                "requested_date": [
                    "Must be on or after the payment's received_date "
                    f"({payment.received_date.isoformat()})."
                ]
            }
        )
    amount = round_money(amount)
    caps = refund_caps(payment)
    _check_caps(amount, caps, payment)
    refund = Refund(
        refund_id=next_id("refund", year=requested_date.year),
        student_id=payment.student_id,
        payment=payment,
        payer_type=payment.payer_type,  # BR-REF-04: back to the payer
        enrollment=enrollment,
        reason=reason,
        amount=amount,
        requested_date=requested_date,
        processed_date=None,
        status=policy.REQUESTED,
        calculation=_snapshot(caps, schedule_pct),
    )
    refund.save(force_insert=True)
    _refund_event(
        refund,
        from_status=None,
        to_status=policy.REQUESTED,
        event_date=requested_date,
        reason=policy.REQUESTED,
        actor=actor,
        source_ref=payment.payment_id,
    )
    return refund


def _locked_refund(refund) -> Refund:
    return Refund.objects.select_related("payment").get(pk=refund.pk)


def _transition(refund: Refund, target: str) -> None:
    check_transition(
        REFUND_TRANSITIONS,
        refund.status,
        target,
        entity="Refund",
        record_id=refund.refund_id,
        rule_id="BR-REF-03",
    )


@transaction.atomic
def approve_refund(refund, *, on_date: date, actor: str) -> Refund:
    """REQUESTED → APPROVED. Re-checks the caps now, leaving this refund out of every total
    it already reserves (D-P8). ``calculation`` keeps the request-time snapshot."""
    refund = _locked_refund(refund)
    _transition(refund, policy.APPROVED)
    _check_status_date(refund, "on_date", on_date)
    caps = refund_caps(refund.payment, exclude_refund_id=refund.refund_id)
    _check_caps(refund.amount, caps, refund.payment, refund.refund_id)
    _refund_event(
        refund,
        from_status=refund.status,
        to_status=policy.APPROVED,
        event_date=on_date,
        reason=policy.APPROVED,
        actor=actor,
    )
    refund.status = policy.APPROVED
    refund.save(update_fields=["status"])
    return refund


@transaction.atomic
def reject_refund(refund, *, on_date: date, actor: str) -> Refund:
    """REQUESTED / APPROVED → REJECTED. Releases the reservation; posts nothing."""
    refund = _locked_refund(refund)
    _transition(refund, policy.REJECTED)
    _check_status_date(refund, "on_date", on_date)
    _refund_event(
        refund,
        from_status=refund.status,
        to_status=policy.REJECTED,
        event_date=on_date,
        reason=policy.REJECTED,
        actor=actor,
    )
    refund.status = policy.REJECTED
    refund.save(update_fields=["status"])
    return refund


@transaction.atomic
def process_refund(refund, *, processed_date: date, actor: str) -> Refund:
    """APPROVED → PROCESSED: re-checks the caps (D-P8), sets ``processed_date`` and posts the
    REFUND ledger entry (+amount), all in one transaction (BR-REF-03)."""
    refund = _locked_refund(refund)
    _transition(refund, policy.PROCESSED)
    _check_status_date(refund, "processed_date", processed_date)
    caps = refund_caps(refund.payment, exclude_refund_id=refund.refund_id)
    _check_caps(refund.amount, caps, refund.payment, refund.refund_id)
    _refund_event(
        refund,
        from_status=refund.status,
        to_status=policy.PROCESSED,
        event_date=processed_date,
        reason=policy.PROCESSED,
        actor=actor,
    )
    refund.status = policy.PROCESSED
    refund.processed_date = processed_date
    refund.save(update_fields=["status", "processed_date"])
    billing.post_refund_ledger(refund)
    return refund


def refundable_summary(student) -> dict:
    """GET /students/{id}/refundable/: the student's caps and each payment's available amount.

    ``max_refundable`` (total) = max_refundable(net paid, credit available, Σ available);
    each payment's own maximum uses its own available amount (§7 Stage C).
    """
    payments = list(
        Payment.objects.filter(student_id=student.pk)
        .prefetch_related("allocations", "refunds")
        .order_by("payment_id")
    )
    refunds = list(Refund.objects.filter(student_id=student.pk))
    ledger = billing_calc.ledger_balance(LedgerEntry.objects.filter(student_id=student.pk), None)
    paid = calc.net_paid([p.amount for p in payments], refunds)
    pending = calc.pending_reservations(refunds)
    credit = calc.credit_available(ledger, pending)
    rows = []
    for p in payments:
        allocs, own_refunds = list(p.allocations.all()), list(p.refunds.all())
        avail = calc.payment_available(p.amount, allocs, own_refunds)
        rows.append(
            {
                "payment_id": p.payment_id,
                "payer_type": p.payer_type,
                "received_date": p.received_date,
                "amount": p.amount,
                "net_allocated": calc.net_allocated(allocs),
                "refunds_reserved_or_processed": dsum(
                    r.amount for r in own_refunds if r.status != policy.REJECTED
                ),
                "available": avail,
                "max_refundable": calc.max_refundable(paid, credit, avail),
            }
        )
    total_available = dsum(max(ZERO, r["available"]) for r in rows)
    return {
        "student_id": student.pk,
        "max_refundable": calc.max_refundable(paid, credit, total_available),
        "calculation": {
            "net_paid": paid,
            "ledger_balance": round_money(ledger),
            "credit_balance": calc.ledger_credit_balance(ledger),
            "pending_refunds": pending,
            "credit_available": credit,
            "payments_available": total_available,
        },
        "payments": rows,
    }
