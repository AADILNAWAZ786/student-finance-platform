"""Refund lifecycle (DATABASE_DESIGN.md §6, BR-REF-03).

∅ → REQUESTED → APPROVED → PROCESSED; REQUESTED / APPROVED → REJECTED.
PROCESSED and REJECTED are terminal. The ledger is debited only on PROCESSED.
"""

from __future__ import annotations

from payments.policy import APPROVED, PROCESSED, REJECTED, REQUESTED

REFUND_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({REQUESTED}),
    REQUESTED: frozenset({APPROVED, REJECTED}),
    APPROVED: frozenset({PROCESSED, REJECTED}),
    PROCESSED: frozenset(),
    REJECTED: frozenset(),
}
