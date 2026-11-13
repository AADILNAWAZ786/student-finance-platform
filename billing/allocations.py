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
      - ``refunds``: ``fn(student) -> list`` of the student's refunds, for statements;
      - ``refund_quote``: ``fn(student) -> dict``, the refundable summary returned with a
        withdrawal or cancellation (Phase 5).
  * **adjustment handlers** (Phase 5, BR-WDR-03), exactly one per kind, used by
    ``billing.withdrawals`` when a withdrawal or cancellation adjusts a line:
      - ``scholarship_reversal`` (scholarships): ``fn(*, invoice_line_id, amount, reason,
        on_date, actor, source_ref) -> list`` reverses ``amount`` of the line's scholarship
        allocations (BR-SCH-07, split by decision D-W4);
      - ``payment_release`` (payments): ``fn(*, deallocations, rule, on_date, date_field,
        actor, source_ref) -> list`` deallocates [(allocation_id, amount)] (LIFO plan
        computed by billing). Neither refreshes the collection status: billing does it once,
        after every row is written.

Registration is idempotent: registering the same function under the same name again is a
no-op (``ready()`` may run more than once); a different function under a taken name raises.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal

KINDS = ("scholarship", "payment")
STUDENT_KINDS = ("student_credit", "refunds", "refund_quote")
ADJUSTMENT_KINDS = ("scholarship_reversal", "payment_release")
_providers: dict[str, dict[str, Callable]] = {kind: {} for kind in KINDS}
_student_providers: dict[str, dict[str, Callable]] = {kind: {} for kind in STUDENT_KINDS}
_release_handlers: dict[str, Callable] = {}
_adjustment_handlers: dict[str, dict[str, Callable]] = {kind: {} for kind in ADJUSTMENT_KINDS}


class PaymentReleaseUnavailable(RuntimeError):
    """A scholarship needs payment money released, but no payments handler is registered.

    Payments registers a handler, so this means an integrity problem (the app is missing):
    it is raised inside the caller's transaction, which rolls back completely.
    """


class AdjustmentHandlerUnavailable(RuntimeError):
    """A withdrawal or cancellation must reverse scholarship or release payment money, but
    exactly one handler of that kind is not registered (an integrity problem): raised inside
    the caller's transaction, which rolls back completely."""


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


def register_adjustment_handler(kind: str, name: str, fn: Callable) -> None:
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError(f"kind must be one of {ADJUSTMENT_KINDS}, got {kind!r}")
    _register(_adjustment_handlers[kind], name, fn)


def _one_handler(kind: str) -> Callable:
    handlers = _adjustment_handlers[kind]
    if len(handlers) != 1:
        raise AdjustmentHandlerUnavailable(
            f"exactly one {kind} handler must be registered, found {sorted(handlers)}"
        )
    return next(iter(handlers.values()))


def reverse_scholarship_for_adjustment(
    *, invoice_line_id: str, amount: Decimal, reason: str, on_date, actor, source_ref
) -> list:
    """BR-WDR-03 step 1 / BR-SCH-07: reverse ``amount`` of the line's scholarship."""
    if amount <= 0:
        return []
    return _one_handler("scholarship_reversal")(
        invoice_line_id=invoice_line_id,
        amount=amount,
        reason=reason,
        on_date=on_date,
        actor=actor,
        source_ref=source_ref,
    )


def release_payments_for_adjustment(
    *, deallocations, rule: str, on_date, date_field: str, actor, source_ref
) -> list:
    """BR-WDR-03 step 2: deallocate [(allocation_id, amount)] with ``rule``."""
    deallocations = [(a, amt) for a, amt in deallocations if amt > 0]
    if not deallocations:
        return []
    return _one_handler("payment_release")(
        deallocations=deallocations,
        rule=rule,
        on_date=on_date,
        date_field=date_field,
        actor=actor,
        source_ref=source_ref,
    )


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
        **{f"adjustment_{kind}": sorted(table) for kind, table in _adjustment_handlers.items()},
    }
