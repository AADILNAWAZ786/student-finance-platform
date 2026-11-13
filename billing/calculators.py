"""Pure billing calculators (ARCHITECTURE.md §5). No ORM access, no clock, no I/O.

They work on plain objects (dataclasses, ``SimpleNamespace`` or model instances whose
related rows are already loaded) and never modify their inputs. The services, the data
generator and reconciliation all call these same functions.

Frozen contracts (ARCHITECTURE §5) implemented here:
    resolve_fee_version(versions, on_date)                       BR-FEE-01/03
    effective_windows(versions)                                  BR-FEE-03
    price_lines(enrollment, fee_rows)                            BR-FEE-02/04, BR-WAV-01
    late_fee(open_amount, due_date, as_of, policy)               BR-LATE-01
    invoice_amounts(lines, scholarship_allocations, payment_allocations) -> InvoiceAmounts
    collection_status(amounts, *, due_date, as_of)               BR-INV-06
    LEDGER_SIGN                                                   BR-LED-01

Phase 2 additions (approved decisions D4 and D13):
    apply_waivers(lines, waivers)      BR-WAV-01: waivers are applied *after* price_lines,
                                       which keeps its frozen two-argument signature
    ledger_balance(entries, as_of)     BR-BAL-01: the one implementation of the ledger
                                       balance; Phase 6's reconciliation.calculators
                                       .balance_from_ledger delegates to it

``release_for_adjustment`` (BR-WDR-03) is Phase 5 (decision D12) and is not defined yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from billing import policy
from core.money import ZERO, apply_rate, dsum, round_money, to_decimal

# --- fee versions (BR-FEE-01 / BR-FEE-03) ----------------------------------------------------


def resolve_fee_version(versions: Iterable, on_date: date):
    """The version in force on ``on_date``: the latest ``effective_from`` ≤ ``on_date``.

    ``versions`` are the rows of one chain (same program, term, fee type and residency).
    Raises ``LookupError`` when none is in force (the service maps it to
    ``422 FEE_SCHEDULE_NOT_FOUND``).
    """
    in_force = [v for v in versions if v.effective_from <= on_date]
    if not in_force:
        raise LookupError(f"no fee schedule version is in force on {on_date}")
    return max(in_force, key=lambda v: (v.effective_from, v.version))


def effective_windows(versions: Iterable) -> dict[str, tuple[date, date | None]]:
    """``{fee_schedule_id: (effective_from, effective_to)}`` for one version chain.

    ``effective_to`` is the day before the next version's ``effective_from``; ``None`` for
    the latest version (open-ended). Nothing is stored: the windows are derived.
    """
    ordered = sorted(versions, key=lambda v: (v.effective_from, v.version))
    windows: dict[str, tuple[date, date | None]] = {}
    for current, following in zip(ordered, [*ordered[1:], None], strict=True):
        end = following.effective_from - timedelta(days=1) if following else None
        windows[current.fee_schedule_id] = (current.effective_from, end)
    return windows


# --- pricing (BR-FEE-02 / BR-FEE-04 / BR-WAV-01) -----------------------------------------------


@dataclass(frozen=True)
class PricedLine:
    """A FEE line before it is written: everything the InvoiceLine row needs."""

    fee_schedule_id: str
    fee_type: str
    charge_basis: str
    description: str
    quantity: Decimal
    unit_amount: Decimal
    gross_amount: Decimal
    is_scholarship_eligible: bool
    is_refundable: bool
    is_waivable: bool
    allocation_priority: int
    waiver_amount: Decimal = ZERO
    waiver_reason: str | None = None

    @property
    def net_amount(self) -> Decimal:
        return round_money(self.gross_amount - self.waiver_amount)


_LABELS = {
    policy.TUITION: "Tuition",
    policy.LAB: "Lab fee",
    policy.REGISTRATION: "Registration fee",
    policy.TECHNOLOGY: "Technology fee",
    policy.LIBRARY: "Library fee",
    policy.STUDENT_ACTIVITY: "Student activity fee",
    policy.INTERNATIONAL_SERVICE: "International student service fee",
}


def _attr(obj, path: str):
    for name in path.split("."):
        obj = obj[name] if isinstance(obj, Mapping) else getattr(obj, name)
    return obj


def price_lines(enrollment, fee_rows: Iterable) -> tuple[PricedLine, ...]:
    """Priced FEE lines for one enrollment, in allocation-priority order.

    ``enrollment`` provides ``credits``, ``program.has_lab`` and ``student.residency``.
    ``fee_rows`` are the versions in force, at most one per fee type. Rows that do not
    apply are skipped (BR-FEE-04: LAB only when the program has a lab,
    INTERNATIONAL_SERVICE only for international students). PER_CREDIT lines use the
    enrolled credits as quantity, FLAT lines quantity 1; ``gross = quantity × unit``.
    Waivers are 0 here; ``apply_waivers`` applies them (BR-WAV-01).
    """
    credits = int(_attr(enrollment, "credits"))
    has_lab = bool(_attr(enrollment, "program.has_lab"))
    residency = _attr(enrollment, "student.residency")
    if credits < 1:
        raise ValueError(f"credits must be ≥ 1, got {credits}")

    applicable = set(policy.applicable_fee_types(has_lab=has_lab, residency=residency))
    seen: set[str] = set()
    lines = []
    for row in fee_rows:
        fee_type = row.fee_type
        if fee_type in seen:
            raise ValueError(f"more than one {fee_type} fee row")
        seen.add(fee_type)
        if fee_type not in applicable:
            continue
        expected = (
            (residency,) if fee_type == policy.TUITION else policy.ALLOWED_RESIDENCIES[fee_type]
        )
        if row.residency not in expected:
            raise ValueError(
                f"{row.fee_schedule_id}: residency {row.residency} does not apply to a "
                f"{residency} student"
            )
        per_credit = row.charge_basis == "PER_CREDIT"
        quantity = Decimal(credits) if per_credit else Decimal(1)
        unit = round_money(row.amount)
        flags = policy.fee_flags(fee_type)
        description = (
            f"{_LABELS[fee_type]} ({credits} credits)" if per_credit else _LABELS[fee_type]
        )
        lines.append(
            PricedLine(
                fee_schedule_id=row.fee_schedule_id,
                fee_type=fee_type,
                charge_basis=row.charge_basis,
                description=description,
                quantity=quantity,
                unit_amount=unit,
                gross_amount=round_money(quantity * unit),
                is_scholarship_eligible=flags.is_scholarship_eligible,
                is_refundable=flags.is_refundable,
                is_waivable=flags.is_waivable,
                allocation_priority=flags.allocation_priority,
            )
        )
    return tuple(sorted(lines, key=lambda line: (line.allocation_priority, line.fee_type)))


class WaiverError(ValueError):
    """One or more waivers break BR-WAV-01. ``errors`` = list of messages, one per problem."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def apply_waivers(lines: Iterable[PricedLine], waivers: Iterable) -> tuple[PricedLine, ...]:
    """Apply waivers to priced FEE lines (BR-WAV-01) and return new lines.

    Each waiver has ``fee_type``, ``amount`` (Decimal) and ``reason``. Rules:
      * at most one waiver per fee type, and that fee type must be on the invoice;
      * only waivable fee types (TECHNOLOGY, LIBRARY, STUDENT_ACTIVITY);
      * ``0 ≤ amount ≤ gross``; ``reason`` is required when amount > 0 and must be one of
        the documented reasons, and must be absent when amount = 0;
      * ``net = gross − waiver`` (0 for a fully waived line).
    Raises ``WaiverError`` listing every problem; the inputs are not modified.
    """
    lines = tuple(lines)
    by_type = {line.fee_type: line for line in lines}
    errors: list[str] = []
    chosen: dict[str, tuple[Decimal, str | None]] = {}
    for i, w in enumerate(waivers):
        fee_type = _attr(w, "fee_type")
        amount = to_decimal(_attr(w, "amount"))
        reason = _attr(w, "reason") if _has(w, "reason") else None
        where = f"waivers[{i}] ({fee_type})"
        if fee_type in chosen:
            errors.append(f"{where}: only one waiver per fee type.")
            continue
        chosen[fee_type] = (amount, reason)
        line = by_type.get(fee_type)
        if line is None:
            errors.append(f"{where}: this fee type is not charged on the invoice.")
            continue
        if not line.is_waivable:
            errors.append(f"{where}: {fee_type} is not a waivable fee type.")
            continue
        if amount < 0:
            errors.append(f"{where}: amount must not be negative.")
        elif amount > line.gross_amount:
            errors.append(
                f"{where}: amount {amount:.2f} exceeds the line gross {line.gross_amount:.2f}."
            )
        if amount > 0 and not reason:
            errors.append(f"{where}: reason is required when the waiver amount is above 0.")
        elif amount == 0 and reason:
            errors.append(f"{where}: reason must be omitted when the waiver amount is 0.")
        elif reason and reason not in policy.WAIVER_REASONS:
            errors.append(f"{where}: reason must be one of {', '.join(policy.WAIVER_REASONS)}.")
    if errors:
        raise WaiverError(errors)
    out = []
    for line in lines:
        if line.fee_type in chosen and chosen[line.fee_type][0] > 0:
            amount, reason = chosen[line.fee_type]
            line = replace(line, waiver_amount=round_money(amount), waiver_reason=reason)
        out.append(line)
    return tuple(out)


def _has(obj, name: str) -> bool:
    return name in obj if isinstance(obj, Mapping) else hasattr(obj, name)


# --- late fee (BR-LATE-01) --------------------------------------------------------------------


def late_fee_date(due_date: date, grace_days: int) -> date:
    """The posting date of a late fee: the day after ``due_date + grace_days``."""
    return due_date + timedelta(days=grace_days + 1)


def late_fee(
    open_amount: object, due_date: date, as_of: date, policy: policy.LateFeePolicy
) -> Decimal:
    """2% of the outstanding amount (half-up), clamped to [minimum, maximum], or 0.

    0 while ``as_of`` is on or before ``due_date + grace_days`` or nothing is outstanding.
    """
    outstanding = to_decimal(open_amount)
    if outstanding <= 0 or as_of < late_fee_date(due_date, policy.grace_days):
        return ZERO
    fee = apply_rate(outstanding, policy.rate)
    return round_money(min(max(fee, policy.minimum), policy.maximum))


# --- invoice amounts and collection status (BR-INV-04 / BR-INV-05 / BR-INV-06) ----------------

PAYER_TYPES = ("SELF", "GUARDIAN", "SPONSOR")


@dataclass(frozen=True)
class InvoiceAmounts:
    """The separated invoice amounts of BR-INV-04 (computed, never stored)."""

    original_charges: Decimal
    adjustments: Decimal
    scholarship_applied: Decimal
    amount_paid: Decimal
    payments_by_payer: dict = field(default_factory=dict)

    @property
    def net_charges(self) -> Decimal:
        return round_money(self.original_charges + self.adjustments)

    @property
    def outstanding(self) -> Decimal:
        return round_money(self.net_charges - self.scholarship_applied - self.amount_paid)

    @property
    def is_settled(self) -> bool:
        return self.outstanding == 0


def invoice_amounts(lines, scholarship_allocations, payment_allocations) -> InvoiceAmounts:
    """Aggregate one invoice's rows (signed sums, so reversals and deallocations subtract).

    ``lines`` have ``line_type`` and ``net_amount``; allocations have ``amount`` and, for
    payment allocations, ``payer_type``. Every payer counts as money paid (BR-PAY-03).
    ``payments_by_payer`` lists the payers that have payment allocations on the invoice (as
    the frozen §7 worked-example test expects); API serializers show 0.00 for the others.
    """
    lines = list(lines)
    payment_allocations = list(payment_allocations)
    by_payer = {
        p: dsum(a.amount for a in payment_allocations if a.payer_type == p)
        for p in PAYER_TYPES
        if any(a.payer_type == p for a in payment_allocations)
    }
    return InvoiceAmounts(
        original_charges=dsum(
            line.net_amount for line in lines if line.line_type in ("FEE", "LATE_FEE")
        ),
        adjustments=dsum(line.net_amount for line in lines if line.line_type == "ADJUSTMENT"),
        scholarship_applied=dsum(a.amount for a in scholarship_allocations),
        amount_paid=dsum(a.amount for a in payment_allocations),
        payments_by_payer=by_payer,
    )


def check_amounts(amounts: InvoiceAmounts) -> None:
    """Raise ``ValueError`` for the arithmetic errors of BR-INV-04 (negative totals)."""
    for name in ("scholarship_applied", "amount_paid", "outstanding"):
        value = getattr(amounts, name)
        if value < 0:
            raise ValueError(f"{name} is negative ({value}): arithmetic error (BR-INV-04)")


def collection_status(amounts: InvoiceAmounts, *, due_date: date, as_of: date) -> str:
    """BR-INV-06, first match wins. All six statuses are derived here."""
    check_amounts(amounts)
    if amounts.outstanding > 0:
        if as_of > due_date:
            return "OVERDUE"
        if amounts.amount_paid > 0:
            return "PARTIALLY_PAID"
        return "UNPAID"
    if amounts.net_charges == 0:
        return "ADJUSTED_TO_ZERO"
    if amounts.amount_paid > 0:
        return "PAID"
    return "SCHOLARSHIP_COVERED"


# --- line open amounts (BR-ALC-02) ------------------------------------------------------------


def line_open_amounts(lines, scholarship_allocations=(), payment_allocations=()) -> dict:
    """``{line_id: open}`` for FEE and LATE_FEE lines.

    open = net + Σ ADJUSTMENT lines adjusting it − Σ scholarship allocations on it −
    Σ payment allocations on it. ADJUSTMENT lines name their target in
    ``adjusts_line_id``; allocations name theirs in ``invoice_line_id``.
    """
    lines = list(lines)
    opens = {
        line.line_id: to_decimal(line.net_amount)
        for line in lines
        if line.line_type in ("FEE", "LATE_FEE")
    }
    for line in lines:
        if line.line_type == "ADJUSTMENT":
            opens[line.adjusts_line_id] += to_decimal(line.net_amount)
    for alloc in [*scholarship_allocations, *payment_allocations]:
        opens[alloc.invoice_line_id] -= to_decimal(alloc.amount)
    return {line_id: round_money(v) for line_id, v in opens.items()}


@dataclass(frozen=True)
class VoidAdjustment:
    adjusts_line_id: str
    fee_type: str
    amount: Decimal  # negative
    allocation_priority: int


def void_adjustments(lines) -> tuple[VoidAdjustment, ...]:
    """BR-INV-08: a VOID adjustment for every FEE/LATE_FEE line that still has a net
    charge, of minus that remaining amount (its net plus any earlier adjustments), so the
    invoice's net charges become exactly 0 and no line is adjusted below zero."""
    lines = list(lines)
    remaining = line_open_amounts(lines)  # no allocations: void requires none (BR-INV-08)
    out = []
    for line in sorted(lines, key=lambda x: x.line_id):
        if line.line_type in ("FEE", "LATE_FEE") and remaining[line.line_id] > 0:
            out.append(
                VoidAdjustment(
                    adjusts_line_id=line.line_id,
                    fee_type=line.fee_type,
                    amount=-remaining[line.line_id],
                    allocation_priority=line.allocation_priority,
                )
            )
    return tuple(out)


# --- withdrawals and cancellations (BR-WDR-01..03, BR-SCH-07) ----------------------------------


@dataclass(frozen=True)
class ExitAdjustment:
    """One ADJUSTMENT to post for a withdrawal or cancellation."""

    adjusts_line_id: str
    fee_type: str
    line_net: Decimal  # N: the line's current net (net + earlier adjustments), D-W3
    amount: Decimal  # A: negative
    allocation_priority: int


def _current_nets(lines) -> dict[str, Decimal]:
    return line_open_amounts(list(lines))  # no allocations: net + Σ its adjustments


def withdrawal_adjustments(lines, pct) -> tuple[ExitAdjustment, ...]:
    """BR-WDR-02: −round_half_up(N × pct) on every refundable FEE / LATE_FEE line
    (``is_refundable``: TUITION, LAB) with N > 0. A zero adjustment is never created."""
    lines = list(lines)
    nets, pct = _current_nets(lines), to_decimal(pct)
    out = []
    for line in sorted(lines, key=lambda x: x.line_id):
        if line.line_type not in ("FEE", "LATE_FEE") or not line.is_refundable:
            continue
        n = nets[line.line_id]
        if n > 0:
            amount = -round_money(n * pct)
            if amount != 0:
                out.append(
                    ExitAdjustment(line.line_id, line.fee_type, n, amount, line.allocation_priority)
                )
    return tuple(out)


def cancellation_adjustments(lines) -> tuple[ExitAdjustment, ...]:
    """BR-WDR-01: −N on every FEE / LATE_FEE line except REGISTRATION (non-refundable) with
    N > 0, so only registration stays owed."""
    lines = list(lines)
    nets = _current_nets(lines)
    out = []
    for line in sorted(lines, key=lambda x: x.line_id):
        if line.line_type not in ("FEE", "LATE_FEE") or line.fee_type == "REGISTRATION":
            continue
        n = nets[line.line_id]
        if n > 0:
            out.append(ExitAdjustment(line.line_id, line.fee_type, n, -n, line.allocation_priority))
    return tuple(out)


@dataclass(frozen=True)
class ReleasePlan:
    """BR-WDR-03 result for one adjusted line.

    ``scholarship_reversal`` (R ≥ 0) is reversed first; ``deallocations`` are
    (allocation_id, amount > 0) in LIFO order; ``excess`` = (S − R) + P − N' (≤ 0: none).
    """

    scholarship_reversal: Decimal
    deallocations: tuple[tuple[str, Decimal], ...]
    line_net: Decimal = ZERO
    adjustment: Decimal = ZERO
    reduced_net: Decimal = ZERO
    scholarship_allocated: Decimal = ZERO
    payments_allocated: Decimal = ZERO
    excess: Decimal = ZERO


def release_for_adjustment(
    *, line_net, adjustment, scholarship_allocated, payment_allocations
) -> ReleasePlan:
    """BR-WDR-03 / BR-SCH-07: what to release when ``adjustment`` (A ≤ 0) is posted on a line
    whose net before it is ``line_net`` (N).

    Step 1, scholarship first: R = min(S, max(round_half_up(S × |A| / N), S − N')).
    Step 2, then payments: excess = (S − R) + P − N'; if > 0 it is deallocated from
    ``payment_allocations`` (objects with ``allocation_id``, ``allocated_on`` and ``amount``,
    the net still allocated) last-in-first-out by (allocated_on, allocation_id) descending.

    Raises ``ValueError`` if A > 0, |A| > N, or the allocations hold less than the excess.
    """
    n, a, s = to_decimal(line_net), to_decimal(adjustment), to_decimal(scholarship_allocated)
    allocations = [x for x in payment_allocations if to_decimal(x.amount) > 0]
    p = dsum(x.amount for x in allocations)
    if a > 0:
        raise ValueError(f"adjustment must be ≤ 0, got {a}")
    if -a > n:
        raise ValueError(f"|adjustment| {-a} exceeds the line net {n}")
    reduced = round_money(n + a)
    if a == 0:
        return ReleasePlan(ZERO, (), n, a, reduced, s, p, ZERO)
    proportional = round_money(s * (-a) / n)
    reversal = max(ZERO, min(s, max(proportional, s - reduced)))
    excess = round_money((s - reversal) + p - reduced)
    deallocations: list[tuple[str, Decimal]] = []
    remaining = excess
    for x in sorted(allocations, key=lambda x: (x.allocated_on, x.allocation_id), reverse=True):
        if remaining <= 0:
            break
        take = min(round_money(x.amount), remaining)
        deallocations.append((x.allocation_id, take))
        remaining = round_money(remaining - take)
    if remaining > 0:
        raise ValueError(f"payment allocations hold {p}, less than the excess {excess}")
    return ReleasePlan(reversal, tuple(deallocations), n, a, reduced, s, p, excess)


# --- ledger (BR-LED-01 / BR-BAL-01) ------------------------------------------------------------

# +1 = the student owes more (debit); -1 = the student owes less (credit).
LEDGER_SIGN: dict[str, int] = {
    "CHARGE": 1,
    "LATE_FEE": 1,
    "SCHOLARSHIP_REVERSAL": 1,
    "REFUND": 1,
    "ADJUSTMENT": -1,
    "SCHOLARSHIP_CREDIT": -1,
    "PAYMENT": -1,
}


def ledger_amount(entry_type: str, magnitude: object) -> Decimal:
    """Signed ledger amount for a source magnitude (≥ 0), per the posting table."""
    m = to_decimal(magnitude)
    if m < 0:
        raise ValueError(f"magnitude must be ≥ 0, got {m}")
    return round_money(LEDGER_SIGN[entry_type] * m)


def ledger_balance(entries: Iterable, as_of: date | None) -> Decimal:
    """BR-BAL-01: Σ ``amount`` of the entries with ``entry_date`` ≤ ``as_of``.

    ``as_of = None`` sums every entry. Negative = credit balance. This is the single
    implementation of BR-BAL-01 (decision D13).
    """
    return dsum(e.amount for e in entries if as_of is None or e.entry_date <= as_of)
