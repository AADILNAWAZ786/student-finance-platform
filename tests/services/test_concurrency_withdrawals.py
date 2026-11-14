"""Phase 5 under concurrency: a withdrawal or cancellation racing another exit, a payment
allocation, a scholarship application or a refund request on the same student. Same harness
as tests/services/test_concurrency.py: real threads on the file-based SQLite test database
(``transaction=True``), every write a ``BEGIN IMMEDIATE`` transaction. Whichever thread wins,
the loser sees the winner's rows: one exit only, every line's open amount ≥ 0, no payment
allocated beyond its amount, and the BR-BAL-02 identity holds. No "database is locked"."""

import threading
from datetime import date
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, connections

from academics import services as academics
from academics.models import Enrollment, EnrollmentEvent
from billing import calculators as billing_calc
from billing import services as billing
from billing.models import Invoice, InvoiceLine
from core.exceptions import DomainError
from payments import services as payments
from payments.models import Payment, PaymentAllocation, Refund
from scholarships import services as scholarships

pytestmark = pytest.mark.django_db(transaction=True)

WD = date(2026, 9, 17)  # day 10 → 60%
CANCEL_ON = date(2026, 8, 20)


def race(*jobs):
    """Each job in its own thread, released together. Returns per job its value, the domain
    error code, or "VALIDATION" for a field error; anything else fails the test."""
    barrier, results, crashes = threading.Barrier(len(jobs)), [None] * len(jobs), []

    def run(i, job):
        try:
            barrier.wait()
            results[i] = job()
        except DomainError as exc:
            results[i] = exc.code
        except ValidationError:
            results[i] = "VALIDATION"
        except OperationalError as exc:  # pragma: no cover - the failure being guarded against
            crashes.append(f"OperationalError: {exc}")
        except Exception as exc:  # pragma: no cover
            crashes.append(repr(exc))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not any(t.is_alive() for t in threads), "a thread hung"
    assert crashes == []
    return results


def _withdraw(invoice):
    return lambda: (
        academics.withdraw_enrollment(
            Enrollment.objects.get(pk=invoice.enrollment_id), withdrawal_date=WD, actor="thread"
        ).kind
    )


def _cancel(invoice):
    return lambda: (
        academics.cancel_enrollment(
            Enrollment.objects.get(pk=invoice.enrollment_id),
            cancellation_date=CANCEL_ON,
            actor="thread",
        ).kind
    )


def _invariants(invoice):
    invoice = Invoice.objects.get(pk=invoice.pk)
    lines = list(invoice.lines.all())
    sch, pay = billing.allocations_for(invoice)
    opens = billing_calc.line_open_amounts(lines, sch, pay)
    assert all(v >= 0 for v in opens.values()), opens
    for p in Payment.objects.filter(student=invoice.student):
        assert payments.available(p) >= 0
    assert billing.student_balance(invoice.student, None)["identity"]["holds"]
    return invoice


def _exit_events(invoice):
    return list(
        EnrollmentEvent.objects.filter(
            enrollment_id=invoice.enrollment_id, reason__in=["WITHDRAWAL", "CANCELLATION"]
        )
    )


def test_two_withdrawals_at_once_give_one_exit_and_one_409(invoice, record, merit_award):
    scholarships.apply_scholarship(merit_award, actor="t")
    record("89000.00", on=date(2026, 8, 20), auto=True)
    assert sorted(race(_withdraw(invoice), _withdraw(invoice))) == [
        "ILLEGAL_TRANSITION",
        "WITHDRAWAL",
    ]
    assert len(_exit_events(invoice)) == 1
    invoice = _invariants(invoice)
    # applied once: one adjustment per refundable line, one release of each kind
    assert InvoiceLine.objects.filter(invoice=invoice, line_type="ADJUSTMENT").count() == 2
    assert PaymentAllocation.objects.filter(allocation_type="DEALLOCATE").count() == 2
    assert billing.current_amounts(invoice).outstanding == D("0.00")


def test_withdraw_racing_cancel_gives_exactly_one_exit(invoice, record):
    record("50000.00", on=date(2026, 8, 18), auto=True)
    results = race(_withdraw(invoice), _cancel(invoice))
    assert sorted(results) in (
        ["CANCELLATION", "ILLEGAL_TRANSITION"],
        ["ILLEGAL_TRANSITION", "WITHDRAWAL"],
    )
    assert len(_exit_events(invoice)) == 1
    invoice = _invariants(invoice)
    enrollment = Enrollment.objects.get(pk=invoice.enrollment_id)
    if results[0] == "WITHDRAWAL":
        assert (enrollment.status, invoice.lifecycle_status) == ("WITHDRAWN", "ISSUED")
    else:
        assert (enrollment.status, invoice.lifecycle_status) == ("CANCELLED", "CANCELLED")
        # BR-WDR-01: only REGISTRATION survives, and the payment is released down to it
        assert billing.current_amounts(invoice).net_charges == D("5000.00")


def test_withdraw_racing_a_payment_allocation(invoice, record, merit_award):
    scholarships.apply_scholarship(merit_award, actor="t")
    payment = record("69000.00", on=date(2026, 8, 20))

    def allocate():
        line = InvoiceLine.objects.select_related("invoice").get(pk=f"{invoice.pk}-L01")
        return payments.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(line, D("69000.00"))],
            allocated_on=date(2026, 8, 20),
            actor="thread",
        ).outcome

    results = race(_withdraw(invoice), allocate)
    assert results[0] == "WITHDRAWAL"
    # allocation first: 69,000 lands and the withdrawal releases the excess (41,400);
    # withdrawal first: L01 is open only 27,600, so 69,000 exceeds it
    assert results[1] in ("ALLOCATED", "ALLOCATION_EXCEEDS_LINE_OPEN")
    invoice = _invariants(invoice)
    released = -sum(
        r.amount
        for r in PaymentAllocation.objects.filter(payment=payment, allocation_type="DEALLOCATE")
    )
    assert released == (D("41400.00") if results[1] == "ALLOCATED" else D("0.00"))


def test_withdraw_racing_a_scholarship_application(invoice, merit_award):
    results = race(
        _withdraw(invoice),
        lambda: scholarships.apply_scholarship(merit_award, actor="thread").status,
    )
    assert results[0] == "WITHDRAWAL"
    assert results[1] is not None
    invoice = _invariants(invoice)
    applied = billing.current_amounts(invoice).scholarship_applied
    # whichever order, the scholarship never covers more than the reduced tuition (57,600)
    assert applied <= D("57600.00")
    if applied:
        assert results[1] == "APPLIED"


def test_withdraw_racing_a_refund_request(invoice, record, merit_award):
    scholarships.apply_scholarship(merit_award, actor="t")
    payment = record("89000.00", on=date(2026, 8, 20), auto=True)

    def refund():
        return payments.request_refund(
            payment=Payment.objects.get(pk=payment.pk),
            amount=D("1000.00"),
            requested_date=date(2026, 9, 18),
            actor="thread",
        ).status

    results = race(_withdraw(invoice), refund)
    assert results[0] == "WITHDRAWAL"
    # refund first: no credit yet (fully allocated); withdrawal first: 45,000 credit
    assert results[1] in ("REQUESTED", "REFUND_EXCEEDS_CREDIT_BALANCE", "REFUND_EXCEEDS_PAYMENT")
    _invariants(invoice)
    assert Refund.objects.count() == (1 if results[1] == "REQUESTED" else 0)
