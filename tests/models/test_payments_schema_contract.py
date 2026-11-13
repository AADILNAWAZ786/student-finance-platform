"""The four payments models match DATABASE_DESIGN.md §3 (payments), read from the frozen
document: columns, lengths, decimal precision, FK targets and enums.

One documented exception (decision D-P1, KNOWN_LIMITATIONS P4-O1): the design gives
``Payment.method`` as Char(14) but lists the value ``SPONSOR_TRANSFER`` (16 characters), so
the column is Char(16). Any other difference fails.
"""

import re

import pytest
from django.apps import apps

from tests.models.test_billing_schema_contract import DESIGN, _model_fields, _section, _tables
from tests.models.test_scholarship_schema_contract import EXCEPTIONS as P3_EXCEPTIONS
from tests.models.test_scholarship_schema_contract import _check as p3_check

EXCEPTIONS = {("Payment", "method"): ("Char(14)", "Char(16)")}
TABLES = ["Payment", "PaymentAllocation", "Refund"]


@pytest.fixture(scope="module")
def design():
    text = DESIGN.read_text(encoding="utf-8")
    tables = _tables(_section(text, "### payments", "### reconciliation"))
    core = _tables(_section(text, "### core", "### students"))
    return {**tables, "StatusEventBase": core["StatusEventBase"]}


def _rows(design, table):
    if table == "RefundEvent":  # "StatusEventBase + refund → Refund"
        rows = [*design["StatusEventBase"], ("refund", "→ Refund", "")]
    else:
        rows = list(design[table])
    if all(name != "recorded_at" for name, _, _ in rows):  # on every table (Conventions)
        rows.append(("recorded_at", "DateTime", ""))
    return rows


def _check(table, field, design_type):
    expected = EXCEPTIONS.get((table, field.name), (design_type, design_type))
    assert expected[0] == design_type, f"exception for {table}.{field.name} is stale"
    # reuse the Phase 3 type checker with this phase's exception applied
    assert not P3_EXCEPTIONS.get((table, field.name))
    p3_check(table, field, expected[1])


def test_the_design_section_has_the_four_tables(design):
    assert set(TABLES) <= set(design)
    text = DESIGN.read_text(encoding="utf-8")
    assert "**RefundEvent**: StatusEventBase + `refund` → Refund." in text


@pytest.mark.parametrize("table", [*TABLES, "RefundEvent"])
def test_table_matches_design(design, table):
    model = apps.get_model("payments", table)
    rows = _rows(design, table)
    actual = _model_fields(model)
    assert set(actual) == {name for name, _, _ in rows}, table
    for name, design_type, _ in rows:
        _check(table, actual[name], design_type)


def test_the_only_exception_is_needed_and_documented(design):
    row = next(r for r in design["Payment"] if r[0] == "method")
    longest = max(re.findall(r"`([A-Z_]+)`", row[2]), key=len)
    assert (row[1], longest, len(longest)) == ("Char(14)", "SPONSOR_TRANSFER", 16)
    field = _model_fields(apps.get_model("payments", "Payment"))["method"]
    assert field.max_length == 16  # the minimum that stores every documented value


@pytest.mark.parametrize(
    ("table", "field"),
    [
        ("Payment", "method"),
        ("Payment", "payer_type"),
        ("Payment", "status"),
        ("PaymentAllocation", "allocation_type"),
        ("PaymentAllocation", "rule_applied"),
        ("Refund", "reason"),
        ("Refund", "status"),
    ],
)
def test_enums_match_design(design, table, field):
    rest = next(r for name, _, r in design[table] if name == field)
    designed = set(re.findall(r"`([A-Z_]+)`", rest))
    model_field = _model_fields(apps.get_model("payments", table))[field]
    assert {v for v, _ in model_field.choices} == designed


def test_refund_payer_type_uses_the_payment_payer_types(design):
    """The design says only "copied from the source payment" (BR-REF-04)."""
    payer = _model_fields(apps.get_model("payments", "Payment"))["payer_type"]
    refund = _model_fields(apps.get_model("payments", "Refund"))["payer_type"]
    assert refund.choices == payer.choices and refund.max_length == payer.max_length == 10


def test_unique_rules_match_design():
    payment = _model_fields(apps.get_model("payments", "Payment"))
    assert payment["external_reference"].unique  # "U (idempotency)"
    for name in ("payment_id",):
        assert payment[name].primary_key
    ledger = _model_fields(apps.get_model("billing", "LedgerEntry"))
    assert ledger["payment"].unique and ledger["refund"].unique  # one entry per source
