"""Mandatory validation 7: ground-truth routing and detector isolation (ARCHITECTURE §12.2).

The frozen import rule (no blind package imports datagen) stays in
tests/architecture/test_ground_truth_isolation.py; this module adds the Phase 7 parts:
evaluation is the only datagen module that touches findings, the generator never reads the
detector's checks, labels exist only in the ground-truth database, no label information sits
in the domain database, and the detector runs with the ground-truth connection unusable.
"""

import ast
from pathlib import Path
from unittest import mock

import pytest
from django.db import connections

from core.models import IdSequence
from datagen.models import GenerationRun, GroundTruthLabel
from reconciliation import services as reconciliation

ROOT = Path(__file__).resolve().parents[2]
DATAGEN = ROOT / "datagen"


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.ClassDef):
            out.add(node.name)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name.split(".")[-1])
            out.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _modules():
    return sorted(p for p in DATAGEN.rglob("*.py") if "migrations" not in p.parts)


def test_only_evaluation_touches_findings():
    users = {p.relative_to(ROOT).as_posix() for p in _modules() if "AnomalyFinding" in _names(p)}
    assert users == {"datagen/evaluation.py"}


def test_the_generator_never_reads_the_detector():
    detector = {
        "reconciliation.checks",
        "reconciliation.engine",
        "reconciliation.loader",
        "reconciliation.dataset",
    }
    for path in (DATAGEN / "pipeline").rglob("*.py"):
        assert not (_names(path) & detector), path


def test_label_model_is_named_only_where_labels_are_written_or_evaluated():
    users = {p.relative_to(ROOT).as_posix() for p in _modules() if "GroundTruthLabel" in _names(p)}
    assert users == {
        "datagen/models.py",
        "datagen/services.py",
        "datagen/evaluation.py",
    }


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_label_table_exists_only_in_the_ground_truth_database():
    default = set(connections["default"].introspection.table_names())
    gt = set(connections["ground_truth"].introspection.table_names())
    assert "datagen_groundtruthlabel" in gt and "datagen_groundtruthlabel" not in default
    assert GroundTruthLabel.objects.db == "ground_truth"
    assert GenerationRun.objects.db == "default"


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_no_label_information_in_the_domain_database(generated):
    keys = list(IdSequence.objects.values_list("key", flat=True))
    assert not [k for k in keys if k.startswith("GT")]  # D-G3
    run = GenerationRun.objects.get()
    assert set(run.checksums) == {"tables", "dataset"}
    assert not [t for t in run.checksums["tables"] if "groundtruth" in t]
    assert not [t for t in run.row_counts if "groundtruth" in t]


@pytest.mark.django_db(databases=["default", "ground_truth"])
def test_the_detector_runs_blind_with_the_ground_truth_connection_unusable(generated):
    """Any use of the ground_truth connection fails; the detector still completes and finds
    exactly what the generated run found."""
    gt = connections["ground_truth"]

    def refuse(*args, **kwargs):
        raise AssertionError("the detector touched the ground-truth database")

    with (
        mock.patch.object(gt, "cursor", side_effect=refuse),
        mock.patch.object(gt, "ensure_connection", side_effect=refuse),
    ):
        run = reconciliation.run_reconciliation(
            scope="ALL", as_of_date=generated.reconciliation_run.as_of_date, triggered_by="API"
        )
    assert run.status == "COMPLETED"
    assert (
        run.summary["findings_by_issue_type"]
        == (generated.reconciliation_run.summary["findings_by_issue_type"])
    )
