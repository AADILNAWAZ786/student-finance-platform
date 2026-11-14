"""Mandatory validation 5: every label matches the anomaly actually injected (DATABASE_DESIGN §7
methods), unrelated rows are not corrupted (R-27), and injected rows carry no tell-tale
(ARCHITECTURE §12.2.3)."""

from decimal import Decimal

import pytest

from billing.calculators import ledger_balance
from billing.models import Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.money import dsum
from datagen import services
from datagen.models import GroundTruthLabel
from datagen.pipeline.engine import Rec
from datagen.pipeline.materialize import MODELS
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent
from reconciliation.models import BalanceSnapshot
from scholarships.calculators import eligible_fee_base
from scholarships.models import Eligibility, Scholarship, ScholarshipEvent

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])

CONTRACT = {  # issue type → (entity, rule, decision, method)
    "PAYMENT_OVER_ALLOCATED": ("Payment", "BR-ALC-01", "REJECT_ALLOCATION", "APPEND"),
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES": ("Scholarship", "BR-SCH-04", "CAP_SCHOLARSHIP", "APPEND"),
    "DUPLICATE_INVOICE": ("Invoice", "BR-INV-01", "VOID_DUPLICATE", "APPEND"),
    "REFUND_EXCEEDS_NET_PAID": ("Refund", "BR-REF-01", "REVERSE_REFUND", "APPEND"),
    "BALANCE_LEDGER_MISMATCH": ("BalanceSnapshot", "BR-BAL-03", "RESTATE_BALANCE", "MODIFY"),
}


def _labels(issue_type=None):
    qs = GroundTruthLabel.objects.using("ground_truth").order_by("label_id")
    return list(qs.filter(issue_type=issue_type) if issue_type else qs)


def test_label_contract_and_numbering(generated):
    labels = _labels()
    assert [x.label_id for x in labels] == [f"GT-2026-{n:06d}" for n in range(1, 16)]
    for x in labels:
        assert (x.entity_type, x.rule_violated, x.expected_decision, x.injection_method) == (
            CONTRACT[x.issue_type]
        )
        assert x.generation_run_id == "GEN-2026-0001"
    assert len({x.student_id for x in labels}) == 15  # one anomaly per student


def test_payment_over_allocated_labels(generated):
    for x in _labels("PAYMENT_OVER_ALLOCATED"):
        p = Payment.objects.get(pk=x.record_id)
        allocation_id, line_id, invoice_id = x.related_ids
        extra = PaymentAllocation.objects.get(pk=allocation_id)
        assert (extra.payment_id, extra.invoice_line_id, extra.allocation_type) == (
            p.pk,
            line_id,
            "ALLOCATE",
        )
        assert InvoiceLine.objects.get(pk=line_id).invoice_id == invoice_id
        assert p.student_id == x.student_id
        net = dsum(p.allocations.values_list("amount", flat=True))
        assert x.expected_value == p.amount and x.injected_value == net > p.amount


def test_scholarship_labels(generated):
    for x in _labels("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"):
        s = Scholarship.objects.get(pk=x.record_id)
        eligibility_id, invoice_id = x.related_ids
        assert (s.eligibility_id, s.invoice_id, s.status) == (
            eligibility_id,
            invoice_id,
            "ACCEPTED",
        )
        assert Eligibility.objects.get(pk=eligibility_id).is_eligible
        base = eligible_fee_base(s.scholarship_type, list(s.invoice.lines.all()))
        assert x.expected_value == base and x.injected_value == s.awarded_amount
        assert Decimal("1.05") * base <= s.awarded_amount <= Decimal("1.50") * base
        assert ScholarshipEvent.objects.filter(scholarship=s).count() == 2


def test_duplicate_invoice_labels(generated):
    for x in _labels("DUPLICATE_INVOICE"):
        dup = Invoice.objects.get(pk=x.record_id)
        original_id, enrollment_id, *line_ids = x.related_ids
        original = Invoice.objects.get(pk=original_id)
        assert dup.enrollment_id == original.enrollment_id == enrollment_id
        assert original_id < dup.pk  # record_id is the later duplicate (DATABASE_DESIGN §3)
        assert (dup.issue_date, dup.total_amount) == (original.issue_date, original.total_amount)
        assert sorted(dup.lines.values_list("pk", flat=True)) == sorted(line_ids)
        assert LedgerEntry.objects.filter(invoice_line__invoice=dup).count() == len(line_ids)
        assert InvoiceEvent.objects.filter(invoice=dup).count() == 2
        assert x.injected_value == dup.total_amount


def test_refund_labels(generated):
    for x in _labels("REFUND_EXCEEDS_NET_PAID"):
        r = Refund.objects.get(pk=x.record_id)
        payment_id, entry_id = x.related_ids
        assert (r.payment_id, r.status, r.amount) == (payment_id, "PROCESSED", x.injected_value)
        assert LedgerEntry.objects.get(pk=entry_id).refund_id == r.pk
        paid = dsum(
            Payment.objects.filter(student_id=r.student_id).values_list("amount", flat=True)
        )
        assert x.expected_value == paid < r.amount
        assert RefundEvent.objects.filter(refund=r).count() == 3


def test_balance_mismatch_labels(generated):
    for x in _labels("BALANCE_LEDGER_MISMATCH"):
        snap = BalanceSnapshot.objects.get(pk=x.record_id)
        ledger = ledger_balance(
            LedgerEntry.objects.filter(student_id=snap.student_id), snap.as_of_date
        )
        assert x.expected_value == ledger and x.injected_value == snap.reported_balance
        assert Decimal("500") <= abs(snap.reported_balance - ledger) <= Decimal("25000")
        assert x.related_ids == []


RELATIONS = ("scholarship",)  # Eligibility → its award: a reverse link, not a stored column


def _fields(rec):
    return {
        k: v
        for k, v in vars(rec).items()
        if k not in ("pk", *RELATIONS)
        and not isinstance(v, Rec | list | dict | set)
        and not callable(v)
    }


def test_unrelated_records_are_not_corrupted():
    """R-27: the injected world equals the baseline world on every baseline record, except the
    labelled snapshots' reported_balance; every extra record is part of an injection."""
    base = services.plan(seed=20261022, scale="starter", inject=False).world
    planned = services.plan(seed=20261022, scale="starter")
    full = planned.world
    labelled_students = {x.student_id for x in planned.labels}
    kinds = (
        "students",
        "enrollments",
        "invoices",
        "lines",
        "invoice_events",
        "ledger",
        "eligibility",
        "scholarships",
        "scholarship_allocations",
        "scholarship_events",
        "payments",
        "payment_allocations",
        "refunds",
        "refund_events",
        "snapshots",
    )
    extra_total = 0
    for kind in kinds:
        b = {getattr(r, "key", None) or r.pk: r for r in getattr(base, kind)}
        f = {getattr(r, "key", None) or r.pk: r for r in getattr(full, kind)}
        assert set(b) <= set(f), kind
        changed = [k for k in b if _fields(b[k]) != _fields(f[k])]
        if kind == "snapshots":
            # snapshots are taken after the transactional injection (§11.2 stage 15): only
            # students carrying an anomaly may differ, and only in what a snapshot reports
            assert {f[k].student.pk for k in changed} <= labelled_students
            for k in changed:
                diff = {a for a in _fields(b[k]) if _fields(b[k])[a] != _fields(f[k])[a]}
                assert diff <= {"reported_balance", "total_debits", "total_credits"}
        else:
            assert changed == [], kind
        extra_total += len(set(f) - set(b))
    # 3 allocations + 3 awards (+6 events) + 3 invoices (+lines, CHARGE entries, 6 events)
    # + 3 refunds (+9 events, 3 REFUND entries)
    assert extra_total > 3 * 4


def test_no_injection_marker_in_the_domain_schema():
    for model in MODELS:
        names = {f.name for f in model._meta.concrete_fields}
        assert not {n for n in names if any(w in n for w in ("anomal", "inject", "label", "truth"))}


def test_injected_rows_are_interleaved_in_their_id_sequences(generated):
    """Injected rows draw from the same sequences as the baseline (stage 17): at least one
    injected award, refund or ledger entry has baseline rows numbered after it."""
    awards = sorted(Scholarship.objects.values_list("pk", flat=True))
    injected = [x.record_id for x in _labels("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES")]
    assert any(awards.index(s) < len(awards) - 3 for s in injected)
    refunds = sorted(Refund.objects.values_list("pk", flat=True))
    injected = [x.record_id for x in _labels("REFUND_EXCEEDS_NET_PAID")]
    assert any(refunds.index(r) < len(refunds) - 3 for r in injected)
