"""Scholarship (award) lifecycle (DATABASE_DESIGN.md §6).

∅ → OFFERED → ACCEPTED → APPLIED; APPLIED → PARTIALLY_REVERSED;
OFFERED / ACCEPTED / APPLIED / PARTIALLY_REVERSED → REVOKED. REVOKED is terminal.

Phase 3: apply accepts automatically (OFFERED → ACCEPTED → APPLIED in one transaction,
D-S13). REVOKED is in the table but not exposed by any service or endpoint (D-S14). A full
reversal leaves the status PARTIALLY_REVERSED; the design has no other status (D-S16).
"""

from __future__ import annotations

OFFERED, ACCEPTED, APPLIED = "OFFERED", "ACCEPTED", "APPLIED"
PARTIALLY_REVERSED, REVOKED = "PARTIALLY_REVERSED", "REVOKED"

SCHOLARSHIP_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({OFFERED}),
    OFFERED: frozenset({ACCEPTED, REVOKED}),
    ACCEPTED: frozenset({APPLIED, REVOKED}),
    APPLIED: frozenset({PARTIALLY_REVERSED, REVOKED}),
    PARTIALLY_REVERSED: frozenset({REVOKED}),
    REVOKED: frozenset(),
}

# an award reserves its full awarded_amount until it is applied (award-stage guardrail)
UNAPPLIED = frozenset({OFFERED, ACCEPTED})
