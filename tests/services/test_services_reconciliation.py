"""reconciliation.services: take_snapshot (D-R6) and run_reconciliation (D-R1, D-R10),
with the run workflow, failure rollback (no partial findings), no domain writes, ground-truth
isolation at runtime, determinism, model guards, and parity with the Phase 3–5 calculations
the detector reuses (D-R4, D-R5, BR-BAL-02)."""

from datetime import date
from decimal import Decimal as D

import pytest
from django.core.exceptions import ValidationError
from django.db import connections
from django.test.utils import CaptureQueriesContext

from billing import services as billing
from billing.models import LedgerEntry
from core.exceptions import BusinessRuleViolation
from payments import services as payments
from payments.models import Payment
from reconciliation import calculators as calc
from reconciliation import engine, loader, services
from reconciliation.checks import REGISTRY
from reconciliation.checks.base import GLOBAL
from reconciliation.models import (
    AnomalyFinding,
    AppendOnlyError,
    BalanceSnapshot,
    ReconciliationRun,
)
from scholarships import services as scholarships
from scholarships.models import Scholarship
from students.models import Student
from tests.reconciliation_fixtures import inject_payment_over_allocated, inject_refund_over_net_paid

pytestmark = pytest.mark.django_db

END = date(2026, 12, 31)


def _domain_dump():
    from reconciliation.loader import MODELS

    def rows(model):
        fields = [f.attname for f in model._meta.concrete_fields]
        return [tuple(str(getattr(o, f)) for f in fields) for o in model.objects.order_by("pk")]

    from core.models import IdSequence

    out = {name: rows(m) for name, m in MODELS.items() if name != "snapshots"}
    out["sequences"] = [
        r for r in rows(IdSequence) if not r[0].startswith(("RECON:", "FND:", "SNAP:"))
    ]
    return out


# --- snapshots -----------------------------------------------------------------------------------


def test_snapshot_one_student_is_bal_01_with_sign_split_totals(clean_world):
    s = clean_world["S1"]
    [res] = services.take_snapshot(as_of_date=date(2026, 9, 30), student=s)
    assert res.created and res.snapshot.snapshot_id == "SNAP-2026-000001"
    entries = list(LedgerEntry.objects.filter(student=s))
    on = [e for e in entries if e.entry_date <= date(2026, 9, 30)]
    snap = res.snapshot
    assert snap.reported_balance == calc.balance_from_ledger(entries, date(2026, 9, 30))
    assert snap.reported_balance == billing.student_balance(s, date(2026, 9, 30))["ledger_balance"]
    assert snap.total_debits == sum((e.amount for e in on if e.amount > 0), D("0.00"))
    assert snap.total_credits == sum((e.amount for e in on if e.amount < 0), D("0.00"))
    assert snap.total_debits + snap.total_credits == snap.reported_balance


def test_snapshot_all_and_existing_snapshots_are_returned_unchanged(clean_world):
    first = services.take_snapshot(as_of_date=date(2026, 9, 30))
    assert len(first) == Student.objects.count() and all(r.created for r in first)
    assert [r.snapshot.student_id for r in first] == sorted(s.pk for s in Student.objects.all())
    # the ledger moves on; a second request returns the stored snapshot, untouched (D-R6)
    s1 = clean_world["S1"]
    before = BalanceSnapshot.objects.get(student=s1)
    payments.record_payment(
        student=s1,
        amount=D("10.00"),
        received_date=date(2026, 9, 29),
        method="UPI",
        payer_type="SELF",
        external_reference="LATE-1",
        actor="t",
    )
    again = services.take_snapshot(as_of_date=date(2026, 9, 30))
    assert not any(r.created for r in again)
    after = BalanceSnapshot.objects.get(student=s1)
    assert (after.snapshot_id, after.reported_balance) == (
        before.snapshot_id,
        before.reported_balance,
    )
    assert BalanceSnapshot.objects.count() == len(first)


def test_snapshot_of_a_student_with_no_ledger_is_zero(clean_world):
    [res] = services.take_snapshot(as_of_date=END, student=clean_world["S15"])
    s = res.snapshot
    assert (s.reported_balance, s.total_debits, s.total_credits) == (D("0.00"),) * 3


def test_snapshot_date_must_be_a_business_date(clean_world):
    with pytest.raises(ValidationError) as exc:
        services.take_snapshot(as_of_date=date(2027, 1, 1))
    assert "as_of_date" in exc.value.message_dict
    assert BalanceSnapshot.objects.count() == 0


def test_snapshots_and_findings_are_append_only(clean_world):
    [res] = services.take_snapshot(as_of_date=END, student=clean_world["S1"])
    with pytest.raises(AppendOnlyError):
        res.snapshot.save()
    with pytest.raises(AppendOnlyError):
        res.snapshot.delete()
    inject_payment_over_allocated(clean_world["S16"])
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    f = run.findings.first()
    with pytest.raises(AppendOnlyError):
        f.save()
    with pytest.raises(AppendOnlyError):
        f.delete()
    with pytest.raises(AppendOnlyError):
        run.save()  # only status / finished_at / summary, named in update_fields
    with pytest.raises(AppendOnlyError):
        run.save(update_fields=["as_of_date"])
    with pytest.raises(AppendOnlyError):
        run.delete()


# --- runs ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "with_student", "field"),
    [("STUDENT", False, "student_id"), ("ALL", True, "student_id"), ("SOME", False, "scope")],
)
def test_run_scope_validation(clean_world, scope, with_student, field):
    with pytest.raises(ValidationError) as exc:
        services.run_reconciliation(
            scope=scope,
            as_of_date=END,
            triggered_by="API",
            student=clean_world["S1"] if with_student else None,
        )
    assert field in exc.value.message_dict
    assert ReconciliationRun.objects.count() == 0


def test_a_run_is_stored_running_before_its_checks(clean_world, monkeypatch):
    seen = {}
    real = loader.load

    def spy(**kw):
        seen["runs"] = list(
            ReconciliationRun.objects.values_list("run_id", "status", "finished_at")
        )
        return real(**kw)

    monkeypatch.setattr(loader, "load", spy)
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="COMMAND")
    assert seen["runs"] == [("RECON-2026-000001", "RUNNING", None)]
    run.refresh_from_db()
    assert (run.status, run.triggered_by) == ("COMPLETED", "COMMAND")
    assert run.finished_at is not None and run.finished_at >= run.started_at


def test_a_failing_check_marks_the_run_failed_with_no_findings(clean_world, monkeypatch):
    inject_payment_over_allocated(clean_world["S16"])

    def boom(ds):
        raise RuntimeError("check exploded")

    monkeypatch.setattr(engine, "run_checks", lambda ds: boom(ds))
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    run.refresh_from_db()
    assert run.status == "FAILED" and run.finished_at is not None
    assert run.summary == {"error": {"type": "RuntimeError", "message": "check exploded"}}
    assert AnomalyFinding.objects.count() == 0


def test_a_failure_while_writing_findings_leaves_none(clean_world, monkeypatch):
    """REC-03: findings and COMPLETED are one transaction; a failure after some findings
    were written rolls them back, sequence included, and the run ends FAILED."""
    inject_payment_over_allocated(clean_world["S16"])
    inject_refund_over_net_paid(clean_world["S17"])
    real = services._write_findings

    def write_then_fail(run, result):
        real(run, result)
        assert AnomalyFinding.objects.count() == 3  # written inside the transaction …
        raise OSError("disk full")

    monkeypatch.setattr(services, "_write_findings", write_then_fail)
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    run.refresh_from_db()
    assert run.status == "FAILED" and run.summary["error"]["type"] == "OSError"
    assert AnomalyFinding.objects.count() == 0  # … and rolled back with it
    monkeypatch.setattr(services, "_write_findings", real)
    ok = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    assert ok.status == "COMPLETED"
    assert list(ok.findings.values_list("finding_id", flat=True)) == [
        "FND-2026-000001",
        "FND-2026-000002",
        "FND-2026-000003",
    ]


def test_runs_never_change_domain_data(clean_world):
    inject_payment_over_allocated(clean_world["S16"])
    services.take_snapshot(as_of_date=END)
    before = _domain_dump()
    for scope, student in (("ALL", None), ("STUDENT", clean_world["S16"])):
        services.run_reconciliation(
            scope=scope, student=student, as_of_date=END, triggered_by="API"
        )
    assert _domain_dump() == before


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_runs_and_snapshots_never_query_the_label_database(clean_world):
    with CaptureQueriesContext(connections["ground_truth"]) as gt:
        services.take_snapshot(as_of_date=END)
        services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
        services.run_reconciliation(
            scope="STUDENT", student=clean_world["S1"], as_of_date=END, triggered_by="API"
        )
    assert gt.captured_queries == []


def test_same_data_gives_the_same_findings_and_summary(clean_world):
    inject_payment_over_allocated(clean_world["S16"])
    inject_refund_over_net_paid(clean_world["S17"])
    a = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    b = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")

    def content(run):
        return [
            (
                f.issue_type,
                f.rule_id,
                f.severity,
                f.entity_type,
                f.record_id,
                f.student_id,
                f.expected_value,
                f.actual_value,
                f.details,
                f.recommended_action,
            )
            for f in run.findings.order_by("finding_id")
        ]

    assert content(a) == content(b) and len(content(a)) == 3
    assert a.summary == b.summary


def test_student_scope_lists_every_skipped_global_check(clean_world):
    run = services.run_reconciliation(
        scope="STUDENT", student=clean_world["S1"], as_of_date=END, triggered_by="API"
    )
    globals_ = sorted(c.check_id for c in REGISTRY.values() if c.scope == GLOBAL)
    assert sorted(x["check_id"] for x in run.summary["skipped_checks"]) == globals_
    assert globals_ == [
        "RL-BILLABLE-TERM-MASTER",
        "S-DUP-MASTER",
        "S-ENUM-MASTER",
        "S-FK-ORPHAN",
        "S-ID-GAPS-YEAR",
        "S-ID-MASTER",
        "T-FEE-CHAIN",
        "T-TERM-CENSUS",
    ]
    assert {x["check_id"] for x in run.summary["checks"]}.isdisjoint(globals_)
    assert (run.scope, run.student_id, run.summary["student_id"]) == (
        "STUDENT",
        clean_world["S1"].pk,
        clean_world["S1"].pk,
    )


def test_student_scope_sees_only_that_students_findings(clean_world):
    p16 = inject_payment_over_allocated(clean_world["S16"])
    inject_refund_over_net_paid(clean_world["S17"])
    run = services.run_reconciliation(
        scope="STUDENT", student=clean_world["S16"], as_of_date=END, triggered_by="API"
    )
    assert list(run.findings.values_list("issue_type", "record_id")) == [
        ("PAYMENT_OVER_ALLOCATED", p16)
    ]


# --- parity with the calculations the detector reuses ---------------------------------------------


@pytest.mark.parametrize("as_of", [date(2026, 8, 20), date(2026, 9, 20), END])
def test_identity_parity_with_billing_student_balance(clean_world, as_of):
    """BR-BAL-02 is evaluated exactly as billing.services.student_balance does."""
    from reconciliation.checks.required import identities

    ds = loader.load(as_of=as_of)
    ours = identities(ds)
    for s in Student.objects.all():
        b = billing.student_balance(s, as_of)
        i = ours[s.pk]
        assert (i.ledger_balance, i.invoice_outstanding, i.unallocated_credit, i.holds) == (
            b["ledger_balance"],
            b["invoice_outstanding"],
            b["unallocated_credit"],
            b["identity"]["holds"],
        ), s.pk


def test_reservation_parity_with_phase_3(clean_world):
    """D-R5: per invoice, the reservations the detector sums equal
    scholarships.services.reserved_on_invoice."""
    for inv_id in sorted(set(Scholarship.objects.values_list("invoice_id", flat=True))):
        awards = list(Scholarship.objects.filter(invoice_id=inv_id))
        ours = sum(
            (calc.award_reservation(a, list(a.allocations.all())) for a in awards), D("0.00")
        )
        assert ours == scholarships.reserved_on_invoice(awards[0].invoice), inv_id


def test_refund_rule_parity_with_phase_4(clean_world):
    """D-R4: a refund Phase 4 accepts is never flagged; the next cent above net paid, which
    Phase 4 refuses with REFUND_EXCEEDS_NET_PAID, is exactly what the detector flags."""
    s = clean_world["S17"]
    p = Payment.objects.get(student=s)
    caps = payments.refund_caps(p)
    assert caps.net_paid == D("20000.00")
    with pytest.raises(BusinessRuleViolation):  # Phase 4 refuses (a cap binds first)
        payments.request_refund(
            payment=p, amount=caps.net_paid + D("0.01"), requested_date=date(2026, 9, 2), actor="t"
        )
    from payments.models import Refund

    ok = [
        Refund(
            refund_id="REF-2026-900001",
            payment_id=p.pk,
            amount=caps.net_paid,
            status="REQUESTED",
            requested_date=date(2026, 9, 2),
        )
    ]
    assert calc.refund_net_paid_breaches([p], ok) == []
    over = [
        Refund(
            refund_id="REF-2026-900001",
            payment_id=p.pk,
            amount=caps.net_paid + D("0.01"),
            status="REQUESTED",
            requested_date=date(2026, 9, 2),
        )
    ]
    assert [b.expected for b in calc.refund_net_paid_breaches([p], over)] == [caps.net_paid]
