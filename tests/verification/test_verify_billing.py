"""V-BILL: Phase 2 verification beyond the per-endpoint tests.

* the billing migration only adds billing tables and leaves existing Phase 1 rows unchanged
* two concurrent generations for one enrollment produce one invoice and one 409 (R3/R5)
* the same scenario produces byte-identical billing rows (IDs included) every time
* a mixed scenario satisfies every billing invariant that reconciliation will re-check
"""

import io
import re
import threading
from datetime import date
from decimal import Decimal as D

import pytest
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor

from billing import calculators as calc
from billing import services
from billing.lifecycle import LIFECYCLE_TRANSITIONS
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.exceptions import StateConflict
from core.ids import is_valid_id, student_number
from tests.billing_fixtures import make_fees

BILLING_TABLES = {
    "billing_feeschedule",
    "billing_invoice",
    "billing_invoiceline",
    "billing_invoiceevent",
    "billing_ledgerentry",
}


# --- migration safety -------------------------------------------------------------------------


def _phase1_rows():
    """Every row of every non-billing table (sorted), excluding the migration ledger."""
    out = {}
    with connection.cursor() as cur:
        for table in sorted(connection.introspection.table_names()):
            if table.startswith("billing_") or table == "django_migrations":
                continue
            cur.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - table names from introspection
            out[table] = sorted(map(repr, cur.fetchall()))
    return out


@pytest.mark.django_db(transaction=True)  # the SQLite schema editor needs no open transaction
def test_migration_sql_only_creates_billing_tables_and_indexes():
    buf = io.StringIO()
    call_command("sqlmigrate", "billing", "0001_initial", stdout=buf)
    sql_text = "\n".join(
        ln for ln in buf.getvalue().splitlines() if not ln.lstrip().startswith("--")
    )
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    statements = [s for s in statements if s not in ("BEGIN", "COMMIT")]
    assert len([s for s in statements if s.startswith("CREATE TABLE")]) == 5
    for sql in statements:
        assert re.match(r'CREATE TABLE "billing_\w+"', sql) or re.match(
            r'CREATE (UNIQUE )?INDEX "\w+" ON "billing_\w+"', sql
        ), sql[:120]
    for verb in ("ALTER", "DROP", "INSERT", "UPDATE", "DELETE"):
        assert not any(s.upper().startswith(verb) for s in statements), verb


@pytest.mark.django_db(transaction=True)
def test_migration_adds_billing_tables_without_touching_phase1_rows(student, enroll_fall):
    enroll_fall(
        student
    )  # a populated Phase 1 database: terms, program, student, enrollment, events
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("billing", None)])  # back to the verified Phase 1 schema
        tables = set(connection.introspection.table_names())
        assert not tables & BILLING_TABLES
        before = _phase1_rows()
        assert before["students_student"] and before["academics_enrollment"]

        executor = MigrationExecutor(connection)
        executor.migrate([("billing", "0001_initial")])  # the Phase 2 migration
        tables = set(connection.introspection.table_names())
        assert BILLING_TABLES <= tables
        assert _phase1_rows() == before  # existing records unchanged
        assert all(m.objects.count() == 0 for m in (FeeSchedule, Invoice, InvoiceLine, LedgerEntry))
    finally:
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )


@pytest.mark.django_db(databases="__all__")
def test_ground_truth_migration_records_billing_but_creates_no_table():
    assert not {
        t
        for t in connections["ground_truth"].introspection.table_names()
        if t.startswith("billing_")
    }
    executor = MigrationExecutor(connections["ground_truth"])
    assert ("billing", "0001_initial") in executor.loader.applied_migrations


# --- concurrency (R3, R5) ------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_generations_give_one_invoice_and_one_409(fee_setup, fall_enrollment):
    barrier, results = threading.Barrier(2), []

    def worker():
        try:
            barrier.wait()
            services.generate_invoice(enrollment=fall_enrollment, actor="thread")
            results.append("created")
        except StateConflict as exc:
            results.append(exc.code)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == ["DUPLICATE_INVOICE", "created"]
    assert Invoice.objects.filter(enrollment=fall_enrollment).count() == 1
    inv = Invoice.objects.get()
    assert inv.lines.count() == LedgerEntry.objects.count() == 6


# --- determinism ----------------------------------------------------------------------------------


def _scenario(make_student, program, terms, enroll_fall):
    term = terms["TERM-2026-FALL"]
    fees = make_fees(program, term)
    services.new_fee_schedule_version(
        fees[("TUITION", "DOMESTIC")], amount=D("9360.00"), effective_from=date(2026, 8, 1)
    )
    a = make_student()
    b = make_student(residency="INTERNATIONAL")
    c = make_student()
    inv_a = services.generate_invoice(
        enrollment=enroll_fall(a),
        waivers=[{"fee_type": "LIBRARY", "amount": D("2000.00"), "reason": "STAFF_DEPENDENT"}],
        actor="s",
    )
    services.generate_invoice(enrollment=enroll_fall(b, credits=12), actor="s")
    inv_c = services.generate_invoice(
        enrollment=enroll_fall(c, enrolled_on=date(2026, 9, 2)), actor="s"
    )
    services.void_invoice(inv_c, void_date=date(2026, 9, 3), actor="s")
    services.generate_invoice(enrollment=inv_c.enrollment, actor="s")
    services.refresh_collection_status(as_of=date(2026, 9, 15), actor="s")
    services.assess_late_fees(as_of=date(2026, 12, 31), dry_run=False, actor="s")
    services.refresh_collection_status(as_of=date(2026, 12, 31), actor="s")
    return inv_a


def _dump():
    def rows(model):
        fields = [f.attname for f in model._meta.concrete_fields if f.name != "recorded_at"]
        return [tuple(str(getattr(o, f)) for f in fields) for o in model.objects.order_by("pk")]

    return {
        m.__name__: rows(m) for m in (FeeSchedule, Invoice, InvoiceLine, InvoiceEvent, LedgerEntry)
    }


@pytest.mark.django_db
def test_same_scenario_gives_identical_rows(make_student, program, terms, enroll_fall):
    dumps = []
    for _ in range(2):
        with transaction.atomic():
            _scenario(make_student, program, terms, enroll_fall)
            dumps.append(_dump())
            transaction.set_rollback(True)  # also rolls the ID sequences back
    assert dumps[0] == dumps[1]
    assert len(dumps[0]["InvoiceLine"]) > 20 and len(dumps[0]["InvoiceEvent"]) > 10


# --- invariants (what reconciliation will re-check, all in Decimal) -------------------------------


@pytest.mark.django_db
def test_every_billing_invariant_holds_after_a_mixed_scenario(
    make_student, program, terms, enroll_fall
):
    _scenario(make_student, program, terms, enroll_fall)
    lines = list(InvoiceLine.objects.select_related("invoice"))
    entries = {e.invoice_line_id: e for e in LedgerEntry.objects.all()}

    # BR-LED-01: exactly one entry per line, signed by type, dated by the line
    assert len(entries) == len(lines) == LedgerEntry.objects.count()
    for line in lines:
        e = entries[line.line_id]
        entry_type = services.ENTRY_TYPE_FOR_LINE[line.line_type]
        assert e.entry_type == entry_type
        assert e.amount == calc.LEDGER_SIGN[entry_type] * abs(line.net_amount)
        assert (e.entry_date, e.student_id) == (line.posted_date, line.invoice.student_id)
        assert is_valid_id("ledger_entry", e.entry_id) and is_valid_id("invoice_line", line.line_id)
        assert line.posted_date >= line.invoice.issue_date

    for line in lines:  # BR-FEE-02, BR-WAV-01 arithmetic in Decimal
        if line.line_type in ("FEE", "LATE_FEE"):
            assert line.gross_amount == (line.quantity * line.unit_amount).quantize(D("0.01"))
            assert line.net_amount == line.gross_amount - line.waiver_amount
            assert 0 <= line.waiver_amount <= line.gross_amount
        if line.line_type == "FEE":
            fs = line.fee_schedule
            assert (line.fee_type, line.allocation_priority) == (
                fs.fee_type,
                fs.allocation_priority,
            )
            assert (line.is_scholarship_eligible, line.is_refundable, line.is_waivable) == (
                fs.is_scholarship_eligible,
                fs.is_refundable,
                fs.is_waivable,
            )

    for inv in Invoice.objects.prefetch_related("lines", "events"):
        inv_lines = list(inv.lines.all())
        fee = [x for x in inv_lines if x.line_type == "FEE"]
        assert inv.total_amount == calc.dsum(x.net_amount for x in fee)  # BR-INV-02
        assert inv.invoice_id[8:14] == student_number(inv.student_id)
        assert all(v >= 0 for v in calc.line_open_amounts(inv_lines).values())  # BR-ALC-02
        for target in fee:
            adj = calc.dsum(x.net_amount for x in inv_lines if x.adjusts_line_id == target.line_id)
            assert adj >= -target.net_amount

        events = sorted(inv.events.all(), key=lambda e: (e.event_date, e.event_id))
        assert [e.event_date for e in events] == sorted(e.event_date for e in events)
        life = [e for e in events if e.dimension == "LIFECYCLE"]
        coll = [e for e in events if e.dimension == "COLLECTION"]
        assert life[0].from_status is None and coll[0].from_status is None
        assert coll[0].to_status == "UNPAID"
        for prev, e in zip([None, *life], life, strict=False):
            assert e.to_status in LIFECYCLE_TRANSITIONS[prev.to_status if prev else None]
            assert e.from_status == (prev.to_status if prev else None)
        for prev, e in zip(coll, coll[1:], strict=False):
            assert e.from_status == prev.to_status != e.to_status
        assert inv.lifecycle_status == life[-1].to_status
        assert inv.collection_status == coll[-1].to_status
        derived = calc.collection_status(
            services.current_amounts(inv, inv_lines),
            due_date=inv.due_date,
            as_of=inv.collection_status_as_of,
        )
        assert inv.collection_status == derived  # what INVOICE_STATUS_MISMATCH will check
        if inv.lifecycle_status == "VOID":
            assert inv.collection_status == "ADJUSTED_TO_ZERO"

    # BR-BAL-01/02 for every student, and the whole ledger equals what is outstanding
    from students.models import Student

    total_outstanding = D("0")
    for s in Student.objects.all():
        b = services.student_balance(s, None)
        assert b["identity"]["holds"] is True
        total_outstanding += b["invoice_outstanding"]
    assert calc.ledger_balance(LedgerEntry.objects.all(), None) == total_outstanding

    # the scenario really exercised waivers, a second fee version, void, late fees, overdue
    assert InvoiceLine.objects.filter(waiver_amount__gt=0).exists()
    assert InvoiceLine.objects.filter(fee_schedule__version=2).exists()
    assert Invoice.objects.filter(lifecycle_status="VOID").count() == 1
    assert InvoiceLine.objects.filter(line_type="LATE_FEE").count() == 3
    assert set(Invoice.objects.values_list("collection_status", flat=True)) == {
        "OVERDUE",
        "ADJUSTED_TO_ZERO",
    }
