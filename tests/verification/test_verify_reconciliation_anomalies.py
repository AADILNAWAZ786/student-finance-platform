"""Safeguards 7 and 8: each of the five required anomaly types, injected the DATABASE_DESIGN
§7 way into the clean world, is found with its documented issue type, rule ID, severity,
recommended action and record_id (DATABASE_DESIGN §3 convention), and nothing else is found
except the one documented consequential finding:

* an injected refund above net paid also over-commits its source payment under BR-ALC-01
  (Σ allocations + Σ refunds not REJECTED ≤ amount), so that payment gets a
  PAYMENT_OVER_ALLOCATED finding: consequential in the ARCHITECTURE §12.3 sense.

Snapshots are taken after the transactional injections (ARCHITECTURE §11.2), so no other
anomaly shows up as a knock-on balance mismatch.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from payments.models import Refund
from reconciliation import services
from reconciliation.models import AnomalyFinding
from tests.reconciliation_fixtures import (
    SNAPSHOT_DATES,
    inject_balance_mismatch,
    inject_duplicate_invoice,
    inject_payment_over_allocated,
    inject_refund_over_net_paid,
    inject_scholarship_over_award,
)

pytestmark = pytest.mark.django_db

END = date(2026, 12, 31)

# issue type → (rule_id, severity, recommended_action, entity_type)
CONTRACT = {
    "PAYMENT_OVER_ALLOCATED": ("BR-ALC-01", "CRITICAL", "REJECT_ALLOCATION", "Payment"),
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES": ("BR-SCH-04", "HIGH", "CAP_SCHOLARSHIP", "Scholarship"),
    "DUPLICATE_INVOICE": ("BR-INV-01", "HIGH", "VOID_DUPLICATE", "Invoice"),
    "REFUND_EXCEEDS_NET_PAID": ("BR-REF-01", "CRITICAL", "REVERSE_REFUND", "Refund"),
    "BALANCE_LEDGER_MISMATCH": ("BR-BAL-03", "HIGH", "RESTATE_BALANCE", "BalanceSnapshot"),
}


def _run():
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    assert run.status == "COMPLETED", run.summary
    return run


def _found(run):
    return {
        (f.issue_type, f.record_id): f
        for f in AnomalyFinding.objects.filter(run=run).order_by("finding_id")
    }


def _assert_contract(f, issue_type, student):
    assert (f.rule_id, f.severity, f.recommended_action, f.entity_type) == CONTRACT[issue_type]
    assert f.student_id == student.pk


def _snapshots():
    for d in SNAPSHOT_DATES:
        services.take_snapshot(as_of_date=d)


def test_payment_over_allocated(clean_world):
    s = clean_world["S16"]
    payment_id = inject_payment_over_allocated(s)
    found = _found(_run())
    assert set(found) == {("PAYMENT_OVER_ALLOCATED", payment_id)}
    f = found[("PAYMENT_OVER_ALLOCATED", payment_id)]
    _assert_contract(f, "PAYMENT_OVER_ALLOCATED", s)
    assert (f.expected_value, f.actual_value) == (D("50000.00"), D("51000.00"))
    assert f.details["over_by"] == "1000.00"


def test_scholarship_exceeds_eligible_fees(clean_world):
    s = clean_world["S16"]
    scholarship_id = inject_scholarship_over_award(s)
    found = _found(_run())
    assert set(found) == {("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", scholarship_id)}
    f = found[("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", scholarship_id)]
    _assert_contract(f, "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", s)
    assert (f.expected_value, f.actual_value) == (D("150000.00"), D("180000.00"))
    assert f.details["problem"] == "AWARD_ABOVE_BASE"


def test_duplicate_invoice(clean_world):
    s = clean_world["S16"]
    invoice_id = inject_duplicate_invoice(s)
    found = _found(_run())
    assert set(found) == {("DUPLICATE_INVOICE", invoice_id)}
    f = found[("DUPLICATE_INVOICE", invoice_id)]
    _assert_contract(f, "DUPLICATE_INVOICE", s)
    assert invoice_id.endswith("-000002")  # the later one, not the original


def test_refund_exceeds_net_paid_and_its_consequence(clean_world):
    s = clean_world["S17"]
    refund_id = inject_refund_over_net_paid(s)
    found = _found(_run())
    payment_id = Refund.objects.get(pk=refund_id).payment_id
    assert set(found) == {
        ("REFUND_EXCEEDS_NET_PAID", refund_id),
        ("PAYMENT_OVER_ALLOCATED", payment_id),  # documented consequential finding
    }
    f = found[("REFUND_EXCEEDS_NET_PAID", refund_id)]
    _assert_contract(f, "REFUND_EXCEEDS_NET_PAID", s)
    assert (f.expected_value, f.actual_value) == (D("20000.00"), D("25000.00"))
    c = found[("PAYMENT_OVER_ALLOCATED", payment_id)]
    assert refund_id in c.details["refund_ids"]


def test_balance_ledger_mismatch(clean_world):
    s = clean_world["S6"]
    _snapshots()
    snapshot_id = inject_balance_mismatch(s)
    found = _found(_run())
    assert set(found) == {("BALANCE_LEDGER_MISMATCH", snapshot_id)}
    f = found[("BALANCE_LEDGER_MISMATCH", snapshot_id)]
    _assert_contract(f, "BALANCE_LEDGER_MISMATCH", s)
    assert f.actual_value - f.expected_value == D("2500.00")


def test_all_five_together_each_once_and_identity_still_holds(clean_world):
    ids = {
        "PAYMENT_OVER_ALLOCATED": inject_payment_over_allocated(clean_world["S16"]),
        "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES": inject_scholarship_over_award(clean_world["S16"]),
        "DUPLICATE_INVOICE": inject_duplicate_invoice(clean_world["S6"]),
        "REFUND_EXCEEDS_NET_PAID": inject_refund_over_net_paid(clean_world["S17"]),
    }
    _snapshots()  # after the transactional injections (ARCHITECTURE §11.2)
    ids["BALANCE_LEDGER_MISMATCH"] = inject_balance_mismatch(clean_world["S2"])
    run = _run()
    found = _found(run)
    required = {(t, rid) for t, rid in ids.items()}
    assert required <= set(found)
    consequential = set(found) - required
    assert {t for t, _ in consequential} == {"PAYMENT_OVER_ALLOCATED"}
    assert len(consequential) == 1
    s = run.summary
    assert s["findings_by_issue_type"]["PAYMENT_OVER_ALLOCATED"] == 2
    for t in ids:
        if t != "PAYMENT_OVER_ALLOCATED":
            assert s["findings_by_issue_type"][t] == 1
    # these corruptions never break the BR-BAL-02 identity (ARCHITECTURE §12.3)
    assert s["students"]["pct_reconciled"] == "100.00"
