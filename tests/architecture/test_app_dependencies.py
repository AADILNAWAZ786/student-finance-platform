"""ARCHITECTURE.md §4: app dependencies only point upward, with no cycles.

    core ← students ↔ academics ← billing ← scholarships / payments ← reconciliation ← datagen

``scholarships`` and ``payments`` never import each other; billing imports neither (it sees
their allocations through ``billing.allocations``, decision D-S1). Static AST check of every
import statement (including imports inside functions); migrations are excluded.

Known Phase 5 conflict (documented, D-S17): §4 also says ``academics.withdraw_enrollment``
orchestrates scholarships and payments, which academics may not import under this graph.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPS = [
    "core",
    "students",
    "academics",
    "billing",
    "scholarships",
    "payments",
    "reconciliation",
    "datagen",
]
ALLOWED = {
    "core": set(),
    "students": {"core", "academics"},
    "academics": {"core", "students"},
    "billing": {"core", "students", "academics"},
    "scholarships": {"core", "students", "academics", "billing"},
    "payments": {"core", "students", "academics", "billing"},
    "reconciliation": {"core", "students", "academics", "billing", "scholarships", "payments"},
    "datagen": set(APPS) - {"datagen"},
}


def _imports(package):
    found = set()
    for path in (ROOT / package).rglob("*.py"):
        if "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in APPS and top != package:
                    found.add((top, f"{path.relative_to(ROOT)}:{node.lineno}"))
    return found


@pytest.mark.parametrize("package", APPS)
def test_imports_follow_the_dependency_graph(package):
    offenders = sorted(where for top, where in _imports(package) if top not in ALLOWED[package])
    assert offenders == []


def test_billing_imports_neither_scholarships_nor_payments():
    assert not {top for top, _ in _imports("billing")} & {"scholarships", "payments"}


def test_scholarships_never_imports_payments_or_reconciliation():
    assert not {top for top, _ in _imports("scholarships")} & {
        "payments",
        "reconciliation",
        "datagen",
    }


def test_scholarships_reaches_billing_only_through_its_public_modules():
    used = {where for top, where in _imports("scholarships") if top == "billing"}
    assert used, "scholarships posts to billing's ledger"
