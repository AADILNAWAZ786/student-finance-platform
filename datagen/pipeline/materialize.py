"""Stage 17: ID materialization and model instances (ARCHITECTURE §11.2).

Deferred records (those whose sequences may contain injected rows) are numbered here, once,
per ``core.ids`` sequence scope, in temporary-key order = (business date, creation order).
Baseline and injected rows share the sequences, so injected rows never stand out by ID
position (ARCHITECTURE §12.2.3). ``IdSequence`` ends equal to the highest counter of every
scope, exactly as the services would leave it, so later API calls continue without gaps.

Nothing here touches the database: ``build`` returns unsaved model instances in insert order.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from academics.models import Enrollment, EnrollmentEvent, Program, Term
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core import ids
from core.models import IdSequence
from datagen.pipeline.engine import World, description_of, ref_id
from payments.models import Payment, PaymentAllocation, Refund, RefundEvent
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

# insert order: parents before children (FKs are also deferred on SQLite)
MODELS = (
    Program,
    Term,
    FeeSchedule,
    ScholarshipType,
    ScholarshipTypeProgram,
    Student,
    StudentStatusEvent,
    Enrollment,
    EnrollmentEvent,
    Invoice,
    InvoiceLine,
    InvoiceEvent,
    Eligibility,
    Scholarship,
    ScholarshipAllocation,
    ScholarshipEvent,
    Payment,
    PaymentAllocation,
    Refund,
    RefundEvent,
    LedgerEntry,
    BalanceSnapshot,
    IdSequence,
)


@dataclass
class Materialized:
    rows: dict  # model -> list of unsaved instances, insert order
    sequences: dict  # IdSequence key -> last value

    def count(self, model) -> int:
        return len(self.rows[model])


def _number(records, scope_of, fmt, sequences):
    """Number records per scope in temporary-key order."""
    groups = defaultdict(list)
    for r in records:
        groups[scope_of(r)].append(r)
    for scope_key, members in groups.items():
        members.sort(key=lambda r: r.key)
        for n, r in enumerate(members, 1):
            r.pk = fmt(r, n)
        sequences[scope_key] = len(members)


def assign_ids(w: World) -> dict:
    sequences = dict(w._counters)
    y = lambda d: d.year  # noqa: E731
    _number(
        w.invoices,
        lambda r: ids.sequence_key("invoice", parent=r.student.pk, year=y(r.issue_date)),
        lambda r, n: ids.invoice_id(r.student.pk, r.issue_date.year, n),
        sequences,
    )
    _number(
        w.lines,
        lambda r: ids.sequence_key("invoice_line", parent=r.invoice.pk),
        lambda r, n: ids.invoice_line_id(r.invoice.pk, n),
        sequences,
    )
    _number(
        w.scholarships,
        lambda r: ids.sequence_key("scholarship", year=y(r.award_date)),
        lambda r, n: ids.scholarship_id(r.award_date.year, n),
        sequences,
    )
    _number(
        w.scholarship_allocations,
        lambda r: ids.sequence_key("scholarship_allocation", parent=r.scholarship.pk),
        lambda r, n: ids.scholarship_allocation_id(r.scholarship.pk, n),
        sequences,
    )
    _number(
        w.payment_allocations,
        lambda r: ids.sequence_key("payment_allocation", parent=r.payment.pk),
        lambda r, n: ids.payment_allocation_id(r.payment.pk, n),
        sequences,
    )
    _number(
        w.refunds,
        lambda r: ids.sequence_key("refund", year=y(r.requested_date)),
        lambda r, n: ids.refund_id(r.requested_date.year, n),
        sequences,
    )
    _number(
        w.ledger,
        lambda r: ids.sequence_key("ledger_entry", year=y(r.entry_date)),
        lambda r, n: ids.ledger_entry_id(r.entry_date.year, n),
        sequences,
    )
    for events in (w.invoice_events, w.scholarship_events, w.refund_events):
        _number(
            events,
            lambda r: ids.sequence_key("status_event", entity=r.entity, year=y(r.event_date)),
            lambda r, n: ids.status_event_id(r.entity, r.event_date.year, n),
            sequences,
        )
    return sequences


def _event_fields(e) -> dict:
    return dict(
        event_id=e.pk,
        from_status=e.from_status,
        to_status=e.to_status,
        event_date=e.event_date,
        reason=e.reason,
        actor=e.actor,
        source_ref=ref_id(e.source_ref),
    )


def build(w: World) -> Materialized:
    sequences = assign_ids(w)
    rows: dict = {m: [] for m in MODELS}
    rows[Program] = [
        Program(
            program_id=p.pk,
            code=p.code,
            name=p.name,
            level=p.level,
            department=p.department,
            is_stem=p.is_stem,
            has_lab=p.has_lab,
            min_credits=p.min_credits,
            max_credits=p.max_credits,
            is_active=p.is_active,
        )
        for p in w.programs
    ]
    rows[Term] = [
        Term(
            term_id=t.pk,
            year=t.year,
            season=t.season,
            is_billable=t.is_billable,
            invoice_date=t.invoice_date,
            due_date=t.due_date,
            start_date=t.start_date,
            census_date=t.census_date,
            end_date=t.end_date,
            late_fee_grace_days=t.late_fee_grace_days,
        )
        for t in w.terms.values()
    ]
    rows[FeeSchedule] = [
        FeeSchedule(
            fee_schedule_id=f.pk,
            program_id=f.program.pk,
            term_id=f.term.pk,
            fee_type=f.fee_type,
            residency=f.residency,
            charge_basis=f.charge_basis,
            amount=f.amount,
            is_scholarship_eligible=f.is_scholarship_eligible,
            is_refundable=f.is_refundable,
            is_waivable=f.is_waivable,
            allocation_priority=f.allocation_priority,
            version=f.version,
            effective_from=f.effective_from,
            supersedes_id=f.supersedes.pk if f.supersedes else None,
        )
        for f in sorted(w.fee_schedules, key=lambda f: (f.version, f.pk))
    ]
    rows[ScholarshipType] = [
        ScholarshipType(
            scholarship_type_id=t.pk,
            name=t.name,
            category=t.category,
            exclusive_group=t.exclusive_group,
            min_gpa=t.min_gpa,
            max_gpa=t.max_gpa,
            min_need_index=t.min_need_index,
            residency=t.residency,
            first_term_only=t.first_term_only,
            program_restricted=t.program_restricted,
            coverage_scope=t.coverage_scope,
            award_basis=t.award_basis,
            award_value=t.award_value,
            max_amount_per_term=t.max_amount_per_term,
            priority=t.priority,
            effective_from=t.effective_from,
            effective_to=t.effective_to,
        )
        for t in w.types
    ]
    rows[ScholarshipTypeProgram] = [
        ScholarshipTypeProgram(
            id=b.pk, scholarship_type_id=b.scholarship_type.pk, program_id=b.program_id
        )
        for b in w.bridges
    ]
    rows[Student] = [
        Student(
            student_id=s.pk,
            first_name=s.first_name,
            last_name=s.last_name,
            email=s.email,
            date_of_birth=s.date_of_birth,
            residency=s.residency,
            program_id=s.program.pk,
            admit_term_id=s.admit_term.pk,
            gpa=s.gpa,
            need_index=s.need_index,
            status=s.status,
        )
        for s in w.students
    ]
    rows[StudentStatusEvent] = [
        StudentStatusEvent(student_id=e.student.pk, **_event_fields(e)) for e in w.student_events
    ]
    rows[Enrollment] = [
        Enrollment(
            enrollment_id=e.pk,
            student_id=e.student.pk,
            program_id=e.program.pk,
            term_id=e.term.pk,
            credits=e.credits,
            status=e.status,
            enrolled_on=e.enrolled_on,
            withdrawal_date=e.withdrawal_date,
            cancellation_date=e.cancellation_date,
        )
        for e in w.enrollments
    ]
    rows[EnrollmentEvent] = [
        EnrollmentEvent(enrollment_id=e.enrollment.pk, **_event_fields(e))
        for e in w.enrollment_events
    ]
    rows[Invoice] = [
        Invoice(
            invoice_id=i.pk,
            student_id=i.student.pk,
            enrollment_id=i.enrollment.pk,
            term_id=i.term.pk,
            issue_date=i.issue_date,
            due_date=i.due_date,
            total_amount=i.total_amount,
            lifecycle_status=i.lifecycle_status,
            collection_status=i.collection_status,
            collection_status_as_of=i.collection_status_as_of,
            currency=i.currency,
        )
        for i in sorted(w.invoices, key=lambda i: i.pk)
    ]
    rows[InvoiceLine] = [
        InvoiceLine(
            line_id=x.pk,
            invoice_id=x.invoice.pk,
            line_type=x.line_type,
            fee_schedule_id=x.fee_schedule.pk if x.fee_schedule else None,
            fee_type=x.fee_type,
            description=description_of(x),
            quantity=x.quantity,
            unit_amount=x.unit_amount,
            gross_amount=x.gross_amount,
            waiver_amount=x.waiver_amount,
            waiver_reason=x.waiver_reason,
            net_amount=x.net_amount,
            adjusts_line_id=x.adjusts.pk if x.adjusts else None,
            adjustment_reason=x.adjustment_reason,
            is_scholarship_eligible=x.is_scholarship_eligible,
            is_refundable=x.is_refundable,
            is_waivable=x.is_waivable,
            allocation_priority=x.allocation_priority,
            posted_date=x.posted_date,
        )
        for x in sorted(w.lines, key=lambda x: x.pk)
    ]
    rows[InvoiceEvent] = [
        InvoiceEvent(invoice_id=e.invoice.pk, dimension=e.dimension, **_event_fields(e))
        for e in sorted(w.invoice_events, key=lambda e: e.pk)
    ]
    rows[Eligibility] = [
        Eligibility(
            eligibility_id=e.pk,
            student_id=e.student.pk,
            scholarship_type_id=e.scholarship_type.pk,
            term_id=e.term.pk,
            evaluated_on=e.evaluated_on,
            is_eligible=e.is_eligible,
            reason_codes=list(e.reason_codes),
            inputs_snapshot=dict(e.inputs_snapshot),
            eligible_fee_base=e.eligible_fee_base,
        )
        for e in w.eligibility
    ]
    rows[Scholarship] = [
        Scholarship(
            scholarship_id=s.pk,
            student_id=s.student.pk,
            scholarship_type_id=s.scholarship_type.pk,
            term_id=s.term.pk,
            eligibility_id=s.eligibility.pk,
            invoice_id=s.invoice.pk,
            awarded_amount=s.awarded_amount,
            award_date=s.award_date,
            status=s.status,
        )
        for s in sorted(w.scholarships, key=lambda s: s.pk)
    ]
    rows[ScholarshipAllocation] = [
        ScholarshipAllocation(
            allocation_id=a.pk,
            scholarship_id=a.scholarship.pk,
            invoice_line_id=a.line.pk,
            allocation_type=a.allocation_type,
            amount=a.amount,
            reverses_id=a.reverses.pk if a.reverses else None,
            reason=a.reason,
            allocated_on=a.allocated_on,
        )
        for a in sorted(w.scholarship_allocations, key=lambda a: a.pk)
    ]
    rows[ScholarshipEvent] = [
        ScholarshipEvent(scholarship_id=e.scholarship.pk, **_event_fields(e))
        for e in sorted(w.scholarship_events, key=lambda e: e.pk)
    ]
    rows[Payment] = [
        Payment(
            payment_id=p.pk,
            student_id=p.student.pk,
            amount=p.amount,
            currency=p.currency,
            received_date=p.received_date,
            method=p.method,
            payer_type=p.payer_type,
            payer_name=p.payer_name,
            external_reference=p.external_reference,
            status=p.status,
        )
        for p in w.payments
    ]
    rows[PaymentAllocation] = [
        PaymentAllocation(
            allocation_id=a.pk,
            payment_id=a.payment.pk,
            invoice_line_id=a.line.pk,
            allocation_type=a.allocation_type,
            amount=a.amount,
            reverses_id=a.reverses.pk if a.reverses else None,
            rule_applied=a.rule_applied,
            allocated_on=a.allocated_on,
        )
        for a in sorted(w.payment_allocations, key=lambda a: a.pk)
    ]
    rows[Refund] = [
        Refund(
            refund_id=r.pk,
            student_id=r.student.pk,
            payment_id=r.payment.pk,
            payer_type=r.payer_type,
            enrollment_id=r.enrollment.pk if r.enrollment else None,
            reason=r.reason,
            amount=r.amount,
            requested_date=r.requested_date,
            processed_date=r.processed_date,
            status=r.status,
            calculation=dict(r.calculation),
        )
        for r in sorted(w.refunds, key=lambda r: r.pk)
    ]
    rows[RefundEvent] = [
        RefundEvent(refund_id=e.refund.pk, **_event_fields(e))
        for e in sorted(w.refund_events, key=lambda e: e.pk)
    ]
    source_attr = {
        "invoice_line": "invoice_line_id",
        "scholarship_allocation": "scholarship_allocation_id",
        "payment": "payment_id",
        "refund": "refund_id",
    }
    rows[LedgerEntry] = [
        LedgerEntry(
            entry_id=e.pk,
            student_id=e.student.pk,
            entry_date=e.entry_date,
            entry_type=e.entry_type,
            amount=e.amount,
            memo=e.memo(),
            **{source_attr[e.source_field]: e.source.pk},
        )
        for e in sorted(w.ledger, key=lambda e: e.pk)
    ]
    rows[BalanceSnapshot] = [
        BalanceSnapshot(
            snapshot_id=s.pk,
            student_id=s.student.pk,
            as_of_date=s.as_of_date,
            reported_balance=s.reported_balance,
            total_debits=s.total_debits,
            total_credits=s.total_credits,
        )
        for s in w.snapshots
    ]
    rows[IdSequence] = [IdSequence(key=k, last_value=v) for k, v in sorted(sequences.items())]
    return Materialized(rows=rows, sequences=sequences)
