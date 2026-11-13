"""Student status transitions (DATABASE_DESIGN.md §6): defined once, enforced by services."""

ACTIVE, ON_HOLD, WITHDRAWN, GRADUATED = "ACTIVE", "ON_HOLD", "WITHDRAWN", "GRADUATED"

STUDENT_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({ACTIVE}),
    ACTIVE: frozenset({ON_HOLD, WITHDRAWN, GRADUATED}),
    ON_HOLD: frozenset({ACTIVE}),
    WITHDRAWN: frozenset(),
    GRADUATED: frozenset(),
}
