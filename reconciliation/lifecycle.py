"""ReconciliationRun status transitions (process rule REC-01, approved decision D-R1).

DATABASE_DESIGN §3 lists the values RUNNING, COMPLETED and FAILED; the transitions are the
approved run workflow: a run is stored RUNNING before its checks, and ends COMPLETED (with
all its findings, in one transaction) or FAILED (with none). Both ends are terminal.
"""

from reconciliation.policy import COMPLETED, FAILED, RUNNING

RUN_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({RUNNING}),
    RUNNING: frozenset({COMPLETED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
}


def run_transition_allowed(current: str | None, target: str) -> bool:
    return target in RUN_TRANSITIONS.get(current, frozenset())
