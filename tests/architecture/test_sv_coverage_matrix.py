"""docs/phase6_sv_coverage.md stays true to the code (P6-O7): every registered check is mapped
to a rule in sections A–C, section E lists exactly the registered checks, every test it cites
exists, and every rule row names a frozen issue type."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "phase6_sv_coverage.md").read_text(encoding="utf-8")
MAPPING, CATALOGUE = DOC.split("## E. The 63 registered checks")
FILES = {
    "U": "tests/unit/test_reconciliation_sv_rules.py",
    "UC": "tests/unit/test_reconciliation_checks.py",
    "V": "tests/verification/test_verify_reconciliation_sv_rules.py",
    "VI": "tests/verification/test_verify_reconciliation_issue_types.py",
    "VA": "tests/verification/test_verify_reconciliation_anomalies.py",
    "UG": "tests/unit/test_reconciliation_counter_gaps.py",
    "VG": "tests/verification/test_verify_reconciliation_counter_gaps.py",
}


def _registry():
    import django

    django.setup()
    from reconciliation.checks import REGISTRY

    return REGISTRY


def test_every_registered_check_is_mapped_and_catalogued():
    registry = _registry()
    assert len(registry) == 63
    catalogued = set(re.findall(r"^\| ([A-Z]{1,2}-[A-Z0-9-]+) \|", CATALOGUE, re.M))
    assert catalogued == set(registry)
    for check_id in registry:
        assert re.search(rf"\b{re.escape(check_id)}\b", MAPPING), f"{check_id} is not mapped"


def test_every_cited_test_exists():
    names = {
        code: set(re.findall(r"^def (test_\w+)", (ROOT / f).read_text(), re.M))
        for code, f in FILES.items()
    }
    every = set().union(*names.values())
    cited = set(re.findall(r"`(test_\w+)`", MAPPING))
    assert cited, "no tests cited"
    assert cited <= every, sorted(cited - every)


def test_section_a_rows_use_frozen_issue_types():
    _registry()
    from reconciliation.policy import ISSUE_TYPES

    rows = [r for r in MAPPING.split("\n") if r.startswith("| SV-")]
    assert len(rows) == 61
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        types = {t.strip() for t in cells[5].split(",")}
        assert types <= set(ISSUE_TYPES), r
