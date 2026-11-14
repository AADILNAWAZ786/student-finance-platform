"""Run every check over a ``Dataset`` and assemble findings and the run summary.

Pure: no database access (the loader reads, the service writes). Deterministic (REC-06):
checks run in ARCHITECTURE §12.1 family order, findings are sorted by (family, check,
issue_type, record_id, details), and the summary is built from those lists only.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from core.money import round_money
from reconciliation import policy
from reconciliation.checks import Finding, ordered_checks
from reconciliation.checks.base import FAMILIES, GLOBAL
from reconciliation.checks.lifecycle import collection_not_evaluable
from reconciliation.dataset import Dataset


@dataclass(frozen=True)
class RankedFinding:
    check_id: str
    family: str
    finding: Finding
    severity: str
    recommended_action: str


@dataclass
class EngineResult:
    findings: list[RankedFinding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


def _sort_key(rf: RankedFinding):
    f = rf.finding
    return (
        FAMILIES.index(rf.family),
        rf.check_id,
        f.issue_type,
        f.record_id,
        json.dumps(f.details, sort_keys=True, default=str),
    )


def run_checks(ds: Dataset) -> EngineResult:
    ranked, per_check, skipped = [], [], []
    for c in ordered_checks():
        if ds.is_student_scope and c.scope == GLOBAL:
            skipped.append({"check_id": c.check_id, "family": c.family, "reason": "GLOBAL_CHECK"})
            continue
        checked, findings = c.fn(ds)
        for f in findings:
            assert f.issue_type in policy.ISSUE_TYPES and len(f.rule_id) <= 12, f
            ranked.append(
                RankedFinding(
                    c.check_id,
                    c.family,
                    f,
                    policy.SEVERITY[f.issue_type],
                    policy.ACTION[f.issue_type],
                )
            )
        per_check.append(
            {
                "check_id": c.check_id,
                "family": c.family,
                "rule_id": c.rule_id,
                "description": c.description,
                "records_checked": checked,
                "findings": len(findings),
            }
        )
    ranked.sort(key=_sort_key)

    not_reconciled = sorted(rf.finding.record_id for rf in ranked if rf.check_id == "B-IDENTITY")
    total = len(ds.students)
    reconciled = total - len(not_reconciled)
    by_type = Counter(rf.finding.issue_type for rf in ranked)
    by_severity = Counter(rf.severity for rf in ranked)
    summary = {
        "as_of_date": ds.as_of.isoformat(),
        "scope": policy.STUDENT if ds.is_student_scope else policy.ALL,
        "student_id": ds.student_id,
        "checks": per_check,
        "skipped_checks": skipped,
        "findings_total": len(ranked),
        "findings_by_issue_type": {t: by_type.get(t, 0) for t in policy.ISSUE_TYPES},
        "findings_by_severity": {s: by_severity.get(s, 0) for s in policy.SEVERITIES},
        "students": {
            "total": total,
            "reconciled": reconciled,
            "pct_reconciled": str(
                round_money(
                    Decimal(100) if total == 0 else Decimal(100 * reconciled) / Decimal(total)
                )
            ),
            "not_reconciled": not_reconciled,
        },
        "invoices_collection_status_not_evaluable": collection_not_evaluable(ds),
    }
    return EngineResult(ranked, summary)
