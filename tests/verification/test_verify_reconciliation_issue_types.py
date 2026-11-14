"""Safeguard 8 for the other issue types: a real corruption of the clean world (written with
``QuerySet.update``, bypassing services and model guards, as legacy damage would) is found
with its documented issue type, rule ID, severity (one of the frozen values, D-R9),
recommended action, entity, record_id and student. INVALID_ENUM and DUPLICATE_RECORD
cannot be stored at all (DB CHECK and UNIQUE constraints), so they are covered on pure rows
in tests/unit/test_reconciliation_checks.py.
"""

from datetime import date
from decimal import Decimal as D

import pytest
from django.db import connection

from academics.models import Enrollment
from billing.models import Invoice, LedgerEntry
from payments.models import Refund
from reconciliation import services
from reconciliation.models import AnomalyFinding
from students.models import Student

pytestmark = pytest.mark.django_db

END = date(2026, 12, 31)


def _findings():
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    assert run.status == "COMPLETED", run.summary
    rows = list(AnomalyFinding.objects.filter(run=run).order_by("finding_id"))
    return run, rows


def _contract(f):
    return (
        f.issue_type,
        f.rule_id,
        f.severity,
        f.recommended_action,
        f.entity_type,
        f.record_id,
        f.student_id,
    )


def test_arithmetic_mismatch_invoice_total(clean_world):
    inv = Invoice.objects.get(student=clean_world["S6"])
    Invoice.objects.filter(pk=inv.pk).update(total_amount=inv.total_amount + D("1.00"))
    _, rows = _findings()
    assert [_contract(f) for f in rows] == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-INV-02",
            "HIGH",
            "INVESTIGATE",
            "Invoice",
            inv.pk,
            inv.student_id,
        )
    ]
    assert (rows[0].expected_value, rows[0].actual_value) == (
        inv.total_amount,
        inv.total_amount + 1,
    )


def test_identity_failure_is_an_arithmetic_mismatch_on_the_student(clean_world):
    """D-R3: a broken ledger shows as BR-LED-01 on the source and BR-BAL-02 on the student,
    who is listed as not reconciled."""
    s = clean_world["S6"]
    entry = LedgerEntry.objects.filter(student=s, entry_type="CHARGE").order_by("pk").first()
    LedgerEntry.objects.filter(pk=entry.pk).update(amount=entry.amount + D("10.00"))
    run, rows = _findings()
    assert [_contract(f) for f in rows] == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-LED-01",
            "HIGH",
            "INVESTIGATE",
            "InvoiceLine",
            entry.invoice_line_id,
            s.pk,
        ),
        ("ARITHMETIC_MISMATCH", "BR-BAL-02", "HIGH", "INVESTIGATE", "Student", s.pk, s.pk),
    ]
    assert rows[1].actual_value - rows[1].expected_value == D("10.00")
    assert run.summary["students"]["not_reconciled"] == [s.pk]
    assert run.summary["students"]["reconciled"] == len(clean_world) - 1


def test_invoice_status_mismatch(clean_world):
    inv = Invoice.objects.get(student=clean_world["S6"])
    Invoice.objects.filter(pk=inv.pk).update(collection_status="PAID")
    _, rows = _findings()
    contract = (
        "INVOICE_STATUS_MISMATCH",
        "BR-INV-07",
        "MEDIUM",
        "INVESTIGATE",
        "Invoice",
        inv.pk,
        inv.student_id,
    )
    # stored ≠ last COLLECTION event, and stored ≠ BR-INV-06 recomputed
    assert [_contract(f) for f in rows] == [contract, contract]
    assert {f.details["check_id"] for f in rows} == {"L-STORED-STATUS", "L-COLLECTION-STATUS"}
    assert [f.details.get("derived_status") for f in rows if "derived_status" in f.details] == [
        "OVERDUE"
    ]


def test_illegal_transition_stored_status_without_event(clean_world):
    s = clean_world["S15"]
    Student.objects.filter(pk=s.pk).update(status="GRADUATED")
    _, rows = _findings()
    assert [_contract(f) for f in rows] == [
        ("ILLEGAL_TRANSITION", "R-48", "MEDIUM", "INVESTIGATE", "Student", s.pk, s.pk)
    ]
    assert rows[0].details["problem"] == "STORED_STATUS_NOT_LAST_EVENT"


def test_date_order(clean_world):
    enr = Enrollment.objects.get(student=clean_world["S6"])
    Enrollment.objects.filter(pk=enr.pk).update(enrolled_on=date(2026, 8, 20))  # issue 08-17
    inv = Invoice.objects.get(enrollment=enr)
    _, rows = _findings()
    assert [_contract(f) for f in rows] == [
        ("DATE_ORDER", "BR-INV-03", "MEDIUM", "INVESTIGATE", "Invoice", inv.pk, inv.student_id)
    ]


def test_relationship_mismatch(clean_world):
    r = (
        Refund.objects.filter(student=clean_world["S1"], payer_type="SPONSOR")
        .order_by("pk")
        .first()
    )
    Refund.objects.filter(pk=r.pk).update(payer_type="SELF")
    _, rows = _findings()
    assert [_contract(f) for f in rows] == [
        (
            "RELATIONSHIP_MISMATCH",
            "BR-REF-04",
            "MEDIUM",
            "INVESTIGATE",
            "Refund",
            r.pk,
            r.student_id,
        )
    ]


def test_orphan_fk(clean_world):
    r = Refund.objects.filter(enrollment__isnull=False).order_by("pk").first()
    with connection.cursor() as c:
        c.execute("PRAGMA defer_foreign_keys = ON")  # checked at COMMIT, never reached here
    Refund.objects.filter(pk=r.pk).update(enrollment_id="ENR-2026-999999")
    try:
        _, rows = _findings()
    finally:  # pytest-django checks constraints before rolling back
        Refund.objects.filter(pk=r.pk).update(enrollment_id=r.enrollment_id)
    assert [_contract(f) for f in rows] == [
        ("ORPHAN_FK", "R-22", "HIGH", "INVESTIGATE", "Refund", r.pk, None)
    ]
    assert rows[0].details["field"] == "enrollment_id"


def test_invalid_id_format(clean_world):
    entry = LedgerEntry.objects.filter(student=clean_world["S6"]).order_by("pk").first()
    bad = "LED-2025-" + entry.pk.rsplit("-", 1)[1]  # its entry_date is in 2026
    LedgerEntry.objects.filter(pk=entry.pk).update(entry_id=bad)
    _, rows = _findings()
    assert [_contract(f) for f in rows] == [
        ("INVALID_ID_FORMAT", "R-28", "LOW", "INVESTIGATE", "LedgerEntry", bad, entry.student_id)
    ]
    assert rows[0].details == {
        "check_id": "S-ID-DOMAIN",
        "family": "STRUCTURAL",
        "kind": "ledger_entry",
        "problem": "PREFIX_OR_YEAR",
        "expected_id": entry.pk,
    }


def test_snapshot_totals_are_checked_when_the_balance_still_matches(clean_world):
    """Corrupt totals on a balance-correct snapshot: ARITHMETIC_MISMATCH (BR-BAL-03). A
    snapshot whose balance is wrong is reported once, as BALANCE_LEDGER_MISMATCH."""
    from reconciliation.models import BalanceSnapshot

    s = clean_world["S1"]
    [res] = services.take_snapshot(as_of_date=END, student=s)
    snap = res.snapshot
    BalanceSnapshot.objects.filter(pk=snap.pk).update(
        total_debits=snap.total_debits + 5, total_credits=snap.total_credits - 5
    )
    _, rows = _findings()
    assert [(_contract(f), f.details["field"]) for f in rows] == [
        (
            (
                "ARITHMETIC_MISMATCH",
                "BR-BAL-03",
                "HIGH",
                "INVESTIGATE",
                "BalanceSnapshot",
                snap.pk,
                s.pk,
            ),
            "total_credits",
        ),
        (
            (
                "ARITHMETIC_MISMATCH",
                "BR-BAL-03",
                "HIGH",
                "INVESTIGATE",
                "BalanceSnapshot",
                snap.pk,
                s.pk,
            ),
            "total_debits",
        ),
    ]
    BalanceSnapshot.objects.filter(pk=snap.pk).update(reported_balance=snap.reported_balance + 1)
    _, rows = _findings()
    assert [f.issue_type for f in rows] == ["BALANCE_LEDGER_MISMATCH"]
