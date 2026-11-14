"""Check registry and the finding draft every check returns.

A check is a pure function ``fn(ds) -> (records_checked, [Finding])`` over a ``Dataset``.
Each one is registered once with its family (ARCHITECTURE §12.1), the frozen rule or
requirement ID it cites (decision D-R8) and whether it can run for one student
(``GLOBAL`` checks are skipped, and listed, in a STUDENT-scope run: decision D-R10).
Severity and recommended action come from ``reconciliation.policy`` by issue type, so a
check cannot pick its own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

FAMILIES = (
    "STRUCTURAL",
    "RELATIONSHIP",
    "TEMPORAL",
    "LIFECYCLE",
    "ARITHMETIC",
    "REQUIRED",
    "RECONCILIATION",
)
GLOBAL, PER_STUDENT = "GLOBAL", "PER_STUDENT"


@dataclass(frozen=True)
class Finding:
    issue_type: str
    rule_id: str
    entity_type: str
    record_id: str
    student_id: str | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None
    details: dict = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Check:
    check_id: str
    family: str
    rule_id: str  # the rule cited by default; a finding may cite a more specific one
    scope: str
    fn: Callable
    description: str


REGISTRY: dict[str, Check] = {}


def check(check_id: str, *, family: str, rule_id: str, scope: str = PER_STUDENT):
    assert family in FAMILIES and scope in (GLOBAL, PER_STUDENT)

    def register(fn):
        if check_id in REGISTRY:
            raise ValueError(f"check {check_id} registered twice")
        first_paragraph = (fn.__doc__ or "").strip().split("\n\n")[0]
        REGISTRY[check_id] = Check(
            check_id, family, rule_id, scope, fn, " ".join(first_paragraph.split())
        )
        return fn

    return register


def ordered_checks() -> list[Check]:
    """Family order of ARCHITECTURE §12.1, then check ID: the run order (REC-06)."""
    return sorted(REGISTRY.values(), key=lambda c: (FAMILIES.index(c.family), c.check_id))
