"""Safeguard 3 (the gate before any anomaly test): a world built only through the Phase 1–5
services gives **zero findings** at every as-of date, with 100 % of students reconciled
(ARCHITECTURE §12.3), and a run changes no domain row (REC-02)."""

import pytest

from reconciliation import services
from reconciliation.models import AnomalyFinding
from tests.reconciliation_fixtures import RUN_DATES, SNAPSHOT_DATES

pytestmark = pytest.mark.django_db


def _describe(run):
    return [
        (f.issue_type, f.rule_id, f.entity_type, f.record_id, f.details)
        for f in AnomalyFinding.objects.filter(run=run).order_by("finding_id")
    ]


@pytest.mark.parametrize("as_of", RUN_DATES, ids=str)
def test_clean_world_has_no_findings(clean_world, as_of):
    for d in SNAPSHOT_DATES:
        services.take_snapshot(as_of_date=d)
    run = services.run_reconciliation(scope="ALL", as_of_date=as_of, triggered_by="API")
    assert run.status == "COMPLETED", run.summary
    assert _describe(run) == []
    s = run.summary
    assert s["findings_total"] == 0
    assert s["students"]["pct_reconciled"] == "100.00"
    assert s["students"]["total"] == len(clean_world)
    assert s["skipped_checks"] == []
    # every check family ran and looked at something
    assert {c["family"] for c in s["checks"]} == {
        "STRUCTURAL",
        "RELATIONSHIP",
        "TEMPORAL",
        "LIFECYCLE",
        "ARITHMETIC",
        "REQUIRED",
        "RECONCILIATION",
    }


def test_clean_world_has_no_findings_per_student(clean_world):
    for d in SNAPSHOT_DATES:
        services.take_snapshot(as_of_date=d)
    for name, student in sorted(clean_world.items()):
        run = services.run_reconciliation(
            scope="STUDENT", student=student, as_of_date=RUN_DATES[-1], triggered_by="API"
        )
        assert run.status == "COMPLETED", (name, run.summary)
        assert _describe(run) == [], name
        assert run.summary["students"] == {
            "total": 1,
            "reconciled": 1,
            "pct_reconciled": "100.00",
            "not_reconciled": [],
        }
