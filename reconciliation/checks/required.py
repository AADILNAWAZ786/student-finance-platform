"""The five required anomaly types (ARCHITECTURE §12.1; DATABASE_DESIGN §3, §7) and the
BR-BAL-02 reconciliation family.

``record_id`` follows the DATABASE_DESIGN §3 convention: payment_id / scholarship_id / the
later duplicate invoice_id / refund_id / snapshot_id. Every rule reuses the domain apps'
calculators (safeguard 4), through ``reconciliation.calculators``.
"""

from __future__ import annotations

from collections import defaultdict

from payments import calculators as pay_calc
from reconciliation import calculators as rec_calc
from reconciliation import policy
from reconciliation.checks.base import Finding, check


def _dated(ds, rows, attr):
    return [r for r in rows if ds.on_or_before(getattr(r, attr))]


@check("Q-PAYMENT-OVER-ALLOCATED", family="REQUIRED", rule_id="BR-ALC-01")
def payment_over_allocated(ds):
    """Σ net allocations of a payment ≤ its amount − Σ its refunds not REJECTED (BR-ALC-01),
    with ``payments.calculators.payment_available``."""
    out, checked = [], 0
    for p in _dated(ds, ds.payments, "received_date"):
        checked += 1
        allocations = _dated(ds, ds.allocations_by_payment.get(p.pk, []), "allocated_on")
        refunds = _dated(ds, ds.refunds_by_payment.get(p.pk, []), "requested_date")
        available = pay_calc.payment_available(p.amount, allocations, refunds)
        if available < 0:
            refunded = p.amount - available - pay_calc.net_allocated(allocations)
            out.append(
                Finding(
                    policy.PAYMENT_OVER_ALLOCATED,
                    "BR-ALC-01",
                    "Payment",
                    p.pk,
                    p.student_id,
                    expected=p.amount - refunded,
                    actual=pay_calc.net_allocated(allocations),
                    details={
                        "payment_amount": str(p.amount),
                        "refunds_not_rejected": str(refunded),
                        "net_allocated": str(pay_calc.net_allocated(allocations)),
                        "over_by": str(-available),
                        "allocation_ids": sorted(a.pk for a in allocations),
                        "refund_ids": sorted(r.pk for r in refunds if r.status != "REJECTED"),
                    },
                )
            )
    return checked, out


@check("Q-SCHOLARSHIP-EXCEEDS-FEES", family="REQUIRED", rule_id="BR-SCH-04")
def scholarship_exceeds_eligible_fees(ds):
    """No award exceeds its type's eligible fee base on its invoice, and the invoice's
    awards never reserve more than that base (BR-SCH-03/04/08; Phase 3 guardrail)."""
    types = ds.by_id["scholarship_types"]
    by_invoice = defaultdict(list)
    for s in _dated(ds, ds.scholarships, "award_date"):
        allocations = _dated(ds, ds.allocations_by_scholarship.get(s.pk, []), "allocated_on")
        by_invoice[s.invoice_id].append((s, allocations))
    out, checked = [], 0
    for invoice_id in sorted(by_invoice):
        awards = by_invoice[invoice_id]
        checked += len(awards)
        if any(a.scholarship_type_id not in types for a, _ in awards):
            continue  # an orphan type is S-FK-ORPHAN's finding
        lines = ds.lines_by_invoice.get(invoice_id, [])
        for b in rec_calc.scholarship_reservation_breaches(awards, types, lines):
            award = ds.by_id["scholarships"][b.record_id]
            rule = (
                "BR-SCH-08"
                if types[award.scholarship_type_id].coverage_scope == "ALL_FEE_LINES"
                else "BR-SCH-04"
            )
            out.append(
                Finding(
                    policy.SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES,
                    rule,
                    "Scholarship",
                    b.record_id,
                    award.student_id,
                    expected=b.expected,
                    actual=b.actual,
                    details={**b.details, "invoice_id": invoice_id},
                )
            )
    return checked, out


@check("Q-DUPLICATE-INVOICE", family="REQUIRED", rule_id="BR-INV-01")
def duplicate_invoice(ds):
    """At most one invoice with lifecycle ≠ VOID per enrollment (BR-INV-01); every later one
    (by issue_date, invoice_id) is a duplicate."""
    by_enrollment = defaultdict(list)
    for inv in _dated(ds, ds.invoices, "issue_date"):
        if inv.lifecycle_status != "VOID":
            by_enrollment[inv.enrollment_id].append(inv)
    out = []
    for enrollment_id in sorted(by_enrollment):
        first, *later = sorted(by_enrollment[enrollment_id], key=lambda i: (i.issue_date, i.pk))
        for inv in later:
            out.append(
                Finding(
                    policy.DUPLICATE_INVOICE,
                    "BR-INV-01",
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    details={"enrollment_id": enrollment_id, "first_invoice_id": first.pk},
                )
            )
    return len(by_enrollment), out


@check("Q-REFUND-EXCEEDS-NET-PAID", family="REQUIRED", rule_id="BR-REF-01")
def refund_exceeds_net_paid(ds):
    """Every refund not REJECTED ≤ net paid at its request, as Phase 4 computes it (BR-REF-01)."""
    payments = ds.per_student("payments")
    refunds = ds.per_student("refunds")
    out, checked = [], 0
    for student_id in sorted(refunds):
        rows = _dated(ds, refunds[student_id], "requested_date")
        checked += len(rows)
        paid = _dated(ds, payments.get(student_id, []), "received_date")
        for b in rec_calc.refund_net_paid_breaches(paid, rows):
            out.append(
                Finding(
                    policy.REFUND_EXCEEDS_NET_PAID,
                    "BR-REF-01",
                    "Refund",
                    b.record_id,
                    student_id,
                    expected=b.expected,
                    actual=b.actual,
                    details=b.details,
                )
            )
    return checked, out


@check("Q-BALANCE-LEDGER-MISMATCH", family="REQUIRED", rule_id="BR-BAL-03")
def balance_ledger_mismatch(ds):
    """A snapshot's reported_balance = BR-BAL-01 at its as_of_date (BR-BAL-03)."""
    entries = ds.per_student("ledger")
    out, checked = [], 0
    for snap in _dated(ds, ds.snapshots, "as_of_date"):
        checked += 1
        ledger = rec_calc.balance_from_ledger(entries.get(snap.student_id, []), snap.as_of_date)
        if snap.reported_balance != ledger:
            out.append(
                Finding(
                    policy.BALANCE_LEDGER_MISMATCH,
                    "BR-BAL-03",
                    "BalanceSnapshot",
                    snap.pk,
                    snap.student_id,
                    expected=ledger,
                    actual=snap.reported_balance,
                    details={
                        "as_of_date": str(snap.as_of_date),
                        "difference": str(snap.reported_balance - ledger),
                    },
                )
            )
    return checked, out


def identities(ds) -> dict[str, rec_calc.Identity]:
    """BR-BAL-02 per student at the run's as-of date."""
    entries = ds.per_student("ledger")
    payments = ds.per_student("payments")
    refunds = ds.per_student("refunds")
    invoices = ds.per_student("invoices")
    allocations = defaultdict(list)
    for a in ds.payment_allocations:
        p = ds.by_id["payments"].get(a.payment_id)
        if p is not None:
            allocations[p.student_id].append(a)
    out = {}
    for stu in ds.students:
        sid = stu.pk
        out[sid] = rec_calc.balance_identity(
            entries=entries.get(sid, []),
            invoices=[
                (
                    inv,
                    ds.lines_by_invoice.get(inv.pk, []),
                    ds.scholarship_allocations_by_invoice.get(inv.pk, []),
                    ds.payment_allocations_by_invoice.get(inv.pk, []),
                )
                for inv in invoices.get(sid, [])
            ],
            payments=payments.get(sid, []),
            payment_allocations=allocations.get(sid, []),
            refunds=refunds.get(sid, []),
            as_of=ds.as_of,
        )
    return out


@check("B-IDENTITY", family="RECONCILIATION", rule_id="BR-BAL-02")
def balance_identity(ds):
    """Per student: ledger balance = Σ invoice outstanding − unallocated credit (BR-BAL-02;
    decision D-R3: reported as ARITHMETIC_MISMATCH on the student)."""
    out = []
    results = identities(ds)
    for sid in sorted(results):
        i = results[sid]
        if not i.holds:
            out.append(
                Finding(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-BAL-02",
                    "Student",
                    sid,
                    sid,
                    expected=i.expected,
                    actual=i.ledger_balance,
                    details={
                        "ledger_balance": str(i.ledger_balance),
                        "invoice_outstanding": str(i.invoice_outstanding),
                        "unallocated_credit": str(i.unallocated_credit),
                    },
                )
            )
    return len(results), out
