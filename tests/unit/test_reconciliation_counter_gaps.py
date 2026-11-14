"""Counter gaps (P6-O15; DATABASE_DESIGN §2, ARCHITECTURE §12.1) on pure rows: each sequence is
a core.ids sequence key, so counters are compared only within the same kind, year and parent;
only holes between 1 and the highest counter present are findings (INVALID_ID_FORMAT, R-28)."""

import random
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from reconciliation.checks import REGISTRY
from reconciliation.checks.structural import GAP_SEQUENCES, PARENT_SCOPED
from reconciliation.dataset import Dataset

AS_OF = date(2026, 12, 31)


def payment(pk, on=date(2026, 8, 25), student="STU-000001"):
    return SimpleNamespace(
        pk=pk, payment_id=pk, student_id=student, received_date=on, amount=D("1.00")
    )


def line(invoice_id, n):
    pk = f"{invoice_id}-L{n:02d}"
    return SimpleNamespace(pk=pk, line_id=pk, invoice_id=invoice_id)


def invoice(pk, student, on=date(2026, 8, 17)):
    return SimpleNamespace(pk=pk, invoice_id=pk, student_id=student, issue_date=on)


def alloc(payment_id, n):
    pk = f"ALLOC-{payment_id}-{n:02d}"
    return SimpleNamespace(pk=pk, allocation_id=pk, payment_id=payment_id)


def gaps(check_id, **tables):
    _, found = REGISTRY[check_id].fn(Dataset(as_of=AS_OF, **tables))
    return [
        (
            f.issue_type,
            f.rule_id,
            f.entity_type,
            f.record_id,
            f.details["sequence"],
            f.details["missing_from"],
            f.details["missing_to"],
            f.details["missing_count"],
        )
        for f in found
    ]


def test_consecutive_counters_have_no_gap():
    pays = [payment(f"PAY-2026-{n:06d}") for n in (1, 2, 3)]
    assert gaps("S-ID-GAPS-YEAR", payments=pays) == []


def test_a_missing_counter_in_the_middle():
    pays = [payment(f"PAY-2026-{n:06d}") for n in (1, 2, 4)]
    assert gaps("S-ID-GAPS-YEAR", payments=pays) == [
        (
            "INVALID_ID_FORMAT",
            "R-28",
            "Payment",
            "PAY-2026-000003",
            "PAY:2026",
            "PAY-2026-000003",
            "PAY-2026-000003",
            1,
        )
    ]


def test_missing_first_counters_and_separate_runs_in_order():
    pays = [payment(f"PAY-2026-{n:06d}") for n in (3, 4, 7, 9)]
    assert [(r[5], r[6], r[7]) for r in gaps("S-ID-GAPS-YEAR", payments=pays)] == [
        ("PAY-2026-000001", "PAY-2026-000002", 2),
        ("PAY-2026-000005", "PAY-2026-000006", 2),
        ("PAY-2026-000008", "PAY-2026-000008", 1),
    ]


def test_only_holes_below_the_highest_counter_are_reported():
    """A counter reserved after the last row (IdSequence ahead of the data) is invisible: the
    detector never reads IdSequence, which is internal and not exported."""
    pays = [payment(f"PAY-2026-{n:06d}") for n in (1, 2)]
    assert gaps("S-ID-GAPS-YEAR", payments=pays) == []


def test_year_boundary_each_year_is_its_own_sequence():
    both_first = [payment("PAY-2025-000001", date(2025, 12, 31)), payment("PAY-2026-000001")]
    assert gaps("S-ID-GAPS-YEAR", payments=both_first) == []
    # 2026 starts at 2: its 000001 is missing; 2025's counters do not fill it
    found = gaps(
        "S-ID-GAPS-YEAR",
        payments=[payment("PAY-2025-000001", date(2025, 12, 31)), payment("PAY-2026-000002")],
    )
    assert [(r[3], r[4]) for r in found] == [("PAY-2026-000001", "PAY:2026")]


def test_lines_are_numbered_per_invoice():
    a, b = "INV-STU-000001-2026-000001", "INV-STU-000001-2026-000002"
    lines = [line(a, 1), line(a, 2), line(b, 1), line(b, 3)]
    invoices = [invoice(a, "STU-000001"), invoice(b, "STU-000001")]
    found = gaps("S-ID-GAPS-PARENT", lines=lines, invoices=invoices)
    assert found == [
        (
            "INVALID_ID_FORMAT",
            "R-28",
            "InvoiceLine",
            f"{b}-L02",
            f"LINE:{b}",
            f"{b}-L02",
            f"{b}-L02",
            1,
        )
    ]


def test_invoices_are_numbered_per_student_and_year():
    ok = [
        invoice("INV-STU-000001-2026-000001", "STU-000001"),
        invoice("INV-STU-000002-2026-000001", "STU-000002"),
        invoice("INV-STU-000001-2025-000001", "STU-000001", date(2025, 8, 17)),
    ]
    assert gaps("S-ID-GAPS-PARENT", invoices=ok) == []
    holes = [*ok, invoice("INV-STU-000002-2026-000003", "STU-000002")]
    found = gaps("S-ID-GAPS-PARENT", invoices=holes)
    assert [(r[3], r[4]) for r in found] == [("INV-STU-000002-2026-000002", "INV:STU-000002:2026")]


def test_allocations_are_numbered_per_payment_and_report_the_student():
    p1, p2 = "PAY-2026-000001", "PAY-2026-000002"
    allocs = [alloc(p1, 1), alloc(p1, 2), alloc(p2, 2)]
    pays = [payment(p1), payment(p2, student="STU-000009")]
    _, found = REGISTRY["S-ID-GAPS-PARENT"].fn(
        Dataset(as_of=AS_OF, payment_allocations=allocs, payments=pays)
    )
    assert [(f.record_id, f.student_id, f.details["next_present_id"]) for f in found] == [
        (f"ALLOC-{p2}-01", "STU-000009", f"ALLOC-{p2}-02")
    ]


def test_status_events_are_numbered_per_entity_and_year():
    def evt(pk, record):
        return SimpleNamespace(
            pk=pk, event_id=pk, event_date=date(2026, 8, 17), invoice_id=record, student_id=record
        )

    inv_events = [evt("EVT-INV-2026-0000001", "x"), evt("EVT-INV-2026-0000002", "x")]
    stu_events = [evt("EVT-STU-2026-0000002", "STU-000001")]  # its own sequence: 1 is missing
    found = gaps("S-ID-GAPS-YEAR", invoice_events=inv_events, student_events=stu_events)
    assert [(r[3], r[4]) for r in found] == [("EVT-STU-2026-0000001", "EVT:STU:2026")]


def test_a_misnumbered_row_is_reported_once_not_again_as_gaps():
    """LED-2025-0000003 dated 2026: S-ID-DOMAIN reports PREFIX_OR_YEAR; it still fills 2026's
    slot 3, and the 2025 sequence holding only it is not checked."""

    def led(pk, on=date(2026, 9, 1)):
        return SimpleNamespace(pk=pk, entry_id=pk, entry_date=on, student_id="STU-000001")

    rows = [
        led("LED-2026-0000001"),
        led("LED-2026-0000002"),
        led("LED-2025-0000003"),
        led("LED-2026-0000004"),
    ]
    assert gaps("S-ID-GAPS-YEAR", ledger=rows) == []
    _, ids_found = REGISTRY["S-ID-DOMAIN"].fn(Dataset(as_of=AS_OF, ledger=rows))
    assert [(f.record_id, f.details["problem"]) for f in ids_found] == [
        ("LED-2025-0000003", "PREFIX_OR_YEAR")
    ]


def test_unparseable_ids_are_left_to_the_format_check():
    pays = [payment("PAY-2026-000001"), payment("PAY-26-9")]
    assert gaps("S-ID-GAPS-YEAR", payments=pays) == []


def test_gap_findings_are_deterministic_whatever_the_row_order():
    pays = [payment(f"PAY-2026-{n:06d}") for n in (2, 5, 9, 12)]
    events = [
        SimpleNamespace(
            pk=f"EVT-REF-2026-{n:07d}", event_date=date(2026, 9, 1), refund_id="REF-2026-000001"
        )
        for n in (1, 4)
    ]
    expected = None
    for seed in range(5):
        random.Random(seed).shuffle(pays)
        random.Random(seed).shuffle(events)
        ds = Dataset(as_of=AS_OF, payments=list(pays), refund_events=list(events))
        got = [(f.record_id, f.details) for f in REGISTRY["S-ID-GAPS-YEAR"].fn(ds)[1]]
        expected = expected or got
        assert got == expected
    # PAY 1, 3–4, 6–8, 10–11 then EVT-REF 2–3: table order, then sequence key, then counter
    assert [r for r, _ in expected] == [
        "PAY-2026-000001",
        "PAY-2026-000003",
        "PAY-2026-000006",
        "PAY-2026-000010",
        "EVT-REF-2026-0000002",
    ]


def test_scopes_are_exactly_core_ids_counter_scopes():
    """Every counter-numbered domain table, nothing with a natural key (DATABASE_DESIGN §2)."""
    assert set(GAP_SEQUENCES) == {
        "students",
        "programs",
        "scholarship_types",
        "enrollments",
        "eligibilities",
        "scholarships",
        "payments",
        "refunds",
        "ledger",
        "snapshots",
        "student_events",
        "enrollment_events",
        "invoice_events",
        "scholarship_events",
        "refund_events",
        "invoices",
        "lines",
        "scholarship_allocations",
        "payment_allocations",
    }
    assert set(PARENT_SCOPED) == {
        "invoices",
        "lines",
        "scholarship_allocations",
        "payment_allocations",
    }
    assert REGISTRY["S-ID-GAPS-YEAR"].scope == "GLOBAL"
    assert REGISTRY["S-ID-GAPS-PARENT"].scope == "PER_STUDENT"


@pytest.mark.parametrize("table", sorted(GAP_SEQUENCES))
def test_every_sequence_finds_a_missing_first_counter(table):
    """For each table: rows numbered 2 only → counter 1 of that sequence is reported."""
    kind, pattern, _ = GAP_SEQUENCES[table]
    samples = {
        "students": "STU-000002",
        "programs": "PRG-002",
        "scholarship_types": "SCT-002",
        "enrollments": "ENR-2026-000002",
        "eligibilities": "ELG-2026-000002",
        "scholarships": "SCH-2026-000002",
        "payments": "PAY-2026-000002",
        "refunds": "REF-2026-000002",
        "ledger": "LED-2026-0000002",
        "snapshots": "SNAP-2026-000002",
        "student_events": "EVT-STU-2026-0000002",
        "enrollment_events": "EVT-ENR-2026-0000002",
        "invoice_events": "EVT-INV-2026-0000002",
        "scholarship_events": "EVT-SCH-2026-0000002",
        "refund_events": "EVT-REF-2026-0000002",
        "invoices": "INV-STU-000001-2026-000002",
        "lines": "INV-STU-000001-2026-000001-L02",
        "scholarship_allocations": "SCHA-SCH-2026-000001-02",
        "payment_allocations": "ALLOC-PAY-2026-000001-02",
    }
    pk = samples[table]
    row = SimpleNamespace(pk=pk)  # no business fields: the ID is taken as numbered
    check = "S-ID-GAPS-PARENT" if table in PARENT_SCOPED else "S-ID-GAPS-YEAR"
    _, found = REGISTRY[check].fn(Dataset(as_of=AS_OF, **{table: [row]}))
    assert [f.details["missing_count"] for f in found] == [1], table
    assert found[0].record_id.endswith(("000001", "001", "0000001", "-L01", "-01"))
