"""Enrollment status transitions (DATABASE_DESIGN.md §6): defined once, enforced by services.

Date conditions attached to each transition:
  ENROLLED -> WITHDRAWN   on/after term start        (Phase 5: withdraw_enrollment)
  ENROLLED -> CANCELLED   before term start          (Phase 5: cancel_enrollment)
  ENROLLED -> COMPLETED   after term end             (academics.services.complete_enrollment)
"""

ENROLLED, WITHDRAWN, CANCELLED, COMPLETED = "ENROLLED", "WITHDRAWN", "CANCELLED", "COMPLETED"

ENROLLMENT_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({ENROLLED}),
    ENROLLED: frozenset({WITHDRAWN, CANCELLED, COMPLETED}),
    WITHDRAWN: frozenset(),
    CANCELLED: frozenset(),
    COMPLETED: frozenset(),
}
