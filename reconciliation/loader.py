"""Load the rows of one reconciliation run from the **default** database into a ``Dataset``.

Read-only and outside any transaction (ARCHITECTURE §8.2; decision D-R2): each query sees
what is committed when it runs, so writes that overlap a run can be observed part-way (the
run is not a snapshot). The detector names no other database (ARCHITECTURE §12.2).
"""

from __future__ import annotations

from datetime import date

from django.db import connections

from academics.models import Enrollment, EnrollmentEvent, Program, Term
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent
from reconciliation.dataset import TABLES, Dataset, OrphanRef
from reconciliation.models import BalanceSnapshot
from scholarships.models import (
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    ScholarshipType,
    ScholarshipTypeProgram,
)
from students.models import Student, StudentStatusEvent

DB = "default"

MODELS = {
    "programs": Program,
    "terms": Term,
    "students": Student,
    "student_events": StudentStatusEvent,
    "enrollments": Enrollment,
    "enrollment_events": EnrollmentEvent,
    "fee_schedules": FeeSchedule,
    "invoices": Invoice,
    "lines": InvoiceLine,
    "invoice_events": InvoiceEvent,
    "ledger": LedgerEntry,
    "scholarship_types": ScholarshipType,
    "type_programs": ScholarshipTypeProgram,
    "eligibilities": Eligibility,
    "scholarships": Scholarship,
    "scholarship_allocations": ScholarshipAllocation,
    "scholarship_events": ScholarshipEvent,
    "payments": Payment,
    "payment_allocations": PaymentAllocation,
    "refunds": Refund,
    "refund_events": RefundEvent,
    "snapshots": BalanceSnapshot,
}
assert set(MODELS) == set(TABLES)

# how a student-owned table reaches its student (STUDENT scope filter)
STUDENT_FILTER = {
    "students": "student_id",
    "student_events": "student_id",
    "enrollments": "student_id",
    "enrollment_events": "enrollment__student_id",
    "invoices": "student_id",
    "lines": "invoice__student_id",
    "invoice_events": "invoice__student_id",
    "ledger": "student_id",
    "eligibilities": "student_id",
    "scholarships": "student_id",
    "scholarship_allocations": "scholarship__student_id",
    "scholarship_events": "scholarship__student_id",
    "payments": "student_id",
    "payment_allocations": "payment__student_id",
    "refunds": "student_id",
    "refund_events": "refund__student_id",
    "snapshots": "student_id",
}

# PaymentAllocation.payer_type reads the payment; load it with the row (no query per row)
SELECT_RELATED = {"payment_allocations": ("payment",)}


def enum_fields() -> dict[str, dict[str, frozenset]]:
    out = {}
    for name, model in MODELS.items():
        fields = {
            f.attname: (frozenset(v for v, _ in f.flatchoices), f.null)
            for f in model._meta.concrete_fields
            if f.choices
        }
        if fields:
            out[TABLES[name][0]] = fields
    return out


def _orphans() -> list[OrphanRef]:
    """SQLite's own ``PRAGMA foreign_key_check`` over every domain table."""
    by_table = {m._meta.db_table: m for m in MODELS.values()}
    found = []
    with connections[DB].cursor() as cursor:
        for table, model in sorted(by_table.items()):
            cursor.execute(f'PRAGMA foreign_key_check("{table}")')
            for _tbl, rowid, parent, fkid in cursor.fetchall():
                cursor.execute(
                    f'SELECT "{model._meta.pk.column}" FROM "{table}" WHERE rowid = %s', [rowid]
                )
                pk = cursor.fetchone()[0]
                cursor.execute(f'PRAGMA foreign_key_list("{table}")')
                column = next((r[3] for r in cursor.fetchall() if r[0] == fkid), "?")
                found.append(OrphanRef(model.__name__, pk, column, parent))
    return found


def load(*, as_of: date, student_id: str | None = None) -> Dataset:
    ds = Dataset(as_of=as_of, student_id=student_id, enum_fields=enum_fields())
    for name, model in MODELS.items():
        qs = model._default_manager.using(DB).all()
        if name in SELECT_RELATED:
            qs = qs.select_related(*SELECT_RELATED[name])
        if student_id is not None and name in STUDENT_FILTER:
            qs = qs.filter(**{STUDENT_FILTER[name]: student_id})
        setattr(ds, name, list(qs.order_by("pk")))
    if student_id is None:
        ds.orphans = _orphans()
    return ds
