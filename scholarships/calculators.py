"""Pure scholarship calculators (ARCHITECTURE.md §5). No ORM access, no clock, no I/O.

Frozen contracts:
    evaluate(student, term, scholarship_type) -> EligibilityResult         BR-SCH-01
    coverable_lines(type, lines)                                          BR-SCH-06/08
    eligible_fee_base(type, lines)                                        BR-SCH-04/08
    award_amount(type, eligible_base, already_awarded)                    BR-SCH-03

Phase 3 additions (approved D-S6, D-S8):
    plan_application(type, lines, amount, *, adjustments, scholarship_allocated,
                     payment_allocated) -> tuple[PlannedAllocation, ...]

Two different limits are kept apart:
  * the **per-term cap** (BR-SCH-03, D-S5) works on *awards*: ``already_awarded`` is the
    cumulative awarded_amount of the student's awards of this type in this term;
  * **BR-SCH-04** works on what is *applied*: Σ net scholarship allocations on an invoice
    ≤ ``eligible_fee_base``. ``plan_application`` also keeps every line's own room
    (net + adjustments − scholarship already on it) ≥ 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from core.money import ZERO, dsum, round_money, to_decimal
from scholarships import policy

# --- eligibility (BR-SCH-01) ------------------------------------------------------------------


@dataclass(frozen=True)
class EligibilityResult:
    is_eligible: bool
    reason_codes: tuple[str, ...]
    inputs_snapshot: dict = field(default_factory=dict)


def _get(obj, name, default=None):
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _id(obj, name):
    """``student.program_id`` from a model, or ``student.program.program_id`` from a stub."""
    value = _get(obj, f"{name}_id")
    if value is None:
        related = _get(obj, name)
        value = related if isinstance(related, str) else _get(related, f"{name}_id")
    return value


def _dec(value) -> Decimal | None:
    return None if value is None else to_decimal(value)


def evaluate(student, term, scholarship_type) -> EligibilityResult:
    """Evaluate one student × type × term against the type's criteria.

    ``student``: gpa, need_index, residency, program_id (or program), admit_term_id (or
    admit_term). ``term``: term_id, invoice_date. ``scholarship_type``: the criteria, plus
    ``program_ids`` (the bridge rows) when program_restricted. Every failing criterion is
    reported, in the fixed order of ``policy.REASON_CODES``. The effective window is
    checked against ``term.invoice_date``, the canonical evaluation date (D-S7).
    """
    gpa = to_decimal(_get(student, "gpa"))
    need = to_decimal(_get(student, "need_index"))
    residency = _get(student, "residency")
    program_id = _id(student, "program")
    admit_term_id = _id(student, "admit_term")
    term_id = _get(term, "term_id")
    on = _get(term, "invoice_date")
    first_term = admit_term_id == term_id
    t = scholarship_type

    reasons = []
    min_gpa, max_gpa, min_need = _dec(t.min_gpa), _dec(t.max_gpa), _dec(t.min_need_index)
    if min_gpa is not None and gpa < min_gpa:
        reasons.append(policy.GPA_BELOW_MIN)
    if max_gpa is not None and gpa >= max_gpa:  # exclusive upper bound (tiered merit)
        reasons.append(policy.GPA_AT_OR_ABOVE_MAX)
    if min_need is not None and need < min_need:
        reasons.append(policy.NEED_BELOW_MIN)
    if t.residency != "ALL" and residency != t.residency:
        reasons.append(policy.RESIDENCY_MISMATCH)
    if t.first_term_only and not first_term:
        reasons.append(policy.NOT_FIRST_TERM)
    if t.program_restricted and program_id not in set(_get(t, "program_ids", ()) or ()):
        reasons.append(policy.PROGRAM_NOT_ELIGIBLE)
    if on < t.effective_from or (t.effective_to is not None and on > t.effective_to):
        reasons.append(policy.TYPE_NOT_IN_EFFECT)

    snapshot = {
        "gpa": f"{gpa:.2f}",
        "need_index": f"{need:.2f}",
        "residency": residency,
        "program": program_id,
        "admit_term": admit_term_id,
        "term": term_id,
        "first_term": first_term,
    }
    if not reasons:
        return EligibilityResult(True, (policy.ALL_CRITERIA_MET,), snapshot)
    return EligibilityResult(False, tuple(reasons), snapshot)


# --- coverage and amounts (BR-SCH-03/04/06/08) ------------------------------------------------


def coverable_lines(scholarship_type, lines) -> list:
    """FEE lines the type may cover, by (allocation_priority, line_id).

    ELIGIBLE_LINES_ONLY: lines flagged ``is_scholarship_eligible`` (TUITION, LAB).
    ALL_FEE_LINES (the full-ride type): every FEE line. Never LATE_FEE or ADJUSTMENT lines.
    """
    scope = scholarship_type.coverage_scope
    if scope == policy.ELIGIBLE_LINES_ONLY:
        chosen = [x for x in lines if x.line_type == "FEE" and x.is_scholarship_eligible]
    elif scope == policy.ALL_FEE_LINES:
        chosen = [x for x in lines if x.line_type == "FEE"]
    else:
        raise ValueError(f"unknown coverage_scope {scope!r}")
    return sorted(chosen, key=lambda x: (x.allocation_priority, x.line_id))


def eligible_fee_base(scholarship_type, lines) -> Decimal:
    """Σ net of the coverable lines (frozen behaviour: adjustment lines are not counted)."""
    return dsum(x.net_amount for x in coverable_lines(scholarship_type, lines))


def award_amount(scholarship_type, eligible_base, already_awarded) -> Decimal:
    """The award for this type: PERCENT × eligible base (half-up) or FIXED, capped by
    ``max_amount_per_term − already_awarded`` (never below 0).

    ``already_awarded`` = cumulative awarded_amount of the student's non-revoked awards of
    this type in this term (D-S5). BR-SCH-04 is checked separately on allocations.
    """
    base = to_decimal(eligible_base)
    if base < 0:
        raise ValueError("eligible_base must not be negative")
    if scholarship_type.award_basis == policy.PERCENT:
        raw = (base * to_decimal(scholarship_type.award_value) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    elif scholarship_type.award_basis == policy.FIXED:
        raw = to_decimal(scholarship_type.award_value)
    else:
        raise ValueError(f"unknown award_basis {scholarship_type.award_basis!r}")
    remaining_cap = to_decimal(scholarship_type.max_amount_per_term) - to_decimal(already_awarded)
    return round_money(max(ZERO, min(raw, remaining_cap)))


# --- application plan (BR-SCH-06/08, BR-ALC-02, BR-ALC-06; D-S6, D-S8) ---------------------------


@dataclass(frozen=True)
class PlannedAllocation:
    line_id: str
    amount: Decimal
    payment_to_release: Decimal  # BR-ALC-06: payment money to deallocate from this line


class InsufficientRoom(ValueError):
    """The coverable lines cannot take the whole amount (application is all-or-nothing)."""

    def __init__(self, amount, room):
        super().__init__(f"amount {amount} exceeds the remaining room {room}")
        self.amount, self.room = amount, room


def plan_application(
    scholarship_type,
    lines,
    amount,
    *,
    adjustments: Mapping[str, Decimal] | None = None,
    scholarship_allocated: Mapping[str, Decimal] | None = None,
    payment_allocated: Mapping[str, Decimal] | None = None,
) -> tuple[PlannedAllocation, ...]:
    """Spread ``amount`` over the type's coverable lines in priority order.

    Each line's room = net + Σ adjustments on it − Σ net scholarship already on it (D-S6).
    Payments do not reduce the room: scholarship takes precedence (BR-ALC-06); the plan
    reports, per line, how much payment money would have to be deallocated. Raises
    ``InsufficientRoom`` unless the whole amount fits (D-S8).
    """
    amount = to_decimal(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    adjustments = adjustments or {}
    scholarship_allocated = scholarship_allocated or {}
    payment_allocated = payment_allocated or {}
    plan, left, total_room = [], amount, ZERO
    for line in coverable_lines(scholarship_type, lines):
        line_id = line.line_id
        charge = to_decimal(line.net_amount) + to_decimal(adjustments.get(line_id, 0))
        sch = to_decimal(scholarship_allocated.get(line_id, 0))
        room = max(ZERO, charge - sch)
        total_room += room
        take = min(room, left)
        if take > 0:
            pay = to_decimal(payment_allocated.get(line_id, 0))
            release = max(ZERO, pay + sch + take - charge)
            plan.append(PlannedAllocation(line_id, round_money(take), round_money(release)))
            left -= take
    if left > 0:
        raise InsufficientRoom(amount, round_money(total_room))
    return tuple(plan)


def net_by_line(allocations: Iterable) -> dict[str, Decimal]:
    """Σ signed amount per ``invoice_line_id``."""
    out: dict[str, Decimal] = {}
    for a in allocations:
        out[a.invoice_line_id] = out.get(a.invoice_line_id, ZERO) + to_decimal(a.amount)
    return {k: round_money(v) for k, v in out.items()}
