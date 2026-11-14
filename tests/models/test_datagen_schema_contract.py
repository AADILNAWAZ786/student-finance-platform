"""GenerationRun and GroundTruthLabel match DATABASE_DESIGN.md §3 (datagen), read from the frozen
document: columns, lengths, decimal precision, required/optional, the enums, and no foreign
key from a label to anything (cross-database links are plain IDs)."""

import re

import pytest
from django.db import models

from datagen.models import REQUIRED_ISSUE_TYPES, GenerationRun, GroundTruthLabel
from tests.models.test_billing_schema_contract import DESIGN, _model_fields, _section, _tables

TABLES = {"GenerationRun": GenerationRun, "GroundTruthLabel": GroundTruthLabel}


@pytest.fixture(scope="module")
def design():
    return _tables(_section(DESIGN.read_text(encoding="utf-8"), "### datagen", "## 4."))


def _rows(design, table):
    """Expand combined rows ("scale_name / scale_factor" | "Char(10) / Decimal(5,4)")."""
    rows = []
    for name, design_type, rest in design[table]:
        names = [n.strip() for n in name.split("/")]
        types = [t.strip() for t in design_type.split(" / ")]
        types = types if len(types) == len(names) else types * len(names)
        req = rest.split("|")[0].strip()
        reqs = [r.strip() for r in req.split("/")] if "/" in req else [req] * len(names)
        rows += list(zip(names, types, reqs, strict=True))
    rows.append(("recorded_at", "DateTime", "✓"))  # every table (Conventions)
    return rows


def _check(field, t):
    if m := re.match(r"Char\((\d+)\)", t):
        assert isinstance(field, models.CharField) and field.max_length == int(m.group(1))
        assert field.primary_key == ("PK" in t)
    elif m := re.match(r"Decimal\((\d+),(\d+)\)", t):
        assert isinstance(field, models.DecimalField)
        assert (field.max_digits, field.decimal_places) == (int(m.group(1)), int(m.group(2)))
    elif t == "JSON":
        assert isinstance(field, models.JSONField)
    elif t == "BigInteger":
        assert isinstance(field, models.BigIntegerField)
    elif t == "DateTime":
        assert isinstance(field, models.DateTimeField)
    else:  # pragma: no cover - a type the datagen section does not use
        raise AssertionError(t)


def test_the_design_section_has_the_two_tables(design):
    assert set(design) == set(TABLES)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_table_matches_design(design, table):
    actual = _model_fields(TABLES[table])
    rows = _rows(design, table)
    assert set(actual) == {n for n, _, _ in rows}, table
    for name, design_type, required in rows:
        field = actual[name]
        _check(field, design_type)
        if not field.primary_key and name != "recorded_at":
            assert field.null == (required == "–"), (table, name, required)


def test_labels_reference_other_rows_by_plain_ids_only():
    assert not [f for f in GroundTruthLabel._meta.get_fields() if f.is_relation]


def test_label_enums_match_design(design):
    rest = {name: r for name, _, r in design["GroundTruthLabel"]}
    assert "one of the 5 required types" in rest["issue_type"]
    field = _model_fields(GroundTruthLabel)
    assert {v for v, _ in field["issue_type"].choices} == set(REQUIRED_ISSUE_TYPES)
    assert {v for v, _ in field["injection_method"].choices} == set(
        re.findall(r"`([A-Z]+)`", rest["injection_method"])
    )
    run_rest = {name: r for name, _, r in design["GenerationRun"]}
    assert "20261022" in run_rest["seed"] and GenerationRun._meta.get_field("seed").default == (
        20261022
    )
