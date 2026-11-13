"""Pure reconciliation calculators (ARCHITECTURE.md §5).

Phase 5 adds only ``balance_from_ledger`` (decision D-W2), so the frozen §7 worked-example
test can run; everything else in reconciliation arrives in Phase 6.

``balance_from_ledger`` is a thin wrapper: ``billing.calculators.ledger_balance`` stays the
single BR-BAL-01 implementation (decision D13), and parity tests prove the two agree.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from billing.calculators import ledger_balance


def balance_from_ledger(entries: Iterable, as_of: date | None = None) -> Decimal:
    """BR-BAL-01: Σ amount of the entries dated on or before ``as_of`` (all if None)."""
    return ledger_balance(entries, as_of)
