"""Phase 6 fixtures: a clean world built only through Phase 1–5 services, and a test-only
injector that corrupts it the way DATABASE_DESIGN §7 describes (through the ORM, bypassing
the services, as the Phase 7 injector will). Nothing here is Phase 7 data generation.

Import into a test folder's conftest with ``from tests.reconciliation_fixtures import *``.

The clean world (FALL invoices from tests/billing_fixtures.py: TUITION 144,000, LAB 6,000,
REGISTRATION 5,000, TECHNOLOGY 4,000, LIBRARY 2,000, STUDENT_ACTIVITY 3,000; issued 08-17,
due 09-14; term 09-07 … 12-19):

* S1  merit + STEM applied, guardian AUTO payment, sponsor overpayment, refunds REQUESTED /
      APPROVED / REJECTED / PROCESSED, then a day-10 withdrawal (scholarship reversal, LIFO
      releases) and a WITHDRAWAL refund
* S2  full-ride (SCHOLARSHIP_COVERED)
* S3  need grant in two awards, one applied and partly reversed by hand; an unapplied award
* S4  payment MANUAL then a scholarship applied over it (BR-ALC-06 release)
* S5  invoice voided; the student put ON_HOLD and back
* S6  unpaid: a late fee, collection refreshed to OVERDUE
* S7  paid, then cancelled before the start (CANCELLATION_RELEASE) and a CANCELLATION refund
* S8–S14  withdrawals on days 0, 7, 8, 14, 15, 27, 28 (every BR-WDR-02 boundary)
* S15 a student with no invoice
* every student snapshotted on four dates
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from django.core.management import call_command

from academics import services as academics
from billing import services as billing
from billing.models import FeeSchedule, InvoiceLine
from payments import services as payments
from payments.models import Payment
from scholarships import services as scholarships
from students import services as students

FALL_START = date(2026, 9, 7)
SNAPSHOT_DATES = (date(2026, 8, 31), date(2026, 9, 30), date(2026, 12, 19), date(2026, 12, 31))
RUN_DATES = (date(2026, 8, 20), date(2026, 9, 20), date(2026, 10, 31), date(2026, 12, 31))


def _line(inv, n):
    return InvoiceLine.objects.select_related("invoice").get(pk=f"{inv.pk}-L{n:02d}")


_refs = iter(range(1, 10_000))


def pay(student, amount, *, on, payer="SELF", auto=False):
    payment, _ = payments.record_payment(
        student=student,
        amount=D(amount),
        received_date=on,
        method="SPONSOR_TRANSFER" if payer == "SPONSOR" else "UPI",
        payer_type=payer,
        external_reference=f"REC-{next(_refs):05d}",
        auto_allocate=auto,
        actor="world",
    )
    return payment


@pytest.fixture
def clean_world(student_with_invoice, make_student, fall, terms):
    """Build the clean world; returns {name: student}."""
    call_command("seed_scholarship_types", verbosity=0)
    world = {}

    def elig(student):
        return {
            e.scholarship_type_id: e
            for e in scholarships.evaluate_eligibility(student=student, term=fall)
        }

    # S1: scholarships, payments, refunds in every state, withdrawal, WITHDRAWAL refund
    s1, inv1 = student_with_invoice(gpa="3.70", need_index="0.20")
    r1 = elig(s1)
    for t in ("SCT-001", "SCT-004"):
        if t in r1 and r1[t].is_eligible:
            scholarships.apply_scholarship(
                scholarships.award_scholarship(eligibility=r1[t], invoice=inv1, actor="w"),
                actor="w",
            )
    pay(s1, "60000.00", on=date(2026, 8, 25), payer="GUARDIAN", auto=True)
    sponsor = pay(s1, "60000.00", on=date(2026, 9, 1), payer="SPONSOR", auto=True)
    req = date(2026, 9, 2)
    r_proc = payments.request_refund(
        payment=sponsor, amount=D("1000.00"), requested_date=req, actor="w"
    )
    payments.approve_refund(r_proc, on_date=date(2026, 9, 3), actor="w")
    payments.process_refund(r_proc, processed_date=date(2026, 9, 4), actor="w")
    r_rej = payments.request_refund(
        payment=sponsor, amount=D("500.00"), requested_date=req, actor="w"
    )
    payments.reject_refund(r_rej, on_date=date(2026, 9, 3), actor="w")
    r_appr = payments.request_refund(
        payment=sponsor, amount=D("400.00"), requested_date=req, actor="w"
    )
    payments.approve_refund(r_appr, on_date=date(2026, 9, 3), actor="w")
    payments.request_refund(payment=sponsor, amount=D("300.00"), requested_date=req, actor="w")
    academics.withdraw_enrollment(inv1.enrollment, withdrawal_date=date(2026, 9, 17), actor="w")
    inv1.enrollment.refresh_from_db()
    quote = payments.refundable_summary(s1)
    best = max(quote["payments"], key=lambda p: p["available"])
    if best["available"] > 0:
        payments.request_refund(
            payment=Payment.objects.get(pk=best["payment_id"]),
            amount=min(best["available"], quote["max_refundable"]),
            requested_date=date(2026, 9, 18),
            reason="WITHDRAWAL",
            enrollment=inv1.enrollment,
            actor="w",
        )
    world["S1"] = s1

    # S2: full ride
    s2, inv2 = student_with_invoice(gpa="3.90", need_index="0.90")
    scholarships.apply_scholarship(
        scholarships.award_scholarship(eligibility=elig(s2)["SCT-007"], invoice=inv2, actor="w"),
        actor="w",
    )
    academics.complete_enrollment(inv2.enrollment, on_date=date(2026, 12, 20), actor="w")
    world["S2"] = s2

    # S3: need grant in two awards; one applied and partly reversed; one unapplied
    s3, inv3 = student_with_invoice(gpa="3.40", need_index="0.65")
    # as tests/verification/test_verify_scholarships.py: the same need type (index 3) twice
    a3 = scholarships.award_scholarship(
        eligibility=scholarships.evaluate_eligibility(student=s3, term=fall)[3],
        invoice=inv3,
        requested_amount=D("15000.00"),
        actor="w",
    )
    scholarships.apply_scholarship(a3, actor="w")
    scholarships.reverse_allocation(
        a3.allocations.get(),
        amount=D("5000.00"),
        reason="WITHDRAWAL",
        on_date=date(2026, 9, 17),
        actor="w",
    )
    scholarships.award_scholarship(
        eligibility=scholarships.evaluate_eligibility(student=s3, term=fall)[3],
        invoice=inv3,
        actor="w",
    )
    world["S3"] = s3

    # S4: payment first, scholarship over it (BR-ALC-06 release)
    s4, inv4 = student_with_invoice(gpa="3.70", need_index="0.20")
    p4 = pay(s4, "144000.00", on=date(2026, 8, 18))
    payments.allocate_payment(
        p4,
        mode="MANUAL",
        lines=[(_line(inv4, 1), D("144000.00"))],
        allocated_on=date(2026, 8, 18),
        actor="w",
    )
    scholarships.apply_scholarship(
        scholarships.award_scholarship(eligibility=elig(s4)["SCT-001"], invoice=inv4, actor="w"),
        allocated_on=date(2026, 8, 20),
        actor="w",
    )
    world["S4"] = s4

    # S5: void; ON_HOLD and back
    s5, inv5 = student_with_invoice()
    billing.void_invoice(inv5, void_date=date(2026, 8, 20), actor="w")
    students.change_status(
        s5, to_status="ON_HOLD", event_date=date(2026, 8, 21), reason="HOLD", actor="w"
    )
    students.change_status(
        s5, to_status="ACTIVE", event_date=date(2026, 8, 28), reason="RELEASE", actor="w"
    )
    world["S5"] = s5

    # S6: unpaid → late fee → overdue (assessed after the other invoices exist)
    s6, _ = student_with_invoice()
    world["S6"] = s6

    # S7: paid, cancelled before the start, CANCELLATION refund
    s7, inv7 = student_with_invoice()
    pay(s7, "30000.00", on=date(2026, 8, 18), auto=True)
    academics.cancel_enrollment(inv7.enrollment, cancellation_date=date(2026, 8, 25), actor="w")
    inv7.enrollment.refresh_from_db()
    quote = payments.refundable_summary(s7)
    if quote["max_refundable"] > 0:
        payments.request_refund(
            payment=Payment.objects.get(pk=quote["payments"][0]["payment_id"]),
            amount=quote["max_refundable"],
            requested_date=date(2026, 8, 26),
            reason="CANCELLATION",
            enrollment=inv7.enrollment,
            actor="w",
        )
    world["S7"] = s7

    # S8–S14: every BR-WDR-02 boundary, each fully paid first
    for day in (0, 7, 8, 14, 15, 27, 28):
        s, inv = student_with_invoice()
        pay(s, "164000.00", on=date(2026, 8, 20), auto=True)
        academics.withdraw_enrollment(
            inv.enrollment,
            withdrawal_date=date.fromordinal(FALL_START.toordinal() + day),
            actor="w",
        )
        world[f"D{day:02d}"] = s

    # S15: no invoice
    world["S15"] = make_student()

    # S16: partial payment (AUTO, fully used on tuition); evaluated, not awarded
    s16, _ = student_with_invoice(gpa="3.70", need_index="0.20")
    pay(s16, "50000.00", on=date(2026, 8, 25), auto=True)
    elig(s16)
    world["S16"] = s16

    # S18: international (INTERNATIONAL_SERVICE line), with a hardship waiver, partly paid
    s18 = make_student(residency="INTERNATIONAL")
    enr18 = academics.enroll(
        student=s18, term=fall, credits=16, enrolled_on=date(2026, 8, 10), actor="w"
    )
    billing.generate_invoice(
        enrollment=enr18,
        waivers=[NS(fee_type="STUDENT_ACTIVITY", amount=D("3000.00"), reason="HARDSHIP")],
        actor="w",
    )
    pay(s18, "100000.00", on=date(2026, 8, 25), payer="SPONSOR", auto=True)
    world["S18"] = s18

    # S17: one payment, fully allocated
    s17, _ = student_with_invoice()
    pay(s17, "20000.00", on=date(2026, 8, 25), auto=True)
    world["S17"] = s17

    # a V2 fee version effective after the FALL invoice date (BR-FEE-03 chain)
    v1 = FeeSchedule.objects.filter(fee_type="LIBRARY", version=1).order_by("pk").first()
    billing.new_fee_schedule_version(v1, amount=D("2500.00"), effective_from=date(2026, 9, 1))

    billing.assess_late_fees(as_of=date(2026, 10, 1), dry_run=False, actor="w")
    billing.refresh_collection_status(as_of=date(2026, 12, 31), actor="w")
    return world


# --- test-only anomaly injection (DATABASE_DESIGN §7 methods; ORM writes, no services) --------

from billing import calculators as billing_calc  # noqa: E402
from billing.models import Invoice, InvoiceEvent, LedgerEntry  # noqa: E402
from core.ids import next_id  # noqa: E402
from payments.models import PaymentAllocation, Refund, RefundEvent  # noqa: E402
from reconciliation.models import BalanceSnapshot  # noqa: E402
from scholarships import calculators as sch_calc  # noqa: E402
from scholarships.models import Eligibility, Scholarship, ScholarshipEvent  # noqa: E402


def _evt(model, entity, owner_field, owner, frm, to, on, **extra):
    model.objects.create(
        event_id=next_id("status_event", entity=entity, year=on.year),
        from_status=frm,
        to_status=to,
        event_date=on,
        reason="IMPORTED",
        actor="legacy",
        **{owner_field: owner},
        **extra,
    )


def inject_payment_over_allocated(student) -> str:
    """APPEND one ALLOCATE row on an already fully allocated payment, to an open line of the
    same student (the line stays open ≥ 0). Returns payment_id."""
    p = Payment.objects.filter(student=student).order_by("pk").first()
    inv = Invoice.objects.filter(student=student).order_by("pk").first()
    line = _line(inv, 2)  # LAB, untouched by the tuition-first AUTO allocation
    PaymentAllocation.objects.create(
        allocation_id=next_id("payment_allocation", parent=p.pk),
        payment=p,
        invoice_line=line,
        allocation_type="ALLOCATE",
        amount=D("1000.00"),
        rule_applied="MANUAL",
        allocated_on=date(2026, 8, 26),
    )
    return p.pk


def inject_scholarship_over_award(student) -> str:
    """APPEND an ACCEPTED award at 1.2 × the eligible base, on an unused positive
    Eligibility row without a group conflict (+2 events). Returns scholarship_id."""
    elg = (
        Eligibility.objects.filter(student=student, is_eligible=True, scholarship__isnull=True)
        .select_related("scholarship_type")
        .order_by("pk")
        .first()
    )
    inv = Invoice.objects.get(student=student, term=elg.term)
    base = sch_calc.eligible_fee_base(elg.scholarship_type, list(inv.lines.all()))
    on = inv.issue_date
    s = Scholarship.objects.create(
        scholarship_id=next_id("scholarship", year=on.year),
        student=student,
        scholarship_type=elg.scholarship_type,
        term=elg.term,
        eligibility=elg,
        invoice=inv,
        awarded_amount=(base * D("1.2")).quantize(D("0.01")),
        award_date=on,
        status="ACCEPTED",
    )
    _evt(ScholarshipEvent, "SCH", "scholarship", s, None, "OFFERED", on)
    _evt(ScholarshipEvent, "SCH", "scholarship", s, "OFFERED", "ACCEPTED", on)
    return s.pk


def inject_duplicate_invoice(student) -> str:
    """APPEND a second invoice for the same enrollment: same issue date, same FEE lines,
    CHARGE entries, events. Returns the later (duplicate) invoice_id."""
    orig = Invoice.objects.filter(student=student).order_by("pk").first()
    dup = Invoice.objects.create(
        invoice_id=next_id("invoice", parent=student.pk, year=orig.issue_date.year),
        student=student,
        enrollment=orig.enrollment,
        term=orig.term,
        issue_date=orig.issue_date,
        due_date=orig.due_date,
        total_amount=orig.total_amount,
        lifecycle_status="ISSUED",
        collection_status="UNPAID",
        collection_status_as_of=orig.issue_date,
    )
    for line in orig.lines.filter(line_type="FEE").order_by("pk"):
        new = InvoiceLine.objects.create(
            line_id=next_id("invoice_line", parent=dup.pk),
            invoice=dup,
            **{
                f.attname: getattr(line, f.attname)
                for f in InvoiceLine._meta.concrete_fields
                if f.attname not in ("line_id", "invoice_id", "recorded_at")
            },
        )
        LedgerEntry.objects.create(
            entry_id=next_id("ledger_entry", year=new.posted_date.year),
            student=student,
            entry_date=new.posted_date,
            entry_type="CHARGE",
            amount=billing_calc.ledger_amount("CHARGE", new.net_amount),
            invoice_line=new,
        )
    for dim, to in (("LIFECYCLE", "ISSUED"), ("COLLECTION", "UNPAID")):
        _evt(InvoiceEvent, "INV", "invoice", dup, None, to, dup.issue_date, dimension=dim)
    return dup.pk


def inject_refund_over_net_paid(student) -> str:
    """APPEND a PROCESSED refund above net paid (+3 events, +1 REFUND entry). Returns
    refund_id. Consequence (BR-ALC-01 counts refunds): its payment is over-allocated."""
    p = Payment.objects.filter(student=student).order_by("pk").first()
    req, done = date(2026, 9, 2), date(2026, 9, 10)
    r = Refund(
        refund_id=next_id("refund", year=req.year),
        student=student,
        payment=p,
        payer_type=p.payer_type,
        reason="OVERPAYMENT",
        amount=p.amount + D("5000.00"),
        requested_date=req,
        processed_date=done,
        status="PROCESSED",
        calculation={},
    )
    r.save(force_insert=True)
    _evt(RefundEvent, "REF", "refund", r, None, "REQUESTED", req)
    _evt(RefundEvent, "REF", "refund", r, "REQUESTED", "APPROVED", done)
    _evt(RefundEvent, "REF", "refund", r, "APPROVED", "PROCESSED", done)
    LedgerEntry.objects.create(
        entry_id=next_id("ledger_entry", year=done.year),
        student=student,
        entry_date=done,
        entry_type="REFUND",
        amount=billing_calc.ledger_amount("REFUND", r.amount),
        refund=r,
    )
    return r.pk


def inject_balance_mismatch(student, *, delta=D("2500.00")) -> str:
    """MODIFY reported_balance of one snapshot (the original stays the ledger value).
    ``QuerySet.update`` bypasses the snapshot's append-only guard, as legacy corruption
    would. Returns snapshot_id."""
    snap = BalanceSnapshot.objects.filter(student=student).order_by("pk").first()
    BalanceSnapshot.objects.filter(pk=snap.pk).update(
        reported_balance=snap.reported_balance + delta
    )
    return snap.pk
