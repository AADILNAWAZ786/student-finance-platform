"""payments.services refunds: caps BR-REF-01/02 with pending reservations (D-P7), re-checks at
approval and processing that leave the refund itself out (D-P8), the request-time snapshot,
lifecycle BR-REF-03, payer BR-REF-04, ledger posting on PROCESSED only, atomicity, and the
ARCHITECTURE §7 worked example (Stages A–C) rebuilt from services."""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from django.core.exceptions import ValidationError

from billing import services as billing
from billing.models import InvoiceLine, LedgerEntry
from core.exceptions import BusinessRuleViolation, StateConflict
from core.models import IdSequence
from payments import calculators as calc
from payments import services
from payments.models import PaymentAllocation, Refund, RefundEvent
from scholarships import services as scholarships
from tests.payment_fixtures import ISSUE

pytestmark = pytest.mark.django_db

REQ, APPROVE, PROCESS = date(2026, 8, 21), date(2026, 8, 22), date(2026, 8, 23)


def _counts():
    return (
        Refund.objects.count(),
        RefundEvent.objects.count(),
        LedgerEntry.objects.count(),
        list(IdSequence.objects.order_by("key").values_list("key", "last_value")),
    )


def _events(refund):
    return [
        (e.from_status, e.to_status, e.event_date, e.reason)
        for e in refund.events.order_by("event_date", "event_id")
    ]


@pytest.fixture
def overpaid(invoice, record):
    """174,000 received; 150,000 on L01/L02: 14,000 still owed, 24,000 unallocated on the
    payment; ledger −10,000 (credit balance 10,000)."""
    payment = record("174000.00", payer="SPONSOR")
    services.allocate_payment(
        payment,
        mode="MANUAL",
        lines=[
            (InvoiceLine.objects.get(pk=f"{invoice.pk}-L01"), D("144000.00")),
            (InvoiceLine.objects.get(pk=f"{invoice.pk}-L02"), D("6000.00")),
        ],
        allocated_on=ISSUE,
        actor="t",
    )
    return payment


# --- request (∅ → REQUESTED) ------------------------------------------------------------------


def test_request_refund_reserves_and_snapshots_the_caps(overpaid, request_refund):
    refund = request_refund(overpaid, "4000.00")
    assert (refund.refund_id, refund.status, refund.payer_type, refund.reason) == (
        "REF-2026-000001",
        "REQUESTED",
        "SPONSOR",  # BR-REF-04: back to the payer of the source payment
        "OVERPAYMENT",
    )
    assert refund.calculation == {
        "net_paid": "174000.00",
        "credit_balance": "10000.00",
        "payment_available": "24000.00",
        "schedule_pct": None,
        "pending_refunds": "0.00",
        "credit_available": "10000.00",
        "max_refundable": "10000.00",
    }
    assert _events(refund) == [(None, "REQUESTED", REQ, "REQUESTED")]
    assert refund.events.get().source_ref == overpaid.payment_id
    assert not LedgerEntry.objects.filter(entry_type="REFUND").exists()  # BR-REF-03
    assert services.available(overpaid) == D("20000.00")  # reserved


def test_BR_REF_02_credit_cap_message_and_details(overpaid, request_refund):
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        request_refund(overpaid, "10000.01")
    assert (exc.value.code, exc.value.rule_id) == ("REFUND_EXCEEDS_CREDIT_BALANCE", "BR-REF-02")
    assert exc.value.details == {
        "payment_id": "PAY-2026-000001",
        "amount": "10000.01",
        "net_paid": "174000.00",
        "ledger_balance": "-10000.00",
        "credit_balance": "10000.00",
        "pending_refunds": "0.00",
        "credit_available": "10000.00",
        "payment_available": "24000.00",
        "max_refundable": "10000.00",
    }
    assert _counts() == before


def test_BR_REF_02_two_pending_requests_cannot_spend_the_same_credit(overpaid, request_refund):
    request_refund(overpaid, "6000.00")
    with pytest.raises(BusinessRuleViolation) as exc:
        request_refund(overpaid, "4000.01")
    assert exc.value.code == "REFUND_EXCEEDS_CREDIT_BALANCE"
    assert (exc.value.details["pending_refunds"], exc.value.details["credit_available"]) == (
        "6000.00",
        "4000.00",
    )
    request_refund(overpaid, "4000.00")  # exactly what is left


def test_BR_REF_02_payment_available_cap(invoice, record, request_refund):
    first = record("194000.00", auto=True)  # 164,000 allocated: 30,000 available
    record("10000.00")  # credit balance 40,000
    with pytest.raises(BusinessRuleViolation) as exc:
        request_refund(first, "30000.01")
    assert (exc.value.code, exc.value.details["payment_available"]) == (
        "REFUND_EXCEEDS_PAYMENT_AVAILABLE",
        "30000.00",
    )


def _adjust_tuition(inv, amount, on):
    """An ADJUSTMENT line on tuition, as a Phase 5 withdrawal credit would post it."""
    line = InvoiceLine.objects.create(
        line_id=f"{inv.pk}-L20",
        invoice=inv,
        line_type="ADJUSTMENT",
        fee_type="TUITION",
        description="credit",
        quantity=1,
        unit_amount=amount,
        gross_amount=amount,
        net_amount=amount,
        adjusts_line=inv.lines.get(fee_type="TUITION"),
        adjustment_reason="WITHDRAWAL",
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=False,
        allocation_priority=10,
        posted_date=on,
    )
    billing.post_ledger(line)


def test_BR_REF_01_net_paid_subtracts_pending_refunds(merit_award, invoice, record, request_refund):
    """Credit above net paid (a tuition credit before the scholarship release Phase 5 will
    post): 50,000 + 39,000 received, unallocated; ledger −140,000. A pending 50,000 refund
    from PAY-A leaves net paid 39,000, so 39,000.01 from PAY-B breaks BR-REF-01 (counting
    only PROCESSED refunds would have allowed 89,000 and failed on the payment cap instead)."""
    scholarships.apply_scholarship(merit_award, actor="t")
    pay_a, pay_b = record("50000.00"), record("39000.00")
    _adjust_tuition(invoice, D("-140000.00"), date(2026, 8, 20))
    request_refund(pay_a, "50000.00")
    with pytest.raises(BusinessRuleViolation) as exc:
        request_refund(pay_b, "39000.01")
    assert (exc.value.code, exc.value.rule_id) == ("REFUND_EXCEEDS_NET_PAID", "BR-REF-01")
    assert (exc.value.details["net_paid"], exc.value.details["credit_available"]) == (
        "39000.00",
        "90000.00",
    )


def test_BR_REF_01_rejected_refunds_do_not_reduce_net_paid(overpaid, request_refund):
    refund = request_refund(overpaid, "10000.00")
    services.reject_refund(refund, on_date=APPROVE, actor="t")
    caps = services.refund_caps(overpaid)
    assert (caps.net_paid, caps.pending_refunds, caps.payment_available) == (
        D("174000.00"),
        D("0.00"),
        D("24000.00"),
    )
    request_refund(overpaid, "10000.00")  # the reservation was released


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"amount": D("0.00")}, "amount"),
        ({"reason": "GOODWILL"}, "reason"),
        ({"reason": "WITHDRAWAL"}, "enrollment"),
        ({"requested_date": date(2026, 8, 16)}, "requested_date"),
        ({"requested_date": date(2027, 1, 2)}, "requested_date"),
    ],
)
def test_request_validation(overpaid, kw, field):
    args = dict(payment=overpaid, amount=D("1.00"), requested_date=REQ, actor="t")
    args.update(kw)
    with pytest.raises(ValidationError) as exc:
        services.request_refund(**args)
    assert list(exc.value.message_dict) == [field]


def test_enrollment_only_for_withdrawal_or_cancellation(overpaid, fall_enrollment, make_student):
    with pytest.raises(ValidationError) as exc:
        services.request_refund(
            payment=overpaid,
            amount=D("1.00"),
            requested_date=REQ,
            enrollment=fall_enrollment,
            actor="t",
        )
    assert exc.value.message_dict == {"enrollment": ["Must be omitted when reason is OVERPAYMENT."]}
    # Phase 5 (D-W6): a WITHDRAWAL refund needs the enrollment to have been withdrawn
    with pytest.raises(ValidationError) as exc:
        services.request_refund(
            payment=overpaid,
            amount=D("1.00"),
            requested_date=REQ,
            reason="WITHDRAWAL",
            enrollment=fall_enrollment,
            actor="t",
        )
    assert exc.value.message_dict == {
        "enrollment": [
            f"{fall_enrollment.pk} is ENROLLED; a WITHDRAWAL refund needs a WITHDRAWN enrollment."
        ]
    }
    from academics import services as academics

    academics.withdraw_enrollment(fall_enrollment, withdrawal_date=date(2026, 9, 17), actor="t")
    fall_enrollment.refresh_from_db()
    refund = services.request_refund(  # the model supports Phase 5's reasons (D-P14)
        payment=overpaid,
        amount=D("1.00"),
        requested_date=REQ,
        reason="WITHDRAWAL",
        enrollment=fall_enrollment,
        schedule_pct=D("0.60"),
        actor="t",
    )
    assert (refund.enrollment_id, refund.calculation["schedule_pct"]) == (
        fall_enrollment.pk,
        "0.60",
    )


# --- approve / process / reject (BR-REF-03, D-P8) ------------------------------------------------


def test_full_lifecycle_posts_the_refund_entry_only_when_processed(overpaid, request_refund):
    refund = request_refund(overpaid, "10000.00")  # the whole credit balance
    services.approve_refund(refund, on_date=APPROVE, actor="t")  # does not reserve itself twice
    assert not LedgerEntry.objects.filter(entry_type="REFUND").exists()
    services.process_refund(refund, processed_date=PROCESS, actor="t")  # nor here
    refund.refresh_from_db()
    assert (refund.status, refund.processed_date) == ("PROCESSED", PROCESS)
    entry = LedgerEntry.objects.get(refund=refund)
    assert (entry.entry_type, entry.amount, entry.entry_date) == ("REFUND", D("10000.00"), PROCESS)
    assert _events(refund) == [
        (None, "REQUESTED", REQ, "REQUESTED"),
        ("REQUESTED", "APPROVED", APPROVE, "APPROVED"),
        ("APPROVED", "PROCESSED", PROCESS, "PROCESSED"),
    ]
    b = billing.student_balance(overpaid.student, None)
    assert (b["ledger_balance"], b["breakdown"]["refunds"], b["unallocated_credit"]) == (
        D("0.00"),
        D("10000.00"),
        D("14000.00"),
    )
    assert b["identity"]["holds"] is True  # 14,000 owed − 14,000 unallocated = 0


def test_calculation_snapshot_is_never_rewritten(overpaid, request_refund):
    refund = request_refund(overpaid, "5000.00")
    snapshot = dict(refund.calculation)
    request_refund(overpaid, "1000.00")  # caps change after the request
    services.approve_refund(refund, on_date=APPROVE, actor="t")
    services.process_refund(refund, processed_date=PROCESS, actor="t")
    refund.refresh_from_db()
    assert refund.calculation == snapshot


def test_approval_rechecks_the_caps_after_a_late_fee(overpaid, request_refund):
    refund = request_refund(overpaid, "10000.00")
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")  # +500
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        services.approve_refund(refund, on_date=date(2026, 9, 30), actor="t")
    assert exc.value.code == "REFUND_EXCEEDS_CREDIT_BALANCE"
    assert exc.value.details["refund_id"] == "REF-2026-000001"
    assert (exc.value.details["credit_balance"], exc.value.details["pending_refunds"]) == (
        "9500.00",
        "0.00",  # the refund itself is not counted against itself
    )
    assert _counts() == before
    refund.refresh_from_db()
    assert refund.status == "REQUESTED"


def test_processing_rechecks_the_caps(overpaid, request_refund):
    refund = request_refund(overpaid, "10000.00")
    services.approve_refund(refund, on_date=APPROVE, actor="t")
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")
    with pytest.raises(BusinessRuleViolation):
        services.process_refund(refund, processed_date=date(2026, 9, 30), actor="t")
    refund.refresh_from_db()
    assert (refund.status, refund.processed_date) == ("APPROVED", None)
    assert not LedgerEntry.objects.filter(entry_type="REFUND").exists()
    services.reject_refund(refund, on_date=date(2026, 9, 30), actor="t")  # APPROVED → REJECTED


@pytest.mark.parametrize(
    ("steps", "action", "target"),
    [
        ([], "process", "PROCESSED"),  # REQUESTED → PROCESSED skips approval
        (["approve"], "approve", "APPROVED"),
        (["approve", "process"], "reject", "REJECTED"),
        (["reject"], "approve", "APPROVED"),
        (["reject"], "reject", "REJECTED"),
    ],
)
def test_BR_REF_03_illegal_transitions_are_409(overpaid, request_refund, steps, action, target):
    refund = request_refund(overpaid, "100.00")
    run = {
        "approve": lambda: services.approve_refund(refund, on_date=APPROVE, actor="t"),
        "process": lambda: services.process_refund(refund, processed_date=PROCESS, actor="t"),
        "reject": lambda: services.reject_refund(refund, on_date=PROCESS, actor="t"),
    }
    for step in steps:
        run[step]()
    refund.refresh_from_db()
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        run[action]()
    assert (exc.value.code, exc.value.rule_id) == ("ILLEGAL_TRANSITION", "BR-REF-03")
    assert (exc.value.details["from_status"], exc.value.details["to_status"]) == (
        refund.status,
        target,
    )
    assert _counts() == before


def test_status_dates_never_go_back_and_stay_in_2026(overpaid, request_refund):
    refund = request_refund(overpaid, "100.00")
    with pytest.raises(ValidationError) as exc:
        services.approve_refund(refund, on_date=date(2026, 8, 20), actor="t")
    assert exc.value.message_dict == {
        "on_date": ["Must be on or after 2026-08-21, the refund's latest status date."]
    }
    services.approve_refund(refund, on_date=date(2026, 9, 1), actor="t")
    with pytest.raises(ValidationError) as exc:
        services.process_refund(refund, processed_date=date(2026, 8, 31), actor="t")
    assert list(exc.value.message_dict) == ["processed_date"]
    with pytest.raises(ValidationError):
        services.process_refund(refund, processed_date=date(2027, 1, 1), actor="t")
    services.process_refund(refund, processed_date=date(2026, 12, 31), actor="t")


def test_process_rolls_back_when_the_ledger_post_fails(overpaid, request_refund, monkeypatch):
    refund = services.approve_refund(request_refund(overpaid, "100.00"), on_date=APPROVE, actor="t")

    def broken(r):
        raise RuntimeError("disk full")

    monkeypatch.setattr(billing, "post_refund_ledger", broken)
    before = _counts()
    with pytest.raises(RuntimeError):
        services.process_refund(refund, processed_date=PROCESS, actor="t")
    assert _counts() == before
    refund.refresh_from_db()
    assert (refund.status, refund.processed_date) == ("APPROVED", None)


def test_request_rolls_back_when_the_event_write_fails(overpaid, monkeypatch):
    monkeypatch.setattr(services, "_refund_event", lambda *a, **k: 1 / 0)
    before = _counts()
    with pytest.raises(ZeroDivisionError):
        services.request_refund(payment=overpaid, amount=D("1.00"), requested_date=REQ, actor="t")
    assert _counts() == before


def test_refundable_summary(overpaid, record, request_refund):
    second = record("2000.00", payer="GUARDIAN", on=date(2026, 8, 20))
    request_refund(overpaid, "3000.00")
    summary = services.refundable_summary(overpaid.student)
    assert summary["calculation"] == {
        "net_paid": D("173000.00"),
        "ledger_balance": D("-12000.00"),
        "credit_balance": D("12000.00"),
        "pending_refunds": D("3000.00"),
        "credit_available": D("9000.00"),
        "payments_available": D("23000.00"),
    }
    assert summary["max_refundable"] == D("9000.00")
    assert [
        (p["payment_id"], p["available"], p["max_refundable"]) for p in summary["payments"]
    ] == [
        (overpaid.payment_id, D("21000.00"), D("9000.00")),
        (second.payment_id, D("2000.00"), D("2000.00")),
    ]


# --- ARCHITECTURE §7: the worked example, Stages A–C ---------------------------------------------

WORKED_PRICES = [
    ("TUITION", "DOMESTIC", "PER_CREDIT", "10000.00"),
    ("TUITION", "INTERNATIONAL", "PER_CREDIT", "16000.00"),
    ("LAB", "ALL", "FLAT", "12000.00"),
    ("REGISTRATION", "ALL", "FLAT", "5000.00"),
    ("TECHNOLOGY", "ALL", "FLAT", "4000.00"),
    ("LIBRARY", "ALL", "FLAT", "2000.00"),
    ("STUDENT_ACTIVITY", "ALL", "FLAT", "3000.00"),
    ("INTERNATIONAL_SERVICE", "INTERNATIONAL", "FLAT", "8000.00"),
]


def _adjust(inv, n, amount, on):
    target = inv.lines.get(line_id=f"{inv.pk}-L{n:02d}")
    line = InvoiceLine.objects.create(
        line_id=f"{inv.pk}-L{n + 10:02d}",
        invoice=inv,
        line_type="ADJUSTMENT",
        fee_type=target.fee_type,
        description=f"Withdrawal credit on {target.line_id}",
        quantity=1,
        unit_amount=amount,
        gross_amount=amount,
        net_amount=amount,
        adjusts_line=target,
        adjustment_reason="WITHDRAWAL",
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=False,
        allocation_priority=target.allocation_priority,
        posted_date=on,
    )
    billing.post_ledger(line)


def test_worked_example_stages_a_to_c(
    make_program, make_student, terms, scholarship_types, evaluate
):
    """§7 with real services. Stage B's withdrawal is posted with the same building blocks
    Phase 5 will orchestrate: payment releases (deallocate, WITHDRAWAL_RELEASE, LIFO), the
    ADJUSTMENT lines, the proportional scholarship reversal."""
    from academics import services as academics
    from tests.billing_fixtures import make_fees

    fall = terms["TERM-2026-FALL"]
    program = make_program(code="BSC-WE")
    make_fees(program, fall, prices=WORKED_PRICES)
    s = make_student(program=program)
    enrollment = academics.enroll(
        student=s, term=fall, credits=16, enrolled_on=date(2026, 8, 10), actor="t"
    )
    inv = billing.generate_invoice(
        enrollment=enrollment,
        waivers=[NS(fee_type="STUDENT_ACTIVITY", amount=D("3000.00"), reason="HARDSHIP")],
        actor="t",
    )
    assert (inv.issue_date, inv.total_amount) == (date(2026, 8, 17), D("183000.00"))

    # Stage A
    award = scholarships.award_scholarship(
        eligibility=evaluate(s)["SCT-001"], invoice=inv, actor="t"
    )
    assert award.awarded_amount == D("86000.00")
    scholarships.apply_scholarship(award, allocated_on=date(2026, 8, 17), actor="t")

    def pay(amount, payer, on):
        p, result = services.record_payment(
            student=s,
            amount=D(amount),
            received_date=on,
            method="BANK_TRANSFER",
            payer_type=payer,
            external_reference=f"WE-{payer}",
            auto_allocate=True,
            actor="t",
        )
        return p, result

    pay1, r1 = pay("60000.00", "GUARDIAN", date(2026, 8, 25))
    assert [(a.invoice_line_id[-3:], a.amount) for a in r1.allocations] == [("L01", D("60000.00"))]
    inv.refresh_from_db()
    assert inv.collection_status == "PARTIALLY_PAID"
    pay2, r2 = pay("50000.00", "SPONSOR", date(2026, 9, 10))
    assert [(a.invoice_line_id[-3:], a.amount) for a in r2.allocations] == [
        ("L01", D("14000.00")),
        ("L02", D("12000.00")),
        ("L03", D("5000.00")),
        ("L04", D("4000.00")),
        ("L05", D("2000.00")),
    ]  # L06 skipped (open 0)
    assert r2.unallocated_after == D("13000.00")
    a = billing.current_amounts(inv)
    assert (a.scholarship_applied, a.amount_paid, a.outstanding) == (
        D("86000.00"),
        D("97000.00"),
        D("0.00"),
    )
    b = billing.student_balance(s, None)
    assert (b["ledger_balance"], b["unallocated_credit"], b["identity"]["holds"]) == (
        D("-13000.00"),
        D("13000.00"),
        True,
    )

    # Stage B (17 Sep, day 10 → 60%): release payments LIFO first, then the adjustments and
    # the scholarship reversal (BR-WDR-03)
    wd = date(2026, 9, 17)
    for line_no, excess in ((1, D("44400.00")), (2, D("7200.00"))):
        rows = list(PaymentAllocation.objects.filter(invoice_line_id=f"{inv.pk}-L{line_no:02d}"))
        for p in calc.plan_release_lifo(rows, excess):
            services.deallocate(
                PaymentAllocation.objects.get(pk=p.allocation_id),
                amount=p.amount,
                rule_applied="WITHDRAWAL_RELEASE",
                on_date=wd,
                actor="t",
            )
    released = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").order_by(
        "allocation_id"
    )
    assert sorted((r.payment_id, r.invoice_line_id[-3:], r.amount) for r in released) == sorted(
        [
            (pay2.payment_id, "L01", D("-14000.00")),
            (pay1.payment_id, "L01", D("-30400.00")),
            (pay2.payment_id, "L02", D("-7200.00")),
        ]
    )
    _adjust(inv, 1, D("-96000.00"), wd)
    _adjust(inv, 2, D("-7200.00"), wd)
    scholarships.reverse_allocation(
        award.allocations.get(), amount=D("51600.00"), reason="WITHDRAWAL", on_date=wd, actor="t"
    )
    a = billing.current_amounts(inv)
    assert (a.net_charges, a.scholarship_applied, a.amount_paid, a.outstanding) == (
        D("79800.00"),
        D("34400.00"),
        D("45400.00"),
        D("0.00"),
    )
    inv.refresh_from_db()
    assert (inv.lifecycle_status, inv.collection_status) == ("ISSUED", "PAID")
    b = billing.student_balance(s, None)
    assert (b["ledger_balance"], b["unallocated_credit"], b["identity"]["holds"]) == (
        D("-64600.00"),
        D("64600.00"),
        True,
    )

    # Stage C (requested 18 Sep, processed 30 Sep)
    summary = services.refundable_summary(s)
    assert summary["max_refundable"] == D("64600.00")
    assert summary["calculation"]["net_paid"] == D("110000.00")
    assert {p["payment_id"]: p["available"] for p in summary["payments"]} == {
        pay1.payment_id: D("30400.00"),
        pay2.payment_id: D("34200.00"),
    }
    req = date(2026, 9, 18)
    with pytest.raises(BusinessRuleViolation) as exc:
        services.request_refund(payment=pay1, amount=D("35000.00"), requested_date=req, actor="t")
    assert (exc.value.code, exc.value.details["payment_available"]) == (
        "REFUND_EXCEEDS_PAYMENT_AVAILABLE",
        "30400.00",
    )
    ref1 = services.request_refund(
        payment=pay2, amount=D("34200.00"), requested_date=req, actor="t"
    )
    ref2 = services.request_refund(
        payment=pay1, amount=D("30400.00"), requested_date=req, actor="t"
    )
    assert (ref1.payer_type, ref2.payer_type) == ("SPONSOR", "GUARDIAN")  # BR-REF-04
    for r in (ref1, ref2):
        services.approve_refund(r, on_date=date(2026, 9, 25), actor="t")
        services.process_refund(r, processed_date=date(2026, 9, 30), actor="t")
    b = billing.student_balance(s, None)
    assert (b["ledger_balance"], b["unallocated_credit"], b["invoice_outstanding"]) == (
        D("0.00"),
        D("0.00"),
        D("0.00"),
    )
    assert b["identity"]["holds"] is True
    net_paid = services.refund_caps(pay1).net_paid
    assert net_paid == D("45400.00") == billing.current_amounts(inv).amount_paid
    ledger = LedgerEntry.objects.filter(student=s)
    assert ledger.count() == 14  # 6 CHARGE, 1 SCH_CREDIT, 2 PAYMENT, 2 ADJ, 1 SCH_REV, 2 REFUND
    assert sorted(ledger.values_list("entry_type", flat=True)).count("REFUND") == 2


def test_worked_example_stages_b_and_c_through_the_withdraw_service(
    make_program, make_student, terms, scholarship_types, evaluate
):
    """§7 Stages B–C again, Phase 5 style: ``academics.withdraw_enrollment`` derives every
    number of the Stage B table itself (A, R, excess, the LIFO deallocations), and Stage C's
    refunds are requested with reason WITHDRAWAL against the withdrawn enrollment (D-W6)."""
    from academics import services as academics
    from tests.billing_fixtures import make_fees

    fall = terms["TERM-2026-FALL"]
    program = make_program(code="BSC-WE")
    make_fees(program, fall, prices=WORKED_PRICES)
    s = make_student(program=program)
    enrollment = academics.enroll(
        student=s, term=fall, credits=16, enrolled_on=date(2026, 8, 10), actor="t"
    )
    inv = billing.generate_invoice(
        enrollment=enrollment,
        waivers=[NS(fee_type="STUDENT_ACTIVITY", amount=D("3000.00"), reason="HARDSHIP")],
        actor="t",
    )
    award = scholarships.award_scholarship(
        eligibility=evaluate(s)["SCT-001"], invoice=inv, actor="t"
    )
    scholarships.apply_scholarship(award, allocated_on=date(2026, 8, 17), actor="t")
    pays = {}
    for amount, payer, on in (
        ("60000.00", "GUARDIAN", date(2026, 8, 25)),
        ("50000.00", "SPONSOR", date(2026, 9, 10)),
    ):
        pays[payer], _ = services.record_payment(
            student=s,
            amount=D(amount),
            received_date=on,
            method="BANK_TRANSFER",
            payer_type=payer,
            external_reference=f"WE5-{payer}",
            auto_allocate=True,
            actor="t",
        )

    # Stage B: one call
    result = academics.withdraw_enrollment(enrollment, withdrawal_date=date(2026, 9, 17), actor="t")
    assert result.refund_pct == D("0.60")
    plan = {p["adjusts_line"][-3:]: p for p in result.financial["invoice"]["release_plan"]}
    assert {
        k: (p["adjustment"], p["scholarship_reversal"], p["excess"]) for k, p in plan.items()
    } == {
        "L01": (D("-96000.00"), D("51600.00"), D("44400.00")),
        "L02": (D("-7200.00"), D("0.00"), D("7200.00")),
    }
    released = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE")
    assert sorted((r.payment_id, r.invoice_line_id[-3:], r.amount) for r in released) == sorted(
        [
            (pays["SPONSOR"].payment_id, "L01", D("-14000.00")),
            (pays["GUARDIAN"].payment_id, "L01", D("-30400.00")),
            (pays["SPONSOR"].payment_id, "L02", D("-7200.00")),
        ]
    )
    a = billing.current_amounts(inv)
    assert (a.net_charges, a.scholarship_applied, a.amount_paid, a.outstanding) == (
        D("79800.00"),
        D("34400.00"),
        D("45400.00"),
        D("0.00"),
    )
    inv.refresh_from_db()
    assert (inv.lifecycle_status, inv.collection_status) == ("ISSUED", "PAID")
    b = billing.student_balance(s, None)
    assert (b["ledger_balance"], b["unallocated_credit"], b["identity"]["holds"]) == (
        D("-64600.00"),
        D("64600.00"),
        True,
    )
    assert result.financial["refund_quote"]["max_refundable"] == D("64600.00")
    assert Refund.objects.count() == 0  # D-W6: never automatic

    # Stage C: WITHDRAWAL refunds tied to the withdrawn enrollment
    enrollment.refresh_from_db()
    req = date(2026, 9, 18)
    with pytest.raises(BusinessRuleViolation) as exc:
        services.request_refund(
            payment=pays["GUARDIAN"],
            amount=D("35000.00"),
            requested_date=req,
            reason="WITHDRAWAL",
            enrollment=enrollment,
            actor="t",
        )
    assert exc.value.code == "REFUND_EXCEEDS_PAYMENT_AVAILABLE"
    refunds = [
        services.request_refund(
            payment=pays[payer],
            amount=D(amount),
            requested_date=req,
            reason="WITHDRAWAL",
            enrollment=enrollment,
            actor="t",
        )
        for payer, amount in (("SPONSOR", "34200.00"), ("GUARDIAN", "30400.00"))
    ]
    for r in refunds:
        assert (r.enrollment_id, r.calculation["schedule_pct"]) == (enrollment.pk, "0.60")
        services.approve_refund(r, on_date=date(2026, 9, 25), actor="t")
        services.process_refund(r, processed_date=date(2026, 9, 30), actor="t")
    b = billing.student_balance(s, None)
    assert (b["ledger_balance"], b["unallocated_credit"], b["invoice_outstanding"]) == (
        D("0.00"),
        D("0.00"),
        D("0.00"),
    )
    assert b["identity"]["holds"] is True
    assert LedgerEntry.objects.filter(student=s).count() == 14  # as §7 states
