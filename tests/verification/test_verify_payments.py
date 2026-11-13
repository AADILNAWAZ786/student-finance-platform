"""V-PAY: Phase 4 verification beyond the per-endpoint tests.

* the migrations are additive: payments 0001 only creates its four tables; billing 0003 only
  extends billing_ledgerentry (rows copied by INSERT … SELECT, never updated or deleted);
  migrating back to Phase 3 and forward again keeps every Phase 1–3 row
* the ground-truth database records the migrations but gets no payments table
* the same scenario gives identical rows (IDs included) every time
* a mixed scenario satisfies every payment, refund and ledger invariant reconciliation will
  re-check (BR-LED-01, BR-ALC-01/02/05, BR-REF-01/02/03, BR-BAL-02)

(The concurrency tests named by ARCHITECTURE §8.2 are in tests/services/test_concurrency.py.)
"""

import io
import re
from datetime import date
from decimal import Decimal as D

import pytest
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor

from billing import calculators as billing_calc
from billing import services as billing
from billing.models import Invoice, LedgerEntry
from core.ids import is_valid_id
from core.money import dsum
from payments import calculators as calc
from payments import services
from payments.lifecycle import REFUND_TRANSITIONS
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent
from scholarships import services as scholarships
from students.models import Student

PAYMENT_TABLES = {
    "payments_payment",
    "payments_paymentallocation",
    "payments_refund",
    "payments_refundevent",
}


def _sql(app, name):
    buf = io.StringIO()
    call_command("sqlmigrate", app, name, stdout=buf)
    text = "\n".join(ln for ln in buf.getvalue().splitlines() if not ln.lstrip().startswith("--"))
    return [
        s.strip() for s in text.split(";") if s.strip() and s.strip() not in ("BEGIN", "COMMIT")
    ]


@pytest.mark.django_db(transaction=True)
def test_payments_migration_only_creates_its_tables():
    statements = _sql("payments", "0001_initial")
    created = {
        re.match(r'CREATE TABLE "(\w+)"', s).group(1)
        for s in statements
        if s.startswith("CREATE TABLE")
    }
    assert created == PAYMENT_TABLES
    for sql in statements:
        assert re.match(r'CREATE TABLE "payments_\w+"', sql) or re.match(
            r'CREATE (UNIQUE )?INDEX "\w+" ON "payments_\w+"', sql
        ), sql[:120]


@pytest.mark.django_db(transaction=True)
def test_billing_0003_only_touches_the_ledger_table_and_copies_every_row():
    statements = _sql("billing", "0003_ledger_payment_sources")
    target = re.compile(
        r"^(?:CREATE TABLE|INSERT INTO|DROP TABLE|ALTER TABLE"
        r'|CREATE (?:UNIQUE )?INDEX "\w+" ON) "(\w+)"'
    )
    for sql in statements:
        m = target.match(sql)
        assert m, sql[:120]
        assert m.group(1) in {"billing_ledgerentry", "new__billing_ledgerentry"}, sql[:160]
    assert not any(s.upper().startswith(("UPDATE", "DELETE")) for s in statements)
    inserts = [s for s in statements if s.upper().startswith("INSERT")]
    assert inserts and all("SELECT" in s and 'FROM "billing_ledgerentry"' in s for s in inserts)
    # the new source columns are nullable and unique, and the two rebuilds that add them copy
    # every existing row with NULL in the new column (never a value)
    creates = [s for s in statements if s.startswith('CREATE TABLE "new__billing_ledgerentry"')]
    assert '"payment_id" varchar(15) NULL UNIQUE' in creates[0]
    assert '"refund_id" varchar(15) NULL UNIQUE' in creates[1]
    assert inserts[0].endswith('"scholarship_allocation_id", NULL FROM "billing_ledgerentry"')
    assert inserts[1].endswith('"payment_id", NULL FROM "billing_ledgerentry"')
    assert len(inserts) == len(creates) == 8  # 2 AddField + 1 AlterField + 3 × (remove, add)


LATER_TABLES = ("payments_",)
NEW_COLUMNS = {"billing_ledgerentry": {"payment_id", "refund_id"}}  # added by billing 0003


def _rows():
    """Every Phase 1–3 row of every Phase 1–3 table, over the Phase 3 columns only."""
    out = {}
    with connection.cursor() as cur:
        for table in sorted(connection.introspection.table_names()):
            if table.startswith(LATER_TABLES) or table == "django_migrations":
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
def test_migrations_keep_every_phase3_row_and_migrate_back(merit_award, invoice):
    scholarships.apply_scholarship(merit_award, actor="t")  # Phase 3 rows on the ledger
    billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=False, actor="t")
    before = _rows()
    kinds = set(LedgerEntry.objects.values_list("entry_type", flat=True))
    assert kinds == {"CHARGE", "LATE_FEE", "SCHOLARSHIP_CREDIT"}
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("billing", "0002_ledger_scholarship_sources"), ("payments", None)])
        assert not set(connection.introspection.table_names()) & PAYMENT_TABLES
        cols = [
            c.name
            for c in connection.introspection.get_table_description(
                connection.cursor(), "billing_ledgerentry"
            )
        ]
        assert not {"payment_id", "refund_id"} & set(cols)
        assert _rows() == before  # back at Phase 3: every row kept

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())  # forward again: Phase 4
        assert PAYMENT_TABLES <= set(connection.introspection.table_names())
        assert _rows() == before  # every Phase 1–3 row unchanged
        assert not LedgerEntry.objects.filter(payment__isnull=False).exists()
        assert not LedgerEntry.objects.filter(refund__isnull=False).exists()
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(databases="__all__")
def test_ground_truth_records_but_creates_no_payments_table():
    assert not [
        t
        for t in connections["ground_truth"].introspection.table_names()
        if t.startswith("payments_")
    ]
    applied = MigrationExecutor(connections["ground_truth"]).loader.applied_migrations
    assert ("payments", "0001_initial") in applied
    assert ("billing", "0003_ledger_payment_sources") in applied


# --- a mixed scenario: determinism and invariants ------------------------------------------------


def _scenario(merit_award, invoice, student_with_invoice, record, manual):
    """Payments from every payer type, AUTO and MANUAL, a BR-ALC-06 release, a withdrawal-style
    release, refunds in every terminal and pending state."""
    s1 = invoice.student
    guardian = record("100000.00", payer="GUARDIAN", on=date(2026, 8, 18), auto=True)
    scholarships.apply_scholarship(merit_award, allocated_on=date(2026, 8, 20), actor="s")
    sponsor = record("100000.00", payer="SPONSOR", on=date(2026, 8, 21), auto=True)
    refunds = []
    for amount, end in (
        ("1000.00", "PROCESSED"),
        ("2000.00", "REJECTED"),
        ("3000.00", "APPROVED"),
        ("4000.00", "REQUESTED"),
    ):
        r = services.request_refund(
            payment=sponsor, amount=D(amount), requested_date=date(2026, 8, 22), actor="s"
        )
        if end in ("APPROVED", "PROCESSED"):
            r = services.approve_refund(r, on_date=date(2026, 8, 23), actor="s")
        if end == "PROCESSED":
            r = services.process_refund(r, processed_date=date(2026, 8, 24), actor="s")
        if end == "REJECTED":
            r = services.reject_refund(r, on_date=date(2026, 8, 23), actor="s")
        refunds.append(r)
    s2, inv2 = student_with_invoice()
    own = record("20000.00", student=s2, payer="SELF", on=date(2026, 8, 18))
    manual(own, (2, "6000.00"), (3, "5000.00"), inv=inv2, on=date(2026, 8, 18))
    services.deallocate(
        own.allocations.order_by("allocation_id").first(),
        amount=D("2400.00"),
        rule_applied="WITHDRAWAL_RELEASE",
        on_date=date(2026, 9, 17),
        actor="s",
    )
    return [s1, s2], [guardian, sponsor, own], refunds


def _dump():
    models = (Payment, PaymentAllocation, Refund, RefundEvent, LedgerEntry, Invoice)

    def rows(model):
        fields = [f.attname for f in model._meta.concrete_fields if f.name != "recorded_at"]
        return [tuple(str(getattr(o, f)) for f in fields) for o in model.objects.order_by("pk")]

    return {m.__name__: rows(m) for m in models}


@pytest.mark.django_db
def test_same_scenario_gives_identical_rows(
    merit_award, invoice, student_with_invoice, record, manual
):
    dumps = []
    for _ in range(2):
        with transaction.atomic():
            _scenario(merit_award, invoice, student_with_invoice, record, manual)
            dumps.append(_dump())
            transaction.set_rollback(True)
    # the external references differ per run (they must be unique): compare everything else
    for d in dumps:
        d["Payment"] = [row[:8] + row[9:] for row in d["Payment"]]
    assert dumps[0] == dumps[1]
    # guardian AUTO 1 (L01) + BR-ALC-06 release 1 + sponsor AUTO 5 (L02–L06; L01 is covered)
    # + student 2: MANUAL 2 + withdrawal-style release 1
    assert len(dumps[0]["PaymentAllocation"]) == 10
    assert len(dumps[0]["RefundEvent"]) == 3 + 2 + 2 + 1  # PROCESSED, REJECTED, APPROVED, REQUESTED


@pytest.mark.django_db
def test_every_payment_invariant_holds(merit_award, invoice, student_with_invoice, record, manual):
    students, payments, refunds = _scenario(
        merit_award, invoice, student_with_invoice, record, manual
    )

    # BR-LED-01: one PAYMENT entry per payment (−amount, received_date); one REFUND entry per
    # PROCESSED refund (+amount, processed_date) and none for any other status; none for
    # allocations
    for p in Payment.objects.all():
        e = LedgerEntry.objects.get(payment=p)
        assert (e.entry_type, e.amount, e.entry_date, e.student_id) == (
            "PAYMENT",
            -p.amount,
            p.received_date,
            p.student_id,
        )
        assert is_valid_id("payment", p.pk)
    for r in Refund.objects.all():
        entries = list(LedgerEntry.objects.filter(refund=r))
        if r.status == "PROCESSED":
            assert [(e.entry_type, e.amount, e.entry_date) for e in entries] == [
                ("REFUND", r.amount, r.processed_date)
            ]
        else:
            assert entries == [] and r.processed_date is None
        assert r.payer_type == r.payment.payer_type  # BR-REF-04
        # lifecycle: every event is a legal step and the last one is the status
        events = sorted(r.events.all(), key=lambda e: (e.event_date, e.event_id))
        assert events[0].from_status is None
        for prev, e in zip([None, *events], events, strict=False):
            assert e.to_status in REFUND_TRANSITIONS[prev.to_status if prev else None]
        assert r.status == events[-1].to_status
        assert set(r.calculation) >= {
            "net_paid",
            "credit_balance",
            "payment_available",
            "schedule_pct",
        }
    assert LedgerEntry.objects.filter(entry_type__in=["PAYMENT", "REFUND"]).count() == (
        Payment.objects.count() + Refund.objects.filter(status="PROCESSED").count()
    )

    # BR-ALC-01: every payment's available amount ≥ 0; BR-ALC-05: Σ deallocations ≤ original,
    # same line and payment; IDs within the frozen format
    for p in Payment.objects.all():
        assert services.available(p) >= 0
        assert PaymentAllocation.objects.filter(payment=p).count() <= 99
    for a in PaymentAllocation.objects.filter(allocation_type="ALLOCATE"):
        released = -dsum(r.amount for r in a.reversals.all())
        assert 0 <= released <= a.amount
        assert all(
            (r.invoice_line_id, r.payment_id) == (a.invoice_line_id, a.payment_id)
            for r in a.reversals.all()
        )
        assert a.invoice_line.invoice.student_id == a.payment.student_id
        assert a.invoice_line.line_type in ("FEE", "LATE_FEE")
        assert a.allocated_on >= a.payment.received_date
    assert {
        a.rule_applied for a in PaymentAllocation.objects.filter(allocation_type="DEALLOCATE")
    } == {
        "SCHOLARSHIP_PRECEDENCE",
        "WITHDRAWAL_RELEASE",
    }

    # BR-ALC-02: every line's open amount ≥ 0
    for inv in Invoice.objects.all():
        sch, pay = billing.allocations_for(inv)
        assert all(
            v >= 0 for v in billing_calc.line_open_amounts(inv.lines.all(), sch, pay).values()
        )

    # BR-REF-01/02 hold for what is pending: Σ pending ≤ credit, net paid ≥ 0
    for s in Student.objects.all():
        caps = (
            services.refund_caps(Payment.objects.filter(student=s).first())
            if s.payments.exists()
            else None
        )
        if caps:
            assert caps.net_paid >= 0 and caps.pending_refunds <= caps.credit_balance
        # BR-BAL-02 for every student, and the balance equals the ledger sum (BR-BAL-01)
        b = billing.student_balance(s, None)
        assert b["identity"]["holds"] is True
        assert b["ledger_balance"] == dsum(
            LedgerEntry.objects.filter(student=s).values_list("amount", flat=True)
        )
        assert b["unallocated_credit"] == calc.unallocated_credit(
            s.payments.all(), PaymentAllocation.objects.filter(payment__student=s), s.refunds.all()
        )
