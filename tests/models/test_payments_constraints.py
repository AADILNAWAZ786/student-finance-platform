"""Database-level rules of the payments tables and the completed LedgerEntry (DATABASE_DESIGN
§3): signs, enums and required-iff CHECKs, the unique external_reference, one ledger entry per
source with exactly one of the four sources, and append-only rows. Rows are written directly
(bypassing services) to prove the database itself refuses them."""

from datetime import date
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, transaction

from billing.models import LedgerEntry
from payments.models import AppendOnlyError, Payment, PaymentAllocation, Refund, RefundEvent

pytestmark = pytest.mark.django_db

ISSUE = date(2026, 8, 17)


def _rejects(obj, match=None):
    """The database refuses the row; ``match`` names the constraint that must refuse it."""
    with pytest.raises(IntegrityError, match=match), transaction.atomic():
        obj.save(force_insert=True)


SOURCE = "ledgerentry_source_matches_type"
SIGN = "ledgerentry_amount_sign_by_type"


def _payment(student, **kw):
    fields = dict(
        payment_id="PAY-2026-000901",
        student=student,
        amount=D("100.00"),
        currency="INR",
        received_date=ISSUE,
        method="UPI",
        payer_type="SELF",
        external_reference="DB-REF-1",
        status="POSTED",
    )
    fields.update(kw)
    return Payment(**fields)


@pytest.fixture
def paid(invoice, record):
    """A 10,000 payment, 6,000 of it on the LAB line (ALLOC-…-01)."""
    payment = record("10000.00")
    from payments import services
    from tests.payment_fixtures import line

    services.allocate_payment(
        payment,
        mode="MANUAL",
        lines=[(line(invoice, 2), D("6000.00"))],
        allocated_on=ISSUE,
        actor="t",
    )
    return payment


def test_payment_valid_row_saves(invoice):
    _payment(invoice.student).save(force_insert=True)
    assert Payment.objects.get(pk="PAY-2026-000901").method == "UPI"


def test_sponsor_transfer_fits_the_method_column(invoice):
    """D-P1 / P4-O1: the design's Char(14) could not store this documented value."""
    _payment(invoice.student, method="SPONSOR_TRANSFER", payer_type="SPONSOR").save(
        force_insert=True
    )
    assert Payment.objects.get().method == "SPONSOR_TRANSFER"


@pytest.mark.parametrize(
    "kw",
    [
        {"amount": D("0.00")},
        {"amount": D("-1.00")},
        {"currency": "USD"},
        {"method": "CRYPTO"},
        {"payer_type": "UNCLE"},
        {"status": "VOID"},
    ],
)
def test_payment_check_constraints(invoice, kw):
    _rejects(_payment(invoice.student, **kw))


def test_external_reference_is_unique_in_the_database(invoice):
    _payment(invoice.student).save(force_insert=True)
    _rejects(_payment(invoice.student, payment_id="PAY-2026-000902"))


def _alloc(payment, invoice, **kw):
    fields = dict(
        allocation_id=f"ALLOC-{payment.pk}-50",
        payment=payment,
        invoice_line_id=f"{invoice.pk}-L01",
        allocation_type="ALLOCATE",
        amount=D("1.00"),
        reverses=None,
        rule_applied="MANUAL",
        allocated_on=ISSUE,
    )
    fields.update(kw)
    return PaymentAllocation(**fields)


@pytest.mark.parametrize(
    "kw",
    [
        {"amount": D("0.00")},
        {"amount": D("-1.00")},  # ALLOCATE must be > 0
        {"allocation_type": "DEALLOCATE"},  # positive and no reverses
        {"allocation_type": "REVERSE"},
        {"rule_applied": "LIFO"},
    ],
)
def test_allocation_check_constraints(paid, invoice, kw):
    _rejects(_alloc(paid, invoice, **kw))


def test_deallocate_needs_negative_amount_and_reverses(paid, invoice):
    original = paid.allocations.get()
    _rejects(_alloc(paid, invoice, allocation_type="DEALLOCATE", amount=D("-1.00")))
    _rejects(
        _alloc(paid, invoice, allocation_type="DEALLOCATE", amount=D("1.00"), reverses=original)
    )
    _rejects(_alloc(paid, invoice, reverses=original))  # ALLOCATE with reverses
    _alloc(
        paid,
        invoice,
        allocation_type="DEALLOCATE",
        amount=D("-1.00"),
        reverses=original,
        invoice_line_id=original.invoice_line_id,
        rule_applied="SCHOLARSHIP_PRECEDENCE",
    ).save(force_insert=True)


def test_allocations_are_append_only(paid):
    row = paid.allocations.get()
    row.amount = D("1.00")
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        paid.delete()
    assert PaymentAllocation.objects.get().amount == D("6000.00")


def test_payer_type_of_an_allocation_is_its_payments(paid):
    assert paid.allocations.get().payer_type == "SELF"


def _refund(payment, **kw):
    fields = dict(
        refund_id="REF-2026-000901",
        student_id=payment.student_id,
        payment=payment,
        payer_type=payment.payer_type,
        enrollment=None,
        reason="OVERPAYMENT",
        amount=D("10.00"),
        requested_date=date(2026, 8, 20),
        processed_date=None,
        status="REQUESTED",
        calculation={},
    )
    fields.update(kw)
    return Refund(**fields)


@pytest.mark.parametrize(
    "kw",
    [
        {"amount": D("0.00")},
        {"reason": "GOODWILL"},
        {"status": "CANCELLED"},
        {"payer_type": "UNCLE"},
        {"reason": "WITHDRAWAL"},  # enrollment required
        {"status": "PROCESSED"},  # processed_date required
        {"processed_date": date(2026, 8, 25)},  # not PROCESSED: must be null
        {"status": "PROCESSED", "processed_date": date(2026, 8, 19)},  # before requested
        {"status": "PROCESSED", "processed_date": date(2027, 1, 1)},  # after 2026-12-31
    ],
)
def test_refund_check_constraints(paid, kw):
    _rejects(_refund(paid, **kw))


def test_refund_enrollment_only_for_withdrawal_or_cancellation(paid, fall_enrollment):
    _rejects(_refund(paid, enrollment=fall_enrollment))  # OVERPAYMENT with an enrollment
    _refund(paid, reason="WITHDRAWAL", enrollment=fall_enrollment).save(force_insert=True)
    _refund(
        paid,
        refund_id="REF-2026-000902",
        status="PROCESSED",
        processed_date=date(2026, 12, 31),  # the last allowed day
    ).save(force_insert=True)


def test_refund_event_is_a_status_event(paid):
    refund = _refund(paid)
    refund.save(force_insert=True)
    RefundEvent.objects.create(
        event_id="EVT-REF-2026-0000901",
        refund=refund,
        from_status=None,
        to_status="REQUESTED",
        event_date=date(2026, 8, 20),
        reason="REQUESTED",
        actor="t",
    )
    with pytest.raises(AppendOnlyError):
        refund.delete()


# --- LedgerEntry: four sources, seven types -------------------------------------------------------


def _entry(student, **kw):
    fields = dict(
        entry_id="LED-2026-0000901",
        student=student,
        entry_date=ISSUE,
        entry_type="PAYMENT",
        amount=D("-1.00"),
    )
    fields.update(kw)
    return LedgerEntry(**fields)


def test_ledger_payment_and_refund_entries(paid, invoice):
    refund = _refund(paid, status="PROCESSED", processed_date=date(2026, 8, 25))
    refund.save(force_insert=True)
    student = invoice.student
    # the service already posted PAYMENT for `paid`; a second entry for it is refused (U)
    _rejects(_entry(student, payment=paid), match="UNIQUE.*payment_id")
    _rejects(_entry(student, payment=None), match=SOURCE)  # no source at all
    _rejects(_entry(student, refund=refund), match=SOURCE)  # PAYMENT with a refund source
    _rejects(_entry(student, entry_type="REFUND", amount=D("-1.00"), refund=refund), match=SIGN)
    _rejects(_entry(student, entry_type="REFUND", amount=D("0.00"), refund=refund), match=SIGN)
    adjustment = _late_line(invoice)  # a line with no ledger entry yet: only the CHECK can refuse
    _rejects(  # two sources
        _entry(
            student,
            entry_type="REFUND",
            amount=D("1.00"),
            refund=refund,
            invoice_line=adjustment,
        ),
        match=SOURCE,
    )
    _entry(student, entry_type="REFUND", amount=D("10.00"), refund=refund).save(force_insert=True)
    _rejects(
        _entry(
            student,
            entry_id="LED-2026-0000902",
            entry_type="REFUND",
            amount=D("1.00"),
            refund=refund,
        )
    )


def test_payment_entry_must_be_negative(invoice):
    payment = _payment(invoice.student)
    payment.save(force_insert=True)
    _rejects(_entry(invoice.student, amount=D("0.00"), payment=payment), match=SIGN)
    _rejects(_entry(invoice.student, amount=D("1.00"), payment=payment), match=SIGN)
    _rejects(
        _entry(invoice.student, entry_type="CHARGE", amount=D("1.00"), payment=payment),
        match=SOURCE,
    )
    _entry(invoice.student, payment=payment).save(force_insert=True)


def test_existing_line_entries_still_need_exactly_their_own_source(invoice):
    payment = _payment(invoice.student)
    payment.save(force_insert=True)
    _rejects(
        _entry(
            invoice.student,
            entry_type="LATE_FEE",
            amount=D("1.00"),
            invoice_line=_late_line(invoice),
            payment=payment,
        ),
        match=SOURCE,
    )


def _late_line(invoice):
    """A LATE_FEE line written without its ledger entry (so its source FK is still free)."""
    from billing.models import InvoiceLine

    return InvoiceLine.objects.get_or_create(
        line_id=f"{invoice.pk}-L09",
        defaults=dict(
            invoice=invoice,
            line_type="LATE_FEE",
            fee_type="LATE_FEE",
            description="late fee",
            quantity=1,
            unit_amount=D("500.00"),
            gross_amount=D("500.00"),
            net_amount=D("500.00"),
            is_scholarship_eligible=False,
            is_refundable=False,
            is_waivable=False,
            allocation_priority=90,
            posted_date=date(2026, 9, 29),
        ),
    )[0]
