"""payments.services: recording (BR-PAY-01..03), allocation (BR-ALC-01..05), releases and
scholarship precedence (BR-ALC-06), the 99-row capacity (D-P13a), ledger postings (BR-LED-01)
and invoice/balance effects. Everything through services, as the API does.

The FALL invoice (tests/payment_fixtures.py): L01 TUITION 144,000, L02 LAB 6,000,
L03 REGISTRATION 5,000, L04 TECHNOLOGY 4,000, L05 LIBRARY 2,000, L06 ACTIVITY 3,000 = 164,000;
issued 2026-08-17, due 2026-09-14.
"""

from datetime import date
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError

from billing import services as billing
from billing.models import Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.exceptions import BusinessRuleViolation, StateConflict
from core.models import IdSequence
from payments import services
from payments.models import Payment, PaymentAllocation
from scholarships import services as scholarships
from tests.payment_fixtures import ISSUE, line

pytestmark = pytest.mark.django_db


def _counts():
    return (
        Payment.objects.count(),
        PaymentAllocation.objects.count(),
        LedgerEntry.objects.count(),
        InvoiceEvent.objects.count(),
        list(IdSequence.objects.order_by("key").values_list("key", "last_value")),
    )


def _rows(payment):
    return [
        (a.invoice_line_id[-3:], a.allocation_type, a.amount, a.rule_applied)
        for a in payment.allocations.order_by("allocation_id")
    ]


# --- recording (BR-PAY-01..03) -----------------------------------------------------------------


def test_record_payment_posts_one_payment_entry_for_the_full_amount(invoice, record):
    payment = record("60000.00", payer="GUARDIAN", on=date(2026, 8, 25))
    assert (payment.payment_id, payment.status, payment.currency) == (
        "PAY-2026-000001",
        "POSTED",
        "INR",
    )
    entry = LedgerEntry.objects.get(payment=payment)
    assert (entry.entry_type, entry.amount, entry.entry_date, entry.student_id) == (
        "PAYMENT",
        D("-60000.00"),
        date(2026, 8, 25),
        "STU-000001",
    )
    assert payment.allocations.count() == 0 and payment.result is None  # BR-PAY-02


def test_BR_PAY_01_duplicate_external_reference_is_409_and_writes_nothing(invoice, record):
    record("100.00", ref="UTR-DUP")
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        record("999.00", ref="UTR-DUP", payer="SPONSOR")
    assert (exc.value.code, exc.value.rule_id) == ("DUPLICATE_PAYMENT", "BR-PAY-01")
    assert exc.value.details == {
        "external_reference": "UTR-DUP",
        "existing_payment_id": "PAY-2026-000001",
    }
    assert _counts() == before


@pytest.mark.parametrize("amount", ["0.00", "-5.00"])
def test_BR_PAY_01_amount_must_be_positive(invoice, record, amount):
    with pytest.raises(ValidationError) as exc:
        record(amount)
    assert exc.value.message_dict == {"amount": ["Must be greater than 0."]}


def test_D_P9_received_date_on_or_after_the_first_invoice(invoice, record):
    with pytest.raises(ValidationError) as exc:
        record("100.00", on=date(2026, 8, 16))
    assert exc.value.message_dict == {
        "received_date": [
            "Must be on or after the student's first invoice issue date (2026-08-17)."
        ]
    }
    record("100.00", on=ISSUE)  # the issue date itself is fine


def test_D_P9_no_invoice_means_no_payment(invoice, make_student, record):
    other = make_student()
    with pytest.raises(ValidationError) as exc:
        record("100.00", student=other)
    assert exc.value.message_dict == {
        "received_date": [
            f"Student {other.pk} has no invoice; a payment cannot be received before billing."
        ]
    }
    assert Payment.objects.count() == 0


def test_received_date_must_be_a_2026_business_date(invoice, record):
    with pytest.raises(ValidationError) as exc:
        record("100.00", on=date(2027, 1, 1))
    assert "received_date" in exc.value.message_dict


@pytest.mark.parametrize(
    ("field", "value"), [("method", "CRYPTO"), ("payer_type", "UNCLE"), ("external_reference", "")]
)
def test_enums_and_reference_are_validated(invoice, field, value):
    kw = dict(
        student=invoice.student,
        amount=D("1.00"),
        received_date=ISSUE,
        method="UPI",
        payer_type="SELF",
        external_reference="X",
        actor="t",
    )
    kw[field] = value
    with pytest.raises(ValidationError) as exc:
        services.record_payment(**kw)
    assert list(exc.value.message_dict) == [field]


def test_BR_PAY_03_every_payer_counts_as_money_paid(invoice, record, manual):
    for payer, n in (("SELF", 1), ("GUARDIAN", 2), ("SPONSOR", 3)):
        manual(record("1000.00", payer=payer), (n, "1000.00"))
    a = billing.current_amounts(invoice)
    assert a.amount_paid == D("3000.00")
    assert a.payments_by_payer == {
        "SELF": D("1000.00"),
        "GUARDIAN": D("1000.00"),
        "SPONSOR": D("1000.00"),
    }


# --- AUTO (BR-ALC-03 / BR-ALC-04, D-P13) ---------------------------------------------------------


def test_auto_oldest_due_first_then_priority_and_status_partially_paid(invoice, record):
    payment = record("60000.00", payer="GUARDIAN", on=date(2026, 8, 25), auto=True)
    assert _rows(payment) == [("L01", "ALLOCATE", D("60000.00"), "AUTO_OLDEST_FIRST")]
    assert payment.result.outcome == "ALLOCATED"
    assert payment.allocations.get().allocated_on == date(2026, 8, 25)  # D-P6 default
    invoice.refresh_from_db()
    assert (invoice.collection_status, invoice.collection_status_as_of) == (
        "PARTIALLY_PAID",
        date(2026, 8, 25),
    )
    event = invoice.events.order_by("-event_id").first()
    assert (event.to_status, event.reason, event.source_ref) == (
        "PARTIALLY_PAID",
        "PAYMENT_ALLOCATED",
        "PAY-2026-000001",
    )


def test_BR_ALC_04_overpayment_remainder_stays_unallocated(invoice, record):
    payment = record("170000.00", auto=True)
    assert [r[0] for r in _rows(payment)] == ["L01", "L02", "L03", "L04", "L05", "L06"]
    assert payment.result.allocated == D("164000.00")
    assert payment.result.unallocated_after == D("6000.00")
    assert services.available(payment) == D("6000.00")
    invoice.refresh_from_db()
    assert invoice.collection_status == "PAID"
    assert billing.unallocated_credit(invoice.student) == D("6000.00")


def test_auto_crosses_invoices_oldest_due_date_first(invoice, record, terms, program):
    """A SPRING invoice (due earlier) is paid before the FALL one."""
    from academics import services as academics
    from tests.billing_fixtures import make_fees

    spring = terms["TERM-2026-SPRING"]
    make_fees(program, spring)
    enrollment = academics.enroll(
        student=invoice.student,
        term=spring,
        credits=12,
        enrolled_on=spring.invoice_date,
        actor="t",
    )
    older = billing.generate_invoice(enrollment=enrollment, actor="t")
    assert older.due_date < invoice.due_date
    payment = record("1000.00", auto=True)
    (row,) = payment.allocations.all()
    assert row.invoice_line.invoice_id == older.invoice_id


def test_D_P13_no_open_lines_writes_nothing_and_keeps_the_credit(invoice, record):
    record("164000.00", auto=True)  # everything paid
    before = _counts()
    payment = record("500.00", auto=True)
    assert payment.result.outcome == "NO_OPEN_LINES"
    assert payment.result.allocations == [] and payment.result.unallocated_after == D("500.00")
    after = _counts()
    assert (after[1], after[3]) == (before[1], before[3])  # no allocation, no invoice event
    assert billing.unallocated_credit(invoice.student) == D("500.00")


def test_D_P13_no_funds_available_is_reported_not_an_error(invoice, record):
    payment = record("1000.00", auto=True)
    before = _counts()
    result = services.allocate_payment(payment, mode="AUTO", actor="t")
    assert (result.outcome, result.allocations, result.available_before) == (
        "NO_FUNDS_AVAILABLE",
        [],
        D("0.00"),
    )
    assert _counts() == before


def test_auto_skips_void_invoices_and_lines_posted_after_allocated_on(invoice, record):
    payment = record("100.00")
    result = services.allocate_payment(payment, mode="AUTO", allocated_on=ISSUE, actor="t")
    assert result.outcome == "ALLOCATED"  # FEE lines are posted on the issue date
    # a late fee posted later is not visible to an allocation dated before it
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")
    late = InvoiceLine.objects.get(invoice=invoice, line_type="LATE_FEE")
    open_lines = services._student_open_lines(invoice.student, allocated_on=date(2026, 9, 28))
    assert late.line_id not in {x.line_id for x in open_lines}
    open_lines = services._student_open_lines(invoice.student, allocated_on=date(2026, 9, 29))
    assert late.line_id in {x.line_id for x in open_lines}


def test_auto_never_touches_a_void_invoice(student_with_invoice, record):
    s, inv = student_with_invoice()
    billing.void_invoice(inv, void_date=date(2026, 8, 20), actor="t")
    payment = record("100.00", student=s, auto=True)
    assert payment.result.outcome == "NO_OPEN_LINES"


# --- MANUAL (BR-ALC-01 / BR-ALC-02, same student) -------------------------------------------------


def test_manual_allocation_all_or_nothing(invoice, record, manual):
    payment = record("10000.00")
    result = manual(payment, (2, "6000.00"), (5, "2000.00"))
    assert [r[:3] for r in _rows(payment)] == [
        ("L02", "ALLOCATE", D("6000.00")),
        ("L05", "ALLOCATE", D("2000.00")),
    ]
    assert result.unallocated_after == D("2000.00") and result.mode == "MANUAL"


def test_BR_ALC_01_manual_total_above_available(invoice, record, manual):
    payment = record("10000.00")
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        manual(payment, (1, "6000.00"), (2, "4000.01"))
    assert (exc.value.code, exc.value.rule_id) == ("ALLOCATION_EXCEEDS_PAYMENT", "BR-ALC-01")
    assert exc.value.details == {
        "payment_id": "PAY-2026-000001",
        "requested": "10000.01",
        "available": "10000.00",
    }
    assert _counts() == before


def test_BR_ALC_02_manual_above_the_line_open_amount(invoice, record, manual):
    payment = record("10000.00")
    manual(payment, (2, "1000.00"))
    with pytest.raises(BusinessRuleViolation) as exc:
        manual(payment, (2, "5000.01"))
    assert (exc.value.code, exc.value.rule_id) == ("ALLOCATION_EXCEEDS_LINE_OPEN", "BR-ALC-02")
    assert exc.value.details["line_open"] == "5000.00"


def test_BR_ALC_02_line_open_counts_scholarship_allocations(merit_award, invoice, record, manual):
    scholarships.apply_scholarship(merit_award, actor="t")  # 75,000 on tuition
    payment = record("100000.00")
    with pytest.raises(BusinessRuleViolation) as exc:
        manual(payment, (1, "69000.01"))
    assert exc.value.details["line_open"] == "69000.00"


def test_allocation_to_another_students_line_is_422(invoice, record, manual, student_with_invoice):
    _, other = student_with_invoice()
    payment = record("1000.00")
    with pytest.raises(BusinessRuleViolation) as exc:
        manual(payment, (1, "10.00"), inv=other)
    assert exc.value.code == "ALLOCATION_STUDENT_MISMATCH"
    assert exc.value.details["invoice_line_ids"] == [f"{other.pk}-L01"]


def test_manual_rejects_adjustment_lines_duplicates_and_bad_dates(invoice, record, manual):
    payment = record("1000.00", on=date(2026, 8, 20))
    with pytest.raises(ValidationError) as exc:
        manual(payment, (1, "1.00"), (1, "2.00"), on=date(2026, 8, 20))
    assert exc.value.message_dict == {"lines": [f"{invoice.pk}-L01 appears more than once."]}
    with pytest.raises(ValidationError) as exc:
        manual(payment, (1, "1.00"), on=date(2026, 8, 19))
    assert exc.value.message_dict == {
        "allocated_on": ["Must be on or after the payment's received_date (2026-08-20)."]
    }
    with pytest.raises(ValidationError) as exc:
        services.allocate_payment(
            payment, mode="MANUAL", lines=[(line(invoice, 1), D("1"))], actor="t"
        )
    assert exc.value.message_dict == {"allocated_on": ["Required when mode is MANUAL."]}
    billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    adjustment = invoice.lines.filter(line_type="ADJUSTMENT").first()
    with pytest.raises(ValidationError) as exc:
        services.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(adjustment, D("1.00"))],
            allocated_on=date(2026, 8, 20),
            actor="t",
        )
    assert "payments are allocated only to FEE and LATE_FEE lines" in exc.value.messages[0]


def test_void_invoice_line_has_nothing_open(invoice, record, manual):
    payment = record("1000.00", on=date(2026, 8, 20))
    billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    with pytest.raises(BusinessRuleViolation) as exc:
        manual(payment, (1, "1.00"), on=date(2026, 8, 20))
    assert exc.value.details["line_open"] == "0.00"


def test_BR_INV_08_an_allocated_invoice_cannot_be_voided(invoice, record, manual):
    manual(record("1000.00"), (1, "1000.00"))
    with pytest.raises(StateConflict) as exc:
        billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    assert exc.value.code == "INVOICE_HAS_ALLOCATIONS"
    assert exc.value.details["amount_paid"] == "1000.00"


def test_allocations_post_nothing_to_the_ledger(invoice, record, manual):
    payment = record("5000.00")
    entries = LedgerEntry.objects.count()
    manual(payment, (2, "5000.00"))
    services.deallocate(
        payment.allocations.get(),
        amount=D("100.00"),
        rule_applied="SCHOLARSHIP_PRECEDENCE",
        on_date=ISSUE,
        actor="t",
    )
    assert LedgerEntry.objects.count() == entries  # BR-LED-01


# --- releases (BR-ALC-05) ------------------------------------------------------------------------


def test_BR_ALC_05_deallocation_appends_a_reversing_row(invoice, record, manual):
    payment = record("6000.00")
    manual(payment, (2, "6000.00"))
    original = payment.allocations.get()
    row = services.deallocate(
        original,
        amount=D("2500.00"),
        rule_applied="WITHDRAWAL_RELEASE",
        on_date=date(2026, 9, 1),
        actor="t",
    )
    assert (row.allocation_id, row.allocation_type, row.amount, row.reverses_id) == (
        "ALLOC-PAY-2026-000001-02",
        "DEALLOCATE",
        D("-2500.00"),
        original.allocation_id,
    )
    original.refresh_from_db()
    assert original.amount == D("6000.00")  # never edited
    assert services.available(payment) == D("2500.00")
    with pytest.raises(ValidationError) as exc:  # Σ deallocations ≤ original
        services.deallocate(
            original,
            amount=D("3500.01"),
            rule_applied="WITHDRAWAL_RELEASE",
            on_date=date(2026, 9, 1),
            actor="t",
        )
    assert "above its 6000.00" in exc.value.messages[0]


def test_deallocation_rules_and_dates(invoice, record, manual):
    payment = record("6000.00", on=date(2026, 8, 20))
    manual(payment, (2, "6000.00"), on=date(2026, 8, 22))
    original = payment.allocations.get()
    with pytest.raises(ValidationError) as exc:
        services.deallocate(
            original, amount=D("1.00"), rule_applied="MANUAL", on_date=date(2026, 8, 22), actor="t"
        )
    assert list(exc.value.message_dict) == ["rule_applied"]
    with pytest.raises(ValidationError) as exc:
        services.deallocate(
            original,
            amount=D("1.00"),
            rule_applied="WITHDRAWAL_RELEASE",
            on_date=date(2026, 8, 21),
            actor="t",
        )
    assert exc.value.message_dict["on_date"][0].startswith("Must be on or after 2026-08-22")
    row = services.deallocate(
        original,
        amount=D("1.00"),
        rule_applied="CANCELLATION_RELEASE",
        on_date=date(2026, 8, 22),
        actor="t",
    )
    with pytest.raises(ValidationError):  # a DEALLOCATE row cannot be released
        services.deallocate(
            row,
            amount=D("1.00"),
            rule_applied="WITHDRAWAL_RELEASE",
            on_date=date(2026, 8, 22),
            actor="t",
        )


# --- BR-ALC-06: scholarship precedence -----------------------------------------------------------


def test_BR_ALC_06_scholarship_releases_payments_lifo_across_payments(merit_award, invoice, record):
    first = record("100000.00", payer="GUARDIAN", on=date(2026, 8, 18), auto=True)  # L01 100,000
    second = record("40000.00", payer="SPONSOR", on=date(2026, 8, 19), auto=True)  # L01 40,000
    scholarships.apply_scholarship(merit_award, allocated_on=date(2026, 8, 20), actor="t")
    # tuition 144,000 must make room for 75,000: release 140,000 + 75,000 − 144,000 = 71,000,
    # newest allocation first: all 40,000 of the second payment, then 31,000 of the first
    released = PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").order_by(
        "allocation_id"
    )
    assert [(r.payment_id, r.amount, r.rule_applied, r.allocated_on) for r in released] == [
        (first.payment_id, D("-31000.00"), "SCHOLARSHIP_PRECEDENCE", date(2026, 8, 20)),
        (second.payment_id, D("-40000.00"), "SCHOLARSHIP_PRECEDENCE", date(2026, 8, 20)),
    ]
    a = billing.current_amounts(invoice)
    assert (a.scholarship_applied, a.amount_paid) == (D("75000.00"), D("69000.00"))
    # the freed money is unallocated credit; nothing was re-allocated (D-P11)
    assert (services.available(first), services.available(second)) == (D("31000.00"), D("40000.00"))
    assert billing.unallocated_credit(invoice.student) == D("71000.00")
    assert billing.student_balance(invoice.student, None)["identity"]["holds"]


def test_BR_ALC_06_release_cannot_predate_the_allocation_it_releases(merit_award, invoice, record):
    record("144000.00", on=date(2026, 8, 25), auto=True)
    before = _counts()
    with pytest.raises(ValidationError) as exc:
        scholarships.apply_scholarship(merit_award, allocated_on=date(2026, 8, 24), actor="t")
    assert list(exc.value.message_dict) == ["allocated_on"]
    assert _counts() == before
    merit_award.refresh_from_db()
    assert merit_award.status == "OFFERED"


def test_BR_ALC_06_inconsistent_release_rolls_the_application_back(
    merit_award, invoice, monkeypatch
):
    """The plan says payments hold more than they do (an injected provider row): the handler
    refuses and the whole scholarship application rolls back."""
    from types import SimpleNamespace as NS

    from billing import allocations as registry

    fake = [
        NS(
            invoice_line_id=f"{invoice.pk}-L01",
            amount=D("100000.00"),
            payer_type="SELF",
            allocated_on=ISSUE,
        )
    ]
    monkeypatch.setitem(registry._providers["payment"], "test-only", lambda inv: fake)
    before = _counts()
    with pytest.raises(services.PaymentReleaseFailed, match="SCH-2026-000001"):
        scholarships.apply_scholarship(merit_award, actor="t")
    assert _counts() == before


# --- D-P13a: 99 allocation rows per payment, deallocations included ------------------------------


def _fill(payment, invoice, rows):
    """Write ``rows`` allocation rows on ``payment``: ALLOCATE 1.00 / DEALLOCATE pairs on L01."""
    while PaymentAllocation.objects.filter(payment=payment).count() < rows:
        (row,) = services.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(line(invoice, 1), D("1.00"))],
            allocated_on=ISSUE,
            actor="t",
        ).allocations
        if PaymentAllocation.objects.filter(payment=payment).count() < rows:
            services.deallocate(
                row,
                amount=D("1.00"),
                rule_applied="SCHOLARSHIP_PRECEDENCE",
                on_date=ISSUE,
                actor="t",
            )


def test_D_P13a_allocation_stops_at_99_rows_counting_deallocations(invoice, record):
    payment = record("50000.00")
    _fill(payment, invoice, 98)
    assert (
        PaymentAllocation.objects.filter(payment=payment, allocation_type="DEALLOCATE").count()
        == 49
    )
    # 98 + 2 lines > 99: refused before anything is written
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        services.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(line(invoice, 2), D("1.00")), (line(invoice, 3), D("1.00"))],
            allocated_on=ISSUE,
            actor="t",
        )
    assert (exc.value.code, exc.value.status_code) == ("ALLOCATION_LIMIT_REACHED", 409)
    assert exc.value.details == {
        "payment_id": "PAY-2026-000001",
        "existing_rows": 98,
        "requested_rows": 2,
        "limit": 99,
    }
    assert _counts() == before
    # exactly one more fits: ALLOC-…-99, the last ID the format allows
    (last,) = services.allocate_payment(
        payment, mode="MANUAL", lines=[(line(invoice, 2), D("1.00"))], allocated_on=ISSUE, actor="t"
    ).allocations
    assert last.allocation_id == "ALLOC-PAY-2026-000001-99"
    with pytest.raises(StateConflict):  # AUTO is checked too
        services.allocate_payment(payment, mode="AUTO", actor="t")


def test_D_P13a_deallocation_is_checked_before_writing(invoice, record):
    payment = record("50000.00")
    _fill(payment, invoice, 99)
    open_row = PaymentAllocation.objects.filter(payment=payment, allocation_type="ALLOCATE").last()
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        services.deallocate(
            open_row, amount=D("1.00"), rule_applied="WITHDRAWAL_RELEASE", on_date=ISSUE, actor="t"
        )
    assert exc.value.details["existing_rows"] == 99
    assert _counts() == before


def test_D_P13a_scholarship_release_is_checked_before_writing(merit_award, invoice, record):
    payment = record("144000.00", on=ISSUE)
    _fill(payment, invoice, 98)  # 49 pairs on L01 cancel out
    services.allocate_payment(
        payment,
        mode="MANUAL",
        lines=[(line(invoice, 1), D("144000.00"))],
        allocated_on=ISSUE,
        actor="t",
    )
    assert PaymentAllocation.objects.filter(payment=payment).count() == 99
    before = _counts()
    with pytest.raises(StateConflict) as exc:  # the release needs a 100th row
        scholarships.apply_scholarship(merit_award, actor="t")
    assert exc.value.code == "ALLOCATION_LIMIT_REACHED"
    assert _counts() == before
    merit_award.refresh_from_db()
    assert merit_award.status == "OFFERED"


# --- atomicity (ARCHITECTURE §8.4) ---------------------------------------------------------------


def test_record_payment_rolls_back_when_the_ledger_post_fails(invoice, monkeypatch):
    def broken(payment):
        raise RuntimeError("disk full")

    monkeypatch.setattr(billing, "post_payment_ledger", broken)
    before = _counts()
    with pytest.raises(RuntimeError):
        services.record_payment(
            student=invoice.student,
            amount=D("1.00"),
            received_date=ISSUE,
            method="UPI",
            payer_type="SELF",
            external_reference="UTR-ATOMIC",
            actor="t",
        )
    assert _counts() == before  # no payment, no entry, no sequence used


def test_auto_allocation_inside_record_payment_rolls_everything_back(invoice, monkeypatch):
    real, calls = services._write_allocation, []

    def flaky(*args, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("crash on the second row")
        return real(*args, **kw)

    monkeypatch.setattr(services, "_write_allocation", flaky)
    before = _counts()
    with pytest.raises(RuntimeError):
        services.record_payment(
            student=invoice.student,
            amount=D("150000.00"),
            received_date=ISSUE,
            method="UPI",
            payer_type="SELF",
            external_reference="UTR-ATOMIC",
            auto_allocate=True,
            actor="t",
        )
    assert _counts() == before  # the payment and its PAYMENT entry are gone too


def test_allocation_rolls_back_when_the_status_refresh_fails(invoice, record, monkeypatch):
    payment = record("1000.00")

    def broken(*args, **kw):
        raise RuntimeError("status")

    monkeypatch.setattr(billing, "recalculate_collection_status", broken)
    before = _counts()
    with pytest.raises(RuntimeError):
        services.allocate_payment(payment, mode="AUTO", actor="t")
    assert _counts() == before


# --- balances (BR-BAL-01 / BR-BAL-02) and providers ----------------------------------------------


def test_balance_breakdown_and_identity_with_payments(invoice, record, manual):
    record("60000.00", payer="GUARDIAN", auto=True)
    manual(record("10000.00", payer="SPONSOR"), (2, "4000.00"))
    b = billing.student_balance(invoice.student, None)
    assert b["ledger_balance"] == D("94000.00")
    assert b["breakdown"]["payments"] == D("-70000.00")
    assert b["breakdown"]["payments_by_payer"] == {
        "SELF": D("0.00"),
        "GUARDIAN": D("-60000.00"),
        "SPONSOR": D("-10000.00"),
    }
    assert (b["invoice_outstanding"], b["unallocated_credit"]) == (D("100000.00"), D("6000.00"))
    assert b["identity"]["holds"] is True


def test_balance_as_of_excludes_later_payments_and_allocations(invoice, record):
    record("60000.00", on=date(2026, 8, 25), auto=True)
    b = billing.student_balance(invoice.student, date(2026, 8, 24))
    assert (b["ledger_balance"], b["unallocated_credit"], b["identity"]["holds"]) == (
        D("164000.00"),
        D("0.00"),
        True,
    )


def test_invoice_allocations_provider_sees_only_its_invoice(invoice, record, student_with_invoice):
    record("100.00", auto=True)
    _, other = student_with_invoice()
    assert services.invoice_allocations(other) == []
    assert len(services.invoice_allocations(invoice)) == 1


def test_registry_has_every_payments_hook():
    from billing import allocations as registry

    assert registry.registered() == {
        "scholarship": ["scholarships"],
        "payment": ["payments"],
        "student_credit": ["payments"],
        "refunds": ["payments"],
        "refund_quote": ["payments"],  # Phase 5
        "payment_release": ["payments"],
        "adjustment_scholarship_reversal": ["scholarships"],  # Phase 5
        "adjustment_payment_release": ["payments"],  # Phase 5
    }
    registry.register_payment_release_handler("payments", services.release_for_scholarship)
    with pytest.raises(ValueError, match="different function"):
        registry.register_student_provider("refunds", "payments", lambda s: [])
    with pytest.raises(ValueError):
        registry.register_student_provider("balances", "x", lambda s: [])


def test_invoice_reads_are_unchanged_for_an_invoice_without_payments(invoice):
    a = billing.current_amounts(Invoice.objects.get(pk=invoice.pk))
    assert (a.amount_paid, a.outstanding) == (D("0.00"), D("164000.00"))
