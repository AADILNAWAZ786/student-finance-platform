"""Pure payment and refund calculators (ARCHITECTURE.md §5). No ORM access, no clock, no
I/O; they work on plain objects and never modify their inputs. Services, the data generator
and reconciliation call these same functions.

Frozen contracts (ARCHITECTURE §5):
    plan_allocation(amount, open_lines) -> list[AllocCalc]          BR-ALC-01..04
    max_refundable(net_paid, credit_balance, payment_available)     BR-REF-01/02

Phase 4 additions (approved plan, decisions D-P7, D-P8, D-P11):
    net_allocated / payment_available / allocation_state            BR-ALC-01, DATABASE_DESIGN
    net_paid / pending_reservations / ledger_credit_balance /
    credit_available / refund_caps                                  BR-REF-01/02 reservations
    unallocated_credit                                              BR-BAL-02
    plan_release_lifo                                               BR-ALC-06 (and BR-WDR-03)

Two credit figures are kept apart on purpose:
  * **unallocated credit** (BR-BAL-02) subtracts only PROCESSED refunds, because only a
    processed refund has posted its REFUND ledger entry; the ledger identity needs exactly
    the money the ledger has seen.
  * **credit available for another refund** (BR-REF-02) starts from the ledger credit
    balance and then subtracts every pending (REQUESTED / APPROVED) refund, so two pending
    requests can never spend the same money.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from core.money import ZERO, dsum, round_money, to_decimal

PENDING = ("REQUESTED", "APPROVED")
REJECTED = "REJECTED"
PROCESSED = "PROCESSED"


# --- AUTO allocation (BR-ALC-01..04) ------------------------------------------------------------


@dataclass(frozen=True)
class AllocCalc:
    line_id: str
    amount: Decimal


def plan_allocation(amount, open_lines) -> list[AllocCalc]:
    """AUTO allocation plan: oldest invoice ``due_date`` first, then line
    ``allocation_priority``, then ``line_id`` (BR-ALC-03). Lines with open 0 are skipped;
    no line gets more than its ``open_amount`` (BR-ALC-02) and the total never exceeds
    ``amount`` (BR-ALC-01). Whatever is left stays unallocated credit (BR-ALC-04).

    Raises ``ValueError`` if ``amount`` ≤ 0 or any ``open_amount`` < 0.
    """
    remaining = to_decimal(amount)
    if remaining <= 0:
        raise ValueError(f"amount must be > 0, got {remaining}")
    lines = list(open_lines)
    for line in lines:
        if to_decimal(line.open_amount) < 0:
            raise ValueError(f"{line.line_id}: open amount {line.open_amount} is negative")
    remaining = round_money(remaining)
    plan: list[AllocCalc] = []
    for line in sorted(lines, key=lambda x: (x.due_date, x.allocation_priority, x.line_id)):
        if remaining <= 0:
            break
        open_amount = round_money(line.open_amount)
        if open_amount == 0:
            continue
        take = min(open_amount, remaining)
        plan.append(AllocCalc(line.line_id, take))
        remaining = round_money(remaining - take)
    return plan


# --- payment amounts (DATABASE_DESIGN payments, BR-ALC-01) --------------------------------------


def net_allocated(allocations: Iterable) -> Decimal:
    """Σ signed allocation amounts (ALLOCATE > 0, DEALLOCATE < 0)."""
    return dsum(a.amount for a in allocations)


def _refunds_not_rejected(refunds: Iterable, exclude_refund_id: str | None) -> list:
    return [
        r
        for r in refunds
        if r.status != REJECTED and (exclude_refund_id is None or r.refund_id != exclude_refund_id)
    ]


def payment_available(
    amount, allocations: Iterable, refunds: Iterable, *, exclude_refund_id: str | None = None
) -> Decimal:
    """``amount − Σ net allocations − Σ refunds not REJECTED`` (BR-ALC-01, BR-REF-02).

    REQUESTED and APPROVED refunds reserve their amount here. ``exclude_refund_id`` leaves
    one refund out, so a refund being approved or processed is not counted against itself.
    """
    refunded = dsum(r.amount for r in _refunds_not_rejected(refunds, exclude_refund_id))
    return round_money(to_decimal(amount) - net_allocated(allocations) - refunded)


def allocation_state(amount, net_allocated_amount) -> str:
    """UNALLOCATED (nothing allocated), FULL (the whole amount allocated) or PARTIAL."""
    net = to_decimal(net_allocated_amount)
    if net == 0:
        return "UNALLOCATED"
    return "FULL" if net >= to_decimal(amount) else "PARTIAL"


# --- refunds (BR-REF-01 / BR-REF-02) -------------------------------------------------------------


def max_refundable(net_paid, credit_balance, payment_available) -> Decimal:
    """The largest refund allowed: the smallest of the three caps, never below 0.

    ``credit_balance`` is the credit the refund may use (after pending reservations, when the
    caller passes ``credit_available``); ``payment_available`` is the source payment's.
    """
    smallest = min(to_decimal(net_paid), to_decimal(credit_balance), to_decimal(payment_available))
    return max(ZERO, round_money(smallest))


def net_paid(payment_amounts: Iterable, refunds: Iterable, *, exclude_refund_id=None) -> Decimal:
    """BR-REF-01: Σ payments − Σ refunds **not REJECTED** (REQUESTED, APPROVED, PROCESSED)."""
    refunded = dsum(r.amount for r in _refunds_not_rejected(refunds, exclude_refund_id))
    return round_money(dsum(payment_amounts) - refunded)


def pending_reservations(refunds: Iterable, *, exclude_refund_id: str | None = None) -> Decimal:
    """Σ REQUESTED + APPROVED refunds: money promised but not yet debited on the ledger."""
    return dsum(
        r.amount
        for r in refunds
        if r.status in PENDING and (exclude_refund_id is None or r.refund_id != exclude_refund_id)
    )


def ledger_credit_balance(ledger_balance) -> Decimal:
    """BR-REF-02 credit balance: −ledger balance when the balance is negative, else 0."""
    balance = round_money(ledger_balance)
    return -balance if balance < 0 else ZERO


def credit_available(ledger_balance, pending) -> Decimal:
    """Credit a new refund may still use: ledger credit balance − pending reservations, ≥ 0.

    Pending refunds have posted nothing to the ledger, so they are subtracted here.
    """
    return max(ZERO, round_money(ledger_credit_balance(ledger_balance) - to_decimal(pending)))


@dataclass(frozen=True)
class RefundCaps:
    net_paid: Decimal
    ledger_balance: Decimal
    credit_balance: Decimal  # BR-REF-02: −ledger balance if negative
    pending_refunds: Decimal  # REQUESTED + APPROVED, excluding the refund being checked
    credit_available: Decimal  # credit_balance − pending_refunds, ≥ 0
    payment_available: Decimal  # the source payment's available amount
    max_refundable: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "net_paid": self.net_paid,
            "ledger_balance": self.ledger_balance,
            "credit_balance": self.credit_balance,
            "pending_refunds": self.pending_refunds,
            "credit_available": self.credit_available,
            "payment_available": self.payment_available,
            "max_refundable": self.max_refundable,
        }


def refund_caps(
    *,
    payment_amounts: Iterable,
    student_refunds: Iterable,
    ledger_balance,
    source_amount,
    source_allocations: Iterable,
    source_refunds: Iterable,
    exclude_refund_id: str | None = None,
) -> RefundCaps:
    """The three BR-REF caps for one refund from one source payment.

    ``exclude_refund_id`` is the refund being approved or processed: it is left out of net
    paid, of the pending reservations and of the payment's refunds, so it is never checked
    against itself (D-P8). At request time it is ``None`` (the refund does not exist yet).
    """
    student_refunds = list(student_refunds)
    paid = net_paid(payment_amounts, student_refunds, exclude_refund_id=exclude_refund_id)
    pending = pending_reservations(student_refunds, exclude_refund_id=exclude_refund_id)
    available = payment_available(
        source_amount, source_allocations, source_refunds, exclude_refund_id=exclude_refund_id
    )
    credit = credit_available(ledger_balance, pending)
    return RefundCaps(
        net_paid=paid,
        ledger_balance=round_money(ledger_balance),
        credit_balance=ledger_credit_balance(ledger_balance),
        pending_refunds=pending,
        credit_available=credit,
        payment_available=available,
        max_refundable=max_refundable(paid, credit, available),
    )


# --- unallocated credit (BR-BAL-02) ---------------------------------------------------------------


def unallocated_credit(payments: Iterable, allocations: Iterable, refunds: Iterable, as_of=None):
    """``Σ payments − Σ net payment allocations − Σ PROCESSED refunds``, each counted only if
    dated on or before ``as_of`` (received_date, allocated_on, processed_date)."""

    def on_or_before(d):
        return as_of is None or (d is not None and d <= as_of)

    received = dsum(p.amount for p in payments if on_or_before(p.received_date))
    allocated = dsum(a.amount for a in allocations if on_or_before(a.allocated_on))
    processed = dsum(
        r.amount for r in refunds if r.status == PROCESSED and on_or_before(r.processed_date)
    )
    return round_money(received - allocated - processed)


# --- releases (BR-ALC-06; BR-WDR-03 step 2 reuses it in Phase 5) ---------------------------------


@dataclass(frozen=True)
class ReleaseCalc:
    allocation_id: str  # the ALLOCATE row a DEALLOCATE row will reference
    amount: Decimal  # > 0: how much of it to release


class InsufficientAllocations(ValueError):
    """The line's payment allocations hold less than the amount to release."""

    def __init__(self, needed: Decimal, held: Decimal):
        super().__init__(f"needed {needed}, but only {held} is allocated")
        self.needed, self.held = needed, held


def plan_release_lifo(allocations: Iterable, amount) -> list[ReleaseCalc]:
    """Release ``amount`` from one line's payment allocations, last-in-first-out by
    (``allocated_on``, ``allocation_id``) descending.

    ``allocations`` are every ALLOCATE and DEALLOCATE row on the line (any payment); each
    ALLOCATE row can give what is left of it after earlier DEALLOCATE rows that reverse it.
    Raises ``ValueError`` for amount ≤ 0 and ``InsufficientAllocations`` if the line holds
    less than ``amount``.
    """
    needed = to_decimal(amount)
    if needed <= 0:
        raise ValueError(f"amount must be > 0, got {needed}")
    rows = list(allocations)
    released: dict[str, Decimal] = {}
    for r in rows:
        if r.allocation_type == "DEALLOCATE":
            released[r.reverses_id] = released.get(r.reverses_id, ZERO) + (-to_decimal(r.amount))
    candidates = []
    for r in rows:
        if r.allocation_type == "ALLOCATE":
            left = round_money(to_decimal(r.amount) - released.get(r.allocation_id, ZERO))
            if left > 0:
                candidates.append((r, left))
    held = dsum(left for _, left in candidates)
    needed = round_money(needed)
    if held < needed:
        raise InsufficientAllocations(needed, held)
    plan: list[ReleaseCalc] = []
    for r, left in sorted(
        candidates, key=lambda c: (c[0].allocated_on, c[0].allocation_id), reverse=True
    ):
        if needed <= 0:
            break
        take = min(left, needed)
        plan.append(ReleaseCalc(r.allocation_id, take))
        needed = round_money(needed - take)
    return plan
