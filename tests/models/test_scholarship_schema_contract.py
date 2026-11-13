"""The six scholarship models match DATABASE_DESIGN.md §3 (scholarships), read from the
frozen document: columns, lengths, decimal precision, FK targets and enums.

One documented exception (KNOWN_LIMITATIONS P3-O1): the design gives
``ScholarshipType.category`` as Char(12) but lists the value ``INTERNATIONAL`` (13
characters), so the column is Char(13). Any other difference fails.
"""

import re

import pytest
from django.apps import apps
from django.db import models

from tests.models.test_billing_schema_contract import DESIGN, _model_fields, _section, _tables

# (table, field) -> (design type, implemented type), the only allowed differences
EXCEPTIONS = {("ScholarshipType", "category"): ("Char(12)", "Char(13)")}
TABLES = [
    "ScholarshipType",
    "ScholarshipTypeProgram",
    "Eligibility",
    "Scholarship",
    "ScholarshipAllocation",
]


@pytest.fixture(scope="module")
def design():
    text = DESIGN.read_text(encoding="utf-8")
    tables = _tables(_section(text, "### scholarships", "### payments"))
    # the ScholarshipType column table is followed by the (non-bold) "Seed types" table,
    # whose header row starts with "ID": keep only the column rows
    rows = tables["ScholarshipType"]
    tables["ScholarshipType"] = rows[: [name for name, _, _ in rows].index("ID")]
    core = _tables(_section(text, "### core", "### students"))
    return {**tables, "StatusEventBase": core["StatusEventBase"]}


def _rows(design, table):
    if table == "ScholarshipEvent":  # "StatusEventBase + scholarship → Scholarship"
        rows = [*design["StatusEventBase"], ("scholarship", "→ Scholarship", "")]
    else:
        rows = list(design[table])
    if all(name != "recorded_at" for name, _, _ in rows):  # on every table (Conventions)
        rows.append(("recorded_at", "DateTime", ""))
    return rows


def _check(table, field, design_type):
    expected = EXCEPTIONS.get((table, field.name), (design_type, design_type))
    assert expected[0] == design_type, f"exception for {table}.{field.name} is stale"
    t = expected[1]
    if m := re.match(r"Char\((\d+)\)", t):
        assert isinstance(field, models.CharField) and field.max_length == int(m.group(1)), (
            table,
            field.name,
        )
        assert field.primary_key == ("PK" in t), (table, field.name)
    elif m := re.match(r"Decimal\((\d+),(\d+)\)", t):
        assert isinstance(field, models.DecimalField), (table, field.name)
        assert (field.max_digits, field.decimal_places) == (int(m.group(1)), int(m.group(2)))
    elif t.startswith("→"):
        assert field.is_relation and field.related_model.__name__ == t[1:].strip(), (
            table,
            field.name,
        )
    elif t.startswith("JSON"):
        assert isinstance(field, models.JSONField), (table, field.name)
    else:
        kinds = {
            "Date": models.DateField,
            "DateTime": models.DateTimeField,
            "Boolean": models.BooleanField,
            "PositiveSmallInt": models.PositiveSmallIntegerField,
        }
        assert isinstance(field, kinds[t]), (table, field.name, t)
        if t == "Date":
            assert not isinstance(field, models.DateTimeField)


@pytest.mark.parametrize("table", [*TABLES, "ScholarshipEvent"])
def test_table_matches_design(design, table):
    model = apps.get_model("scholarships", table)
    rows = _rows(design, table)
    actual = _model_fields(model)
    assert set(actual) == {name for name, _, _ in rows}, table
    for name, design_type, _ in rows:
        _check(table, actual[name], design_type)


def test_the_only_exception_is_needed_and_documented(design):
    row = next(r for r in design["ScholarshipType"] if r[0] == "category")
    longest = max(re.findall(r"`([A-Z_]+)`", row[2]), key=len)
    assert (row[1], longest, len(longest)) == ("Char(12)", "INTERNATIONAL", 13)


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("ScholarshipType", "category"),
        ("ScholarshipType", "residency"),
        ("ScholarshipType", "award_basis"),
        ("Scholarship", "status"),
        ("ScholarshipAllocation", "reason"),
    ],
)
def test_enums_match_design(design, table, field):
    rest = next(r for name, _, r in design[table] if name == field)
    designed = set(re.findall(r"`([A-Z_]+)`", rest))
    model_field = _model_fields(apps.get_model("scholarships", table))[field]
    assert {v for v, _ in model_field.choices} == designed


def test_coverage_scope_and_allocation_type_match_design(design):
    scope = next(r for n, _, r in design["ScholarshipType"] if n == "coverage_scope")
    assert {"ELIGIBLE_LINES_ONLY", "ALL_FEE_LINES"} <= set(re.findall(r"`([A-Z_]+)`", scope))
    kind = next(r for n, _, r in design["ScholarshipAllocation"] if n == "allocation_type")
    assert set(re.findall(r"`([A-Z_]+)`", kind)) == {"APPLY", "REVERSE"}
    m = apps.get_model("scholarships", "ScholarshipType")
    assert {v for v, _ in _model_fields(m)["coverage_scope"].choices} == {
        "ELIGIBLE_LINES_ONLY",
        "ALL_FEE_LINES",
    }


def test_unique_rules_match_design():
    sch = _model_fields(apps.get_model("scholarships", "Scholarship"))
    assert sch["eligibility"].unique  # "U (1:0..1)"
    names = {
        c.name for c in apps.get_model("scholarships", "ScholarshipTypeProgram")._meta.constraints
    }
    assert "scholtypeprog_unique_type_program" in names
    elig = apps.get_model("scholarships", "Eligibility")._meta
    assert not elig.unique_together and not [c for c in elig.constraints if hasattr(c, "fields")]
