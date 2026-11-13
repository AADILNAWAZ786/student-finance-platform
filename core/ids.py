"""Deterministic business IDs (DATABASE_DESIGN.md §2).

Two layers:
  * pure formatters/validators, which never touch the database (used by the generator);
  * ``next_id`` / ``reserve_sequence``, which allocate counters from ``IdSequence``
    inside the caller's transaction (used by services).

IDs are immutable: they are assigned once, before insert, and never reformatted.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from django.core.validators import RegexValidator
from django.db import transaction


class IdFormatError(ValueError):
    """A component of a business ID is missing, out of range or malformed."""


SEASONS = ("SPRING", "SUMMER", "FALL")
FEE_TYPES = (
    "TUITION",
    "LAB",
    "REGISTRATION",
    "TECHNOLOGY",
    "LIBRARY",
    "STUDENT_ACTIVITY",
    "INTERNATIONAL_SERVICE",
)
RESIDENCY_CODES = {"DOMESTIC": "DOM", "INTERNATIONAL": "INTL", "ALL": "ALL"}
EVENT_ENTITIES = ("STU", "ENR", "INV", "SCH", "REF")

_SEASON_RE = "|".join(SEASONS)
_FEE_RE = "|".join(FEE_TYPES)
_EVT_RE = "|".join(EVENT_ENTITIES)

# Full-match patterns, one per entity (DATABASE_DESIGN.md §2).
ID_PATTERNS: dict[str, str] = {
    "student": r"STU-\d{6}",
    "program": r"PRG-\d{3}",
    "term": rf"TERM-\d{{4}}-(?:{_SEASON_RE})",
    "fee_schedule": (
        rf"FEE-PRG-\d{{3}}-\d{{4}}-(?:{_SEASON_RE})-(?:{_FEE_RE})-(?:DOM|INTL|ALL)-V\d{{1,2}}"
    ),
    "enrollment": r"ENR-\d{4}-\d{6}",
    "invoice": r"INV-STU-\d{6}-\d{4}-\d{6}",
    "invoice_line": r"INV-STU-\d{6}-\d{4}-\d{6}-L\d{2}",
    "scholarship_type": r"SCT-\d{3}",
    "scholarship_type_program": r"SCT-\d{3}-PRG-\d{3}",
    "eligibility": r"ELG-\d{4}-\d{6}",
    "scholarship": r"SCH-\d{4}-\d{6}",
    "scholarship_allocation": r"SCHA-SCH-\d{4}-\d{6}-\d{2}",
    "payment": r"PAY-\d{4}-\d{6}",
    "payment_allocation": r"ALLOC-PAY-\d{4}-\d{6}-\d{2}",
    "refund": r"REF-\d{4}-\d{6}",
    "ledger_entry": r"LED-\d{4}-\d{7}",
    "status_event": rf"EVT-(?:{_EVT_RE})-\d{{4}}-\d{{7}}",
    "balance_snapshot": r"SNAP-\d{4}-\d{6}",
    "reconciliation_run": r"RECON-\d{4}-\d{6}",
    "anomaly_finding": r"FND-\d{4}-\d{6}",
    "generation_run": r"GEN-\d{4}-\d{4}",
    "ground_truth_label": r"GT-\d{4}-\d{6}",
}
_COMPILED = {kind: re.compile(rf"\A{p}\Z") for kind, p in ID_PATTERNS.items()}

# Field lengths used by models (DATABASE_DESIGN.md §2).
ID_MAX_LENGTHS: dict[str, int] = {
    "student": 10,
    "program": 7,
    "term": 16,
    "fee_schedule": 64,
    "enrollment": 15,
    "invoice": 26,
    "invoice_line": 30,
    "scholarship_type": 7,
    "scholarship_type_program": 16,
    "eligibility": 15,
    "scholarship": 15,
    "scholarship_allocation": 24,
    "payment": 15,
    "payment_allocation": 24,
    "refund": 15,
    "ledger_entry": 16,
    "status_event": 20,
    "balance_snapshot": 16,
    "reconciliation_run": 17,
    "anomaly_finding": 15,
    "generation_run": 13,
    "ground_truth_label": 14,
}


# --- validation ---------------------------------------------------------------


def is_valid_id(kind: str, value: object) -> bool:
    if kind not in _COMPILED:
        raise KeyError(f"unknown ID kind {kind!r}")
    return isinstance(value, str) and bool(_COMPILED[kind].match(value))


def validate_id(kind: str, value: object) -> str:
    if not is_valid_id(kind, value):
        raise IdFormatError(f"{value!r} is not a valid {kind} ID")
    return value  # type: ignore[return-value]


def id_validator(kind: str) -> RegexValidator:
    """Model/serializer validator for a business-ID field."""
    return RegexValidator(
        regex=rf"\A{ID_PATTERNS[kind]}\Z",
        message=f"Enter a valid {kind.replace('_', ' ')} ID.",
        code=f"invalid_{kind}_id",
    )


# --- pure formatters -------------------------------------------------------------


def _counter(n: object, width: int, what: str) -> str:
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n < 10**width:
        raise IdFormatError(f"{what} counter must be an int in 1..{10**width - 1}, got {n!r}")
    return f"{n:0{width}d}"


def _year(year: object) -> str:
    if not isinstance(year, int) or isinstance(year, bool) or not 2000 <= year <= 2999:
        raise IdFormatError(f"year must be an int in 2000..2999, got {year!r}")
    return str(year)


def student_id(n: int) -> str:
    return f"STU-{_counter(n, 6, 'student')}"


def student_number(sid: str) -> str:
    """Numeric part of a student ID, as used inside invoice IDs."""
    return validate_id("student", sid)[4:]


def program_id(n: int) -> str:
    return f"PRG-{_counter(n, 3, 'program')}"


def term_id(year: int, season: str) -> str:
    if season not in SEASONS:
        raise IdFormatError(f"season must be one of {SEASONS}, got {season!r}")
    return f"TERM-{_year(year)}-{season}"


def fee_schedule_id(prog_id: str, trm_id: str, fee_type: str, residency: str, version: int) -> str:
    validate_id("program", prog_id)
    validate_id("term", trm_id)
    if fee_type not in FEE_TYPES:
        raise IdFormatError(f"unknown fee type {fee_type!r}")
    if residency not in RESIDENCY_CODES:
        raise IdFormatError(f"unknown residency {residency!r}")
    term_part = trm_id[len("TERM-") :]
    return (
        f"FEE-{prog_id}-{term_part}-{fee_type}-{RESIDENCY_CODES[residency]}-"
        f"V{int(_counter(version, 2, 'fee schedule version'))}"
    )


def enrollment_id(year: int, n: int) -> str:
    return f"ENR-{_year(year)}-{_counter(n, 6, 'enrollment')}"


def invoice_id(sid: str, year: int, n: int) -> str:
    return f"INV-STU-{student_number(sid)}-{_year(year)}-{_counter(n, 6, 'invoice')}"


def invoice_line_id(inv_id: str, n: int) -> str:
    return f"{validate_id('invoice', inv_id)}-L{_counter(n, 2, 'invoice line')}"


def scholarship_type_id(n: int) -> str:
    return f"SCT-{_counter(n, 3, 'scholarship type')}"


def scholarship_type_program_id(type_id: str, prog_id: str) -> str:
    return f"{validate_id('scholarship_type', type_id)}-{validate_id('program', prog_id)}"


def eligibility_id(year: int, n: int) -> str:
    return f"ELG-{_year(year)}-{_counter(n, 6, 'eligibility')}"


def scholarship_id(year: int, n: int) -> str:
    return f"SCH-{_year(year)}-{_counter(n, 6, 'scholarship')}"


def scholarship_allocation_id(sch_id: str, n: int) -> str:
    return f"SCHA-{validate_id('scholarship', sch_id)}-{_counter(n, 2, 'scholarship allocation')}"


def payment_id(year: int, n: int) -> str:
    return f"PAY-{_year(year)}-{_counter(n, 6, 'payment')}"


def payment_allocation_id(pay_id: str, n: int) -> str:
    return f"ALLOC-{validate_id('payment', pay_id)}-{_counter(n, 2, 'payment allocation')}"


def refund_id(year: int, n: int) -> str:
    return f"REF-{_year(year)}-{_counter(n, 6, 'refund')}"


def ledger_entry_id(year: int, n: int) -> str:
    return f"LED-{_year(year)}-{_counter(n, 7, 'ledger entry')}"


def status_event_id(entity: str, year: int, n: int) -> str:
    if entity not in EVENT_ENTITIES:
        raise IdFormatError(f"event entity must be one of {EVENT_ENTITIES}, got {entity!r}")
    return f"EVT-{entity}-{_year(year)}-{_counter(n, 7, 'status event')}"


def balance_snapshot_id(year: int, n: int) -> str:
    return f"SNAP-{_year(year)}-{_counter(n, 6, 'balance snapshot')}"


def reconciliation_run_id(year: int, n: int) -> str:
    return f"RECON-{_year(year)}-{_counter(n, 6, 'reconciliation run')}"


def anomaly_finding_id(year: int, n: int) -> str:
    return f"FND-{_year(year)}-{_counter(n, 6, 'anomaly finding')}"


def generation_run_id(year: int, n: int) -> str:
    return f"GEN-{_year(year)}-{_counter(n, 4, 'generation run')}"


def ground_truth_label_id(year: int, n: int) -> str:
    return f"GT-{_year(year)}-{_counter(n, 6, 'ground truth label')}"


# --- sequence-backed allocation ------------------------------------------------------
# kind -> (sequence key builder, formatter(counter, **scope))
_Scope = dict[str, object]
_SEQUENCED: dict[str, tuple[Callable[[_Scope], str], Callable[[int, _Scope], str]]] = {
    "student": (lambda s: "STU", lambda n, s: student_id(n)),
    "program": (lambda s: "PRG", lambda n, s: program_id(n)),
    "scholarship_type": (lambda s: "SCT", lambda n, s: scholarship_type_id(n)),
    "enrollment": (lambda s: f"ENR:{_year(s['year'])}", lambda n, s: enrollment_id(s["year"], n)),
    "invoice": (
        lambda s: f"INV:{validate_id('student', s['parent'])}:{_year(s['year'])}",
        lambda n, s: invoice_id(s["parent"], s["year"], n),
    ),
    "invoice_line": (
        lambda s: f"LINE:{validate_id('invoice', s['parent'])}",
        lambda n, s: invoice_line_id(s["parent"], n),
    ),
    "eligibility": (lambda s: f"ELG:{_year(s['year'])}", lambda n, s: eligibility_id(s["year"], n)),
    "scholarship": (lambda s: f"SCH:{_year(s['year'])}", lambda n, s: scholarship_id(s["year"], n)),
    "scholarship_allocation": (
        lambda s: f"SCHA:{validate_id('scholarship', s['parent'])}",
        lambda n, s: scholarship_allocation_id(s["parent"], n),
    ),
    "payment": (lambda s: f"PAY:{_year(s['year'])}", lambda n, s: payment_id(s["year"], n)),
    "payment_allocation": (
        lambda s: f"ALLOC:{validate_id('payment', s['parent'])}",
        lambda n, s: payment_allocation_id(s["parent"], n),
    ),
    "refund": (lambda s: f"REF:{_year(s['year'])}", lambda n, s: refund_id(s["year"], n)),
    "ledger_entry": (
        lambda s: f"LED:{_year(s['year'])}",
        lambda n, s: ledger_entry_id(s["year"], n),
    ),
    "status_event": (
        lambda s: f"EVT:{s['entity']}:{_year(s['year'])}",
        lambda n, s: status_event_id(s["entity"], s["year"], n),
    ),
    "balance_snapshot": (
        lambda s: f"SNAP:{_year(s['year'])}",
        lambda n, s: balance_snapshot_id(s["year"], n),
    ),
    "reconciliation_run": (
        lambda s: f"RECON:{_year(s['year'])}",
        lambda n, s: reconciliation_run_id(s["year"], n),
    ),
    "anomaly_finding": (
        lambda s: f"FND:{_year(s['year'])}",
        lambda n, s: anomaly_finding_id(s["year"], n),
    ),
    "generation_run": (
        lambda s: f"GEN:{_year(s['year'])}",
        lambda n, s: generation_run_id(s["year"], n),
    ),
}


def sequence_key(kind: str, **scope: object) -> str:
    try:
        key_builder, _ = _SEQUENCED[kind]
    except KeyError:
        raise IdFormatError(f"{kind!r} IDs are not sequence-allocated") from None
    try:
        return key_builder(scope)
    except KeyError as missing:
        raise IdFormatError(f"{kind} ID needs scope {missing}") from None


def reserve_sequence(key: str, count: int = 1, *, using: str = "default") -> int:
    """Reserve ``count`` consecutive counter values for ``key``; return the first.

    Runs inside the caller's transaction (a savepoint if one is open), so a rollback
    also releases the reservation. ``select_for_update`` is a no-op on SQLite, where
    the IMMEDIATE transaction mode serializes writers instead (ARCHITECTURE §8.2).
    """
    from core.models import IdSequence  # local import: keep formatters ORM-free

    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"count must be a positive int, got {count!r}")
    with transaction.atomic(using=using):
        seq, _ = (
            IdSequence.objects.using(using)
            .select_for_update()
            .get_or_create(key=key, defaults={"last_value": 0})
        )
        first = seq.last_value + 1
        seq.last_value += count
        seq.save(using=using, update_fields=["last_value"])
    return first


def next_id(kind: str, *, using: str = "default", **scope: object) -> str:
    """Allocate and format the next ID of ``kind``.

    Examples: ``next_id("payment", year=2026)`` -> ``PAY-2026-000001``;
    ``next_id("invoice", parent="STU-000001", year=2026)`` -> ``INV-STU-000001-2026-000001``;
    ``next_id("payment_allocation", parent="PAY-2026-000001")`` -> ``ALLOC-PAY-2026-000001-01``.
    """
    key = sequence_key(kind, **scope)
    _, formatter = _SEQUENCED[kind]
    with transaction.atomic(using=using):
        n = reserve_sequence(key, using=using)
        return formatter(n, scope)  # raises IdFormatError on overflow -> rolls back
