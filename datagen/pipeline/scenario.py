"""Baseline scenario: ARCHITECTURE §11.2 stages 1–13 as dated business actions.

Stages 1–5 (master data, students) run first. Stages 6–13 are scheduled as actions and
executed in strict business-date order, so every operation sees exactly the rows a service
called on that date would see. Within one date the order is fixed by ``PRIORITY`` and then by
a deterministic tie-break (student number, term, slot). Each stage draws from its own
``stage_rng`` sub-stream (ARCHITECTURE §11.3).
"""

from __future__ import annotations

import hashlib
import heapq
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial

from academics.calendar import standard_calendar
from core.money import round_money
from datagen.config import Settings
from datagen.pipeline.engine import GenerationError, World
from datagen.pipeline.validation import boundary_quota
from datagen.rng import beta_integer, choose, sample, shuffled, stage_rng, truncated_normal
from scholarships import policy as spolicy
from scholarships.management.commands.seed_scholarship_types import FROM as TYPES_FROM
from scholarships.management.commands.seed_scholarship_types import SPECS as TYPE_SPECS

PRIORITY = {
    "enroll": 0,
    "invoice": 1,
    "eligibility": 2,
    "award": 3,
    "plan_payments": 4,
    "cancel": 5,
    "payment": 6,
    "late_fee": 7,
    "withdraw": 8,
    "refund_request": 9,
    "refund_approve": 10,
    "refund_process": 11,
    "complete": 12,
    "status": 13,
    "refresh": 14,
}
BILLABLE = ("TERM-2026-SPRING", "TERM-2026-SUMMER", "TERM-2026-FALL")
YEAR_END = date(2026, 12, 31)
WHOLE = Decimal(1)


def whole(value) -> Decimal:
    return round_money(Decimal(value).quantize(WHOLE, rounding=ROUND_HALF_UP))


def quota(share, total) -> int:
    return int((Decimal(str(share)) * total).quantize(WHOLE, rounding=ROUND_HALF_UP))


@dataclass
class Stats:
    """What the scenario planted (reported in the validation summary)."""

    boundary: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)
    overpayment_slots: int = 0
    advance_payments: int = 0
    rejected_refunds: int = 0
    full_ride_students: int = 0

    def hit(self, name: str, n: int = 1):
        self.boundary[name] = self.boundary.get(name, 0) + n


class Timeline:
    def __init__(self):
        self._heap: list = []
        self._n = 0

    def add(self, on: date, kind: str, order: tuple, fn):
        if on > YEAR_END:
            raise GenerationError(f"{kind} scheduled after {YEAR_END}: {on}")
        self._n += 1
        heapq.heappush(self._heap, (on, PRIORITY[kind], order, self._n, fn))

    def run(self):
        while self._heap:
            on, _, _, _, fn = heapq.heappop(self._heap)
            fn(on)


class Scenario:
    def __init__(self, settings: Settings, world: World | None = None):
        self.s = settings
        self.w = world or World(actor=settings.get("actor"))
        self.stats = Stats()
        self.timeline = Timeline()
        self.counts = settings.counts

    def rng(self, stage: str):
        return stage_rng(self.s.seed, stage)

    # --- stages 1–4: master data ------------------------------------------------------------

    def master_data(self):
        w, s = self.w, self.s
        for p in s.get("programs"):
            w.create_program(
                code=p["code"],
                name=p["name"],
                level=p["level"],
                department=p["department"],
                is_stem=p["stem"],
                has_lab=p["lab"],
                min_credits=p["min"],
                max_credits=p["max"],
                is_active=True,
            )
        self.tuition = {
            prog.pk: Decimal(p["tuition"])
            for prog, p in zip(w.programs, s.get("programs"), strict=True)
        }
        for spec in standard_calendar():
            w.create_term(spec)
        fees, pricing = s.get("fees"), s.get("pricing")
        start = date.fromisoformat(fees["effective_from"])
        intl = Decimal(pricing["international_tuition_multiplier"])
        for term_id in BILLABLE:
            term = w.terms[term_id]
            season = Decimal(pricing["season_tuition_multiplier"][term.season])
            for program in w.programs:
                base = self.tuition[program.pk] * season
                rows = [
                    ("TUITION", "DOMESTIC", "PER_CREDIT", base),
                    ("TUITION", "INTERNATIONAL", "PER_CREDIT", base * intl),
                ]
                if program.has_lab:
                    rows.append(("LAB", "ALL", "FLAT", Decimal(fees["LAB"])))
                for fee_type in ("REGISTRATION", "TECHNOLOGY", "LIBRARY", "STUDENT_ACTIVITY"):
                    rows.append((fee_type, "ALL", "FLAT", Decimal(fees[fee_type])))
                rows.append(
                    (
                        "INTERNATIONAL_SERVICE",
                        "INTERNATIONAL",
                        "FLAT",
                        Decimal(fees["INTERNATIONAL_SERVICE"]),
                    )
                )
                for fee_type, residency, basis, amount in rows:
                    w.create_fee_schedule(
                        program=program,
                        term=term,
                        fee_type=fee_type,
                        residency=residency,
                        charge_basis=basis,
                        amount=whole(amount),
                        effective_from=start,
                    )
        v2 = pricing["fall_tuition_v2"]
        fall_tuition = sorted(
            (
                f
                for f in w.fee_schedules
                if f.term.pk == "TERM-2026-FALL" and f.fee_type == "TUITION"
            ),
            key=lambda f: f.pk,
        )
        for base in sample(self.rng("fee_versions"), fall_tuition, int(v2["count"])):
            w.create_fee_schedule(
                program=base.program,
                term=base.term,
                fee_type=base.fee_type,
                residency=base.residency,
                charge_basis=base.charge_basis,
                amount=whole(base.amount * Decimal(v2["increase"])),
                effective_from=date.fromisoformat(v2["effective_from"]),
                base=base,
            )
        stem = sorted(p.pk for p in w.programs if p.is_stem)
        for spec in TYPE_SPECS:
            w.create_scholarship_type(
                spec=spec,
                effective_from=TYPES_FROM,
                program_ids=stem if spec.program_restricted else (),
            )

    # --- stage 5: students ------------------------------------------------------------------

    def students(self):
        w, s, rng = self.w, self.s, self.rng("students")
        n = self.counts["students"]
        conf = s.get("students")
        order = shuffled(rng, range(n))
        n_intl = quota(conf["international_share"], n)
        n_full = max(1, quota(conf["full_ride_share"], n))
        n_first = quota(conf["first_term_admit_share"], n)
        intl = set(order[:n_intl])
        full_ride = set(order[n_intl : n_intl + n_full])
        # one international student priced at the top of the range: planted for the ₹5,000
        # late-fee maximum boundary (ARCHITECTURE §11.2)
        large = order[0]
        first_term = set(shuffled(rng, range(n))[:n_first])
        historical = [t for t in w.terms.values() if not t.is_billable]
        spring = w.terms["TERM-2026-SPRING"]
        programs = w.programs
        top = max(programs, key=lambda p: (self.tuition[p.pk], p.max_credits, p.pk))
        g = conf["gpa"]
        lo_age, hi_age = conf["age_at_admit"]
        created = date.fromisoformat(conf["created_on"])
        self.full_ride_students, self.large_student = set(), None
        for i in range(n):
            if i in full_ride:
                gpa = Decimal(rng.randint(385, 400)) / 100
                need = Decimal(rng.randint(85, 100)) / 100
            elif i == large:
                gpa = Decimal(rng.randint(150, 290)) / 100
                need = Decimal(rng.randint(0, 30)) / 100
            else:
                gpa = Decimal(
                    f"{truncated_normal(rng, g['mean'], g['sd'], g['min'], g['max']):.2f}"
                )
                need = Decimal(f"{beta_integer(rng, *conf['need_index_beta']):.2f}")
                if gpa >= Decimal("3.85") and need >= Decimal("0.85"):
                    need = Decimal(rng.randint(0, 84)) / 100  # full ride only by quota
            program = top if i == large else choose(rng, programs)
            admit = spring if i in first_term else choose(rng, historical)
            age = rng.randint(lo_age, hi_age)
            dob = date(admit.start_date.year - age - 1, 1, 1) + timedelta(days=rng.randrange(365))
            student = w.create_student(
                first_name=choose(rng, conf["first_names"]),
                last_name=choose(rng, conf["last_names"]),
                date_of_birth=dob,
                residency="INTERNATIONAL" if (i in intl or i == large) else "DOMESTIC",
                program=program,
                admit_term=admit,
                gpa=gpa,
                need_index=need,
                event_date=created,
            )
            if i in full_ride:
                self.full_ride_students.add(student.pk)
            if i == large:
                self.large_student = student
        self.stats.full_ride_students = len(self.full_ride_students)

    # --- stage 6–13: the dated timeline ------------------------------------------------------

    def schedule(self):
        self._schedule_enrollments()
        self._schedule_exits()
        self._schedule_holds()
        for term_id in BILLABLE:
            term = self.w.terms[term_id]
            self.timeline.add(
                term.invoice_date,
                "eligibility",
                (term_id,),
                lambda on, t=term: self._eligibility(t),
            )
            self.timeline.add(
                term.invoice_date, "award", (term_id,), lambda on, t=term: self._awards(t)
            )
            late = term.due_date + timedelta(days=term.late_fee_grace_days + 1)
            self.timeline.add(
                late, "late_fee", (term_id,), lambda on: self.w.assess_late_fees(as_of=on)
            )
            self.timeline.add(
                term.end_date + timedelta(days=1),
                "complete",
                (term_id,),
                lambda on, t=term: self._complete(t, on),
            )
        spring = self.w.terms["TERM-2026-SPRING"]
        self.timeline.add(
            spring.invoice_date, "plan_payments", (), lambda on: self._plan_payments()
        )
        self.timeline.add(
            YEAR_END, "refresh", (), lambda on: self.w.refresh_collection_status(as_of=on)
        )

    def run(self):
        self.master_data()
        self.students()
        self.schedule()
        self.timeline.run()
        return self.w

    # enrollments and invoices (stages 6–7) --------------------------------------------------

    def _schedule_enrollments(self):
        rng, wrng = self.rng("enrollments"), self.rng("waivers")
        lo, hi = self.s.get("enrollment", "days_before_invoice")
        slots = [(t, st) for t in BILLABLE for st in self.w.students]
        waived = sample(
            wrng, range(len(slots)), quota(self.s.get("waivers", "invoice_share"), len(slots))
        )
        waiver_of = {}
        for rank, idx in enumerate(waived):
            waiver_of[idx] = rank
        for idx, (term_id, student) in enumerate(slots):
            term = self.w.terms[term_id]
            program = student.program
            credits = (
                program.max_credits
                if student is self.large_student
                else rng.randint(program.min_credits, program.max_credits)
            )
            enrolled_on = max(
                date(2026, 1, 1), term.invoice_date - timedelta(days=rng.randint(lo, hi))
            )
            waivers = self._waiver(wrng, student, waiver_of[idx]) if idx in waiver_of else ()
            order = (student.number,)
            self.timeline.add(
                enrolled_on,
                "enroll",
                order,
                lambda on, s=student, t=term, c=credits: self.w.enroll(
                    student=s, term=t, credits=c, enrolled_on=on
                ),
            )
            self.timeline.add(
                term.invoice_date,
                "invoice",
                order,
                lambda on, s=student, t=term, wv=waivers: self._invoice(s, t, wv),
            )

    def _waiver(self, rng, student, rank):
        fees = self.s.get("fees")
        fee_type = choose(rng, ["TECHNOLOGY", "LIBRARY", "STUDENT_ACTIVITY"])
        if rank == 0:
            fee_type = "STUDENT_ACTIVITY"
        gross = Decimal(fees[fee_type])
        full = rank == 0 or rng.random() < 0.3  # rank 0: the fully waived line boundary case
        amount = gross if full else whole(gross * Decimal(rng.randint(25, 75)) / 100)
        if fee_type == "STUDENT_ACTIVITY" and student.program.level == "PG":
            reason = "PG_ACTIVITY_EXEMPT"
        else:
            reason = choose(rng, ["STAFF_DEPENDENT", "HARDSHIP", "SPONSOR_AGREEMENT"])
        return ({"fee_type": fee_type, "amount": round_money(amount), "reason": reason},)

    def _invoice(self, student, term, waivers):
        enrollment = student.enrollments[term.pk]
        invoice = self.w.generate_invoice(enrollment=enrollment, waivers=waivers)
        if any(x.waiver_amount == x.gross_amount and x.waiver_amount > 0 for x in invoice.lines):
            self.stats.hit("fully_waived_line")

    # scholarships (stage 8) ----------------------------------------------------------------------

    def _eligibility(self, term):
        for student in self.w.students:
            self.w.evaluate_eligibility(student=student, term=term)

    def _awards(self, term):
        quota_n = self.counts["awards_per_term"]
        candidates = [e for e in self.w.eligibility if e.term is term and e.is_eligible]
        candidates.sort(
            key=lambda e: (
                0 if e.scholarship_type.coverage_scope == spolicy.ALL_FEE_LINES else 1,
                e.scholarship_type.priority,
                -e.student.gpa,
                -e.student.need_index,
                e.student.pk,
            )
        )
        awarded = 0
        for e in candidates:
            if awarded == quota_n:
                break
            if self.w.award_conflict(e):
                continue
            invoice = self.w.current_invoice(e.student, term)
            if self.w.award_amount(e, invoice) <= 0:
                continue
            scholarship = self.w.award_scholarship(eligibility=e, invoice=invoice)
            self.w.apply_scholarship(scholarship)
            awarded += 1
            stype = scholarship.scholarship_type
            if (
                stype.award_basis == "PERCENT"
                and scholarship.awarded_amount == stype.max_amount_per_term
            ):
                self.stats.hit("scholarship_at_cap")  # a percentage award capped exactly
            if stype.coverage_scope == spolicy.ALL_FEE_LINES:
                self.stats.hit("full_ride_covers_registration")
        if awarded != quota_n:
            raise GenerationError(
                f"{term.pk}: only {awarded} of the {quota_n} quota awards could be made"
            )

    # payments (stage 9) ------------------------------------------------------------------------

    def _plan_payments(self):
        """Payment profiles and every payment slot of the year, totalling exactly the target."""
        rng = self.rng("payments")
        conf = self.s.get("payments")
        payable = [s for s in self.w.students if s.pk not in self.full_ride_students]
        profiles = conf["profiles"]
        names = ["LATE_LARGE", "LATE", "PARTIAL", "PARTIAL_SMALL", "INSTALMENTS", "FULL"]
        need = {p: quota(profiles[p], len(payable)) for p in names if p != "FULL"}
        assigned = {}
        if self.large_student is not None and need["LATE_LARGE"]:
            assigned[self.large_student.pk] = "LATE_LARGE"
            need["LATE_LARGE"] -= 1
        pool = [s for s in shuffled(rng, payable) if s.pk not in assigned]
        for p in names[:-1]:
            for _ in range(max(0, need[p])):
                assigned[pool.pop().pk] = p
        for s in pool:
            assigned[s.pk] = "FULL"
        for s in payable:
            s.profile = assigned[s.pk]
        self.stats.profiles = {p: sum(1 for v in assigned.values() if v == p) for p in names}

        lo_n, hi_n = conf["instalments"]
        plan = {}  # (student, term) -> number of payments
        for s in payable:
            for term_id in BILLABLE:
                plan[(s.pk, term_id)] = rng.randint(lo_n, hi_n) if s.profile == "INSTALMENTS" else 1
        target = self.counts["payments"]
        adjustable = [k for k in sorted(plan) if assigned[k[0]] in ("INSTALMENTS", "FULL")]
        if not adjustable:
            raise GenerationError("no invoices can absorb the payment count adjustment")
        i = 0
        while sum(plan.values()) != target:  # deterministic exact adjustment (§11.2)
            key = adjustable[i % len(adjustable)]
            if sum(plan.values()) < target and plan[key] < 4:
                plan[key] += 1
            elif sum(plan.values()) > target and plan[key] > 1:
                plan[key] -= 1
            i += 1
            if i > 100 * len(adjustable):
                raise GenerationError("cannot reach the exact payment count")

        # overpayment invoices (2–5 % of invoices), the first one by ₹0.01 (boundary case)
        n_invoices = len(self.w.students) * len(BILLABLE)
        candidates = [k for k in sorted(plan) if assigned[k[0]] in ("FULL", "INSTALMENTS")]
        over = sample(
            self.rng("overpayments"),
            candidates,
            quota(conf["overpayment_invoice_share"], n_invoices),
        )
        extra = {}
        planted = boundary_quota(self.s.dec("scale_factor"))
        lo_x, hi_x = conf["overpay_extra"]
        for rank, key in enumerate(over):
            # the first k by ₹0.01: the boundary case, planted k = boundary_quota times (P8-B2)
            extra[key] = Decimal("0.01") if rank < planted else Decimal(rng.randint(lo_x, hi_x))
        refund_share = Decimal(self.s.get("refunds", "overpayment_refund_share"))
        refunded = set(sample(self.rng("refunds"), over, quota(refund_share, len(over))))
        self.stats.overpayment_slots = len(over)

        slots = []
        for (spk, term_id), count in sorted(plan.items()):
            student = self._student(spk)
            term = self.w.terms[term_id]
            for k, on in enumerate(self._payment_dates(rng, student.profile, term, count), 1):
                slots.append((on, student.number, term_id, k, count, student))
        slots.sort(key=lambda x: x[:4])
        payers = self._payer_types(len(slots))
        lo_f, hi_f = (int(Decimal(x) * 100) for x in conf["partial_fraction"])
        for (on, number, term_id, k, count, student), payer in zip(slots, payers, strict=True):
            last = k == count
            key = (student.pk, term_id)
            fn = partial(
                self._pay,
                student=student,
                k=k,
                n=count,
                payer=payer,
                extra=extra.get(key) if last else None,
                refund=last and key in refunded,
                fraction=rng.randint(lo_f, hi_f),
                small=rng.randint(2000, 20000),
            )
            self.timeline.add(on, "payment", (number, term_id, k), fn)

    def _student(self, pk):
        if not hasattr(self, "_by_pk"):
            self._by_pk = {s.pk: s for s in self.w.students}
        return self._by_pk[pk]

    def _payment_dates(self, rng, profile, term, count):
        if profile in ("LATE", "LATE_LARGE"):
            late = term.due_date + timedelta(days=term.late_fee_grace_days + 1)
            lo, hi = self.s.get("payments", "late_window_days")
            return [min(term.end_date, YEAR_END, late + timedelta(days=rng.randint(lo, hi)))]
        span = (term.due_date - term.invoice_date).days
        dates = []
        for _ in range(count):
            u = rng.random()
            dates.append(term.invoice_date + timedelta(days=math.floor(span * math.sqrt(u))))
        return sorted(dates)

    def _payer_types(self, n):
        mix = self.s.get("payments", "payer_mix")
        guardian, sponsor = quota(mix["GUARDIAN"], n), quota(mix["SPONSOR"], n)
        types = ["GUARDIAN"] * guardian + ["SPONSOR"] * sponsor
        types += ["SELF"] * (n - len(types))
        return shuffled(self.rng("payers"), types)

    def _pay(self, on, *, student, k, n, payer, extra, refund, fraction, small):
        w = self.w
        open_total = w.student_open_total(student, on=on)
        profile = student.profile
        if open_total <= 0:
            amount = Decimal("1000.00")  # nothing open: an advance, kept as unallocated credit
            self.stats.advance_payments += 1
        elif profile in ("PARTIAL",):
            amount = max(Decimal(1), whole(open_total * fraction / 100))
        elif profile == "PARTIAL_SMALL":
            amount = open_total - small if open_total > small + 1000 else whole(open_total / 2)
        elif k < n:  # one of several payments on this invoice
            amount = max(Decimal(1), whole(open_total / (n - k + 1)))
        else:
            amount = open_total
        if open_total > 0 and amount == open_total and extra is None:
            self.stats.hit("payment_equals_outstanding")
        if extra is not None:
            amount += extra
            if extra == Decimal("0.01"):
                self.stats.hit("overpayment_one_paisa")
        payment, _ = w.record_payment(
            student=student,
            amount=round_money(amount),
            received_date=on,
            method=choose(
                self.rng(f"method:{student.pk}:{on}:{k}"), self.s.get("payments", "methods", payer)
            ),
            payer_type=payer,
            external_reference=self._reference(len(w.payments) + 1),
            payer_name=self._payer_name(student, payer),
        )
        if refund and w.available(payment) > 0:
            self._schedule_refunds(student, on, reason="OVERPAYMENT", payments=[payment])

    def _reference(self, n: int) -> str:
        digest = hashlib.sha256(f"{self.s.seed}:payment:{n}".encode()).hexdigest()
        return f"TXN-{digest[:20].upper()}"

    @staticmethod
    def _payer_name(student, payer):
        if payer == "SELF":
            return f"{student.first_name} {student.last_name}"
        if payer == "GUARDIAN":
            return f"Guardian of {student.first_name} {student.last_name}"
        return "Synthetic Sponsor Trust"

    # exits and refunds (stages 11–12) ------------------------------------------------------------

    def _schedule_exits(self):
        rng = self.rng("exits")
        conf = self.s.get("exits")
        slots = [(t, s) for t in BILLABLE for s in self.w.students if s is not self.large_student]
        n_total = len(self.w.students) * len(BILLABLE)
        n_w, n_c = (
            quota(conf["withdrawal_share"], n_total),
            quota(conf["cancellation_share"], n_total),
        )
        chosen = sample(rng, slots, n_w + n_c)
        withdrawals, cancellations = chosen[:n_w], chosen[n_w:]
        boundary = list(conf["boundary_days"])
        planted = boundary_quota(self.s.dec("scale_factor"))
        for rank, (term_id, student) in enumerate(withdrawals):
            term = self.w.terms[term_id]
            longest = min(conf["max_withdrawal_day"], (term.end_date - term.start_date).days)
            # every boundary day planted k = boundary_quota times, in order (P8-B3)
            if rank < len(boundary) * planted:
                day = boundary[rank % len(boundary)]
            else:
                day = rng.randint(0, longest)
            on = term.start_date + timedelta(days=day)
            self.timeline.add(
                on,
                "withdraw",
                (student.number, term_id),
                lambda d, s=student, t=term, day=day: self._exit(s, t, d, "WITHDRAWAL", day),
            )
        for rank, (term_id, student) in enumerate(cancellations):
            term = self.w.terms[term_id]
            if rank == 0:
                on = term.invoice_date  # before any payment: registration still owed
            else:
                on = term.invoice_date + timedelta(
                    days=rng.randint(0, (term.start_date - term.invoice_date).days - 1)
                )
            self.timeline.add(
                on,
                "cancel",
                (student.number, term_id),
                lambda d, s=student, t=term: self._exit(s, t, d, "CANCELLATION", None),
            )

    def _exit(self, student, term, on, kind, day):
        enrollment = student.enrollments[term.pk]
        self.w.exit_enrollment(enrollment, kind=kind, on_date=on)
        if kind == "WITHDRAWAL":
            if day in (0, 7, 8, 14, 15, 27, 28):
                self.stats.hit(f"withdrawal_day_{day}")
        else:
            invoice = self.w.current_invoice(student, term)
            if invoice is not None and self.w.current_amounts(invoice).outstanding > 0:
                self.stats.hit("cancellation_registration_owed")
        self._schedule_refunds(student, on, reason=kind, enrollment=enrollment)

    def _schedule_refunds(self, student, on, *, reason, enrollment=None, payments=None):
        rng = self.rng(f"refund:{student.pk}:{on}:{reason}")
        r = self.s.get("refunds")
        req = min(YEAR_END, on + timedelta(days=rng.randint(*r["request_after_days"])))
        approve = min(YEAR_END, req + timedelta(days=rng.randint(*r["approve_after_days"])))
        process = min(YEAR_END, approve + timedelta(days=rng.randint(*r["process_after_days"])))
        self.timeline.add(
            req,
            "refund_request",
            (student.number, reason),
            lambda d: self._request_refunds(
                student, d, reason, enrollment, payments, approve, process
            ),
        )

    def _request_refunds(self, student, on, reason, enrollment, payments, approve, process):
        w = self.w
        for payment in payments or sorted(student.payments, key=lambda p: p.pk):
            if payment.received_date > on or w.available(payment) <= 0:
                continue
            caps = w.refund_caps(payment)
            amount = caps.max_refundable
            if amount <= 0:
                continue
            refund = w.request_refund(
                payment=payment,
                amount=amount,
                requested_date=on,
                reason=reason,
                enrollment=enrollment,
            )
            self.timeline.add(
                approve,
                "refund_approve",
                (student.number, refund.key),
                lambda d, rf=refund: self._advance(rf, "APPROVED", d),
            )
            self.timeline.add(
                process,
                "refund_process",
                (student.number, refund.key),
                lambda d, rf=refund: self._advance(rf, "PROCESSED", d),
            )

    def _advance(self, refund, target, on):
        if refund.status in ("REJECTED", "PROCESSED"):
            return
        if not self.w.advance_refund(refund, target=target, on_date=on):
            self.stats.rejected_refunds += 1

    # completion and holds --------------------------------------------------------------------

    def _complete(self, term, on):
        for student in self.w.students:
            enrollment = student.enrollments.get(term.pk)
            if enrollment is not None and enrollment.status == "ENROLLED":
                self.w.complete_enrollment(enrollment, on_date=on)

    def _schedule_holds(self):
        rng = self.rng("holds")
        n = quota(self.s.get("students", "hold_share"), len(self.w.students))
        for student in sorted(sample(rng, self.w.students, n), key=lambda s: s.number):
            start = date(2026, 2, 1) + timedelta(days=rng.randrange(270))
            back = min(YEAR_END, start + timedelta(days=rng.randint(10, 40)))
            self.timeline.add(
                start,
                "status",
                (student.number, 0),
                lambda d, s=student: self.w.change_status(
                    s, to_status="ON_HOLD", event_date=d, reason="FINANCIAL_HOLD"
                ),
            )
            self.timeline.add(
                back,
                "status",
                (student.number, 1),
                lambda d, s=student: self.w.change_status(
                    s, to_status="ACTIVE", event_date=d, reason="HOLD_RELEASED"
                ),
            )
