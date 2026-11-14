"""Stages 14 and 16: inject the five required anomaly types (DATABASE_DESIGN §7).

The injector writes records directly, bypassing every service rule, as legacy or imported
corruption would (ARCHITECTURE §10). Each injection records a ``LabelSpec``; stage 17 turns it
into a ``GroundTruthLabel`` once IDs exist. Injected records look like baseline records:
same actor, same ID sequences (numbered with the baseline in stage 17), plausible business
dates, ordinary enum values; the domain tables have no injection column (§12.2.3).

Methods (DATABASE_DESIGN §7):
  PAYMENT_OVER_ALLOCATED            APPEND one ALLOCATE row on a fully allocated payment, to an
                                    open line of the same student (the line stays open ≥ 0)
  SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES APPEND an ACCEPTED award of 1.05–1.5 × the eligible base on
                                    an unused positive Eligibility row with no group conflict
  DUPLICATE_INVOICE                 APPEND a second invoice for the same enrollment: same issue
                                    date, same FEE lines, CHARGE entries, events
  REFUND_EXCEEDS_NET_PAID           APPEND a PROCESSED refund above net paid (+3 events, +1
                                    REFUND entry)
  BALANCE_LEDGER_MISMATCH           MODIFY reported_balance of one snapshot by ± ₹500–₹25,000
Every anomaly is placed on a different student, so each label has its own findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from billing import calculators as bcalc
from core.money import dsum, round_money
from datagen.pipeline.engine import GenerationError, Rec, World, refund_snapshot
from datagen.rng import sample, stage_rng
from scholarships import calculators as scalc
from scholarships import policy as spolicy

YEAR_END = date(2026, 12, 31)
RULE_DECISION = {
    "PAYMENT_OVER_ALLOCATED": ("BR-ALC-01", "REJECT_ALLOCATION", "Payment"),
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES": ("BR-SCH-04", "CAP_SCHOLARSHIP", "Scholarship"),
    "DUPLICATE_INVOICE": ("BR-INV-01", "VOID_DUPLICATE", "Invoice"),
    "REFUND_EXCEEDS_NET_PAID": ("BR-REF-01", "REVERSE_REFUND", "Refund"),
    "BALANCE_LEDGER_MISMATCH": ("BR-BAL-03", "RESTATE_BALANCE", "BalanceSnapshot"),
}
TRANSACTIONAL = (
    "PAYMENT_OVER_ALLOCATED",
    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
    "DUPLICATE_INVOICE",
    "REFUND_EXCEEDS_NET_PAID",
)


@dataclass
class LabelSpec:
    issue_type: str
    record: Rec
    student: Rec
    related: list = field(default_factory=list)
    method: str = "APPEND"
    expected_value: Decimal | None = None
    injected_value: Decimal | None = None
    rule: str | None = None

    @property
    def entity_type(self) -> str:
        return RULE_DECISION[self.issue_type][2]

    @property
    def rule_violated(self) -> str:
        return self.rule or RULE_DECISION[self.issue_type][0]

    @property
    def expected_decision(self) -> str:
        return RULE_DECISION[self.issue_type][1]


class Injector:
    def __init__(self, world: World, *, seed: int, per_type: int, conf: dict):
        self.w, self.seed, self.k, self.conf = world, seed, per_type, conf
        self.used: set[str] = set()  # students already carrying an anomaly
        self.labels: list[LabelSpec] = []

    def _pick(self, issue_type, candidates, student_of):
        rng = stage_rng(self.seed, f"inject:{issue_type}")
        pool = [c for c in candidates if student_of(c).pk not in self.used]
        if len(pool) < self.k:
            raise GenerationError(f"{issue_type}: {len(pool)} candidates for {self.k} injections")
        chosen = []
        for c in sample(rng, pool, len(pool)):
            if student_of(c).pk in self.used:
                continue
            self.used.add(student_of(c).pk)
            chosen.append(c)
            if len(chosen) == self.k:
                break
        if len(chosen) < self.k:
            raise GenerationError(f"{issue_type}: not enough distinct students")
        return chosen, rng

    # --- stage 14 ---------------------------------------------------------------------------------

    def transactional(self):
        self.payment_over_allocated()
        self.scholarship_over_award()
        self.duplicate_invoice()
        self.refund_over_net_paid()

    @staticmethod
    def _clean_completed(invoice) -> bool:
        return invoice.lifecycle_status == "ISSUED" and invoice.enrollment.status == "COMPLETED"

    def payment_over_allocated(self):
        w = self.w
        candidates = []
        for p in sorted(w.payments, key=lambda p: p.pk):
            if p.refunds or not p.allocations or w.available(p) != 0:
                continue
            lines = self._open_lines(p.student, minimum=Decimal("1000.00"))
            if lines:
                candidates.append((p, lines))
        chosen, rng = self._pick("PAYMENT_OVER_ALLOCATED", candidates, lambda c: c[0].student)
        lo, hi = self.conf["over_allocation_amount"]
        for payment, lines in chosen:
            line, open_amount = lines[rng.randrange(len(lines))]
            amount = min(Decimal(rng.randint(lo, hi)), round_money(open_amount - 1))
            on = max(payment.received_date, line.posted_date)
            on = min(YEAR_END, on + timedelta(days=rng.randint(0, 30)))
            alloc = w.new_pay_alloc(payment, line, amount=amount, rule="MANUAL", allocated_on=on)
            self.labels.append(
                LabelSpec(
                    "PAYMENT_OVER_ALLOCATED",
                    payment,
                    payment.student,
                    related=[alloc, line, line.invoice],
                    expected_value=payment.amount,
                    injected_value=round_money(payment.amount + amount),
                )
            )

    def _open_lines(self, student, *, minimum):
        out = []
        for invoice in sorted(student.invoices, key=lambda i: i.key):
            if invoice.lifecycle_status != "ISSUED":
                continue
            opens = bcalc.line_open_amounts(invoice.lines, invoice.sch_allocs, invoice.pay_allocs)
            for line in sorted(invoice.lines, key=lambda x: x.key):
                if line.line_type in ("FEE", "LATE_FEE") and opens[line.key] > minimum:
                    out.append((line, opens[line.key]))
        return out

    def scholarship_over_award(self):
        w = self.w
        candidates = []
        for e in w.eligibility:  # creation order = ELG ID order
            if not e.is_eligible or e.scholarship is not None:
                continue
            if e.scholarship_type.coverage_scope == spolicy.ALL_FEE_LINES:
                continue
            invoice = w.current_invoice(e.student, e.term)
            if invoice is None or not self._clean_completed(invoice) or w.award_conflict(e):
                continue
            if scalc.eligible_fee_base(e.scholarship_type, invoice.lines) > 0:
                candidates.append((e, invoice))
        chosen, rng = self._pick(
            "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", candidates, lambda c: c[0].student
        )
        lo, hi = (int(Decimal(x) * 100) for x in self.conf["scholarship_overaward_factor"])
        for e, invoice in chosen:
            base = scalc.eligible_fee_base(e.scholarship_type, invoice.lines)
            amount = round_money(base * Decimal(rng.randint(lo, hi)) / 100)
            on = invoice.issue_date
            s = w.inject_award(eligibility=e, invoice=invoice, amount=amount, award_date=on)
            self.labels.append(
                LabelSpec(
                    "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES",
                    s,
                    e.student,
                    related=[e, invoice],
                    expected_value=base,
                    injected_value=amount,
                )
            )

    def duplicate_invoice(self):
        w = self.w
        candidates = [
            i
            for i in sorted(w.invoices, key=lambda i: i.key)
            if self._clean_completed(i) and len(i.enrollment.invoices) == 1
        ]
        chosen, _ = self._pick("DUPLICATE_INVOICE", candidates, lambda i: i.student)
        for original in chosen:
            dup = w.inject_duplicate_invoice(original)
            fee_lines = [x for x in dup.lines]
            self.labels.append(
                LabelSpec(
                    "DUPLICATE_INVOICE",
                    dup,
                    original.student,
                    related=[original, original.enrollment, *fee_lines],
                    expected_value=None,
                    injected_value=dup.total_amount,
                )
            )

    def refund_over_net_paid(self):
        w = self.w
        candidates = [
            p
            for p in sorted(w.payments, key=lambda p: p.pk)
            if not p.student.refunds
            and p.received_date <= date(2026, 12, 1)
            and p is min(p.student.payments, key=lambda x: x.pk)
        ]
        chosen, rng = self._pick("REFUND_EXCEEDS_NET_PAID", candidates, lambda p: p.student)
        lo, hi = self.conf["refund_excess"]
        for payment in chosen:
            student = payment.student
            net_paid = dsum(p.amount for p in student.payments)
            amount = round_money(net_paid + rng.randint(lo, hi))
            requested = min(
                date(2026, 12, 20), payment.received_date + timedelta(days=rng.randint(5, 30))
            )
            processed = min(YEAR_END, requested + timedelta(days=rng.randint(2, 8)))
            refund = w.inject_refund(
                payment=payment,
                amount=amount,
                requested=requested,
                processed=processed,
                calculation=refund_snapshot(w.refund_caps(payment), None),
            )
            entry = student.ledger[-1]
            self.labels.append(
                LabelSpec(
                    "REFUND_EXCEEDS_NET_PAID",
                    refund,
                    student,
                    related=[payment, entry],
                    expected_value=net_paid,
                    injected_value=amount,
                )
            )

    # --- stage 16 ---------------------------------------------------------------------------------

    def balance_mismatches(self):
        w = self.w
        candidates = sorted(w.snapshots, key=lambda s: s.pk)
        chosen, rng = self._pick("BALANCE_LEDGER_MISMATCH", candidates, lambda s: s.student)
        lo, hi = self.conf["balance_delta"]
        for snap in chosen:
            delta = Decimal(rng.randint(lo, hi)) * (1 if rng.random() < 0.5 else -1)
            original = snap.reported_balance
            snap.reported_balance = round_money(original + delta)
            self.labels.append(
                LabelSpec(
                    "BALANCE_LEDGER_MISMATCH",
                    snap,
                    snap.student,
                    related=[],
                    method="MODIFY",
                    expected_value=original,
                    injected_value=snap.reported_balance,
                )
            )
