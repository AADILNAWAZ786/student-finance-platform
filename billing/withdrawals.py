"""Billing side of a withdrawal or cancellation (BR-WDR-01..03, BR-SCH-07, BR-INV-08).

``academics.services.withdraw_enrollment`` / ``cancel_enrollment`` orchestrate (decision
D-W1); after changing the enrollment they call ``apply_enrollment_exit`` through the academics
exit registry, inside the same transaction. This module then:

  1. finds the enrollment's invoice: the non-VOID one (BR-INV-01). None → nothing to do
     (decision D-W7);
  2. computes the ADJUSTMENT for each line: withdrawal −round_half_up(N × pct) on refundable
     lines (BR-WDR-02); cancellation −N on every line except REGISTRATION (BR-WDR-01); N is
     the line's current net (net + earlier adjustments, decision D-W3);
  3. computes each line's release plan with ``calculators.release_for_adjustment``
     (BR-WDR-03: scholarship reversal R first, then the payment excess, LIFO);
  4. writes everything: payment DEALLOCATE rows (payments' handler), the ADJUSTMENT lines and
     their ledger entries, the scholarship REVERSE rows and entries (scholarships' handler,
     split across awards by decision D-W4), and on cancellation ISSUED → CANCELLED;
  5. recalculates the collection status once, after every row exists (an intermediate state
     can have a negative outstanding, which BR-INV-04 forbids).

All of it happens in the caller's transaction: any failure rolls everything back. Rows are
only ever added; nothing existing is changed except the invoice's two status columns.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import ValidationError

from billing import allocations as registry
from billing import calculators as calc
from billing import services as billing
from billing.lifecycle import CANCELLED, LIFECYCLE_TRANSITIONS
from billing.models import Invoice, InvoiceLine
from core.ids import next_id
from core.lifecycle import check_transition
from core.money import ZERO, dsum

WITHDRAWAL, CANCELLATION = "WITHDRAWAL", "CANCELLATION"
RELEASE_RULE = {WITHDRAWAL: "WITHDRAWAL_RELEASE", CANCELLATION: "CANCELLATION_RELEASE"}
DATE_FIELD = {WITHDRAWAL: "withdrawal_date", CANCELLATION: "cancellation_date"}


class _OpenAllocation:
    """A payment ALLOCATE row with the net amount it still holds."""

    __slots__ = ("allocation_id", "allocated_on", "amount")

    def __init__(self, allocation_id, allocated_on, amount):
        self.allocation_id, self.allocated_on, self.amount = allocation_id, allocated_on, amount


def _open_payment_allocations(payment_allocations) -> dict[str, list[_OpenAllocation]]:
    """line_id → ALLOCATE rows with what is left of each after earlier DEALLOCATE rows."""
    rows = list(payment_allocations)
    released: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        if r.allocation_type == "DEALLOCATE":
            released[r.reverses_id] += r.amount  # negative
    out: dict[str, list[_OpenAllocation]] = defaultdict(list)
    for r in rows:
        if r.allocation_type == "ALLOCATE":
            left = r.amount + released[r.allocation_id]
            if left > 0:
                out[r.invoice_line_id].append(
                    _OpenAllocation(r.allocation_id, r.allocated_on, left)
                )
    return out


def find_invoice(enrollment) -> Invoice | None:
    """The enrollment's non-VOID invoice (BR-INV-01 allows at most one), or None."""
    return (
        Invoice.objects.filter(enrollment_id=enrollment.pk)
        .exclude(lifecycle_status="VOID")
        .order_by("invoice_id")
        .first()
    )


def refund_quote(student):
    """The payments refundable summary (D-P4 provider), or None without payments."""
    values = registry.student_values("refund_quote", student)
    return values[0] if values else None


def apply_enrollment_exit(*, kind: str, enrollment, on_date, refund_pct, actor: str) -> dict:
    """The exit handler billing registers with academics (see the module docstring)."""
    found = find_invoice(enrollment)
    if found is None:
        return {"invoice": None, "refund_quote": refund_quote(enrollment.student)}
    invoice = Invoice.objects.select_for_update().get(pk=found.pk)
    if kind == CANCELLATION:  # BR-INV-08: ISSUED → CANCELLED (409 from any other state)
        check_transition(
            LIFECYCLE_TRANSITIONS,
            invoice.lifecycle_status,
            CANCELLED,
            entity="Invoice",
            record_id=invoice.invoice_id,
            rule_id="BR-INV-08",
        )
    lines = list(invoice.lines.all())
    by_id = {line.line_id: line for line in lines}
    if kind == WITHDRAWAL:
        adjustments = calc.withdrawal_adjustments(lines, refund_pct)
    else:
        adjustments = calc.cancellation_adjustments(lines)

    # an adjustment cannot be dated before the line it adjusts (the same principle as a
    # deallocation, which cannot predate the allocation it releases)
    early = sorted(
        a.adjusts_line_id for a in adjustments if by_id[a.adjusts_line_id].posted_date > on_date
    )
    if early:
        latest = max(by_id[i].posted_date for i in early)
        raise ValidationError(
            {
                DATE_FIELD[kind]: [
                    f"Must be on or after {latest.isoformat()}, the posting date of "
                    f"{', '.join(early)}, which it adjusts."
                ]
            }
        )

    scholarship_allocs, payment_allocs = billing.allocations_for(invoice)
    open_payments = _open_payment_allocations(payment_allocs)
    plans = []
    for adj in adjustments:
        held = dsum(
            a.amount for a in scholarship_allocs if a.invoice_line_id == adj.adjusts_line_id
        )
        plan = calc.release_for_adjustment(
            line_net=adj.line_net,
            adjustment=adj.amount,
            scholarship_allocated=held,
            payment_allocations=open_payments.get(adj.adjusts_line_id, []),
        )
        plans.append((adj, plan))

    source_ref = enrollment.enrollment_id
    # BR-WDR-03 step 2 rows: one call, so the 99-row capacity of every payment is checked
    # before any of them is written
    deallocated = registry.release_payments_for_adjustment(
        deallocations=[d for _, plan in plans for d in plan.deallocations],
        rule=RELEASE_RULE[kind],
        on_date=on_date,
        date_field=DATE_FIELD[kind],
        actor=actor,
        source_ref=source_ref,
    )
    dealloc_by_original = defaultdict(list)
    for row in deallocated:
        dealloc_by_original[row.reverses_id].append(row)

    report = []
    for adj, plan in plans:
        line = InvoiceLine(
            line_id=next_id("invoice_line", parent=invoice.invoice_id),
            invoice=invoice,
            line_type="ADJUSTMENT",
            fee_schedule=None,
            fee_type=adj.fee_type,
            description=(
                f"Withdrawal credit {refund_pct * 100:.0f}% of {adj.adjusts_line_id}"
                if kind == WITHDRAWAL
                else f"Cancellation of {adj.adjusts_line_id}"
            ),
            quantity=1,
            unit_amount=adj.amount,
            gross_amount=adj.amount,
            waiver_amount=ZERO,
            waiver_reason=None,
            net_amount=adj.amount,
            adjusts_line_id=adj.adjusts_line_id,
            adjustment_reason=kind,
            is_scholarship_eligible=False,
            is_refundable=False,
            is_waivable=False,
            allocation_priority=adj.allocation_priority,
            posted_date=on_date,
        )
        line.save(force_insert=True)
        billing.post_ledger(line)
        # BR-WDR-03 step 1 / BR-SCH-07 rows
        reversals = registry.reverse_scholarship_for_adjustment(
            invoice_line_id=adj.adjusts_line_id,
            amount=plan.scholarship_reversal,
            reason=kind,
            on_date=on_date,
            actor=actor,
            source_ref=source_ref,
        )
        report.append(
            {
                "adjusts_line": adj.adjusts_line_id,
                "fee_type": adj.fee_type,
                "adjustment_line": line.line_id,
                "line_net": plan.line_net,
                "adjustment": plan.adjustment,
                "reduced_net": plan.reduced_net,
                "scholarship_allocated": plan.scholarship_allocated,
                "scholarship_reversal": plan.scholarship_reversal,
                "scholarship_reversals": [
                    {
                        "allocation_id": r.allocation_id,
                        "reverses": r.reverses_id,
                        "amount": -r.amount,
                    }
                    for r in reversals
                ],
                "payments_allocated": plan.payments_allocated,
                "excess": max(plan.excess, ZERO),
                "deallocations": [
                    {
                        "allocation_id": row.allocation_id,
                        "reverses": original,
                        "amount": -row.amount,
                    }
                    for original, _ in plan.deallocations
                    for row in dealloc_by_original[original]
                ],
            }
        )

    if kind == CANCELLATION:
        billing._event(
            invoice,
            dimension="LIFECYCLE",
            from_status=invoice.lifecycle_status,
            to_status=CANCELLED,
            event_date=on_date,
            reason="ENROLLMENT_CANCELLED",
            actor=actor,
            source_ref=source_ref,
        )
        invoice.lifecycle_status = CANCELLED
        invoice.save(update_fields=["lifecycle_status"])
    billing.recalculate_collection_status(
        invoice, as_of=on_date, reason=kind, actor=actor, source_ref=source_ref
    )
    invoice.refresh_from_db()
    amounts = billing.current_amounts(invoice)
    return {
        "invoice": {
            "invoice_id": invoice.invoice_id,
            "lifecycle_status": invoice.lifecycle_status,
            "collection_status": invoice.collection_status,
            "amounts": {
                "original_charges": amounts.original_charges,
                "adjustments": amounts.adjustments,
                "net_charges": amounts.net_charges,
                "scholarship_applied": amounts.scholarship_applied,
                "amount_paid": amounts.amount_paid,
                "outstanding": amounts.outstanding,
            },
            "release_plan": report,
        },
        "refund_quote": refund_quote(enrollment.student),
    }
