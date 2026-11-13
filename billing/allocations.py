"""Allocation registry: how billing sees allocations owned by higher apps (decision D-S1).

Billing sits below scholarships and payments in the dependency graph (ARCHITECTURE §4), so
it never imports them. Instead they register here from their ``AppConfig.ready()``:

  * an **allocation provider** ``fn(invoice) -> iterable`` per kind (``scholarship`` or
    ``payment``). Each item has ``invoice_line_id``, ``amount`` (signed) and
    ``allocated_on``; payment items also have ``payer_type``. Billing uses them for the
    invoice amounts (BR-INV-04), line open amounts (BR-ALC-02), void (BR-INV-08), late
    fees, statements and balances.
  * a **payment-release handler** (BR-ALC-06) that deallocates payment money LIFO when a
    scholarship needs room on a line. Payments registers it (Phase 4). If none were
    registered, any non-zero release would fail loudly (``PaymentReleaseUnavailable``),
    rolling the scholarship application back.
  * a **student provider** ``fn(student, **kwargs)`` per student-level kind (Phase 4,
    decision D-P4), registered by payments:
      - ``student_credit``: ``fn(student, as_of=None) -> Decimal``, the BR-BAL-02
        unallocated credit computed from payment rows (Σ payments − Σ net payment
        allocations − Σ PROCESSED refunds, each dated on or before ``as_of``);
      - ``refunds``: ``fn(student) -> list`` of the student's refunds, for statements.

Registration is idempotent: registering the same function under the same name again is a
no-op (``ready()`` may run more than once); a different function under a taken name raises.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal

KINDS = ("scholarship", "payment")
STUDENT_KINDS = ("student_credit", "refunds")
_providers: dict[str, dict[str, Callable]] = {kind: {} for kind in KINDS}
_student_providers: dict[str, dict[str, Callable]] = {kind: {} for kind in STUDENT_KINDS}
_release_handlers: dict[str, Callable] = {}


class PaymentReleaseUnavailable(RuntimeError):
    """A scholarship needs payment money released, but no payments handler is registered.

    Payments registers a handler, so this means an integrity problem (the app is missing):
    it is raised inside the caller's transaction, which rolls back completely.
    """


def _register(table: dict[str, Callable], name: str, fn: Callable) -> None:
    current = table.get(name)
    if current is None:
        table[name] = fn
    elif current is not fn:
        raise ValueError(f"{name!r} is already registered with a different function")


def register_allocation_provider(kind: str, name: str, fn: Callable) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    _register(_providers[kind], name, fn)


def register_payment_release_handler(name: str, fn: Callable) -> None:
    _register(_release_handlers, name, fn)


def register_student_provider(kind: str, name: str, fn: Callable) -> None:
    if kind not in STUDENT_KINDS:
        raise ValueError(f"kind must be one of {STUDENT_KINDS}, got {kind!r}")
    _register(_student_providers[kind], name, fn)


def allocations(kind: str, invoice) -> list:
    """Every registered provider's allocations of ``kind`` on ``invoice``."""
    out: list = []
    for name in sorted(_providers[kind]):
        out.extend(_providers[kind][name](invoice))
    return out


def student_values(kind: str, student, **kwargs) -> list:
    """Every registered student provider's value of ``kind`` (one item per provider)."""
    return [
        _student_providers[kind][name](student, **kwargs)
        for name in sorted(_student_providers[kind])
    ]


def release_payments(
    *, invoice, releases: Mapping[str, Decimal], on_date, actor, source_ref
) -> None:
    """BR-ALC-06: free ``releases[line_id]`` of payment money on each line for a scholarship."""
    needed = {line_id: amount for line_id, amount in releases.items() if amount > 0}
    if not needed:
        return
    if not _release_handlers:
        raise PaymentReleaseUnavailable(
            f"{source_ref} needs payment allocations released on {sorted(needed)}, but no "
            "payments handler is registered"
        )
    for name in sorted(_release_handlers):
        _release_handlers[name](
            invoice=invoice, releases=needed, on_date=on_date, actor=actor, source_ref=source_ref
        )


def registered() -> dict[str, list[str]]:
    """Names currently registered (for tests and diagnostics)."""
    return {
        **{kind: sorted(table) for kind, table in _providers.items()},
        **{kind: sorted(table) for kind, table in _student_providers.items()},
        "payment_release": sorted(_release_handlers),
    }
