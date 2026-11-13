"""V-WDR: Phase 5 verification beyond the per-endpoint and service tests.

* Phase 5 needs no migration: the model state matches the migrations exactly
* withdrawals and cancellations only ever touch the default database (the ground-truth
  database receives no query)
* the same scenario gives identical rows (IDs included) every time
* a mixed scenario (every BR-WDR-02 band, a cancellation with payments, an exit with no
  invoice) satisfies every financial invariant reconciliation will re-check: BR-WDR-01/02/03,
  BR-SCH-07, BR-ALC-01/02/05, BR-LED-01, BR-BAL-02, append-only history

(Concurrency: tests/services/test_concurrency_withdrawals.py.)
"""

import io
from datetime import date
from decimal import Decimal as D

import pytest
from django.core.management import call_command
from django.db import connections, transaction
from django.test.utils import CaptureQueriesContext

from academics import calculators as academic_calc
from academics import services as academics
from academics.models import Enrollment, EnrollmentEvent
from billing import calculators as billing_calc
from billing import services as billing
from billing.models import Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.money import apply_rate, dsum
from payments import services as payments
from payments.models import Payment, PaymentAllocation
from reconciliation.calculators import balance_from_ledger
from scholarships import services as scholarships
from scholarships.models import ScholarshipAllocation

# (withdrawal date, BR-WDR-02 fraction): days 0, 7, 8, 14, 15, 27, 28 from the 09-07 start
BANDS = [
    (date(2026, 9, 7), D("0.80")),
    (date(2026, 9, 14), D("0.80")),
    (date(2026, 9, 15), D("0.60")),
    (date(2026, 9, 21), D("0.60")),
    (date(2026, 9, 22), D("0.40")),
    (date(2026, 10, 4), D("0.40")),
    (date(2026, 10, 5), D("0.00")),
]


def test_phase5_needs_no_migration(db):
    out = io.StringIO()
    call_command("makemigrations", "--check", "--dry-run", stdout=out)
    assert "No changes detected" in out.getvalue()


def test_schedule_matches_br_wdr_02_at_every_boundary(fall):
    for on, pct in BANDS:
        assert academic_calc.withdrawal_refund_pct(fall, on) == pct, on


# --- a mixed scenario ---


def _scenario(invoice, merit_award, student_with_invoice, record):
    """Student 1: merit + guardian payment, withdrawn on day 10 (60 %). Students 2–8: a
    self-paid invoice each, withdrawn at every BR-WDR-02 boundary. Student 9: paid, then
    cancelled before the start. Returns the exits."""
    exits = []
    scholarships.apply_scholarship(merit_award, actor="s")
    record("89000.00", payer="GUARDIAN", on=date(2026, 8, 20), auto=True)
    exits.append(
        academics.withdraw_enrollment(
            invoice.enrollment, withdrawal_date=date(2026, 9, 17), actor="s"
        )
    )
    for on, _ in BANDS:
        s, inv = student_with_invoice()
        record("164000.00", student=s, on=date(2026, 8, 20), auto=True)
        exits.append(academics.withdraw_enrollment(inv.enrollment, withdrawal_date=on, actor="s"))
    s, inv = student_with_invoice()
    record("30000.00", student=s, on=date(2026, 8, 18), auto=True)
    exits.append(
        academics.cancel_enrollment(inv.enrollment, cancellation_date=date(2026, 8, 25), actor="s")
    )
    return exits


def _dump():
    models = (
        Enrollment,
        EnrollmentEvent,
        Invoice,
        InvoiceEvent,
        InvoiceLine,
        LedgerEntry,
        PaymentAllocation,
        ScholarshipAllocation,
    )

    def rows(model):
        skip = {"recorded_at", "created_at", "updated_at", "event_at"}
        fields = [f.attname for f in model._meta.concrete_fields if f.name not in skip]
        return [tuple(str(getattr(o, f)) for f in fields) for o in model.objects.order_by("pk")]

    return {m.__name__: rows(m) for m in models}


@pytest.mark.django_db
def test_same_scenario_gives_identical_rows(invoice, merit_award, student_with_invoice, record):
    dumps = []
    for _ in range(2):
        with transaction.atomic():
            _scenario(invoice, merit_award, student_with_invoice, record)
            dumps.append(_dump())
            transaction.set_rollback(True)
    # students differ only by the generated contact data the factory varies per call: compare
    # everything the exits wrote
    for name in (
        "EnrollmentEvent",
        "InvoiceLine",
        "LedgerEntry",
        "PaymentAllocation",
        "ScholarshipAllocation",
        "InvoiceEvent",
    ):
        assert dumps[0][name] == dumps[1][name], name


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_exits_never_touch_the_ground_truth_database(
    invoice, merit_award, student_with_invoice, record
):
    with CaptureQueriesContext(connections["ground_truth"]) as gt:
        _scenario(invoice, merit_award, student_with_invoice, record)
    assert gt.captured_queries == []


@pytest.mark.django_db
def test_every_financial_invariant_holds_after_exits(
    invoice, merit_award, student_with_invoice, record
):
    before = {
        "lines": {ln.pk: (ln.net_amount, ln.posted_date) for ln in InvoiceLine.objects.all()},
    }
    exits = _scenario(invoice, merit_award, student_with_invoice, record)
    assert [e.kind for e in exits] == ["WITHDRAWAL"] * 8 + ["CANCELLATION"]

    # append-only: every row that existed before an exit is unchanged (new rows only)
    for pk, (net, posted) in before["lines"].items():
        line = InvoiceLine.objects.get(pk=pk)
        assert (line.net_amount, line.posted_date) == (net, posted)

    for e in exits:
        enr = Enrollment.objects.get(pk=e.enrollment.pk)
        inv = Invoice.objects.exclude(lifecycle_status="VOID").get(enrollment=enr)
        lines = list(inv.lines.all())
        adjustments = [ln for ln in lines if ln.line_type == "ADJUSTMENT"]
        originals = {ln.pk: ln for ln in lines if ln.line_type != "ADJUSTMENT"}

        if e.kind == "WITHDRAWAL":
            assert enr.status == "WITHDRAWN" and enr.withdrawal_date == e.effective_date
            assert inv.lifecycle_status == "ISSUED"
            # BR-WDR-02: −round_half_up(N × pct) on each refundable line, nothing else
            expected = {
                pk: -apply_rate(ln.net_amount, e.refund_pct)
                for pk, ln in originals.items()
                if ln.is_refundable and e.refund_pct > 0
            }
            assert {a.adjusts_line_id: a.net_amount for a in adjustments} == expected
            assert {a.adjustment_reason for a in adjustments} <= {"WITHDRAWAL"}
        else:
            assert enr.status == "CANCELLED" and inv.lifecycle_status == "CANCELLED"
            # BR-WDR-01: every line except REGISTRATION nets to zero
            for pk, ln in originals.items():
                net = ln.net_amount + dsum(
                    a.net_amount for a in adjustments if a.adjusts_line_id == pk
                )
                assert net == (ln.net_amount if ln.fee_type == "REGISTRATION" else D("0.00")), pk
            assert InvoiceEvent.objects.filter(invoice=inv, to_status="CANCELLED").count() == 1

        for a in adjustments:
            assert a.posted_date == e.effective_date
            # BR-LED-01: one ADJUSTMENT ledger entry per adjustment line, same amount and date
            entry = LedgerEntry.objects.get(invoice_line=a)
            assert (entry.amount, entry.entry_date, entry.student_id) == (
                a.net_amount,
                a.posted_date,
                inv.student_id,
            )

        # BR-ALC-02 / BR-WDR-03: no line is over-covered after the releases
        sch, pay = billing.allocations_for(inv)
        opens = billing_calc.line_open_amounts(lines, sch, pay)
        assert all(v >= 0 for v in opens.values()), opens

        # releases are dated on the exit and carry the right rule
        rule = {"WITHDRAWAL": "WITHDRAWAL_RELEASE", "CANCELLATION": "CANCELLATION_RELEASE"}[e.kind]
        for d in PaymentAllocation.objects.filter(
            invoice_line__invoice=inv, allocation_type="DEALLOCATE"
        ):
            assert (d.rule_applied, d.allocated_on) == (rule, e.effective_date)
        for r in ScholarshipAllocation.objects.filter(
            invoice_line__invoice=inv, allocation_type="REVERSE"
        ):
            assert r.allocated_on == e.effective_date

        # BR-BAL-02 and the reconciliation wrapper agree with billing
        bal = billing.student_balance(inv.student, None)
        assert bal["identity"]["holds"], bal
        entries = LedgerEntry.objects.filter(student=inv.student)
        assert balance_from_ledger(entries) == billing_calc.ledger_balance(entries, None)

    # BR-ALC-01/05 across every payment
    for p in Payment.objects.all():
        assert payments.available(p) >= 0
    for a in PaymentAllocation.objects.filter(allocation_type="ALLOCATE"):
        released = -dsum(r.amount for r in a.reversals.all())
        assert 0 <= released <= a.amount
    # BR-SCH-07: Σ reversals ≤ original per scholarship allocation
    for a in ScholarshipAllocation.objects.filter(allocation_type="APPLY"):
        assert 0 <= -dsum(r.amount for r in a.reversals.all()) <= a.amount

    # the day-10 worked numbers for student 1 (merit 75,000; guardian 89,000)
    l01 = InvoiceLine.objects.get(pk=f"{invoice.pk}-L01")
    assert -dsum(
        r.amount
        for r in ScholarshipAllocation.objects.filter(invoice_line=l01, allocation_type="REVERSE")
    ) == D("45000.00")
    assert -dsum(
        r.amount
        for r in PaymentAllocation.objects.filter(invoice_line=l01, allocation_type="DEALLOCATE")
    ) == D("41400.00")
