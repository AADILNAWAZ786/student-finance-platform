"""DATA_DICTIONARY.md documents the implementation (Phase 9, P9-D6; R-30): every concrete field
of the eleven challenge entities appears exactly once, with the database type, nullability,
default, enumeration values, key and uniqueness the models define; the six ID conventions'
examples match the primary-key validators."""

import re
from pathlib import Path

import pytest
from django.apps import apps
from django.core.validators import RegexValidator
from django.db import connection, models

ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
ENTITIES = [
    "students.Student",
    "academics.Program",
    "billing.FeeSchedule",
    "academics.Enrollment",
    "billing.Invoice",
    "billing.InvoiceLine",
    "scholarships.Scholarship",
    "scholarships.Eligibility",
    "payments.Payment",
    "payments.PaymentAllocation",
    "payments.Refund",
]
COLUMNS = [
    "Field",
    "Meaning",
    "DB type",
    "Required",
    "Default",
    "Allowed values / units",
    "Key / uniqueness",
    "Validation",
    "Stored / derived",
]


def _table(model) -> dict[str, dict[str, str]]:
    """The field table under '### <Model> (`<db_table>`)'."""
    heading = f"### {model.__name__} (`{model._meta.db_table}`)"
    assert DOC.count(heading) == 1, heading
    section = DOC.split(heading, 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.startswith("|")]
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    assert header == COLUMNS, model.__name__
    rows = {}
    for ln in lines[2:]:
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip().strip("|"))]
        assert len(cells) == len(COLUMNS), ln
        name = cells[0].strip("`")
        assert name not in rows, f"{model.__name__}.{name} listed twice"
        rows[name] = dict(zip(COLUMNS, cells, strict=True))
    return rows


@pytest.mark.parametrize("label", ENTITIES)
def test_every_field_is_documented_as_implemented(label):
    model = apps.get_model(label)
    rows = _table(model)
    fields = {f.attname: f for f in model._meta.concrete_fields}
    assert set(rows) == set(fields), f"{label}: {set(rows) ^ set(fields)}"
    for name, f in fields.items():
        row, where = rows[name], f"{label}.{name}"
        db = f.db_type(connection)
        assert db in row["DB type"], (where, db, row["DB type"])
        if isinstance(f, models.DecimalField):
            assert f"({f.max_digits},{f.decimal_places})" in row["DB type"], where
        assert row["Required"] == ("no" if f.null else "yes"), where
        if f.has_default():
            default = f.default() if callable(f.default) else f.default
            assert str(default) in row["Default"], (where, default, row["Default"])
        elif getattr(f, "auto_now_add", False):
            assert row["Default"] == "set at insert", where
        else:
            assert row["Default"] == "—", where
        for value, _ in f.choices or []:
            assert f"`{value}`" in row["Allowed values / units"], (where, value)
        key = row["Key / uniqueness"]
        if f.primary_key:
            assert key.startswith("PK"), where
        if f.is_relation:
            assert f"FK → {f.related_model.__name__} (PROTECT)" in key, (where, key)
            assert f.remote_field.on_delete is models.PROTECT, where
        if f.unique and not f.primary_key:
            assert "unique" in key, where
        if name == "recorded_at":
            assert "not exported" in row["Validation"], where


def test_the_six_challenge_id_examples_match_the_validators():
    section = DOC.split("## 3. ID conventions", 1)[1].split("## 4.", 1)[0]
    examples = {
        "students.Student": "STU-000001",
        "academics.Term": "TERM-2026-FALL",
        "billing.Invoice": "INV-STU-000001-2026-000001",
        "scholarships.Scholarship": "SCH-2026-000001",
        "payments.Payment": "PAY-2026-000001",
        "payments.PaymentAllocation": "ALLOC-PAY-2026-000001-01",
    }
    for label, example in examples.items():
        assert f"`{example}`" in section, example
        pk = apps.get_model(label)._meta.pk
        regexes = [v for v in pk.validators if isinstance(v, RegexValidator)]
        if regexes:
            assert all(v.regex.match(example) for v in regexes), (label, example)
        else:  # Term: a natural key built by core.ids.term_id
            from core.ids import term_id

            assert term_id(2026, "FALL") == example


def test_every_required_section_is_present():
    for heading in (
        "## 6. Relationships and cardinality",
        "## 7. Effective dates",
        "## 8. Lifecycle states and event history",
        "## 10. Ground truth kept apart from the input features",
    ):
        assert heading in DOC
    for bridge in ("ScholarshipTypeProgram", "ScholarshipAllocation", "PaymentAllocation"):
        assert f"bridge `{bridge}`" in DOC or f"**Bridge** {bridge}" in DOC or bridge in DOC
