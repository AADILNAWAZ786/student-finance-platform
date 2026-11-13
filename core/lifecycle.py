"""Shared lifecycle check. Each app defines its own transition table in ``<app>/lifecycle.py``
(DATABASE_DESIGN.md §6); this helper enforces it uniformly."""

from __future__ import annotations

from collections.abc import Mapping, Set

from core.exceptions import domain_error

Transitions = Mapping[str | None, Set[str]]


def transition_allowed(transitions: Transitions, current: str | None, target: str) -> bool:
    return target in transitions.get(current, frozenset())


def check_transition(
    transitions: Transitions,
    current: str | None,
    target: str,
    *,
    entity: str,
    record_id: str | None = None,
    rule_id: str | None = None,
) -> None:
    """Raise ``409 ILLEGAL_TRANSITION`` unless ``current -> target`` is in the table."""
    if not transition_allowed(transitions, current, target):
        allowed = sorted(transitions.get(current, frozenset()))
        raise domain_error(
            "ILLEGAL_TRANSITION",
            f"{entity} cannot move from {current} to {target}.",
            rule_id=rule_id,
            details={
                "entity": entity,
                "record_id": record_id,
                "from_status": current,
                "to_status": target,
                "allowed": allowed,
            },
        )
