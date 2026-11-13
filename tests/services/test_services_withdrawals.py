"""Phase 5 services: academics.withdraw_enrollment / cancel_enrollment and everything they
drive through the registries (BR-WDR-01..03, BR-SCH-07, BR-INV-08, BR-ALC-02, BR-LED-01,
BR-BAL-02; decisions D-W1..D-W9).

The FALL invoice (STU-000001, 16 credits, enrolled 2026-08-10, issued 08-17, due 09-14;
term start 09-07, census 10-05, end 12-19): L01 TUITION 144,000 and L02 LAB 6,000 are
refundable; L03 REGISTRATION 5,000, L04 TECHNOLOGY 4,000, L05 LIBRARY 2,000,
L06 STUDENT_ACTIVITY 3,000 are not. Total 164,000.
"""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError

from academics import services as academics
from academics.models import Enrollment, EnrollmentEvent
from billing import services as billing
from billing.models import Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.exceptions import StateConflict
from core.models import IdSequence
from payments import services as payments
from payments.models import PaymentAllocation
from scholarships import services as scholarships
from scholarships.models import Scholarship, ScholarshipAllocation, ScholarshipEvent
from tests.payment_fixtures import line

pytestmark = pytest.mark.django_db

START = date(2026, 9, 7)
WD = date(2026, 9, 17)  # day 10 → 60%


def _counts():
    return (
        list(Enrollment.objects.order_by("pk").values_list("pk", "status")),
        EnrollmentEvent.objects.count(),
        InvoiceLine.objects.count(),
        LedgerEntry.objects.count(),
        InvoiceEvent.objects.count(),
        PaymentAllocation.objects.count(),
        ScholarshipAllocation.objects.count(),
        ScholarshipEvent.objects.count(),
        list(
            Invoice.objects.order_by("pk").values_list(
                "pk", "lifecycle_status", "collection_status"
            )
        ),
        list(IdSequence.objects.order_by("key").values_list("key", "last_value")),
    )


def _adjustments(invoice):
    return [
        (a.adjusts_line_id[-3:], a.net_amount, a.adjustment_reason, a.posted_date)
        for a in invoice.lines.filter(line_type="ADJUSTMENT").order_by("line_id")
    ]


def _identity(student):
    b = billing.student_balance(student, None)
    assert b["identity"]["holds"] is True, b
    return b


def withdraw(invoice, on=WD):
    return academics.withdraw_enrollment(invoice.enrollment, withdrawal_date=on, actor="t")


def cancel(invoice, on=date(2026, 8, 20)):
    return academics.cancel_enrollment(invoice.enrollment, cancellation_date=on, actor="t")


# --- withdrawal (BR-WDR-02) ---------------------------------------------------------------------


def test_withdrawal_adjusts_refundable_lines_and_keeps_the_invoice_issued(invoice):
    result = withdraw(invoice)
    enrollment = Enrollment.objects.get(pk=invoice.enrollment_id)
    assert (enrollment.status, enrollment.withdrawal_date) == ("WITHDRAWN", WD)
    event = enrollment.events.order_by("-event_id").first()
    assert (event.from_status, event.to_status, event.event_date, event.reason) == (
        "ENROLLED",
        "WITHDRAWN",
        WD,
        "WITHDRAWAL",
    )
    assert (result.kind, result.refund_pct) == ("WITHDRAWAL", D("0.60"))
    assert _adjustments(invoice) == [
        ("L01", D("-86400.00"), "WITHDRAWAL", WD),
        ("L02", D("-3600.00"), "WITHDRAWAL", WD),
    ]
    for adj in invoice.lines.filter(line_type="ADJUSTMENT"):
        entry = LedgerEntry.objects.get(invoice_line=adj)  # BR-LED-01
        assert (entry.entry_type, entry.amount, entry.entry_date) == (
            "ADJUSTMENT",
            adj.net_amount,
            WD,
        )
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "ISSUED"  # §7: a withdrawal does not cancel it
    assert invoice.collection_status == "OVERDUE"  # 74,000 owed after the 09-14 due date
    fin = result.financial["invoice"]
    assert fin["amounts"]["outstanding"] == D("74000.00")
    assert [
        (r["adjusts_line"], r["adjustment"], r["scholarship_reversal"], r["deallocations"])
        for r in fin["release_plan"]
    ] == [
        (f"{invoice.pk}-L01", D("-86400.00"), D("0.00"), []),
        (f"{invoice.pk}-L02", D("-3600.00"), D("0.00"), []),
    ]
    _identity(invoice.student)


@pytest.mark.parametrize(
    ("day", "pct", "tuition"),
    [
        (0, "0.80", "-115200.00"),
        (7, "0.80", "-115200.00"),
        (8, "0.60", "-86400.00"),
        (14, "0.60", "-86400.00"),
        (15, "0.40", "-57600.00"),
        (27, "0.40", "-57600.00"),
    ],
)
def test_BR_WDR_02_bands(invoice, day, pct, tuition):
    result = withdraw(invoice, on=START + timedelta(days=day))
    assert result.refund_pct == D(pct)
    assert _adjustments(invoice)[0][:2] == ("L01", D(tuition))


@pytest.mark.parametrize("day", [28, 29, 103])  # census date, after it, the term end
def test_BR_WDR_02_from_the_census_date_nothing_is_adjusted(invoice, day):
    before = LedgerEntry.objects.count()
    result = withdraw(invoice, on=START + timedelta(days=day))
    assert result.refund_pct == D("0.00")
    assert _adjustments(invoice) == [] and LedgerEntry.objects.count() == before
    assert Enrollment.objects.get(pk=invoice.enrollment_id).status == "WITHDRAWN"
    assert result.financial["invoice"]["release_plan"] == []


@pytest.mark.parametrize(
    ("on", "message"),
    [
        (
            date(2026, 9, 6),
            "Must be on or after the term start (2026-09-07); before the start, cancel the "
            "enrollment instead.",
        ),
        (date(2026, 12, 20), "Must be on or before the term end (2026-12-19)."),
    ],
)
def test_withdrawal_date_must_be_within_the_term(invoice, on, message):
    before = _counts()
    with pytest.raises(ValidationError) as exc:
        withdraw(invoice, on=on)
    assert exc.value.message_dict == {"withdrawal_date": [message]}
    assert _counts() == before


def test_withdrawal_date_not_before_enrolled_on(make_student, fee_setup, enroll_fall):
    s = make_student()
    late = enroll_fall(s, enrolled_on=date(2026, 9, 14))  # add/drop: start + 7
    with pytest.raises(ValidationError) as exc:
        academics.withdraw_enrollment(late, withdrawal_date=date(2026, 9, 10), actor="t")
    assert exc.value.message_dict == {
        "withdrawal_date": ["Must be on or after enrolled_on (2026-09-14)."]
    }


def test_repeated_requests_are_409_and_change_nothing(invoice):
    withdraw(invoice)
    before = _counts()
    for again in (lambda: withdraw(invoice), lambda: cancel(invoice)):
        with pytest.raises(StateConflict) as exc:
            again()
        assert exc.value.code == "ILLEGAL_TRANSITION"
    assert _counts() == before


# --- BR-WDR-03: scholarship first, then payments LIFO ------------------------------------------


def test_scholarship_reversed_first_then_payments_released(merit_award, invoice, record):
    scholarships.apply_scholarship(merit_award, actor="t")  # L01 75,000
    payment = record("89000.00", payer="GUARDIAN", on=date(2026, 8, 20), auto=True)
    _identity(invoice.student)
    result = withdraw(invoice)
    plan = {r["adjusts_line"][-3:]: r for r in result.financial["invoice"]["release_plan"]}
    # L01: N 144,000, A −86,400, N' 57,600, S 75,000 → R = 45,000; P 69,000 → excess 41,400
    assert (plan["L01"]["scholarship_reversal"], plan["L01"]["excess"]) == (
        D("45000.00"),
        D("41400.00"),
    )
    assert [d["amount"] for d in plan["L01"]["deallocations"]] == [D("41400.00")]
    # L02: N 6,000, A −3,600, S 0, P 6,000 → excess 3,600
    assert [d["amount"] for d in plan["L02"]["deallocations"]] == [D("3600.00")]
    rows = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").order_by("allocation_id")
    assert [(r.invoice_line_id[-3:], r.amount, r.rule_applied, r.allocated_on) for r in rows] == [
        ("L01", D("-41400.00"), "WITHDRAWAL_RELEASE", WD),
        ("L02", D("-3600.00"), "WITHDRAWAL_RELEASE", WD),
    ]
    reversal = ScholarshipAllocation.objects.get(allocation_type="REVERSE")
    assert (reversal.amount, reversal.reason, reversal.allocated_on) == (
        D("-45000.00"),
        "WITHDRAWAL",
        WD,
    )
    assert (
        LedgerEntry.objects.get(scholarship_allocation=reversal).entry_type
        == "SCHOLARSHIP_REVERSAL"
    )
    merit_award.refresh_from_db()
    assert merit_award.status == "PARTIALLY_REVERSED"
    a = billing.current_amounts(invoice)
    assert (a.net_charges, a.scholarship_applied, a.amount_paid, a.outstanding) == (
        D("74000.00"),
        D("30000.00"),
        D("44000.00"),
        D("0.00"),
    )
    invoice.refresh_from_db()
    assert invoice.collection_status == "PAID"
    assert payments.available(payment) == D("45000.00")  # freed money is unallocated credit
    b = _identity(invoice.student)
    assert (b["ledger_balance"], b["unallocated_credit"]) == (D("-45000.00"), D("45000.00"))
    quote = result.financial["refund_quote"]
    assert quote["max_refundable"] == D("45000.00")
    assert not payment.refunds.exists()  # D-W6: never automatic


def test_payments_released_newest_first_across_payments(invoice, record, manual):
    old = record("30000.00", payer="GUARDIAN", on=date(2026, 8, 18))
    new = record("30000.00", payer="SPONSOR", on=date(2026, 8, 19))
    manual(old, (1, "30000.00"), on=date(2026, 8, 18))
    manual(new, (1, "30000.00"), on=date(2026, 8, 19))
    withdraw(invoice, on=START)  # 80%: L01 N' 28,800, P 60,000 → excess 31,200
    rows = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").order_by("allocation_id")
    assert sorted((r.payment_id, r.amount) for r in rows) == [
        (old.payment_id, D("-1200.00")),
        (new.payment_id, D("-30000.00")),  # the newest allocation goes first
    ]


def test_D_W4_reversal_split_across_two_scholarships_on_one_line(
    scholarship_types, invoice, evaluate
):
    rows = evaluate(invoice.student)
    merit = scholarships.award_scholarship(eligibility=rows["SCT-001"], invoice=invoice, actor="t")
    stem = scholarships.award_scholarship(eligibility=rows["SCT-004"], invoice=invoice, actor="t")
    for award in (merit, stem):
        scholarships.apply_scholarship(award, actor="t")
    assert billing.current_amounts(invoice).scholarship_applied == D("95000.00")  # both on L01
    result = withdraw(invoice)
    # R = round(95,000 × 86,400 / 144,000) = 57,000, split 75 : 20 → 45,000 + 12,000
    plan = result.financial["invoice"]["release_plan"][0]
    assert plan["scholarship_reversal"] == D("57000.00")
    by_award = {
        r.scholarship_id: r.amount
        for r in ScholarshipAllocation.objects.filter(allocation_type="REVERSE")
    }
    assert by_award == {merit.pk: D("-45000.00"), stem.pk: D("-12000.00")}
    assert sum(r["amount"] for r in plan["scholarship_reversals"]) == D("57000.00")
    assert set(Scholarship.objects.values_list("status", flat=True)) == {"PARTIALLY_REVERSED"}
    _identity(invoice.student)


def test_full_ride_withdrawal_keeps_non_refundable_lines_covered(
    scholarship_types, student_with_invoice, evaluate
):
    s, inv = student_with_invoice(gpa="3.90", need_index="0.90")
    full = scholarships.award_scholarship(
        eligibility=evaluate(s)["SCT-007"], invoice=inv, actor="t"
    )
    scholarships.apply_scholarship(full, actor="t")
    academics.withdraw_enrollment(inv.enrollment, withdrawal_date=WD, actor="t")
    reversals = ScholarshipAllocation.objects.filter(allocation_type="REVERSE").order_by(
        "allocation_id"
    )
    assert [(r.invoice_line_id[-3:], r.amount) for r in reversals] == [
        ("L01", D("-86400.00")),  # the reversal matches the adjustment exactly
        ("L02", D("-3600.00")),
    ]
    a = billing.current_amounts(inv)
    assert (a.outstanding, a.amount_paid) == (D("0.00"), D("0.00"))
    inv.refresh_from_db()
    assert inv.collection_status == "SCHOLARSHIP_COVERED"
    _identity(s)


# --- cancellation (BR-WDR-01, BR-INV-08) ---------------------------------------------------------


def test_cancellation_leaves_only_registration_and_cancels_the_invoice(invoice):
    on = date(2026, 8, 20)
    result = cancel(invoice, on=on)
    enrollment = Enrollment.objects.get(pk=invoice.enrollment_id)
    assert (enrollment.status, enrollment.cancellation_date) == ("CANCELLED", on)
    assert (result.kind, result.refund_pct) == ("CANCELLATION", None)
    assert _adjustments(invoice) == [
        ("L01", D("-144000.00"), "CANCELLATION", on),
        ("L02", D("-6000.00"), "CANCELLATION", on),
        ("L04", D("-4000.00"), "CANCELLATION", on),
        ("L05", D("-2000.00"), "CANCELLATION", on),
        ("L06", D("-3000.00"), "CANCELLATION", on),
    ]  # L03 REGISTRATION stays owed
    invoice.refresh_from_db()
    assert (invoice.lifecycle_status, invoice.collection_status) == ("CANCELLED", "UNPAID")
    event = invoice.events.get(dimension="LIFECYCLE", to_status="CANCELLED")
    assert (event.from_status, event.event_date, event.reason, event.source_ref) == (
        "ISSUED",
        on,
        "ENROLLMENT_CANCELLED",
        invoice.enrollment_id,
    )
    assert billing.current_amounts(invoice).outstanding == D("5000.00")
    # the collection status stays live: past the due date, registration is overdue
    billing.refresh_collection_status(as_of=date(2026, 9, 15), actor="t")
    invoice.refresh_from_db()
    assert (invoice.lifecycle_status, invoice.collection_status) == ("CANCELLED", "OVERDUE")
    _identity(invoice.student)


def test_cancellation_releases_everything_except_registration(merit_award, invoice, record):
    scholarships.apply_scholarship(merit_award, actor="t")
    payment = record("89000.00", on=date(2026, 8, 18), auto=True)  # pays every line
    cancel(invoice, on=date(2026, 8, 20))
    assert ScholarshipAllocation.objects.get(allocation_type="REVERSE").amount == D("-75000.00")
    released = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE")
    assert {r.rule_applied for r in released} == {"CANCELLATION_RELEASE"}
    assert payments.available(payment) == D("84000.00")  # only registration stays paid
    invoice.refresh_from_db()
    assert (invoice.lifecycle_status, invoice.collection_status) == ("CANCELLED", "PAID")
    b = _identity(invoice.student)
    assert b["unallocated_credit"] == D("84000.00")


def test_cancellation_on_or_after_the_start_is_400(invoice):
    with pytest.raises(ValidationError) as exc:
        cancel(invoice, on=START)
    assert exc.value.message_dict == {
        "cancellation_date": [
            "Must be before the term start (2026-09-07); from the start on, withdraw the "
            "enrollment instead."
        ]
    }


def test_cancellation_cannot_predate_the_lines_it_adjusts(invoice):
    """Enrolled 08-10, invoice issued 08-17: a cancellation dated 08-12 would post credits
    before the charges exist."""
    before = _counts()
    with pytest.raises(ValidationError) as exc:
        cancel(invoice, on=date(2026, 8, 12))
    assert list(exc.value.message_dict) == ["cancellation_date"]
    assert exc.value.messages[0].startswith("Must be on or after 2026-08-17, the posting date of")
    assert _counts() == before


def test_cancellation_before_enrolled_on_is_400(invoice):
    with pytest.raises(ValidationError) as exc:
        cancel(invoice, on=date(2026, 8, 9))
    assert exc.value.message_dict == {
        "cancellation_date": ["Must be on or after enrolled_on (2026-08-10)."]
    }


# --- D-W7: no invoice / VOID invoice -------------------------------------------------------------


def test_no_invoice_means_only_the_enrollment_changes(fall_enrollment, fee_setup):
    before = LedgerEntry.objects.count()
    result = academics.withdraw_enrollment(fall_enrollment, withdrawal_date=WD, actor="t")
    assert result.financial["invoice"] is None
    assert Enrollment.objects.get(pk=fall_enrollment.pk).status == "WITHDRAWN"
    assert LedgerEntry.objects.count() == before


def test_void_invoice_is_left_alone(invoice):
    billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    before = (InvoiceLine.objects.count(), LedgerEntry.objects.count())
    result = cancel(invoice, on=date(2026, 8, 21))
    assert result.financial["invoice"] is None
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "VOID"
    assert (InvoiceLine.objects.count(), LedgerEntry.objects.count()) == before


# --- D-W8 / D-W9: dates and what is left alone ----------------------------------------------------


def test_withdrawal_can_be_dated_before_a_later_posting(invoice, record):
    """D-W8: no invoice-wide "latest date" rule: a late fee posted 09-29 does not block a
    withdrawal dated 09-17 entered afterwards."""
    record("1000.00", auto=True)
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")
    withdraw(invoice)  # dated 09-17
    assert _adjustments(invoice)[0][3] == WD
    _identity(invoice.student)


def test_a_release_cannot_predate_the_allocation_it_releases(invoice, record, manual):
    """D-W8 keeps the existing deallocation rule: the date of an allocation still bounds it."""
    payment = record("144000.00", on=date(2026, 9, 20))
    manual(payment, (1, "144000.00"), on=date(2026, 9, 20))
    before = _counts()
    with pytest.raises(ValidationError) as exc:
        withdraw(invoice, on=date(2026, 9, 17))
    assert list(exc.value.message_dict) == ["withdrawal_date"]
    assert "the date of payment allocation" in exc.value.messages[0]
    assert _counts() == before


def test_D_W9_student_status_awards_and_refunds_are_left_alone(
    merit_award, invoice, record, request_refund
):
    payment = record("174000.00", on=date(2026, 8, 18), auto=True)  # 10,000 unallocated
    pending = request_refund(payment, "1000.00", on=date(2026, 8, 21))
    withdraw(invoice)
    invoice.student.refresh_from_db()
    assert invoice.student.status == "ACTIVE"
    merit_award.refresh_from_db()
    assert merit_award.status == "OFFERED"  # unapplied award: not revoked
    pending.refresh_from_db()
    assert pending.status == "REQUESTED"  # reservation kept; caps re-checked at approval


# --- 99-row capacity and atomicity ---------------------------------------------------------------


def test_capacity_limit_rolls_the_whole_withdrawal_back(invoice, record):
    payment = record("50000.00")
    while PaymentAllocation.objects.filter(payment=payment).count() < 98:
        (row,) = payments.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(line(invoice, 1), D("1.00"))],
            allocated_on=date(2026, 8, 17),
            actor="t",
        ).allocations
        payments.deallocate(
            row,
            amount=D("1.00"),
            rule_applied="SCHOLARSHIP_PRECEDENCE",
            on_date=date(2026, 8, 17),
            actor="t",
        )
    payments.allocate_payment(
        payment,
        mode="MANUAL",
        lines=[(line(invoice, 1), D("50000.00"))],
        allocated_on=date(2026, 8, 17),
        actor="t",
    )
    before = _counts()
    with pytest.raises(StateConflict) as exc:  # the release needs a 100th row
        withdraw(invoice, on=START)  # 80%: N' 28,800 < 50,000 paid → release 21,200
    assert exc.value.code == "ALLOCATION_LIMIT_REACHED"
    assert _counts() == before


@pytest.mark.parametrize(
    "target",
    [
        "billing.allocations.release_payments_for_adjustment",
        "billing.services.post_ledger",
        "billing.allocations.reverse_scholarship_for_adjustment",
        "billing.services.recalculate_collection_status",
        "billing.withdrawals.refund_quote",
    ],
)
def test_failure_at_any_step_rolls_everything_back(
    merit_award, invoice, record, monkeypatch, target
):
    scholarships.apply_scholarship(merit_award, actor="t")
    record("89000.00", on=date(2026, 8, 20), auto=True)
    module, name = target.rsplit(".", 1)
    real = getattr(__import__(module, fromlist=[name]), name)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        # per-line steps fail on their second call (after the first line was written);
        # the once-per-withdrawal steps fail on their only call
        per_line = name in ("post_ledger", "reverse_scholarship_for_adjustment")
        if len(calls) == 2 or not per_line:
            raise RuntimeError(f"crash in {name}")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, flaky)
    before = _counts()
    with pytest.raises(RuntimeError, match="crash"):
        withdraw(invoice)
    assert _counts() == before


def test_cancellation_failure_after_the_lifecycle_change_rolls_back(invoice, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("crash in recalculate")

    monkeypatch.setattr("billing.services.recalculate_collection_status", broken)
    before = _counts()
    with pytest.raises(RuntimeError):
        cancel(invoice)
    assert _counts() == before


def test_history_is_append_only(merit_award, invoice, record):
    scholarships.apply_scholarship(merit_award, actor="t")
    record("89000.00", on=date(2026, 8, 20), auto=True)
    snapshot = {
        "lines": list(InvoiceLine.objects.values()),
        "ledger": list(LedgerEntry.objects.values()),
        "pay": list(PaymentAllocation.objects.values()),
        "sch": list(ScholarshipAllocation.objects.values()),
    }
    withdraw(invoice)
    for model, key in (
        (InvoiceLine, "lines"),
        (LedgerEntry, "ledger"),
        (PaymentAllocation, "pay"),
        (ScholarshipAllocation, "sch"),
    ):
        now = {r[model._meta.pk.attname]: r for r in model.objects.values()}
        for old in snapshot[key]:
            assert now[old[model._meta.pk.attname]] == old, (model.__name__, old)
