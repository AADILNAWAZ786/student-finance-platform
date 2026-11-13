"""Invoice lifecycle status (BR-INV-08, DATABASE_DESIGN.md §6).

Lifecycle is separate from collection status: ``∅ → ISSUED``, ``ISSUED → CANCELLED``,
``ISSUED → VOID``. CANCELLED and VOID are terminal. Collection statuses (UNPAID, …) are
never lifecycle states.
"""

from __future__ import annotations

from billing.calculators import InvoiceAmounts, check_amounts
from core.lifecycle import transition_allowed

ISSUED, CANCELLED, VOID = "ISSUED", "CANCELLED", "VOID"

LIFECYCLE_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({ISSUED}),
    ISSUED: frozenset({CANCELLED, VOID}),
    CANCELLED: frozenset(),
    VOID: frozenset(),
}

COLLECTION_STATUSES = (
    "UNPAID",
    "PARTIALLY_PAID",
    "OVERDUE",
    "PAID",
    "SCHOLARSHIP_COVERED",
    "ADJUSTED_TO_ZERO",
)


def lifecycle_transition_allowed(current: str | None, target: str) -> bool:
    return transition_allowed(LIFECYCLE_TRANSITIONS, current, target)


def can_void(amounts: InvoiceAmounts) -> bool:
    """BR-INV-08: void only when the *current* signed scholarship_applied and amount_paid
    are both 0. Allocations that were later fully reversed or deallocated (net 0) do not
    block a void; there is no permanent "ever allocated" prohibition."""
    check_amounts(amounts)
    return amounts.scholarship_applied == 0 and amounts.amount_paid == 0
