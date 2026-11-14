"""Evaluation: compare one reconciliation run with the ground truth (ARCHITECTURE §12.2–§12.3).

This is the ONLY module that reads both the detector's findings (default database) and the
labels (ground_truth database). The detector never reaches it: no blind package may import
``datagen`` (tests/architecture/test_ground_truth_isolation.py).

Algorithm (frozen §12.3, approved decision D-G4):
  1. Findings and labels are joined on (issue_type, record_id): a match is a true positive (TP);
     a label without a matching finding is a false negative (FN).
  2. Of the remaining findings, one whose record_id is in some label's ``related_ids`` and
     whose issue type differs from that label's type is **consequential** (counted
     separately, never a false positive).
  3. Every other remaining finding is **unexpected**. If its type is one of the five required
     types it is also a false positive (FP) of that type.
  Per required type: precision = TP / (TP + FP), recall = TP / (TP + FN),
  F1 = 2PR / (P + R). 0/0 is reported as null, never as 1; F1 is 0 when P or R is 0.
  Students: the non-anomalous students are those no label names; each must be reconciled
  (BR-BAL-02, ``summary.students.not_reconciled``).
Targets: precision = recall = 1 for each type, 0 unexpected, 100 % non-anomalous reconciled.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from datagen.models import REQUIRED_ISSUE_TYPES

FOUR = Decimal("0.0001")


class EvaluationError(ValueError):
    """The requested evaluation is not possible (unknown run, wrong scope, not completed)."""


@dataclass(frozen=True)
class F:  # a finding, as the evaluation sees it
    finding_id: str
    issue_type: str
    record_id: str
    student_id: str | None = None


@dataclass(frozen=True)
class L:  # a label, as the evaluation sees it
    label_id: str
    issue_type: str
    record_id: str
    student_id: str
    related_ids: tuple = ()


def _ratio(num: int, den: int) -> str | None:
    if den == 0:
        return None
    return str((Decimal(num) / Decimal(den)).quantize(FOUR, rounding=ROUND_HALF_UP))


def _f1(p: str | None, r: str | None) -> str | None:
    if p is None or r is None:
        return None
    p_, r_ = Decimal(p), Decimal(r)
    if p_ + r_ == 0:
        return "0.0000"
    return str((2 * p_ * r_ / (p_ + r_)).quantize(FOUR, rounding=ROUND_HALF_UP))


def compute(
    findings: Iterable[F],
    labels: Iterable[L],
    *,
    students: Iterable[str],
    not_reconciled: Iterable[str],
) -> dict:
    """The metrics of §12.3 from plain findings and labels (no database)."""
    findings = sorted(findings, key=lambda f: f.finding_id)
    labels = sorted(labels, key=lambda x: x.label_id)
    by_key = {(x.issue_type, x.record_id): x for x in labels}
    matched_labels: set = set()
    tp_findings, rest = [], []
    for f in findings:
        key = (f.issue_type, f.record_id)
        if key in by_key and key not in matched_labels:
            matched_labels.add(key)
            tp_findings.append(f)
        else:
            rest.append(f)
    consequential, unexpected = [], []
    for f in rest:
        sources = sorted(
            x.label_id
            for x in labels
            if f.record_id in x.related_ids and f.issue_type != x.issue_type
        )
        if sources:
            consequential.append((f, sources))
        else:
            unexpected.append(f)

    by_type = {}
    for t in REQUIRED_ISSUE_TYPES:
        tp = sum(1 for f in tp_findings if f.issue_type == t)
        fp = sum(1 for f in unexpected if f.issue_type == t)
        n_labels = sum(1 for x in labels if x.issue_type == t)
        fn = n_labels - tp
        precision, recall = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
        by_type[t] = {
            "labels": n_labels,
            "findings": sum(1 for f in findings if f.issue_type == t),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "consequential": sum(1 for f, _ in consequential if f.issue_type == t),
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
        }
    tp_all = sum(v["tp"] for v in by_type.values())
    fp_all = sum(v["fp"] for v in by_type.values())
    fn_all = sum(v["fn"] for v in by_type.values())
    p_all, r_all = _ratio(tp_all, tp_all + fp_all), _ratio(tp_all, tp_all + fn_all)

    labelled = {x.student_id for x in labels}
    clean = sorted(set(students) - labelled)
    failed = sorted(set(not_reconciled) & set(clean))
    pct = _ratio(len(clean) - len(failed), len(clean))
    pct_reconciled = None if pct is None else f"{Decimal(pct) * 100:.2f}"

    unexpected_by_type: dict = {}
    for f in unexpected:
        unexpected_by_type[f.issue_type] = unexpected_by_type.get(f.issue_type, 0) + 1
    # a type with neither labels nor findings is vacuously met (0/0 → null); any label without
    # its finding (recall < 1) or any false positive (precision < 1) fails the target
    meets = (
        all(
            v["precision"] in (None, "1.0000") and v["recall"] in (None, "1.0000")
            for v in by_type.values()
        )
        and not unexpected
        and not failed
    )
    return {
        "by_type": by_type,
        "overall": {
            "labels": len(labels),
            "findings": len(findings),
            "tp": tp_all,
            "fp": fp_all,
            "fn": fn_all,
            "precision": p_all,
            "recall": r_all,
            "f1": _f1(p_all, r_all),
        },
        "consequential": len(consequential),
        "consequential_findings": [
            {
                "finding_id": f.finding_id,
                "issue_type": f.issue_type,
                "record_id": f.record_id,
                "caused_by_labels": src,
            }
            for f, src in consequential
        ],
        "unexpected": len(unexpected),
        "unexpected_by_type": dict(sorted(unexpected_by_type.items())),
        "unexpected_findings": [
            {"finding_id": f.finding_id, "issue_type": f.issue_type, "record_id": f.record_id}
            for f in unexpected[:100]
        ],
        "false_negatives": sorted(
            x.label_id for x in labels if (x.issue_type, x.record_id) not in matched_labels
        ),
        "students": {
            "total": len(set(students)),
            "with_labels": len(labelled),
            "non_anomalous": len(clean),
            "non_anomalous_not_reconciled": failed,
            "pct_non_anomalous_reconciled": pct_reconciled,
        },
        "targets": {
            "precision_per_type": "1.0000",
            "recall_per_type": "1.0000",
            "unexpected": 0,
            "pct_non_anomalous_reconciled": "100.00",
        },
        "meets_targets": meets,
    }


def labels_of(generation_run_id: str):
    """The label rows of one generation run, as ``(model, using, queryset)`` for the exporter
    (Phase 8): the label model stays named only where labels are written or evaluated."""
    from datagen.models import GroundTruthLabel

    qs = GroundTruthLabel.objects.using("ground_truth").filter(generation_run_id=generation_run_id)
    return GroundTruthLabel, "ground_truth", qs


def findings_of(reconciliation_run_id: str):
    """The finding rows of one reconciliation run, as ``(model, using, queryset)``."""
    from reconciliation.models import AnomalyFinding

    return AnomalyFinding, "default", AnomalyFinding.objects.filter(run_id=reconciliation_run_id)


def evaluate(*, generation_run_id: str, reconciliation_run_id: str) -> dict:
    """Evaluate a stored reconciliation run against a generation run's labels."""
    from datagen.models import GenerationRun, GroundTruthLabel
    from reconciliation.models import AnomalyFinding, ReconciliationRun
    from students.models import Student

    gen = GenerationRun.objects.filter(pk=generation_run_id).first()
    if gen is None:
        raise EvaluationError(f"Unknown generation run {generation_run_id}.")
    run = ReconciliationRun.objects.filter(pk=reconciliation_run_id).first()
    if run is None:
        raise EvaluationError(f"Unknown reconciliation run {reconciliation_run_id}.")
    if run.status != "COMPLETED":
        raise EvaluationError(f"{run.pk} is {run.status}; only a COMPLETED run can be evaluated.")
    if run.scope != "ALL":
        raise EvaluationError(
            f"{run.pk} has scope {run.scope}; only an ALL-scope run can be evaluated (labels "
            "outside a student scope would count as false negatives)."
        )
    labels = [
        L(x.label_id, x.issue_type, x.record_id, x.student_id, tuple(x.related_ids))
        for x in GroundTruthLabel.objects.using("ground_truth").filter(generation_run_id=gen.pk)
    ]
    findings = [
        F(f.finding_id, f.issue_type, f.record_id, f.student_id)
        for f in AnomalyFinding.objects.filter(run=run)
    ]
    report = compute(
        findings,
        labels,
        students=Student.objects.values_list("student_id", flat=True),
        not_reconciled=run.summary.get("students", {}).get("not_reconciled", []),
    )
    from datagen.services import stored_label_checksum

    return {
        "generation_run": gen.pk,
        "reconciliation_run": run.pk,
        "as_of_date": run.as_of_date.isoformat(),
        "seed": gen.seed,
        "scale_name": gen.scale_name,
        "config_hash": gen.config_hash,
        "labels_checksum": stored_label_checksum(gen.pk),
        **report,
    }
