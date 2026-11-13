"""core.ids: formats from DATABASE_DESIGN.md §2 and sequence allocation."""

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from core import ids
from core.ids import IdFormatError, next_id, reserve_sequence
from core.models import IdSequence

pytestmark = pytest.mark.phase0


# --- formatters reproduce every example in DATABASE_DESIGN.md §2 ---------------


@pytest.mark.parametrize(
    ("produced", "expected"),
    [
        (ids.student_id(1), "STU-000001"),
        (ids.program_id(4), "PRG-004"),
        (ids.term_id(2026, "FALL"), "TERM-2026-FALL"),
        (
            ids.fee_schedule_id("PRG-004", "TERM-2026-FALL", "TUITION", "DOMESTIC", 1),
            "FEE-PRG-004-2026-FALL-TUITION-DOM-V1",
        ),
        (ids.enrollment_id(2026, 1), "ENR-2026-000001"),
        (ids.invoice_id("STU-000001", 2026, 1), "INV-STU-000001-2026-000001"),
        (ids.invoice_line_id("INV-STU-000001-2026-000001", 3), "INV-STU-000001-2026-000001-L03"),
        (ids.scholarship_type_id(7), "SCT-007"),
        (ids.scholarship_type_program_id("SCT-004", "PRG-004"), "SCT-004-PRG-004"),
        (ids.eligibility_id(2026, 1), "ELG-2026-000001"),
        (ids.scholarship_id(2026, 1), "SCH-2026-000001"),
        (ids.scholarship_allocation_id("SCH-2026-000001", 1), "SCHA-SCH-2026-000001-01"),
        (ids.payment_id(2026, 1), "PAY-2026-000001"),
        (ids.payment_allocation_id("PAY-2026-000001", 1), "ALLOC-PAY-2026-000001-01"),
        (ids.refund_id(2026, 1), "REF-2026-000001"),
        (ids.ledger_entry_id(2026, 1), "LED-2026-0000001"),
        (ids.status_event_id("INV", 2026, 1), "EVT-INV-2026-0000001"),
        (ids.balance_snapshot_id(2026, 1), "SNAP-2026-000001"),
        (ids.reconciliation_run_id(2026, 1), "RECON-2026-000001"),
        (ids.anomaly_finding_id(2026, 1), "FND-2026-000001"),
        (ids.generation_run_id(2026, 1), "GEN-2026-0001"),
        (ids.ground_truth_label_id(2026, 1), "GT-2026-000001"),
    ],
)
def test_formats_match_design_examples(produced, expected):
    assert produced == expected


def test_brief_id_conventions():
    # the six conventions quoted in the YottaFlex brief
    assert ids.student_id(1) == "STU-000001"
    assert ids.term_id(2026, "FALL") == "TERM-2026-FALL"
    assert ids.invoice_id(ids.student_id(1), 2026, 1) == "INV-STU-000001-2026-000001"
    assert ids.scholarship_id(2026, 1) == "SCH-2026-000001"
    assert ids.payment_id(2026, 1) == "PAY-2026-000001"
    assert ids.payment_allocation_id(ids.payment_id(2026, 1), 1) == "ALLOC-PAY-2026-000001-01"


def test_every_kind_has_pattern_and_length_and_examples_fit():
    assert set(ids.ID_PATTERNS) == set(ids.ID_MAX_LENGTHS)
    longest_fee = ids.fee_schedule_id(
        "PRG-020", "TERM-2026-SUMMER", "INTERNATIONAL_SERVICE", "INTERNATIONAL", 99
    )
    assert len(longest_fee) <= ids.ID_MAX_LENGTHS["fee_schedule"]
    assert ids.is_valid_id("fee_schedule", longest_fee)


@pytest.mark.parametrize(
    ("kind", "value", "valid"),
    [
        ("student", "STU-000001", True),
        ("student", "STU-00001", False),
        ("student", "STU-000001\n", False),
        ("student", "stu-000001", False),
        ("term", "TERM-2026-WINTER", False),
        ("invoice", "INV-STU-STU-000001-2026-000001", False),
        ("payment_allocation", "ALLOC-PAY-2026-000001-1", False),
        ("ledger_entry", "LED-2026-000001", False),
        ("fee_schedule", "FEE-PRG-004-2026-FALL-PARKING-DOM-V1", False),
    ],
)
def test_validation(kind, value, valid):
    assert ids.is_valid_id(kind, value) is valid


def test_validator_for_models():
    v = ids.id_validator("payment")
    v("PAY-2026-000001")
    with pytest.raises(ValidationError):
        v("PAY-2026-1")


@pytest.mark.parametrize(
    "call",
    [
        lambda: ids.student_id(0),
        lambda: ids.student_id(1_000_000),
        lambda: ids.student_id(True),
        lambda: ids.student_id("1"),
        lambda: ids.payment_allocation_id("PAY-2026-000001", 100),
        lambda: ids.term_id(2026, "WINTER"),
        lambda: ids.payment_id(1999, 1),
        lambda: ids.invoice_id("STU-1", 2026, 1),
        lambda: ids.fee_schedule_id("PRG-004", "TERM-2026-FALL", "PARKING", "DOMESTIC", 1),
        lambda: ids.status_event_id("XYZ", 2026, 1),
    ],
)
def test_out_of_range_or_malformed_components_raise(call):
    with pytest.raises(IdFormatError):
        call()


# --- sequences ---------------------------------------------------------------------


@pytest.mark.django_db
def test_next_id_is_sequential_per_scope():
    assert next_id("payment", year=2026) == "PAY-2026-000001"
    assert next_id("payment", year=2026) == "PAY-2026-000002"
    assert next_id("payment", year=2027) == "PAY-2027-000001"
    assert next_id("invoice", parent="STU-000001", year=2026) == "INV-STU-000001-2026-000001"
    assert next_id("invoice", parent="STU-000002", year=2026) == "INV-STU-000002-2026-000001"
    assert next_id("payment_allocation", parent="PAY-2026-000001") == "ALLOC-PAY-2026-000001-01"
    assert next_id("payment_allocation", parent="PAY-2026-000001") == "ALLOC-PAY-2026-000001-02"
    assert next_id("status_event", entity="INV", year=2026) == "EVT-INV-2026-0000001"
    assert IdSequence.objects.get(key="PAY:2026").last_value == 2


@pytest.mark.django_db
def test_reserve_block_for_bulk_generation():
    assert reserve_sequence("LED:2026", count=1000) == 1
    assert reserve_sequence("LED:2026", count=10) == 1001
    assert IdSequence.objects.get(key="LED:2026").last_value == 1010


@pytest.mark.django_db
def test_rollback_releases_the_counter():
    next_id("refund", year=2026)
    with pytest.raises(RuntimeError), transaction.atomic():
        assert next_id("refund", year=2026) == "REF-2026-000002"
        raise RuntimeError("service failed after allocating an ID")
    assert next_id("refund", year=2026) == "REF-2026-000002"


@pytest.mark.django_db
def test_overflow_does_not_consume_a_counter():
    IdSequence.objects.create(key="ALLOC:PAY-2026-000009", last_value=99)
    with pytest.raises(IdFormatError):
        next_id("payment_allocation", parent="PAY-2026-000009")
    assert IdSequence.objects.get(key="ALLOC:PAY-2026-000009").last_value == 99


@pytest.mark.django_db
def test_missing_scope_and_unknown_kind():
    with pytest.raises(IdFormatError):
        next_id("payment")  # year missing
    with pytest.raises(IdFormatError):
        next_id("term", year=2026)  # natural key, not sequence-allocated
    with pytest.raises(ValueError):
        reserve_sequence("PAY:2026", count=0)
