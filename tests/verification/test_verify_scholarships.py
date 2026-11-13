"""V-SCH: Phase 3 verification beyond the per-endpoint tests.

* the migrations are additive: scholarships 0001 only creates its tables; billing 0002 only
  extends billing_ledgerentry, keeping every existing Phase 1/2 row; migrating back works
* two concurrent awards in one exclusive group give one award and one 409
* the same scenario gives identical rows (IDs included) every time
* a mixed scenario satisfies every scholarship invariant reconciliation will re-check
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

from billing import calculators as billing_calc
from billing import services as billing
from billing.models import Invoice, InvoiceLine, LedgerEntry
from core.exceptions import StateConflict
from core.ids import is_valid_id
from scholarships import calculators as calc
from scholarships import services
from scholarships.lifecycle import SCHOLARSHIP_TRANSITIONS
from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
)

SCHOLARSHIP_TABLES = {
    "scholarships_scholarshiptype",
    "scholarships_scholarshiptypeprogram",
    "scholarships_eligibility",
    "scholarships_scholarship",
    "scholarships_scholarshipallocation",
    "scholarships_scholarshipevent",
}


def _sql(app, name):
    buf = io.StringIO()
    call_command("sqlmigrate", app, name, stdout=buf)
    text = "\n".join(ln for ln in buf.getvalue().splitlines() if not ln.lstrip().startswith("--"))
    return [
        s.strip() for s in text.split(";") if s.strip() and s.strip() not in ("BEGIN", "COMMIT")
    ]


@pytest.mark.django_db(transaction=True)
def test_scholarships_migration_only_creates_its_tables():
    statements = _sql("scholarships", "0001_initial")
    assert len([s for s in statements if s.startswith("CREATE TABLE")]) == 6
    for sql in statements:
        assert re.match(r'CREATE TABLE "scholarships_\w+"', sql) or re.match(
            r'CREATE (UNIQUE )?INDEX "\w+" ON "scholarships_\w+"', sql
        ), sql[:120]


@pytest.mark.django_db(transaction=True)
def test_billing_0002_only_touches_the_ledger_table():
    statements = _sql("billing", "0002_ledger_scholarship_sources")
    target = re.compile(
        r"^(?:CREATE TABLE|INSERT INTO|DROP TABLE|ALTER TABLE"
        r'|CREATE (?:UNIQUE )?INDEX "\w+" ON) "(\w+)"'
    )
    for sql in statements:
        m = target.match(sql)
        assert m, sql[:120]
        assert m.group(1) in {"billing_ledgerentry", "new__billing_ledgerentry"}, sql[:160]
    assert not any(s.upper().startswith(("UPDATE", "DELETE")) for s in statements)
    # SQLite rebuilds the table by copying every row (INSERT … SELECT), never by deleting data
    inserts = [s for s in statements if s.upper().startswith("INSERT")]
    assert inserts and all("SELECT" in s and 'FROM "billing_ledgerentry"' in s for s in inserts)


NEW_COLUMNS = {"billing_ledgerentry": {"scholarship_allocation_id"}}  # added by billing 0002


def _rows():
    """Every Phase 1/2 row of every non-scholarship table, over the Phase 2 columns only."""
    out = {}
    with connection.cursor() as cur:
        for table in sorted(connection.introspection.table_names()):
            if table.startswith("scholarships_") or table == "django_migrations":
                continue
            cols = [
                c.name
                for c in connection.introspection.get_table_description(cur, table)
                if c.name not in NEW_COLUMNS.get(table, set())
            ]
            names = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'SELECT {names} FROM "{table}"')  # noqa: S608 - names from introspection
            out[table] = sorted(map(repr, cur.fetchall()))
    return out


@pytest.mark.django_db(transaction=True)
def test_migrations_keep_every_phase2_row_and_migrate_back(invoice):
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")  # more ledger rows
    before = _rows()
    assert len(before["billing_ledgerentry"]) == 7 and before["billing_invoiceline"]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("billing", "0001_initial"), ("scholarships", None)])  # back to Phase 2
        tables = set(connection.introspection.table_names())
        assert not tables & SCHOLARSHIP_TABLES
        cols = [
            c.name
            for c in connection.introspection.get_table_description(
                connection.cursor(), "billing_ledgerentry"
            )
        ]
        assert "scholarship_allocation_id" not in cols
        assert _rows() == before  # migrating back keeps every row

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())  # forward again: Phase 3
        assert SCHOLARSHIP_TABLES <= set(connection.introspection.table_names())
        assert _rows() == before  # every Phase 1/2 row unchanged, byte for byte
        e = LedgerEntry.objects.order_by("entry_id").first()
        assert (e.scholarship_allocation_id, e.entry_type) == (None, "CHARGE")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(databases="__all__")
def test_ground_truth_records_but_creates_no_scholarship_table():
    assert not [
        t
        for t in connections["ground_truth"].introspection.table_names()
        if t.startswith("scholarships_")
    ]
    applied = MigrationExecutor(connections["ground_truth"]).loader.applied_migrations
    assert ("scholarships", "0001_initial") in applied
    assert ("billing", "0002_ledger_scholarship_sources") in applied


# --- concurrency (exclusive group race) -----------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_awards_in_one_group_give_one_award_and_one_409(invoice, fall):
    for n in (1, 2):
        services.create_scholarship_type(
            name=f"Group award {n}",
            category="MERIT",
            exclusive_group="RACE",
            min_gpa=D("3.00"),
            residency="ALL",
            first_term_only=False,
            program_restricted=False,
            coverage_scope="ELIGIBLE_LINES_ONLY",
            award_basis="FIXED",
            award_value=D("1000.00"),
            max_amount_per_term=D("1000.00"),
            priority=n,
            effective_from=date(2026, 1, 1),
        )
    rows = services.evaluate_eligibility(student=invoice.student, term=fall)
    barrier, results = threading.Barrier(2), []

    def worker(elig):
        try:
            barrier.wait()
            services.award_scholarship(eligibility=elig, invoice=invoice, actor="thread")
            results.append("awarded")
        except StateConflict as exc:
            results.append(exc.code)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(e,)) for e in rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == ["SCHOLARSHIP_GROUP_CONFLICT", "awarded"]
    assert Scholarship.objects.count() == 1 and ScholarshipEvent.objects.count() == 1


# --- determinism and invariants -------------------------------------------------------------------


def _scenario(make_student, student_with_invoice, fall):
    call_command("seed_scholarship_types", verbosity=0)
    out = []
    # 1. merit + STEM, both applied, on a standard invoice
    s1, inv1 = student_with_invoice()
    r1 = {e.scholarship_type_id: e for e in services.evaluate_eligibility(student=s1, term=fall)}
    for t in ("SCT-001", "SCT-004"):
        services.apply_scholarship(
            services.award_scholarship(eligibility=r1[t], invoice=inv1, actor="s"), actor="s"
        )
    # 2. full-ride, applied: SCHOLARSHIP_COVERED
    s2, inv2 = student_with_invoice(gpa="3.90", need_index="0.90")
    r2 = {e.scholarship_type_id: e for e in services.evaluate_eligibility(student=s2, term=fall)}
    services.apply_scholarship(
        services.award_scholarship(eligibility=r2["SCT-007"], invoice=inv2, actor="s"), actor="s"
    )
    # 3. need grant in two awards (per-term cap), one applied, then partly reversed
    s3, inv3 = student_with_invoice(gpa="3.40", need_index="0.65")
    a3 = services.award_scholarship(
        eligibility=services.evaluate_eligibility(student=s3, term=fall)[3],
        invoice=inv3,
        requested_amount=D("15000.00"),
        actor="s",
    )
    services.apply_scholarship(a3, actor="s")
    b3 = services.award_scholarship(
        eligibility=services.evaluate_eligibility(student=s3, term=fall)[3], invoice=inv3, actor="s"
    )
    services.reverse_allocation(
        a3.allocations.get(),
        amount=D("5000.00"),
        reason="WITHDRAWAL",
        on_date=date(2026, 9, 17),
        actor="s",
    )
    out += [inv1, inv2, inv3, b3]
    # 4. an ineligible student and a refresh after the due date
    student_with_invoice(gpa="2.10", need_index="0.10")
    billing.refresh_collection_status(as_of=date(2026, 12, 31), actor="s")
    return out


def _dump():
    models = (
        ScholarshipType,
        Eligibility,
        Scholarship,
        ScholarshipAllocation,
        ScholarshipEvent,
        LedgerEntry,
        Invoice,
    )

    def rows(model):
        fields = [f.attname for f in model._meta.concrete_fields if f.name != "recorded_at"]
        return [tuple(str(getattr(o, f)) for f in fields) for o in model.objects.order_by("pk")]

    return {m.__name__: rows(m) for m in models}


@pytest.mark.django_db
def test_same_scenario_gives_identical_rows(make_student, student_with_invoice, fall, program):
    dumps = []
    for _ in range(2):
        with transaction.atomic():
            _scenario(make_student, student_with_invoice, fall)
            dumps.append(_dump())
            transaction.set_rollback(True)
    assert dumps[0] == dumps[1]
    assert len(dumps[0]["ScholarshipAllocation"]) == 10
    assert len(dumps[0]["Eligibility"]) == 7 * 4  # 4 evaluations of the 7 types


@pytest.mark.django_db
def test_every_scholarship_invariant_holds(make_student, student_with_invoice, fall, program):
    inv1, inv2, inv3, unapplied = _scenario(make_student, student_with_invoice, fall)
    allocations = list(
        ScholarshipAllocation.objects.select_related(
            "scholarship__scholarship_type", "invoice_line"
        )
    )
    entries = {
        e.scholarship_allocation_id: e
        for e in LedgerEntry.objects.filter(scholarship_allocation__isnull=False)
    }

    # BR-LED-01: one entry per allocation, signed by type, dated allocated_on
    assert len(entries) == len(allocations)
    for a in allocations:
        e = entries[a.pk]
        assert (
            e.entry_type
            == {"APPLY": "SCHOLARSHIP_CREDIT", "REVERSE": "SCHOLARSHIP_REVERSAL"}[a.allocation_type]
        )
        assert e.amount == -a.amount and e.entry_date == a.allocated_on
        assert e.student_id == a.scholarship.student_id and is_valid_id(
            "scholarship_allocation", a.pk
        )
        # allocations target only coverable FEE lines of the award's own invoice
        assert a.invoice_line.invoice_id == a.scholarship.invoice_id
        covered = {
            x.line_id
            for x in calc.coverable_lines(
                a.scholarship.scholarship_type, a.scholarship.invoice.lines.all()
            )
        }
        assert a.invoice_line_id in covered
        if a.allocation_type == "REVERSE":
            assert a.reverses.allocation_type == "APPLY"
    assert LedgerEntry.objects.count() == InvoiceLine.objects.count() + len(allocations)

    for award in Scholarship.objects.select_related("scholarship_type"):
        applied = billing_calc.dsum(x.amount for x in award.allocations.all())
        assert 0 <= applied <= award.awarded_amount  # allocations never exceed the award
        events = sorted(award.events.all(), key=lambda e: (e.event_date, e.event_id))
        assert events[0].from_status is None
        for prev, e in zip([None, *events], events, strict=False):
            assert e.to_status in SCHOLARSHIP_TRANSITIONS[prev.to_status if prev else None]
        assert award.status == events[-1].to_status
        elig = award.eligibility
        assert elig.is_eligible and (elig.student_id, elig.scholarship_type_id, elig.term_id) == (
            award.student_id,
            award.scholarship_type_id,
            award.term_id,
        )

    # per-term cap (D-S5) on awards
    from collections import defaultdict

    per_term = defaultdict(D)
    for award in Scholarship.objects.exclude(status="REVOKED"):
        per_term[(award.student_id, award.scholarship_type_id, award.term_id)] += (
            award.awarded_amount
        )
    for (_, type_id, _), total in per_term.items():
        assert total <= ScholarshipType.objects.get(pk=type_id).max_amount_per_term

    # BR-SCH-04 on allocations; line open ≥ 0; stored collection status = BR-INV-06
    for inv in Invoice.objects.all():
        lines = list(inv.lines.all())
        scholarship = services.invoice_allocations(inv)
        types = {a.scholarship.scholarship_type for a in scholarship}
        if types:
            base = max(calc.eligible_fee_base(t, lines) for t in types)
            assert billing_calc.dsum(a.amount for a in scholarship) <= base
        assert all(v >= 0 for v in billing_calc.line_open_amounts(lines, scholarship).values())
        derived = billing_calc.collection_status(
            billing.current_amounts(inv, lines),
            due_date=inv.due_date,
            as_of=inv.collection_status_as_of,
        )
        assert inv.collection_status == derived

    # BR-BAL-02 for every student; evaluations are complete and dated on the invoice date
    from students.models import Student

    assert all(billing.student_balance(s, None)["identity"]["holds"] for s in Student.objects.all())
    assert set(Eligibility.objects.values_list("evaluated_on", flat=True)) == {fall.invoice_date}

    # the scenario really reached the interesting states
    inv2.refresh_from_db()
    assert inv2.collection_status == "SCHOLARSHIP_COVERED"
    assert unapplied.status == "OFFERED" and unapplied.awarded_amount == D("25000.00")
    assert ScholarshipAllocation.objects.filter(allocation_type="REVERSE").count() == 1
    assert set(Scholarship.objects.values_list("status", flat=True)) == {
        "APPLIED",
        "OFFERED",
        "PARTIALLY_REVERSED",
    }
