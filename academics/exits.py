"""Enrollment-exit registry (Phase 5, decision D-W1).

``academics.services.withdraw_enrollment`` / ``cancel_enrollment`` are the documented entry
points (ARCHITECTURE §4) and orchestrate the whole withdrawal or cancellation, but academics
sits below billing, scholarships and payments and may not import them. Billing therefore
registers an **exit handler** here from its ``AppConfig.ready()``; academics calls it inside
the same transaction, after the enrollment's own status change. Billing in turn reaches
scholarships and payments through its own registry (``billing.allocations``).

Handler signature: ``fn(*, kind, enrollment, on_date, refund_pct, actor) -> dict | None``,
``kind`` = "WITHDRAWAL" or "CANCELLATION". The returned dict is the handler's result (for
billing: the invoice, its release plan and the refund quote).

Registration is idempotent (same function, same name: no-op); a different function under
a taken name raises.
"""

from __future__ import annotations

from collections.abc import Callable

KINDS = ("WITHDRAWAL", "CANCELLATION")
_handlers: dict[str, Callable] = {}


def register_exit_handler(name: str, fn: Callable) -> None:
    current = _handlers.get(name)
    if current is None:
        _handlers[name] = fn
    elif current is not fn:
        raise ValueError(f"{name!r} is already registered with a different function")


def run_exit_handlers(*, kind: str, enrollment, on_date, refund_pct, actor: str) -> dict:
    """Call every registered handler (sorted by name); returns {name: result}."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return {
        name: _handlers[name](
            kind=kind, enrollment=enrollment, on_date=on_date, refund_pct=refund_pct, actor=actor
        )
        for name in sorted(_handlers)
    }


def registered() -> list[str]:
    return sorted(_handlers)
