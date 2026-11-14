"""Reconciliation services (ARCHITECTURE §4, §8.2, §8.4): ``take_snapshot`` and
``run_reconciliation``. Neither ever writes, changes or deletes a domain row (REC-02,
safeguard 1); both use only the default database (ARCHITECTURE §12.2).

Run workflow (decision D-R1, REC-01, REC-03):

1. one short transaction stores the run as RUNNING;
2. the rows are loaded and every check runs **outside any transaction** (ARCHITECTURE
   §8.2). Reads are not a snapshot (decision D-R2): a write committed while the run reads
   can be seen by some checks and not by others, so overlapping writes may show up as
   findings on the records being written. Re-run once the writes have finished;
3. one short transaction writes every finding and marks the run COMPLETED.

If step 2 or 3 fails, step 3's transaction rolls back (no partial findings) and the run is
marked FAILED in its own transaction, with the error in ``summary``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from billing.services import check_business_date
from core.ids import anomaly_finding_id, next_id, reserve_sequence, sequence_key
from reconciliation import calculators as calc
from reconciliation import engine, loader, policy
from reconciliation.lifecycle import run_transition_allowed
from reconciliation.models import AnomalyFinding, BalanceSnapshot, ReconciliationRun
from students.models import Student

log = logging.getLogger(__name__)

DB = "default"


# --- snapshots (BR-BAL-03; DATABASE_DESIGN §3) ------------------------------------------------


@dataclass(frozen=True)
class SnapshotResult:
    snapshot: BalanceSnapshot
    created: bool


def _snapshot_for(student_id: str, as_of_date: date) -> SnapshotResult:
    existing = BalanceSnapshot.objects.filter(student_id=student_id, as_of_date=as_of_date).first()
    if existing is not None:  # decision D-R6: returned unchanged
        return SnapshotResult(existing, False)
    from billing.models import LedgerEntry

    totals = calc.snapshot_totals(
        LedgerEntry.objects.filter(student_id=student_id, entry_date__lte=as_of_date), as_of_date
    )
    snap = BalanceSnapshot(
        snapshot_id=next_id("balance_snapshot", year=as_of_date.year),
        student_id=student_id,
        as_of_date=as_of_date,
        reported_balance=totals.balance,
        total_debits=totals.debits,
        total_credits=totals.credits,
    )
    snap.save(force_insert=True)
    return SnapshotResult(snap, True)


def take_snapshot(*, as_of_date: date, student: Student | None = None) -> list[SnapshotResult]:
    """Snapshot one student, or every student (in student_id order), at ``as_of_date``:
    reported_balance = BR-BAL-01, total_debits / total_credits = Σ positive / Σ negative
    entries dated on or before it. One transaction. An existing (student, as_of_date)
    snapshot is returned unchanged with ``created=False`` (decision D-R6)."""
    check_business_date("as_of_date", as_of_date)
    with transaction.atomic(using=DB):
        ids = (
            [student.pk]
            if student is not None
            else list(Student.objects.order_by("pk").values_list("pk", flat=True))
        )
        return [_snapshot_for(sid, as_of_date) for sid in ids]


# --- runs (ARCHITECTURE §12.1) -------------------------------------------------------------------


def _set_status(run: ReconciliationRun, status: str, summary: dict) -> None:
    if not run_transition_allowed(run.status, status):
        raise RuntimeError(f"{run.run_id}: {run.status} → {status} is not allowed")
    run.status = status
    run.summary = summary
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "finished_at", "summary"])


def _write_findings(run: ReconciliationRun, result: engine.EngineResult) -> None:
    year = run.as_of_date.year
    rows = result.findings
    first = reserve_sequence(sequence_key("anomaly_finding", year=year), len(rows)) if rows else 0
    AnomalyFinding.objects.bulk_create(
        [
            AnomalyFinding(
                finding_id=anomaly_finding_id(year, first + i),
                run=run,
                issue_type=rf.finding.issue_type,
                rule_id=rf.finding.rule_id,
                severity=rf.severity,
                entity_type=rf.finding.entity_type,
                record_id=rf.finding.record_id,
                student_id=rf.finding.student_id,
                expected_value=rf.finding.expected,
                actual_value=rf.finding.actual,
                details={"check_id": rf.check_id, "family": rf.family, **rf.finding.details},
                recommended_action=rf.recommended_action,
            )
            for i, rf in enumerate(rows)
        ],
        batch_size=2000,
    )


def validate_scope(scope: str, student: Student | None) -> None:
    if scope not in policy.SCOPES:
        raise ValidationError({"scope": [f"Must be one of {', '.join(policy.SCOPES)}."]})
    if scope == policy.STUDENT and student is None:
        raise ValidationError({"student_id": ["Required when scope is STUDENT."]})
    if scope == policy.ALL and student is not None:
        raise ValidationError({"student_id": ["Must be omitted when scope is ALL."]})


def run_reconciliation(
    *, scope: str, as_of_date: date, student: Student | None = None, triggered_by: str
) -> ReconciliationRun:
    """Run every check (one student or all) at ``as_of_date``. Returns the run: COMPLETED
    with its findings, or FAILED with none and the error in ``summary["error"]``."""
    validate_scope(scope, student)
    check_business_date("as_of_date", as_of_date)
    if triggered_by not in policy.TRIGGERS:
        raise ValueError(f"triggered_by must be one of {policy.TRIGGERS}")
    if not run_transition_allowed(None, policy.RUNNING):  # pragma: no cover - static table
        raise RuntimeError("runs must start RUNNING")
    with transaction.atomic(using=DB):  # 1. RUNNING
        run = ReconciliationRun(
            run_id=next_id("reconciliation_run", year=as_of_date.year),
            scope=scope,
            student=student,
            as_of_date=as_of_date,
            status=policy.RUNNING,
            started_at=timezone.now(),
            triggered_by=triggered_by,
            summary={},
        )
        run.save(force_insert=True)
    try:
        ds = loader.load(as_of=as_of_date, student_id=student.pk if student else None)  # 2.
        result = engine.run_checks(ds)
        with transaction.atomic(using=DB):  # 3. findings + COMPLETED, all or nothing
            _write_findings(run, result)
            _set_status(run, policy.COMPLETED, result.summary)
    except Exception as exc:  # noqa: BLE001 - any failure ends the run FAILED
        log.exception("reconciliation run %s failed", run.run_id)
        run.refresh_from_db()
        with transaction.atomic(using=DB):
            _set_status(
                run,
                policy.FAILED,
                {"error": {"type": type(exc).__name__, "message": str(exc)[:500]}},
            )
    return run
