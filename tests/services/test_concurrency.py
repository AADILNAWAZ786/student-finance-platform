"""ARCHITECTURE §8.2 (Phase 4): two threads writing the same money at the same time, on the
file-based SQLite test database (pytest-django ``transaction=True``, so each thread commits
for real). Every write is a ``BEGIN IMMEDIATE`` transaction whose checks read the rows only
after the write lock is taken, so the second writer waits (``timeout`` 20 s) and then sees
the first one's rows: BR-ALC-01/02 and BR-REF-02 hold, and no thread ever gets
"database is locked"."""

import threading
from datetime import date
from decimal import Decimal as D

import pytest
from django.db import OperationalError, connections

from billing import calculators as billing_calc
from billing import services as billing
from billing.models import InvoiceLine
from core.exceptions import DomainError
from payments import services
from payments.models import Payment, PaymentAllocation, Refund
from scholarships import services as scholarships
from tests.payment_fixtures import ISSUE

pytestmark = pytest.mark.django_db(transaction=True)


def race(*jobs):
    """Run each job in its own thread, released together by a barrier. Returns one result
    per job: the job's return value or the domain error code. Any other exception (e.g. an
    OperationalError "database is locked") fails the test."""
    barrier, results, crashes = threading.Barrier(len(jobs)), [None] * len(jobs), []

    def run(i, job):
        try:
            barrier.wait()
            results[i] = job()
        except DomainError as exc:
            results[i] = exc.code
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


def _line(invoice, n):
    return InvoiceLine.objects.select_related("invoice").get(pk=f"{invoice.pk}-L{n:02d}")


def test_two_auto_allocations_of_one_payment_never_exceed_it(invoice, record):
    """The ARCHITECTURE §8.2 test: BR-ALC-01 holds under concurrency."""
    payment = record("150000.00")

    def auto():
        return services.allocate_payment(payment, mode="AUTO", actor="thread").outcome

    assert sorted(race(auto, auto)) == ["ALLOCATED", "NO_FUNDS_AVAILABLE"]
    rows = PaymentAllocation.objects.filter(payment=payment)
    assert sum(r.amount for r in rows) == D("150000.00")  # not 300,000
    assert [r.invoice_line_id[-3:] for r in rows.order_by("allocation_id")] == ["L01", "L02"]
    assert services.available(Payment.objects.get(pk=payment.pk)) == D("0.00")


def test_two_payments_racing_for_one_line_never_overfill_it(invoice, record):
    """BR-ALC-02 under concurrency: LAB (6,000) is offered 6,000 by two payments at once."""
    a, b = record("6000.00"), record("6000.00")

    def manual(payment):
        return lambda: (
            services.allocate_payment(
                payment,
                mode="MANUAL",
                lines=[(_line(invoice, 2), D("6000.00"))],
                allocated_on=ISSUE,
                actor="thread",
            ).outcome
        )

    assert sorted(race(manual(a), manual(b))) == ["ALLOCATED", "ALLOCATION_EXCEEDS_LINE_OPEN"]
    lines = list(invoice.lines.all())
    sch, pay = billing.allocations_for(invoice)
    assert billing_calc.line_open_amounts(lines, sch, pay)[f"{invoice.pk}-L02"] == D("0.00")


def test_two_refund_requests_cannot_spend_the_same_payment_money(invoice, record):
    """BR-REF-02 reservations under concurrency: 30,000 available, two requests of 20,000."""
    source = record("194000.00", auto=True)  # 164,000 allocated, 30,000 left
    record("40000.00")  # plenty of credit (70,000): the payment cap is the binding one

    def request():
        return services.request_refund(
            payment=source, amount=D("20000.00"), requested_date=date(2026, 8, 21), actor="t"
        ).status

    assert sorted(race(request, request)) == ["REFUND_EXCEEDS_PAYMENT_AVAILABLE", "REQUESTED"]
    assert Refund.objects.count() == 1
    assert services.available(Payment.objects.get(pk=source.pk)) == D("10000.00")


def test_two_refund_requests_cannot_spend_the_same_credit(invoice, record):
    """Credit 10,000 shared by two payments' requests of 6,000 each."""
    first = record("164000.00", auto=True)  # fully allocated
    one, two = record("5000.00"), record("5000.00")  # credit 10,000 in total
    assert services.available(first) == D("0.00")

    def request(payment, amount):
        return lambda: (
            services.request_refund(
                payment=payment, amount=D(amount), requested_date=date(2026, 8, 21), actor="t"
            ).status
        )

    results = race(request(one, "5000.00"), request(two, "5000.00"))
    assert results == ["REQUESTED", "REQUESTED"]  # exactly the credit: both fit
    third = record("6000.00")  # credit now 16,000, of which 10,000 is reserved
    results = race(request(third, "6000.00"), request(third, "6000.00"))
    # the loser fails on the credit cap, which is checked before the payment cap
    assert sorted(results) == ["REFUND_EXCEEDS_CREDIT_BALANCE", "REQUESTED"]
    caps = services.refund_caps(third)
    assert (caps.pending_refunds, caps.credit_available) == (D("16000.00"), D("0.00"))


def test_two_approvals_of_one_refund_give_one_approval_and_one_409(invoice, record):
    payment = record("174000.00", auto=True)  # 10,000 unallocated credit
    refund = services.request_refund(
        payment=payment, amount=D("5000.00"), requested_date=date(2026, 8, 21), actor="t"
    )

    def approve():
        return services.approve_refund(refund, on_date=date(2026, 8, 22), actor="t").status

    assert sorted(race(approve, approve)) == ["APPROVED", "ILLEGAL_TRANSITION"]
    assert Refund.objects.get().events.count() == 2  # REQUESTED, APPROVED


def test_the_same_external_reference_twice_at_once_gives_one_payment(invoice):
    def pay():
        payment, _ = services.record_payment(
            student=invoice.student,
            amount=D("1000.00"),
            received_date=ISSUE,
            method="UPI",
            payer_type="SELF",
            external_reference="UTR-RACE",
            actor="thread",
        )
        return payment.payment_id

    assert sorted(race(pay, pay)) == ["DUPLICATE_PAYMENT", "PAY-2026-000001"]
    assert Payment.objects.count() == 1


def test_scholarship_apply_racing_a_payment_on_the_same_line(merit_award, invoice, record):
    """Whichever wins, scholarship precedence (BR-ALC-06) and the line's open ≥ 0 hold."""
    payment = record("144000.00")

    def apply():
        return scholarships.apply_scholarship(merit_award, actor="thread").status

    def allocate():
        return services.allocate_payment(
            payment,
            mode="MANUAL",
            lines=[(_line(invoice, 1), D("144000.00"))],
            allocated_on=ISSUE,
            actor="thread",
        ).outcome

    results = race(apply, allocate)
    assert results[0] == "APPLIED"
    assert results[1] in ("ALLOCATED", "ALLOCATION_EXCEEDS_LINE_OPEN")
    a = billing.current_amounts(invoice)
    assert a.scholarship_applied == D("75000.00")
    lines = list(invoice.lines.all())
    sch, pay = billing.allocations_for(invoice)
    assert billing_calc.line_open_amounts(lines, sch, pay)[f"{invoice.pk}-L01"] >= 0
    assert billing.student_balance(invoice.student, None)["identity"]["holds"]
