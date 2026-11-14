"""The rows one reconciliation run looks at, held in memory, plus lookup indexes.

The checks (``reconciliation.checks``) read only this object: they never query the
database, so every check is a pure function that unit tests can feed with plain objects
(ARCHITECTURE §5). ``reconciliation.loader`` fills it from the default database.

Rows are attribute objects (model instances in production, ``SimpleNamespace`` in unit
tests) with the field names of DATABASE_DESIGN §3; foreign keys are read by their ``*_id``
attribute, so no check ever triggers a query.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property

# every domain table the detector reads, in DATABASE_DESIGN §3 order: attribute → (entity
# name for findings, ID kind in core.ids or None, student-owned?)
TABLES: dict[str, tuple[str, str | None, bool]] = {
    "programs": ("Program", "program", False),
    "terms": ("Term", "term", False),
    "students": ("Student", "student", True),
    "student_events": ("StudentStatusEvent", "status_event", True),
    "enrollments": ("Enrollment", "enrollment", True),
    "enrollment_events": ("EnrollmentEvent", "status_event", True),
    "fee_schedules": ("FeeSchedule", "fee_schedule", False),
    "invoices": ("Invoice", "invoice", True),
    "lines": ("InvoiceLine", "invoice_line", True),
    "invoice_events": ("InvoiceEvent", "status_event", True),
    "ledger": ("LedgerEntry", "ledger_entry", True),
    "scholarship_types": ("ScholarshipType", "scholarship_type", False),
    "type_programs": ("ScholarshipTypeProgram", "scholarship_type_program", False),
    "eligibilities": ("Eligibility", "eligibility", True),
    "scholarships": ("Scholarship", "scholarship", True),
    "scholarship_allocations": ("ScholarshipAllocation", "scholarship_allocation", True),
    "scholarship_events": ("ScholarshipEvent", "status_event", True),
    "payments": ("Payment", "payment", True),
    "payment_allocations": ("PaymentAllocation", "payment_allocation", True),
    "refunds": ("Refund", "refund", True),
    "refund_events": ("RefundEvent", "status_event", True),
    "snapshots": ("BalanceSnapshot", "balance_snapshot", True),
}


@dataclass(frozen=True)
class OrphanRef:
    """One row whose foreign key points at nothing (from SQLite's foreign_key_check)."""

    entity_type: str
    record_id: str
    field: str
    target: str


@dataclass
class Dataset:
    as_of: date
    student_id: str | None = None  # set for a STUDENT-scope run
    programs: list = field(default_factory=list)
    terms: list = field(default_factory=list)
    students: list = field(default_factory=list)
    student_events: list = field(default_factory=list)
    enrollments: list = field(default_factory=list)
    enrollment_events: list = field(default_factory=list)
    fee_schedules: list = field(default_factory=list)
    invoices: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    invoice_events: list = field(default_factory=list)
    ledger: list = field(default_factory=list)
    scholarship_types: list = field(default_factory=list)
    type_programs: list = field(default_factory=list)
    eligibilities: list = field(default_factory=list)
    scholarships: list = field(default_factory=list)
    scholarship_allocations: list = field(default_factory=list)
    scholarship_events: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    payment_allocations: list = field(default_factory=list)
    refunds: list = field(default_factory=list)
    refund_events: list = field(default_factory=list)
    snapshots: list = field(default_factory=list)
    # {entity_type: {field: allowed values}} for INVALID_ENUM (from the models' choices)
    enum_fields: dict = field(default_factory=dict)
    orphans: list = field(default_factory=list)  # [OrphanRef], ALL scope only

    @property
    def is_student_scope(self) -> bool:
        return self.student_id is not None

    def on_or_before(self, d: date | None) -> bool:
        """REC-08: a dated row counts for an as-of rule when its date is ≤ as_of."""
        return d is not None and d <= self.as_of

    # --- indexes (built once per run) -----------------------------------------------------

    @cached_property
    def by_id(self) -> dict[str, dict]:
        """{table attribute: {pk: row}}."""
        return {name: {_pk(r): r for r in getattr(self, name)} for name in TABLES}

    def _group(self, rows, attr) -> dict[str, list]:
        out: dict[str, list] = defaultdict(list)
        for r in rows:
            out[getattr(r, attr)].append(r)
        return out

    @cached_property
    def lines_by_invoice(self) -> dict[str, list]:
        return self._group(self.lines, "invoice_id")

    @cached_property
    def invoice_of_line(self) -> dict[str, str]:
        return {r.line_id: r.invoice_id for r in self.lines}

    @cached_property
    def scholarship_allocations_by_invoice(self) -> dict[str, list]:
        out: dict[str, list] = defaultdict(list)
        for a in self.scholarship_allocations:
            out[self.invoice_of_line.get(a.invoice_line_id)].append(a)
        return out

    @cached_property
    def payment_allocations_by_invoice(self) -> dict[str, list]:
        out: dict[str, list] = defaultdict(list)
        for a in self.payment_allocations:
            out[self.invoice_of_line.get(a.invoice_line_id)].append(a)
        return out

    @cached_property
    def allocations_by_scholarship(self) -> dict[str, list]:
        return self._group(self.scholarship_allocations, "scholarship_id")

    @cached_property
    def allocations_by_payment(self) -> dict[str, list]:
        return self._group(self.payment_allocations, "payment_id")

    @cached_property
    def refunds_by_payment(self) -> dict[str, list]:
        return self._group(self.refunds, "payment_id")

    def per_student(self, name: str) -> dict[str, list]:
        return self._group(getattr(self, name), "student_id")


def _pk(row) -> str:
    return row.pk  # model instances have it; unit-test rows set it explicitly
