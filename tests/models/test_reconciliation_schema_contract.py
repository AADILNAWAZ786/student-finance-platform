"""The three reconciliation models match DATABASE_DESIGN.md §3 (reconciliation), read from the
frozen document: columns, lengths, decimal precision, FK targets, required/optional, enums,
and the (student, as_of_date) uniqueness. No exception."""

import re

import pytest
from django.apps import apps
from django.db import IntegrityError, transaction

from tests.models.test_billing_schema_contract import DESIGN, _model_fields, _section, _tables
from tests.models.test_scholarship_schema_contract import _check

TABLES = ["BalanceSnapshot", "ReconciliationRun", "AnomalyFinding"]


@pytest.fixture(scope="module")
def design():
    return _tables(
        _section(DESIGN.read_text(encoding="utf-8"), "### reconciliation", "### datagen")
    )


def _rows(design, table):
    """Expand the design's combined rows ("started_at / finished_at" with "✓ / –")."""
    rows = []
    for name, design_type, rest in design[table]:
        names = [n.strip() for n in name.split("/")]
        req = rest.split("|")[0].strip()
        reqs = [r.strip() for r in req.split("/")] if "/" in req else [req] * len(names)
        rows += [(n, design_type, r) for n, r in zip(names, reqs, strict=True)]
    if all(n != "recorded_at" for n, _, _ in rows):  # on every table (Conventions)
        rows.append(("recorded_at", "DateTime", "✓"))
    return rows


def test_the_design_section_has_the_three_tables(design):
    assert set(design) == set(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_table_matches_design(design, table):
    actual = _model_fields(apps.get_model("reconciliation", table))
    rows = _rows(design, table)
    assert set(actual) == {n for n, _, _ in rows}, table
    for name, design_type, required in rows:
        field = actual[name]
        _check(table, field, design_type)
        if not field.primary_key and name != "recorded_at":
            assert field.null == (required == "–"), (table, name, required)


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("ReconciliationRun", "scope"),
        ("ReconciliationRun", "status"),
        ("ReconciliationRun", "triggered_by"),
        ("AnomalyFinding", "issue_type"),
        ("AnomalyFinding", "severity"),
        ("AnomalyFinding", "recommended_action"),
    ],
)
def test_enums_match_design(design, table, field):
    rest = next(r for name, _, r in design[table] if name == field)
    designed = set(re.findall(r"`([A-Z_]+)`", rest))
    model_field = _model_fields(apps.get_model("reconciliation", table))[field]
    assert {v for v, _ in model_field.choices} == designed


@pytest.mark.django_db
def test_constraints_reject_what_the_design_forbids(student):
    from datetime import date

    from django.utils import timezone

    from reconciliation.models import BalanceSnapshot, ReconciliationRun

    def snap(i, **kw):
        fields = dict(
            snapshot_id=f"SNAP-2026-00000{i}",
            student=student,
            as_of_date=date(2026, 12, 31),
            reported_balance=0,
            total_debits=0,
            total_credits=0,
        )
        fields.update(kw)
        return BalanceSnapshot(**fields)

    snap(1).save(force_insert=True)
    for bad in (
        snap(2),
        snap(3, as_of_date=date(2026, 12, 30), total_debits=-1),
        snap(4, as_of_date=date(2026, 12, 29), total_credits=1),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            bad.save(force_insert=True)  # U(student, as_of_date); debits ≥ 0; credits ≤ 0

    def run(i, **kw):
        fields = dict(
            run_id=f"RECON-2026-00000{i}",
            scope="ALL",
            as_of_date=date(2026, 12, 31),
            status="RUNNING",
            started_at=timezone.now(),
            triggered_by="API",
        )
        fields.update(kw)
        return ReconciliationRun(**fields)

    run(1).save(force_insert=True)
    for bad in (
        run(2, scope="STUDENT"),
        run(3, student=student),
        run(4, status="DONE"),
        run(5, status="COMPLETED"),
        run(6, finished_at=timezone.now()),
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            bad.save(force_insert=True)
