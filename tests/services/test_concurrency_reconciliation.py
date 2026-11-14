"""Reconciliation under concurrency (ARCHITECTURE §8.2; decision D-R2), with real threads on the
file-based SQLite test database (``transaction=True``), as tests/services/test_concurrency.py.

What is guaranteed, and tested here: no "database is locked"; every run ends COMPLETED with
its findings written atomically; concurrent runs and snapshots never interfere with each
other or with domain writes; a run never changes domain data.

What is NOT guaranteed (D-R2, documented in services.py and KNOWN_LIMITATIONS): a run reads
outside any transaction, so it is not a snapshot. A write committed while a run reads can be
seen by some checks and not others, so a run that overlaps writes may report findings on the
records being written. These tests only accept such findings on the student being written,
and show that a run after the writes finish is clean.
"""

import threading
from datetime import date
from decimal import Decimal as D

import pytest
from django.db import OperationalError, connections

from academics import services as academics
from academics.models import Enrollment
from payments import services as payments
from payments.models import Payment
from reconciliation import services
from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun

pytestmark = pytest.mark.django_db(transaction=True)

END = date(2026, 12, 31)


def race(*jobs):
    barrier, results, crashes = threading.Barrier(len(jobs)), [None] * len(jobs), []

    def run(i, job):
        try:
            barrier.wait()
            results[i] = job()
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
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a thread hung"
    assert crashes == []
    return results


def _run_all():
    return services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API").pk


def _clean_run():
    run = ReconciliationRun.objects.get(pk=_run_all())
    assert run.status == "COMPLETED" and run.findings.count() == 0, list(
        run.findings.values_list("issue_type", "record_id")
    )


def test_two_runs_at_once_both_complete_with_the_same_findings(clean_world):
    from tests.reconciliation_fixtures import inject_payment_over_allocated

    inject_payment_over_allocated(clean_world["S16"])
    a, b = race(_run_all, _run_all)
    runs = {r.pk: r for r in ReconciliationRun.objects.all()}
    assert set(runs) == {a, b} and len(runs) == 2
    assert {r.status for r in runs.values()} == {"COMPLETED"}

    def content(run_id):
        return [
            (f.issue_type, f.record_id, f.details)
            for f in AnomalyFinding.objects.filter(run_id=run_id).order_by("finding_id")
        ]

    assert content(a) == content(b) and len(content(a)) == 1
    assert AnomalyFinding.objects.values("finding_id").distinct().count() == 2


def test_a_run_during_payment_writes_completes_and_a_later_run_is_clean(clean_world):
    s = clean_world["S6"]  # unpaid, overdue

    def write():
        for i in range(5):
            p, _ = payments.record_payment(
                student=s,
                amount=D("1000.00"),
                received_date=date(2026, 9, 20),
                method="UPI",
                payer_type="SELF",
                external_reference=f"CONC-{i}",
                auto_allocate=True,
                actor="t",
            )
        return Payment.objects.filter(student=s).count()

    run_id, written = race(_run_all, write)
    assert written == 5
    run = ReconciliationRun.objects.get(pk=run_id)
    assert run.status == "COMPLETED"
    # observations made mid-write may show findings, but only on the student being written
    assert set(run.findings.values_list("student_id", flat=True)) <= {s.pk}
    _clean_run()


def test_a_run_during_a_withdrawal_completes_and_a_later_run_is_clean(clean_world):
    enr = Enrollment.objects.get(student=clean_world["S16"])

    def withdraw():
        return academics.withdraw_enrollment(
            Enrollment.objects.get(pk=enr.pk), withdrawal_date=date(2026, 9, 17), actor="t"
        ).kind

    run_id, kind = race(_run_all, withdraw)
    assert kind == "WITHDRAWAL"
    run = ReconciliationRun.objects.get(pk=run_id)
    assert run.status == "COMPLETED"
    assert set(run.findings.values_list("student_id", flat=True)) <= {clean_world["S16"].pk}
    _clean_run()


def test_two_snapshot_requests_at_once_create_one_snapshot(clean_world):
    s = clean_world["S1"]

    def snap():
        [r] = services.take_snapshot(as_of_date=END, student=s)
        return (r.snapshot.snapshot_id, r.created)

    a, b = race(snap, snap)
    assert a[0] == b[0] and sorted([a[1], b[1]]) == [False, True]
    assert BalanceSnapshot.objects.filter(student=s).count() == 1


def test_a_snapshot_racing_a_payment_reports_one_of_the_two_ledger_states(clean_world):
    """Whichever commits first, the snapshot is the ledger balance of one consistent moment
    (it is taken in one transaction); if the payment lands after it, the later mismatch is a
    true BR-BAL-03 finding, not a detector error."""
    from billing import services as billing

    s = clean_world["S6"]
    before = billing.student_balance(s, END)["ledger_balance"]

    def pay():
        payments.record_payment(
            student=s,
            amount=D("700.00"),
            received_date=date(2026, 9, 20),
            method="UPI",
            payer_type="SELF",
            external_reference="CONC-SNAP",
            actor="t",
        )

    def snap():
        return services.take_snapshot(as_of_date=END, student=s)[0].snapshot.reported_balance

    reported, _ = race(snap, pay)
    assert reported in (before, before - D("700.00"))
    run = ReconciliationRun.objects.get(pk=_run_all())
    found = list(run.findings.values_list("issue_type", flat=True))
    assert found == ([] if reported == before - D("700.00") else ["BALANCE_LEDGER_MISMATCH"])
