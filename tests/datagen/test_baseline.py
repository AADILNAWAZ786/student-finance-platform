"""Mandatory validation 4: a baseline-only starter generation (no injection) gives zero findings
across every one of the 63 detector checks, and every student reconciles (BR-BAL-02)."""

import pytest

from datagen.models import GroundTruthLabel
from reconciliation.checks import REGISTRY
from reconciliation.models import AnomalyFinding

pytestmark = pytest.mark.django_db(databases=["default", "ground_truth"])


def test_baseline_has_no_labels_and_exact_counts(baseline):
    assert GroundTruthLabel.objects.using("ground_truth").count() == 0
    counts = baseline.run.row_counts
    assert (counts["billing_invoice"], counts["scholarships_scholarship"]) == (300, 150)
    assert counts["payments_payment"] == 500


def test_baseline_has_zero_findings_on_every_check(baseline):
    run = baseline.reconciliation_run
    assert run.status == "COMPLETED"
    checks = {c["check_id"]: c for c in run.summary["checks"]}
    assert set(checks) == set(REGISTRY) and len(checks) == 63
    assert {k: c["findings"] for k, c in checks.items() if c["findings"]} == {}
    assert AnomalyFinding.objects.filter(run=run).count() == 0
    assert run.summary["findings_total"] == 0
    assert run.summary["students"] == {
        **run.summary["students"],
        "total": 100,
        "reconciled": 100,
        "pct_reconciled": "100.00",
        "not_reconciled": [],
    }


def test_every_check_examined_the_generated_rows(baseline):
    """Zero findings is not zero work: the checks that read rows did read them."""
    checks = {c["check_id"]: c for c in baseline.reconciliation_run.summary["checks"]}
    for check_id in (
        "S-ID-GAPS-YEAR",
        "S-ID-GAPS-PARENT",
        "A-LINE-AMOUNTS",
        "B-IDENTITY",
        "A-LATE-FEE",
        "A-ELIGIBILITY",
        "A-EXIT-ADJUSTMENTS",
    ):
        assert checks[check_id]["records_checked"] > 0, check_id


def test_baseline_evaluation_is_vacuous_but_clean(baseline):
    ev = baseline.evaluation
    assert ev["overall"]["labels"] == ev["overall"]["findings"] == 0
    assert ev["unexpected"] == 0 and ev["students"]["pct_non_anomalous_reconciled"] == "100.00"
    for m in ev["by_type"].values():  # 0/0 is reported as null, never as 1
        assert (m["precision"], m["recall"], m["f1"]) == (None, None, None)
    assert ev["meets_targets"] is True  # vacuously: nothing labelled, nothing found
