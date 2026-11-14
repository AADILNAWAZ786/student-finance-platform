"""In-memory generation engine (approved decision D-G1; ARCHITECTURE §5, §11.2).

Every operation here mirrors one Phase 1–5 service function, step for step, but builds plain
in-memory records instead of writing rows. All financial decisions come from the domain
apps' own calculators (ARCHITECTURE P1), so the generator, the services and the detector
share one implementation of every rule. ``tests/datagen/test_parity.py`` replays the
operation log through the real services and compares the resulting rows, IDs included.

Keys and IDs
------------
* Kinds that never receive injected rows (programs, terms, fee schedules, scholarship types,
  students and their events, enrollments and their events, eligibility, payments, snapshots)
  get their final ID at creation, in creation order, exactly as ``core.ids.next_id`` would.
* Kinds that can receive injected rows (invoices, lines, invoice events, ledger entries,
  scholarships, their allocations and events, payment allocations, refunds and refund
  events) get a **temporary key**: a tuple that sorts exactly like the final ID will
  (e.g. a line key = (invoice key, posted_date, seq)). The calculators' tie-breaks on
  ``line_id`` / ``allocation_id`` therefore give the same answers before and after stage 17,
  which numbers each sequence in (business date, creation order) across baseline *and*
  injected rows (ARCHITECTURE §11.2).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import partial

from academics.calculators import withdrawal_refund_pct
from academics.lifecycle import CANCELLED as ENR_CANCELLED
from academics.lifecycle import COMPLETED, ENROLLED, WITHDRAWN
from billing import calculators as bcalc
from billing import policy as bpolicy
from core import ids
from core.money import ZERO, dsum, format_money, round_money
from payments import calculators as pcalc
from payments import policy as ppolicy
from reconciliation import calculators as rcalc
from scholarships import calculators as scalc
from scholarships import policy as spolicy
from scholarships.lifecycle import APPLIED, PARTIALLY_REVERSED, REVOKED, UNAPPLIED

ENTRY_TYPE_FOR_LINE = {"FEE": "CHARGE", "LATE_FEE": "LATE_FEE", "ADJUSTMENT": "ADJUSTMENT"}
SCHOLARSHIP_ENTRY_TYPE = {"APPLY": "SCHOLARSHIP_CREDIT", "REVERSE": "SCHOLARSHIP_REVERSAL"}
RELEASE_RULE = {"WITHDRAWAL": "WITHDRAWAL_RELEASE", "CANCELLATION": "CANCELLATION_RELEASE"}
REFUND_ENROLLMENT_STATUS = {"WITHDRAWAL": WITHDRAWN, "CANCELLATION": ENR_CANCELLED}


class GenerationError(RuntimeError):
    """The generator reached a state the services would refuse (a generator defect)."""


class Rec:
    """A plain record. Attributes mirror the model's attnames where the calculators read them."""

    kind = "record"

    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.pk = fields.get("pk")

    def __repr__(self):
        return f"<{self.kind} {self.pk or getattr(self, 'key', '?')}>"


def ref_id(value):
    """A record's final ID (for source_ref / memo / details), a plain value otherwise."""
    return value.pk if isinstance(value, Rec) else value


# --- record kinds ------------------------------------------------------------------------------


class Program(Rec):
    kind = "program"


class Term(Rec):
    kind = "term"


class FeeSchedule(Rec):
    kind = "fee_schedule"


class SType(Rec):
    kind = "scholarship_type"


class Bridge(Rec):
    kind = "scholarship_type_program"


class Student(Rec):
    kind = "student"

    @property
    def program_id(self):
        return self.program.pk

    @property
    def admit_term_id(self):
        return self.admit_term.pk


class Event(Rec):
    kind = "status_event"


class Enrollment(Rec):
    kind = "enrollment"


class Invoice(Rec):
    kind = "invoice"


class Line(Rec):
    kind = "invoice_line"

    @property
    def line_id(self):
        return self.key

    @property
    def adjusts_line_id(self):
        return self.adjusts.key if self.adjusts is not None else None

    @property
    def fee_schedule_id(self):
        return self.fee_schedule.pk if self.fee_schedule is not None else None


class Ledger(Rec):
    kind = "ledger_entry"


class Eligibility(Rec):
    kind = "eligibility"


class Scholarship(Rec):
    kind = "scholarship"


class SchAlloc(Rec):
    kind = "scholarship_allocation"

    @property
    def allocation_id(self):
        return self.key

    @property
    def invoice_line_id(self):
        return self.line.key

    @property
    def reverses_id(self):
        return self.reverses.key if self.reverses is not None else None


class Payment(Rec):
    kind = "payment"

    @property
    def payment_id(self):
        return self.pk


class PayAlloc(Rec):
    kind = "payment_allocation"

    @property
    def allocation_id(self):
        return self.key

    @property
    def invoice_line_id(self):
        return self.line.key

    @property
    def reverses_id(self):
        return self.reverses.key if self.reverses is not None else None

    @property
    def payer_type(self):
        return self.payment.payer_type


class Refund(Rec):
    kind = "refund"

    @property
    def refund_id(self):
        return self.key


class Snapshot(Rec):
    kind = "balance_snapshot"


class _OpenLine:
    __slots__ = ("line_id", "invoice", "due_date", "allocation_priority", "open_amount")

    def __init__(self, line_id, invoice, due_date, allocation_priority, open_amount):
        self.line_id, self.invoice, self.due_date = line_id, invoice, due_date
        self.allocation_priority, self.open_amount = allocation_priority, open_amount


class _OpenAllocation:
    __slots__ = ("allocation_id", "allocated_on", "amount")

    def __init__(self, allocation_id, allocated_on, amount):
        self.allocation_id, self.allocated_on, self.amount = allocation_id, allocated_on, amount


# --- the world ------------------------------------------------------------------------------------


class World:
    """All records of one generation, plus the operation log for the parity replay."""

    def __init__(self, *, actor: str = "datagen"):
        self.actor = actor
        self._seq = 0
        self._counters: dict[str, int] = defaultdict(int)
        self.ops: list[tuple] = []
        self.programs: list[Program] = []
        self.terms: dict[str, Term] = {}
        self.fee_schedules: list[FeeSchedule] = []
        self.types: list[SType] = []
        self.bridges: list[Bridge] = []
        self.students: list[Student] = []
        self.student_events: list[Event] = []
        self.enrollments: list[Enrollment] = []
        self.enrollment_events: list[Event] = []
        self.invoices: list[Invoice] = []
        self.lines: list[Line] = []
        self.invoice_events: list[Event] = []
        self.ledger: list[Ledger] = []
        self.eligibility: list[Eligibility] = []
        self.scholarships: list[Scholarship] = []
        self.scholarship_allocations: list[SchAlloc] = []
        self.scholarship_events: list[Event] = []
        self.payments: list[Payment] = []
        self.payment_allocations: list[PayAlloc] = []
        self.refunds: list[Refund] = []
        self.refund_events: list[Event] = []
        self.snapshots: list[Snapshot] = []
        self._fees_by_program_term: dict = defaultdict(list)

    # --- sequencing ---------------------------------------------------------------------------

    def seq(self) -> int:
        self._seq += 1
        return self._seq

    def take(self, key: str) -> int:
        """Next counter of an immediately numbered sequence (as ``core.ids.next_id``)."""
        self._counters[key] += 1
        return self._counters[key]

    def log(self, op: str, **args):
        self.ops.append((op, args))

    # --- shared helpers (mirroring billing.services) -----------------------------------------

    def _event(
        self,
        store,
        owner_field,
        owner,
        entity,
        *,
        from_status,
        to_status,
        event_date,
        reason,
        source_ref=None,
        **extra,
    ):
        ev = Event(
            key=(event_date.year, event_date, self.seq()),
            entity=entity,
            from_status=from_status,
            to_status=to_status,
            event_date=event_date,
            reason=reason,
            actor=self.actor,
            source_ref=source_ref,
            **{owner_field: owner},
            **extra,
        )
        store.append(ev)
        owner.events.append(ev)
        return ev

    def _immediate_event(self, store, owner_field, owner, entity, **kw):
        ev = self._event(store, owner_field, owner, entity, **kw)
        n = self.take(ids.sequence_key("status_event", entity=entity, year=kw["event_date"].year))
        ev.pk = ids.status_event_id(entity, kw["event_date"].year, n)
        return ev

    def _ledger(self, student, entry_date, entry_type, magnitude, source_field, source, memo):
        entry = Ledger(
            key=(entry_date.year, entry_date, self.seq()),
            student=student,
            entry_date=entry_date,
            entry_type=entry_type,
            amount=bcalc.ledger_amount(entry_type, magnitude),
            source_field=source_field,
            source=source,
            memo=memo,
        )
        self.ledger.append(entry)
        student.ledger.append(entry)
        return entry

    def post_line_ledger(self, line: Line) -> Ledger:
        entry_type = ENTRY_TYPE_FOR_LINE[line.line_type]
        return self._ledger(
            line.invoice.student,
            line.posted_date,
            entry_type,
            abs(line.net_amount),
            "invoice_line",
            line,
            lambda: line.description_text()[:120],
        )

    def post_scholarship_ledger(self, alloc: SchAlloc) -> Ledger:
        entry_type = SCHOLARSHIP_ENTRY_TYPE[alloc.allocation_type]
        return self._ledger(
            alloc.scholarship.student,
            alloc.allocated_on,
            entry_type,
            abs(alloc.amount),
            "scholarship_allocation",
            alloc,
            lambda: f"{alloc.allocation_type} {alloc.pk} on {alloc.line.pk}"[:120],
        )

    def post_payment_ledger(self, payment: Payment) -> Ledger:
        return self._ledger(
            payment.student,
            payment.received_date,
            "PAYMENT",
            payment.amount,
            "payment",
            payment,
            lambda: f"Payment {payment.pk} from {payment.payer_type} ({payment.method})"[:120],
        )

    def post_refund_ledger(self, refund: Refund) -> Ledger:
        return self._ledger(
            refund.student,
            refund.processed_date,
            "REFUND",
            refund.amount,
            "refund",
            refund,
            lambda: f"Refund {refund.pk} of {refund.payment.pk} to {refund.payer_type}"[:120],
        )

    def inv_event(
        self, invoice, *, dimension, from_status, to_status, event_date, reason, source_ref=None
    ):
        return self._event(
            self.invoice_events,
            "invoice",
            invoice,
            "INV",
            dimension=dimension,
            from_status=from_status,
            to_status=to_status,
            event_date=event_date,
            reason=reason,
            source_ref=source_ref,
        )

    @staticmethod
    def current_amounts(invoice, *, as_of: date | None = None) -> bcalc.InvoiceAmounts:
        lines = invoice.lines
        sch, pay = invoice.sch_allocs, invoice.pay_allocs
        if as_of is not None:
            lines = [x for x in lines if x.posted_date <= as_of]
            sch = [a for a in sch if a.allocated_on <= as_of]
            pay = [a for a in pay if a.allocated_on <= as_of]
        return bcalc.invoice_amounts(lines, sch, pay)

    def set_collection_status(self, invoice, *, as_of, reason, source_ref=None):
        new = bcalc.collection_status(
            self.current_amounts(invoice), due_date=invoice.due_date, as_of=as_of
        )
        old = invoice.collection_status
        if new != old:
            self.inv_event(
                invoice,
                dimension="COLLECTION",
                from_status=old,
                to_status=new,
                event_date=as_of,
                reason=reason,
                source_ref=source_ref,
            )
        invoice.collection_status = new
        invoice.collection_status_as_of = as_of
        return old, new

    def recalculate_collection_status(self, invoice, *, as_of, reason, source_ref=None):
        effective = max(as_of, invoice.collection_status_as_of)
        return self.set_collection_status(
            invoice, as_of=effective, reason=reason, source_ref=source_ref
        )

    # --- master data (academics / billing / scholarships create_* services) -------------------

    def create_program(self, **fields) -> Program:
        n = self.take("PRG")
        program = Program(pk=ids.program_id(n), **fields)
        self.programs.append(program)
        self.log("create_program", rec=program, fields=dict(fields))
        return program

    def create_term(self, spec) -> Term:
        fields = spec.as_dict()
        term = Term(**fields)
        term.pk = fields["term_id"]
        self.terms[term.pk] = term
        self.log("create_term", rec=term, fields=fields)
        return term

    def create_fee_schedule(
        self, *, program, term, fee_type, residency, charge_basis, amount, effective_from, base=None
    ) -> FeeSchedule:
        flags = bpolicy.fee_flags(fee_type)
        version = 1 if base is None else base.version + 1
        schedule = FeeSchedule(
            pk=ids.fee_schedule_id(program.pk, term.pk, fee_type, residency, version),
            program=program,
            term=term,
            fee_type=fee_type,
            residency=residency,
            charge_basis=charge_basis,
            amount=round_money(amount),
            is_scholarship_eligible=flags.is_scholarship_eligible,
            is_refundable=flags.is_refundable,
            is_waivable=flags.is_waivable,
            allocation_priority=flags.allocation_priority,
            version=version,
            effective_from=effective_from,
            supersedes=base,
        )
        schedule.fee_schedule_id = schedule.pk
        self.fee_schedules.append(schedule)
        self._fees_by_program_term[(program.pk, term.pk)].append(schedule)
        if base is None:
            self.log("create_fee_schedule", rec=schedule)
        else:
            self.log("new_fee_schedule_version", rec=schedule, base=base)
        return schedule

    def create_scholarship_type(self, *, spec, effective_from, program_ids) -> SType:
        n = self.take("SCT")
        fields = {
            f: getattr(spec, f) for f in spec.__dataclass_fields__ if f != "scholarship_type_id"
        }
        stype = SType(
            pk=ids.scholarship_type_id(n),
            effective_from=effective_from,
            effective_to=None,
            program_ids=frozenset(program_ids),
            **fields,
        )
        stype.scholarship_type_id = stype.pk
        stype.award_value = round_money(stype.award_value)
        stype.max_amount_per_term = round_money(stype.max_amount_per_term)
        self.types.append(stype)
        for program_id in sorted(program_ids):
            self.bridges.append(
                Bridge(
                    pk=ids.scholarship_type_program_id(stype.pk, program_id),
                    scholarship_type=stype,
                    program_id=program_id,
                )
            )
        self.log(
            "create_scholarship_type",
            rec=stype,
            fields=fields,
            effective_from=effective_from,
            program_ids=sorted(program_ids),
        )
        return stype

    # --- students -----------------------------------------------------------------------------

    def create_student(self, *, event_date, **fields) -> Student:
        n = self.take("STU")
        student = Student(
            pk=ids.student_id(n),
            number=n,
            status="ACTIVE",
            events=[],
            ledger=[],
            invoices=[],
            payments=[],
            refunds=[],
            enrollments={},
            **fields,
        )
        student.email = f"stu{n:06d}@example.edu"
        self.students.append(student)
        self._immediate_event(
            self.student_events,
            "student",
            student,
            "STU",
            from_status=None,
            to_status="ACTIVE",
            event_date=event_date,
            reason="CREATED",
        )
        self.log("create_student", rec=student, event_date=event_date)
        return student

    def change_status(self, student, *, to_status, event_date, reason):
        previous = student.status
        student.status = to_status
        self._immediate_event(
            self.student_events,
            "student",
            student,
            "STU",
            from_status=previous,
            to_status=to_status,
            event_date=event_date,
            reason=reason,
        )
        self.log(
            "change_status",
            student=student,
            to_status=to_status,
            event_date=event_date,
            reason=reason,
        )

    # --- enrollments (academics.services) -----------------------------------------------------

    def enroll(self, *, student, term, credits, enrolled_on) -> Enrollment:
        n = self.take(ids.sequence_key("enrollment", year=term.year))
        enrollment = Enrollment(
            pk=ids.enrollment_id(term.year, n),
            student=student,
            program=student.program,
            term=term,
            credits=credits,
            status=ENROLLED,
            enrolled_on=enrolled_on,
            withdrawal_date=None,
            cancellation_date=None,
            events=[],
            invoices=[],
        )
        self.enrollments.append(enrollment)
        student.enrollments[term.pk] = enrollment
        self._immediate_event(
            self.enrollment_events,
            "enrollment",
            enrollment,
            "ENR",
            from_status=None,
            to_status=ENROLLED,
            event_date=enrolled_on,
            reason="ENROLLED",
        )
        self.log(
            "enroll",
            rec=enrollment,
            student=student,
            term=term,
            credits=credits,
            enrolled_on=enrolled_on,
        )
        return enrollment

    def complete_enrollment(self, enrollment, *, on_date):
        if enrollment.status != ENROLLED or on_date <= enrollment.term.end_date:
            raise GenerationError(f"cannot complete {enrollment.pk}")
        previous = enrollment.status
        enrollment.status = COMPLETED
        self._immediate_event(
            self.enrollment_events,
            "enrollment",
            enrollment,
            "ENR",
            from_status=previous,
            to_status=COMPLETED,
            event_date=on_date,
            reason="TERM_ENDED",
        )
        self.log("complete_enrollment", enrollment=enrollment, on_date=on_date)

    # --- invoices (billing.services.generate_invoice) -----------------------------------------

    def resolve_fees(self, enrollment, on_date):
        student = enrollment.student
        has_lab = enrollment.program.has_lab
        groups = defaultdict(list)
        for row in self._fees_by_program_term[(enrollment.program.pk, enrollment.term.pk)]:
            groups[(row.fee_type, row.residency)].append(row)
        required = set(bpolicy.required_fee_types(has_lab=has_lab, residency=student.residency))
        rows, missing = [], []
        for fee_type in bpolicy.applicable_fee_types(has_lab=has_lab, residency=student.residency):
            residency = (
                student.residency
                if fee_type == bpolicy.TUITION
                else bpolicy.ALLOWED_RESIDENCIES[fee_type][0]
            )
            try:
                rows.append(
                    bcalc.resolve_fee_version(groups.get((fee_type, residency), ()), on_date)
                )
            except LookupError:
                if fee_type in required:
                    missing.append(fee_type)
        if missing:
            raise GenerationError(f"{enrollment.pk}: no fee schedule for {missing}")
        return rows

    def generate_invoice(self, *, enrollment, waivers=()) -> Invoice:
        if enrollment.status != ENROLLED:
            raise GenerationError(f"{enrollment.pk} is {enrollment.status}")
        if any(i.lifecycle_status != "VOID" for i in enrollment.invoices):
            raise GenerationError(f"{enrollment.pk} already has an invoice")
        term, student = enrollment.term, enrollment.student
        priced = bcalc.price_lines(enrollment, self.resolve_fees(enrollment, term.invoice_date))
        priced = bcalc.apply_waivers(priced, waivers)
        issue_date = max(term.invoice_date, enrollment.enrolled_on)
        invoice = self.new_invoice(
            student=student,
            enrollment=enrollment,
            term=term,
            issue_date=issue_date,
            due_date=term.due_date,
            total_amount=dsum(p.net_amount for p in priced),
        )
        for p in priced:
            line = self.new_line(
                invoice,
                line_type="FEE",
                fee_schedule=self._fee_by_id(p.fee_schedule_id),
                fee_type=p.fee_type,
                description=p.description,
                quantity=p.quantity,
                unit_amount=p.unit_amount,
                gross_amount=p.gross_amount,
                waiver_amount=p.waiver_amount,
                waiver_reason=p.waiver_reason,
                net_amount=p.net_amount,
                is_scholarship_eligible=p.is_scholarship_eligible,
                is_refundable=p.is_refundable,
                is_waivable=p.is_waivable,
                allocation_priority=p.allocation_priority,
                posted_date=issue_date,
            )
            self.post_line_ledger(line)
        for dimension, status in (("LIFECYCLE", "ISSUED"), ("COLLECTION", "UNPAID")):
            self.inv_event(
                invoice,
                dimension=dimension,
                from_status=None,
                to_status=status,
                event_date=issue_date,
                reason="CREATED",
                source_ref=enrollment,
            )
        self.set_collection_status(invoice, as_of=issue_date, reason="RECALCULATED")
        self.log(
            "generate_invoice",
            rec=invoice,
            enrollment=enrollment,
            waivers=[dict(w) for w in waivers],
        )
        return invoice

    def _fee_by_id(self, fee_schedule_id):
        if not hasattr(self, "_fee_index"):
            self._fee_index = {}
        if fee_schedule_id not in self._fee_index:
            self._fee_index = {f.pk: f for f in self.fee_schedules}
        return self._fee_index[fee_schedule_id]

    def new_invoice(
        self,
        *,
        student,
        enrollment,
        term,
        issue_date,
        due_date,
        total_amount,
        lifecycle_status="ISSUED",
        collection_status="UNPAID",
    ):
        invoice = Invoice(
            key=(student.number, issue_date.year, issue_date, self.seq()),
            student=student,
            enrollment=enrollment,
            term=term,
            issue_date=issue_date,
            due_date=due_date,
            total_amount=total_amount,
            lifecycle_status=lifecycle_status,
            collection_status=collection_status,
            collection_status_as_of=issue_date,
            currency=bpolicy.CURRENCY,
            lines=[],
            sch_allocs=[],
            pay_allocs=[],
            events=[],
            scholarships=[],
        )
        self.invoices.append(invoice)
        student.invoices.append(invoice)
        enrollment.invoices.append(invoice)
        return invoice

    def new_line(
        self,
        invoice,
        *,
        posted_date,
        adjusts=None,
        adjustment_reason=None,
        fee_schedule=None,
        **fields,
    ) -> Line:
        line = Line(
            key=(invoice.key, posted_date, self.seq()),
            invoice=invoice,
            posted_date=posted_date,
            adjusts=adjusts,
            adjustment_reason=adjustment_reason,
            fee_schedule=fee_schedule,
            pay_allocs=[],
            sch_allocs=[],
            **fields,
        )
        self.lines.append(line)
        invoice.lines.append(line)
        return line

    # --- scholarships (scholarships.services) -------------------------------------------------

    def evaluate_eligibility(self, *, student, term) -> list[Eligibility]:
        evaluated_on = term.invoice_date
        invoice = self.current_invoice(student, term)
        lines = list(invoice.lines) if invoice else None
        rows = []
        for stype in sorted(self.types, key=lambda t: (t.priority, t.pk)):
            result = scalc.evaluate(student, term, stype)
            n = self.take(ids.sequence_key("eligibility", year=evaluated_on.year))
            row = Eligibility(
                pk=ids.eligibility_id(evaluated_on.year, n),
                student=student,
                scholarship_type=stype,
                term=term,
                evaluated_on=evaluated_on,
                is_eligible=result.is_eligible,
                reason_codes=list(result.reason_codes),
                inputs_snapshot=result.inputs_snapshot,
                eligible_fee_base=scalc.eligible_fee_base(stype, lines)
                if lines is not None
                else None,
                scholarship=None,
            )
            self.eligibility.append(row)
            rows.append(row)
        self.log("evaluate_eligibility", student=student, term=term, recs=list(rows))
        return rows

    @staticmethod
    def current_invoice(student, term):
        found = [i for i in student.invoices if i.term is term and i.lifecycle_status != "VOID"]
        return min(found, key=lambda i: i.key) if found else None

    @staticmethod
    def applied_amount(scholarship):
        return dsum(a.amount for a in scholarship.allocations)

    def reserved_on_invoice(self, invoice, *, exclude=None):
        total = ZERO
        for award in invoice.scholarships:
            if award.status == REVOKED or award is exclude:
                continue
            total += (
                award.awarded_amount if award.status in UNAPPLIED else self.applied_amount(award)
            )
        return round_money(total)

    def award_conflict(self, eligibility) -> str | None:
        """Why ``award_scholarship`` would refuse this eligibility (None if it would not)."""
        stype, student, term = eligibility.scholarship_type, eligibility.student, eligibility.term
        if not eligibility.is_eligible:
            return "NOT_ELIGIBLE"
        if eligibility.scholarship is not None:
            return "USED"
        invoice = self.current_invoice(student, term)
        if invoice is None or invoice.lifecycle_status != "ISSUED":
            return "INVOICE"
        active = [s for s in student_scholarships(student, term) if s.status != REVOKED]
        if active and stype.coverage_scope == spolicy.ALL_FEE_LINES:
            return "BR-SCH-08"
        if any(s.scholarship_type.coverage_scope == spolicy.ALL_FEE_LINES for s in active):
            return "BR-SCH-08"
        if stype.exclusive_group and any(
            s.scholarship_type.exclusive_group == stype.exclusive_group for s in active
        ):
            return "BR-SCH-05"
        return None

    def award_amount(self, eligibility, invoice) -> Decimal:
        stype, student, term = eligibility.scholarship_type, eligibility.student, eligibility.term
        active = [s for s in student_scholarships(student, term) if s.status != REVOKED]
        base = scalc.eligible_fee_base(stype, invoice.lines)
        already = dsum(s.awarded_amount for s in active if s.scholarship_type is stype)
        computed = scalc.award_amount(stype, base, already)
        available = round_money(max(ZERO, base - self.reserved_on_invoice(invoice)))
        return min(computed, available)

    def award_scholarship(self, *, eligibility, invoice) -> Scholarship:
        problem = self.award_conflict(eligibility)
        if problem:
            raise GenerationError(f"award refused: {problem}")
        amount = self.award_amount(eligibility, invoice)
        if amount <= 0:
            raise GenerationError("award refused: maximum award is 0.00")
        award_date = max(eligibility.evaluated_on, invoice.issue_date)
        scholarship = Scholarship(
            key=(award_date.year, award_date, self.seq()),
            student=eligibility.student,
            scholarship_type=eligibility.scholarship_type,
            term=eligibility.term,
            eligibility=eligibility,
            invoice=invoice,
            awarded_amount=amount,
            award_date=award_date,
            status="OFFERED",
            allocations=[],
            events=[],
        )
        eligibility.scholarship = scholarship
        invoice.scholarships.append(scholarship)
        self.scholarships.append(scholarship)
        self.sch_event(
            scholarship,
            from_status=None,
            to_status="OFFERED",
            event_date=award_date,
            reason="AWARDED",
            source_ref=eligibility,
        )
        self.log("award_scholarship", rec=scholarship, eligibility=eligibility, invoice=invoice)
        return scholarship

    def sch_event(self, scholarship, **kw):
        return self._event(self.scholarship_events, "scholarship", scholarship, "SCH", **kw)

    @staticmethod
    def latest_invoice_date(invoice) -> date:
        dates = [invoice.issue_date, invoice.collection_status_as_of]
        dates += [e.event_date for e in invoice.events] + [x.posted_date for x in invoice.lines]
        return max(dates)

    def apply_scholarship(self, scholarship) -> None:
        status = scholarship.status
        if status not in ("OFFERED", "ACCEPTED"):
            raise GenerationError(f"cannot apply a {status} award")
        invoice = scholarship.invoice
        if invoice.lifecycle_status != "ISSUED":
            raise GenerationError("scholarships apply only to ISSUED invoices")
        allocated_on = max(
            scholarship.award_date,
            self.latest_invoice_date(invoice),
            max((e.event_date for e in scholarship.events), default=scholarship.award_date),
        )
        stype = scholarship.scholarship_type
        lines = invoice.lines
        existing = invoice.sch_allocs
        amount = round_money(scholarship.awarded_amount - self.applied_amount(scholarship))
        base = scalc.eligible_fee_base(stype, lines)
        if dsum(a.amount for a in existing) + amount > base:
            raise GenerationError("BR-SCH-04 would be broken by the application")
        adjustments: dict = {}
        for line in lines:
            if line.line_type == "ADJUSTMENT":
                adjustments[line.adjusts_line_id] = (
                    adjustments.get(line.adjusts_line_id, ZERO) + line.net_amount
                )
        plan = scalc.plan_application(
            stype,
            lines,
            amount,
            adjustments=adjustments,
            scholarship_allocated=scalc.net_by_line(existing),
            payment_allocated=scalc.net_by_line(invoice.pay_allocs),
        )
        if any(p.payment_to_release > 0 for p in plan):
            # BR-ALC-06 is not exercised by the baseline timeline (awards precede payments)
            raise GenerationError("scholarship precedence release is not part of the baseline")
        line_by_key = {x.key: x for x in lines}
        for p in plan:
            self.new_sch_alloc(
                scholarship,
                line_by_key[p.line_id],
                allocation_type=spolicy.APPLY,
                amount=p.amount,
                reverses=None,
                reason=spolicy.AWARD,
                allocated_on=allocated_on,
            )
        if status == "OFFERED":
            self.sch_event(
                scholarship,
                from_status="OFFERED",
                to_status="ACCEPTED",
                event_date=allocated_on,
                reason="ACCEPTED_ON_APPLY",
            )
        self.sch_event(
            scholarship,
            from_status="ACCEPTED",
            to_status=APPLIED,
            event_date=allocated_on,
            reason="APPLIED",
            source_ref=invoice,
        )
        scholarship.status = APPLIED
        self.recalculate_collection_status(
            invoice, as_of=allocated_on, reason="SCHOLARSHIP_APPLIED", source_ref=scholarship
        )
        self.log("apply_scholarship", scholarship=scholarship)

    def new_sch_alloc(self, scholarship, line, **fields) -> SchAlloc:
        alloc = SchAlloc(
            key=(scholarship.key, fields["allocated_on"], self.seq()),
            scholarship=scholarship,
            line=line,
            **fields,
        )
        self.scholarship_allocations.append(alloc)
        scholarship.allocations.append(alloc)
        line.sch_allocs.append(alloc)
        line.invoice.sch_allocs.append(alloc)
        self.post_scholarship_ledger(alloc)
        return alloc

    def append_reversal(self, original, *, amount, reason, on_date):
        scholarship = original.scholarship
        reversal = self.new_sch_alloc(
            scholarship,
            original.line,
            allocation_type=spolicy.REVERSE,
            amount=-round_money(amount),
            reverses=original,
            reason=reason,
            allocated_on=on_date,
        )
        if scholarship.status == APPLIED:
            self.sch_event(
                scholarship,
                from_status=APPLIED,
                to_status=PARTIALLY_REVERSED,
                event_date=on_date,
                reason=reason,
                source_ref=reversal,
            )
            scholarship.status = PARTIALLY_REVERSED
        return reversal

    def reverse_for_adjustment(self, *, line, amount, reason, on_date):
        amount = round_money(amount)
        if amount <= 0:
            return []
        rows = line.sch_allocs
        reversed_by: dict = {}
        for r in rows:
            if r.allocation_type == spolicy.REVERSE:
                reversed_by[r.reverses_id] = reversed_by.get(r.reverses_id, ZERO) - r.amount
        remaining = {
            r.allocation_id: r.amount - reversed_by.get(r.allocation_id, ZERO)
            for r in rows
            if r.allocation_type == spolicy.APPLY
        }
        shares = scalc.split_reversal(remaining, amount)
        by_id = {r.allocation_id: r for r in rows}
        for allocation_id in sorted(shares):
            if on_date < by_id[allocation_id].allocated_on:
                raise GenerationError("a reversal cannot predate its allocation")
        return [
            self.append_reversal(by_id[a], amount=shares[a], reason=reason, on_date=on_date)
            for a in sorted(shares)
        ]

    # --- payments (payments.services) ---------------------------------------------------------

    def record_payment(
        self,
        *,
        student,
        amount,
        received_date,
        method,
        payer_type,
        external_reference,
        payer_name=None,
    ):
        first_issue = min((i.issue_date for i in student.invoices), default=None)
        if first_issue is None or received_date < first_issue:
            raise GenerationError(f"{student.pk}: payment before the first invoice")
        n = self.take(ids.sequence_key("payment", year=received_date.year))
        payment = Payment(
            pk=ids.payment_id(received_date.year, n),
            student=student,
            amount=round_money(amount),
            currency=ppolicy.CURRENCY,
            received_date=received_date,
            method=method,
            payer_type=payer_type,
            payer_name=payer_name or None,
            external_reference=external_reference,
            status=ppolicy.POSTED,
            allocations=[],
            refunds=[],
        )
        self.payments.append(payment)
        student.payments.append(payment)
        self.post_payment_ledger(payment)
        rows = self._allocate_auto(payment, allocated_on=received_date)
        self.log("record_payment", rec=payment, student=student)
        return payment, rows

    def available(self, payment) -> Decimal:
        return pcalc.payment_available(payment.amount, payment.allocations, payment.refunds)

    def student_open_lines(self, student, *, allocated_on) -> list[_OpenLine]:
        out = []
        for invoice in sorted(student.invoices, key=lambda i: i.key):
            if invoice.lifecycle_status not in ppolicy.AUTO_INVOICE_LIFECYCLES:
                continue
            opens = bcalc.line_open_amounts(invoice.lines, invoice.sch_allocs, invoice.pay_allocs)
            for line in invoice.lines:
                if (
                    line.line_type in ppolicy.ALLOCATABLE_LINE_TYPES
                    and line.posted_date <= allocated_on
                ):
                    out.append(
                        _OpenLine(
                            line.key,
                            invoice,
                            invoice.due_date,
                            line.allocation_priority,
                            opens[line.key],
                        )
                    )
        return out

    def student_open_total(self, student, *, on) -> Decimal:
        return dsum(x.open_amount for x in self.student_open_lines(student, allocated_on=on))

    def _allocate_auto(self, payment, *, allocated_on):
        funds = self.available(payment)
        if funds <= 0:
            return []
        lines = [
            x
            for x in self.student_open_lines(payment.student, allocated_on=allocated_on)
            if x.open_amount > 0
        ]
        if not lines:
            return []
        plan = pcalc.plan_allocation(funds, lines)
        if len(payment.allocations) + len(plan) > ppolicy.MAX_ALLOCATIONS_PER_PAYMENT:
            raise GenerationError(f"{payment.pk}: more than 99 allocation rows")
        by_key = {x.line_id: x for x in lines}
        line_index = {ln.key: ln for inv in payment.student.invoices for ln in inv.lines}
        rows = [
            self.new_pay_alloc(
                payment,
                line_index[p.line_id],
                amount=p.amount,
                rule=ppolicy.AUTO_OLDEST_FIRST,
                allocated_on=allocated_on,
            )
            for p in plan
        ]
        touched = {by_key[p.line_id].invoice.key: by_key[p.line_id].invoice for p in plan}
        for key in sorted(touched):
            self.recalculate_collection_status(
                touched[key], as_of=allocated_on, reason="PAYMENT_ALLOCATED", source_ref=payment
            )
        return rows

    def new_pay_alloc(self, payment, line, *, amount, rule, allocated_on, reverses=None):
        deallocate = reverses is not None
        alloc = PayAlloc(
            key=(payment.pk, allocated_on, self.seq()),
            payment=payment,
            line=line,
            allocation_type=ppolicy.DEALLOCATE if deallocate else ppolicy.ALLOCATE,
            amount=-round_money(amount) if deallocate else round_money(amount),
            reverses=reverses,
            rule_applied=rule,
            allocated_on=allocated_on,
        )
        self.payment_allocations.append(alloc)
        payment.allocations.append(alloc)
        line.pay_allocs.append(alloc)
        line.invoice.pay_allocs.append(alloc)
        return alloc

    # --- late fees and collection refresh (billing.services) ----------------------------------

    def assess_late_fees(self, *, as_of) -> int:
        posted_count = 0
        for invoice in sorted(self.invoices, key=lambda i: i.key):
            if invoice.lifecycle_status != "ISSUED":
                continue
            if any(x.line_type == "LATE_FEE" for x in invoice.lines):
                continue
            grace = invoice.term.late_fee_grace_days
            posted = bcalc.late_fee_date(invoice.due_date, grace)
            if posted > as_of:
                continue
            cutoff = posted - timedelta(days=1)
            outstanding = self.current_amounts(invoice, as_of=cutoff).outstanding
            fee = bcalc.late_fee(
                outstanding, invoice.due_date, posted, bpolicy.LateFeePolicy(grace_days=grace)
            )
            if fee <= 0:
                continue
            line = self.new_line(
                invoice,
                line_type="LATE_FEE",
                fee_type=bpolicy.LATE_FEE,
                description=f"Late fee: 2% of {format_money(outstanding)} outstanding",
                quantity=Decimal(1),
                unit_amount=fee,
                gross_amount=fee,
                waiver_amount=ZERO,
                waiver_reason=None,
                net_amount=fee,
                is_scholarship_eligible=False,
                is_refundable=False,
                is_waivable=False,
                allocation_priority=bpolicy.LATE_FEE_PRIORITY,
                posted_date=posted,
            )
            self.post_line_ledger(line)
            self.set_collection_status(
                invoice,
                as_of=max(posted, invoice.collection_status_as_of),
                reason="LATE_FEE_POSTED",
                source_ref=line,
            )
            posted_count += 1
        self.log("assess_late_fees", as_of=as_of)
        return posted_count

    def refresh_collection_status(self, *, as_of) -> None:
        for invoice in sorted(self.invoices, key=lambda i: i.key):
            if invoice.lifecycle_status == "VOID" or invoice.collection_status_as_of > as_of:
                continue
            target = bcalc.collection_status(
                self.current_amounts(invoice), due_date=invoice.due_date, as_of=as_of
            )
            reason = "DUE_DATE_PASSED" if target == "OVERDUE" else "RECALCULATED"
            self.set_collection_status(invoice, as_of=as_of, reason=reason)
        self.log("refresh_collection_status", as_of=as_of)

    # --- withdrawals and cancellations (academics.services + billing.withdrawals) ------------

    def exit_enrollment(self, enrollment, *, kind, on_date):
        term = enrollment.term
        if enrollment.status != ENROLLED:
            raise GenerationError(f"{enrollment.pk} is {enrollment.status}")
        if kind == "WITHDRAWAL":
            if not term.start_date <= on_date <= term.end_date or on_date < enrollment.enrolled_on:
                raise GenerationError(f"bad withdrawal date {on_date}")
            target, pct, field = WITHDRAWN, withdrawal_refund_pct(term, on_date), "withdrawal_date"
        else:
            if on_date >= term.start_date or on_date < enrollment.enrolled_on:
                raise GenerationError(f"bad cancellation date {on_date}")
            target, pct, field = ENR_CANCELLED, None, "cancellation_date"
        previous = enrollment.status
        enrollment.status = target
        setattr(enrollment, field, on_date)
        self._immediate_event(
            self.enrollment_events,
            "enrollment",
            enrollment,
            "ENR",
            from_status=previous,
            to_status=target,
            event_date=on_date,
            reason=kind,
        )
        self._exit_handler(enrollment, kind=kind, on_date=on_date, pct=pct)
        self.log("exit_enrollment", enrollment=enrollment, kind=kind, on_date=on_date)

    def _exit_handler(self, enrollment, *, kind, on_date, pct):
        found = [i for i in enrollment.invoices if i.lifecycle_status != "VOID"]
        if not found:
            return
        invoice = min(found, key=lambda i: i.key)
        if kind == "CANCELLATION" and invoice.lifecycle_status != "ISSUED":
            raise GenerationError(f"{invoice} cannot be cancelled")
        lines = list(invoice.lines)
        if kind == "WITHDRAWAL":
            adjustments = bcalc.withdrawal_adjustments(lines, pct)
        else:
            adjustments = bcalc.cancellation_adjustments(lines)
        by_key = {x.key: x for x in lines}
        if any(by_key[a.adjusts_line_id].posted_date > on_date for a in adjustments):
            raise GenerationError("an adjustment cannot predate the line it adjusts")
        released: dict = defaultdict(lambda: ZERO)
        for r in invoice.pay_allocs:
            if r.allocation_type == "DEALLOCATE":
                released[r.reverses_id] += r.amount
        open_payments: dict = defaultdict(list)
        for r in invoice.pay_allocs:
            if r.allocation_type == "ALLOCATE":
                left = r.amount + released[r.allocation_id]
                if left > 0:
                    open_payments[r.invoice_line_id].append(
                        _OpenAllocation(r.allocation_id, r.allocated_on, left)
                    )
        plans = []
        for adj in adjustments:
            held = dsum(
                a.amount for a in invoice.sch_allocs if a.invoice_line_id == adj.adjusts_line_id
            )
            plan = bcalc.release_for_adjustment(
                line_net=adj.line_net,
                adjustment=adj.amount,
                scholarship_allocated=held,
                payment_allocations=open_payments.get(adj.adjusts_line_id, []),
            )
            plans.append((adj, plan))
        alloc_index = {a.key: a for a in invoice.pay_allocs}
        for _, plan in plans:
            for allocation_id, amount in plan.deallocations:
                if amount <= 0:
                    continue
                original = alloc_index[allocation_id]
                if on_date < original.allocated_on:
                    raise GenerationError("a release cannot predate its allocation")
                self.new_pay_alloc(
                    original.payment,
                    original.line,
                    amount=amount,
                    rule=RELEASE_RULE[kind],
                    allocated_on=on_date,
                    reverses=original,
                )
        for adj, plan in plans:
            target = by_key[adj.adjusts_line_id]
            text = partial(_exit_description, kind, target, pct)
            line = self.new_line(
                invoice,
                line_type="ADJUSTMENT",
                fee_type=adj.fee_type,
                description=text,
                quantity=Decimal(1),
                unit_amount=adj.amount,
                gross_amount=adj.amount,
                waiver_amount=ZERO,
                waiver_reason=None,
                net_amount=adj.amount,
                adjusts=target,
                adjustment_reason=kind,
                is_scholarship_eligible=False,
                is_refundable=False,
                is_waivable=False,
                allocation_priority=adj.allocation_priority,
                posted_date=on_date,
            )
            self.post_line_ledger(line)
            self.reverse_for_adjustment(
                line=target, amount=plan.scholarship_reversal, reason=kind, on_date=on_date
            )
        if kind == "CANCELLATION":
            self.inv_event(
                invoice,
                dimension="LIFECYCLE",
                from_status=invoice.lifecycle_status,
                to_status="CANCELLED",
                event_date=on_date,
                reason="ENROLLMENT_CANCELLED",
                source_ref=enrollment,
            )
            invoice.lifecycle_status = "CANCELLED"
        self.recalculate_collection_status(
            invoice, as_of=on_date, reason=kind, source_ref=enrollment
        )

    # --- refunds (payments.services) ----------------------------------------------------------

    def refund_caps(self, payment, *, exclude=None) -> pcalc.RefundCaps:
        student = payment.student
        exclude_key = exclude.key if exclude is not None else None
        return pcalc.refund_caps(
            payment_amounts=[p.amount for p in student.payments],
            student_refunds=student.refunds,
            ledger_balance=bcalc.ledger_balance(student.ledger, None),
            source_amount=payment.amount,
            source_allocations=payment.allocations,
            source_refunds=payment.refunds,
            exclude_refund_id=exclude_key,
        )

    @staticmethod
    def caps_allow(amount, caps) -> bool:
        return (
            amount <= caps.net_paid
            and amount <= caps.credit_available
            and amount <= caps.payment_available
        )

    def request_refund(self, *, payment, amount, requested_date, reason, enrollment=None):
        amount = round_money(amount)
        if requested_date < payment.received_date:
            raise GenerationError("refund requested before the payment was received")
        schedule_pct = None
        if reason == "WITHDRAWAL":
            schedule_pct = withdrawal_refund_pct(enrollment.term, enrollment.withdrawal_date)
        if reason in REFUND_ENROLLMENT_STATUS and (
            enrollment is None or enrollment.status != REFUND_ENROLLMENT_STATUS[reason]
        ):
            raise GenerationError("refund enrollment status mismatch")
        caps = self.refund_caps(payment)
        if not self.caps_allow(amount, caps):
            raise GenerationError(f"refund {amount} breaks the BR-REF caps")
        refund = Refund(
            key=(requested_date.year, requested_date, self.seq()),
            student=payment.student,
            payment=payment,
            payer_type=payment.payer_type,
            enrollment=enrollment,
            reason=reason,
            amount=amount,
            requested_date=requested_date,
            processed_date=None,
            status=ppolicy.REQUESTED,
            calculation=refund_snapshot(caps, schedule_pct),
            events=[],
        )
        self.refunds.append(refund)
        payment.refunds.append(refund)
        payment.student.refunds.append(refund)
        self.refund_event(
            refund,
            from_status=None,
            to_status=ppolicy.REQUESTED,
            event_date=requested_date,
            reason=ppolicy.REQUESTED,
            source_ref=payment,
        )
        self.log(
            "request_refund",
            rec=refund,
            payment=payment,
            amount=amount,
            requested_date=requested_date,
            reason=reason,
            enrollment=enrollment,
        )
        return refund

    def refund_event(self, refund, **kw):
        return self._event(self.refund_events, "refund", refund, "REF", **kw)

    def advance_refund(self, refund, *, target, on_date) -> bool:
        """approve_refund / process_refund; a cap breach at that moment rejects instead (the
        services would answer 422, and an operator would then reject the request)."""
        last = max([refund.requested_date] + [e.event_date for e in refund.events])
        if on_date < last:
            raise GenerationError("a refund status date cannot go back")
        caps = self.refund_caps(refund.payment, exclude=refund)
        if not self.caps_allow(refund.amount, caps):
            target = ppolicy.REJECTED
        self.refund_event(
            refund, from_status=refund.status, to_status=target, event_date=on_date, reason=target
        )
        refund.status = target
        if target == ppolicy.PROCESSED:
            refund.processed_date = on_date
            self.post_refund_ledger(refund)
        self.log("advance_refund", refund=refund, target=target, on_date=on_date)
        return target != ppolicy.REJECTED

    # --- snapshots (reconciliation.services.take_snapshot) ------------------------------------

    def take_snapshots(self, *, as_of_date) -> None:
        taken = {(s.student.pk, s.as_of_date) for s in self.snapshots}
        for student in sorted(self.students, key=lambda s: s.pk):
            if (student.pk, as_of_date) in taken:
                continue
            totals = rcalc.snapshot_totals(
                [e for e in student.ledger if e.entry_date <= as_of_date], as_of_date
            )
            n = self.take(ids.sequence_key("balance_snapshot", year=as_of_date.year))
            self.snapshots.append(
                Snapshot(
                    pk=ids.balance_snapshot_id(as_of_date.year, n),
                    student=student,
                    as_of_date=as_of_date,
                    reported_balance=totals.balance,
                    total_debits=totals.debits,
                    total_credits=totals.credits,
                )
            )
        self.log("take_snapshots", as_of_date=as_of_date)

    # --- raw writes for the anomaly injector (no service rule applies) ------------------------

    def inject_award(self, *, eligibility, invoice, amount, award_date) -> Scholarship:
        scholarship = Scholarship(
            key=(award_date.year, award_date, self.seq()),
            student=eligibility.student,
            scholarship_type=eligibility.scholarship_type,
            term=eligibility.term,
            eligibility=eligibility,
            invoice=invoice,
            awarded_amount=round_money(amount),
            award_date=award_date,
            status="ACCEPTED",
            allocations=[],
            events=[],
        )
        eligibility.scholarship = scholarship
        invoice.scholarships.append(scholarship)
        self.scholarships.append(scholarship)
        self.sch_event(
            scholarship,
            from_status=None,
            to_status="OFFERED",
            event_date=award_date,
            reason="AWARDED",
            source_ref=eligibility,
        )
        self.sch_event(
            scholarship,
            from_status="OFFERED",
            to_status="ACCEPTED",
            event_date=award_date,
            reason="ACCEPTED_ON_APPLY",
        )
        return scholarship

    def inject_duplicate_invoice(self, original) -> Invoice:
        dup = self.new_invoice(
            student=original.student,
            enrollment=original.enrollment,
            term=original.term,
            issue_date=original.issue_date,
            due_date=original.due_date,
            total_amount=original.total_amount,
        )
        copied = (
            "line_type",
            "fee_type",
            "description",
            "quantity",
            "unit_amount",
            "gross_amount",
            "waiver_amount",
            "waiver_reason",
            "net_amount",
            "is_scholarship_eligible",
            "is_refundable",
            "is_waivable",
            "allocation_priority",
        )
        for line in sorted(original.lines, key=lambda x: x.key):
            if line.line_type != "FEE":
                continue
            new = self.new_line(
                dup,
                posted_date=line.posted_date,
                fee_schedule=line.fee_schedule,
                **{f: getattr(line, f) for f in copied},
            )
            self.post_line_ledger(new)
        for dimension, status in (("LIFECYCLE", "ISSUED"), ("COLLECTION", "UNPAID")):
            self.inv_event(
                dup,
                dimension=dimension,
                from_status=None,
                to_status=status,
                event_date=dup.issue_date,
                reason="CREATED",
                source_ref=original.enrollment,
            )
        return dup

    def inject_refund(self, *, payment, amount, requested, processed, calculation) -> Refund:
        refund = Refund(
            key=(requested.year, requested, self.seq()),
            student=payment.student,
            payment=payment,
            payer_type=payment.payer_type,
            enrollment=None,
            reason=ppolicy.OVERPAYMENT,
            amount=round_money(amount),
            requested_date=requested,
            processed_date=processed,
            status=ppolicy.PROCESSED,
            calculation=calculation,
            events=[],
        )
        self.refunds.append(refund)
        payment.refunds.append(refund)
        payment.student.refunds.append(refund)
        steps = (
            (None, ppolicy.REQUESTED, requested, payment),
            (ppolicy.REQUESTED, ppolicy.APPROVED, processed, None),
            (ppolicy.APPROVED, ppolicy.PROCESSED, processed, None),
        )
        for old, new, on, src in steps:
            self.refund_event(
                refund, from_status=old, to_status=new, event_date=on, reason=new, source_ref=src
            )
        self.post_refund_ledger(refund)
        return refund


def student_scholarships(student, term):
    return sorted(
        (s for inv in student.invoices for s in inv.scholarships if s.term is term),
        key=lambda s: s.key,
    )


def refund_snapshot(caps: pcalc.RefundCaps, schedule_pct) -> dict:
    """payments.services._snapshot: the request-time audit snapshot."""
    return {
        "net_paid": format_money(caps.net_paid),
        "credit_balance": format_money(caps.credit_balance),
        "payment_available": format_money(caps.payment_available),
        "schedule_pct": None if schedule_pct is None else str(schedule_pct),
        "pending_refunds": format_money(caps.pending_refunds),
        "credit_available": format_money(caps.credit_available),
        "max_refundable": format_money(caps.max_refundable),
    }


def _exit_description(kind, target, pct) -> str:
    """billing.withdrawals line descriptions (resolved after IDs exist)."""
    if kind == "WITHDRAWAL":
        return f"Withdrawal credit {pct * 100:.0f}% of {target.pk}"
    return f"Cancellation of {target.pk}"


def description_of(line) -> str:
    d = line.description
    return d() if callable(d) else d


Line.description_text = description_of
