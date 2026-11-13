"""The billing models match DATABASE_DESIGN.md §3 (billing), read from the frozen document.

Four tables must match the design exactly. LedgerEntry is staged (decision D1): the only
differences allowed are the source columns and entry types that later phases add, listed
in STAGED below. When a later phase adds one, it must be removed from STAGED, so the final
schema is proved equal to the design.
"""

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings
from django.db import models

from billing.calculators import LEDGER_SIGN

DESIGN = Path(settings.BASE_DIR) / "docs" / "DATABASE_DESIGN.md"

# column / entry type -> the phase that adds it (ARCHITECTURE §16). Phase 3 added
# scholarship_allocation, SCHOLARSHIP_CREDIT and SCHOLARSHIP_REVERSAL (billing 0002).
STAGED_COLUMNS = {"payment": 4, "refund": 4}
STAGED_ENTRY_TYPES = {"PAYMENT": 4, "REFUND": 4}


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _tables(section: str) -> dict[str, list[tuple[str, str, str]]]:
    """{table: [(field, type, rest-of-row)]} from the **Name** headed markdown tables."""
    tables, current = {}, None
    for line in section.splitlines():
        if line.startswith("**"):  # a new bold heading ends the previous table
            head = re.match(r"\*\*(\w+)\*\*", line)
            current = head.group(1) if head else None
            if current:
                tables[current] = []
            continue
        if (
            current
            and line.startswith("| ")
            and not line.startswith("| Field")
            and "---" not in line
        ):
            cells = [c.strip() for c in line.strip("|").split("|")]
            for name in cells[0].split(","):
                tables[current].append((name.strip(), cells[1], " | ".join(cells[2:])))
    return {k: v for k, v in tables.items() if v}


@pytest.fixture(scope="module")
def design():
    text = DESIGN.read_text(encoding="utf-8")
    billing = _tables(_section(text, "### billing", "### scholarships"))
    core = _tables(_section(text, "### core", "### students"))
    return {**billing, "StatusEventBase": core["StatusEventBase"]}


def _model_fields(model):
    return {f.name: f for f in model._meta.get_fields() if f.concrete and not f.auto_created}


def _expected_fields(design, table):
    """The design's columns; ``recorded_at`` is on every table (DATABASE_DESIGN Conventions)."""
    if table == "InvoiceEvent":  # "StatusEventBase + invoice → Invoice + dimension"
        rows = [
            *design["StatusEventBase"],
            ("invoice", "→ Invoice", ""),
            ("dimension", "Char(10)", ""),
        ]
    else:
        rows = list(design[table])
    if all(name != "recorded_at" for name, _, _ in rows):
        rows.append(("recorded_at", "DateTime", "system clock; not exported"))
    return rows


def _check_type(field, design_type):
    m = re.match(r"Char\((\d+)\)", design_type)
    if m:
        assert isinstance(field, models.CharField), field
        assert field.max_length == int(m.group(1)), (field.name, field.max_length, design_type)
        assert field.primary_key == ("PK" in design_type), field.name
        return
    m = re.match(r"Decimal\((\d+),(\d+)\)", design_type)
    if m:
        assert isinstance(field, models.DecimalField), field
        assert (field.max_digits, field.decimal_places) == (int(m.group(1)), int(m.group(2))), (
            field.name
        )
        return
    if design_type.startswith("→"):
        assert field.is_relation and field.related_model.__name__ == design_type[1:].strip(), (
            field.name
        )
        return
    kinds = {
        "Date": models.DateField,
        "DateTime": models.DateTimeField,
        "Boolean": models.BooleanField,
        "PositiveSmallInt": models.PositiveSmallIntegerField,
    }
    assert isinstance(field, kinds[design_type]), (field.name, design_type)
    if design_type == "Date":
        assert not isinstance(field, models.DateTimeField), field.name


@pytest.mark.parametrize("table", ["FeeSchedule", "Invoice", "InvoiceLine", "InvoiceEvent"])
def test_table_matches_design_exactly(design, table):
    model = apps.get_model("billing", table)
    expected = _expected_fields(design, table)
    actual = _model_fields(model)
    assert set(actual) == {name for name, _, _ in expected}
    for name, design_type, _ in expected:
        _check_type(actual[name], design_type)


def test_feeschedule_has_no_effective_to_column(design):
    assert "effective_to" not in _model_fields(apps.get_model("billing", "FeeSchedule"))


def test_D1_ledger_entry_is_the_design_minus_the_staged_sources(design):
    model = apps.get_model("billing", "LedgerEntry")
    expected = {name: t for name, t, _ in _expected_fields(design, "LedgerEntry")}
    actual = _model_fields(model)
    assert set(actual) | set(STAGED_COLUMNS) == set(expected)
    assert set(actual) & set(STAGED_COLUMNS) == set()
    for name, field in actual.items():
        _check_type(field, expected[name])
    for source in ("invoice_line", "scholarship_allocation"):
        assert actual[source].unique and actual[source].null  # "U", optional source


def test_D1_staged_sources_name_their_design_models(design):
    rows = {name: (t, rest) for name, t, rest in design["LedgerEntry"]}
    assert {k: rows[k][0] for k in STAGED_COLUMNS} == {
        "payment": "→ Payment",
        "refund": "→ Refund",
    }
    assert rows["scholarship_allocation"][0] == "→ ScholarshipAllocation"  # Phase 3: built
    assert all(rows[k][1].rstrip().endswith("U") for k in STAGED_COLUMNS)


def test_D1_entry_types_are_the_design_minus_the_staged_types(design):
    rest = next(r for name, _, r in design["LedgerEntry"] if name == "entry_type")
    designed = set(re.findall(r"`([A-Z_]+)`", rest))
    assert designed == {
        "CHARGE",
        "LATE_FEE",
        "ADJUSTMENT",
        "SCHOLARSHIP_CREDIT",
        "SCHOLARSHIP_REVERSAL",
        "PAYMENT",
        "REFUND",
    }
    model = apps.get_model("billing", "LedgerEntry")
    implemented = {value for value, _ in model.EntryType.choices}
    assert implemented | set(STAGED_ENTRY_TYPES) == designed
    assert implemented & set(STAGED_ENTRY_TYPES) == set()
    assert set(LEDGER_SIGN) == designed  # the frozen posting-table signs cover every type


@pytest.mark.parametrize(
    ("table", "field", "values"),
    [
        ("FeeSchedule", "fee_type", None),
        ("FeeSchedule", "residency", None),
        ("FeeSchedule", "charge_basis", None),
        ("Invoice", "lifecycle_status", None),
        ("Invoice", "collection_status", None),
        ("InvoiceLine", "line_type", None),
        ("InvoiceLine", "waiver_reason", None),
        ("InvoiceLine", "adjustment_reason", None),
    ],
)
def test_enum_values_match_design(design, table, field, values):
    row = next((t, r) for name, t, r in _expected_fields(design, table) if name == field)
    designed = set(re.findall(r"`([A-Z_]+)`", row[1]))
    model_field = _model_fields(apps.get_model("billing", table))[field]
    assert {v for v, _ in model_field.choices} == designed, (table, field)
