"""ARCHITECTURE.md §12.2: the detector and domain apps cannot reach ground truth.

Static (AST) check over the source tree; no Django needed. It fails until the listed
packages exist, which is intended: it is an acceptance test for Phase 6.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BLIND_PACKAGES = [
    "core",
    "students",
    "academics",
    "billing",
    "scholarships",
    "payments",
    "reconciliation",
]
FORBIDDEN_NAMES = {"GroundTruthLabel", "ground_truth"}


def _python_files(package):
    pkg = ROOT / package
    assert pkg.is_dir(), f"package {package}/ not implemented yet"
    return [p for p in pkg.rglob("*.py") if "migrations" not in p.parts]


@pytest.mark.parametrize("package", BLIND_PACKAGES)
def test_blind_packages_do_not_import_datagen(package):
    offenders = []
    for path in _python_files(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "datagen" or n.startswith("datagen.") for n in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("package", BLIND_PACKAGES)
def test_blind_packages_never_name_ground_truth(package):
    offenders = []
    for path in _python_files(package):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Constant) and node.value in FORBIDDEN_NAMES:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []
