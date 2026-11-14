"""Structural checks (ARCHITECTURE §12.1): FK orphans, enum validity, ID format, and the
DATABASE_DESIGN §4 uniqueness catalogue (PK uniqueness included).

IDs (decision D-R13): every ID must match its ``core.ids`` pattern and equal the ID the
``core.ids`` formatter builds from the row itself: its parent IDs (an invoice's student
number, a line's invoice, an allocation's payment or award) and the business year the
services pass to ``next_id`` (received_date, award_date, term year, entry_date, …).
Counter gaps (S-ID-GAPS-*, below) are checked within each core.ids sequence scope: the owner
confirmed DATABASE_DESIGN §2 and ARCHITECTURE §12.1 as the explicit requirement D-R13 asked for.
"""

from __future__ import annotations

import re
from collections import defaultdict

from core import ids
from reconciliation import policy
from reconciliation.checks.base import GLOBAL, Finding, check
from reconciliation.dataset import TABLES

R_ORPHAN, R_ID, R_STRUCT = "R-22", "R-28", "R-39"

EVENT_ENTITY = {
    "student_events": "STU",
    "enrollment_events": "ENR",
    "invoice_events": "INV",
    "scholarship_events": "SCH",
    "refund_events": "REF",
}


def _counter(pk: str) -> int:
    last = pk.rsplit("-", 1)[1]
    return int(last[1:] if last.startswith(("L", "V")) else last)


def _expected_id(name: str, row, ds) -> str | None:
    """The ID ``core.ids`` builds for this row, or None when the kind has nothing to derive
    beyond its pattern (programs, students, scholarship types)."""
    pk = row.pk
    if name in EVENT_ENTITY:
        return ids.status_event_id(EVENT_ENTITY[name], row.event_date.year, _counter(pk))
    match name:
        case "terms":
            return ids.term_id(row.year, row.season)
        case "fee_schedules":
            return ids.fee_schedule_id(
                row.program_id, row.term_id, row.fee_type, row.residency, row.version
            )
        case "enrollments":
            term = ds.by_id["terms"].get(row.term_id)
            return None if term is None else ids.enrollment_id(term.year, _counter(pk))
        case "invoices":
            return ids.invoice_id(row.student_id, row.issue_date.year, _counter(pk))
        case "lines":
            return ids.invoice_line_id(row.invoice_id, _counter(pk))
        case "ledger":
            return ids.ledger_entry_id(row.entry_date.year, _counter(pk))
        case "type_programs":
            return ids.scholarship_type_program_id(row.scholarship_type_id, row.program_id)
        case "eligibilities":
            return ids.eligibility_id(row.evaluated_on.year, _counter(pk))
        case "scholarships":
            return ids.scholarship_id(row.award_date.year, _counter(pk))
        case "scholarship_allocations":
            return ids.scholarship_allocation_id(row.scholarship_id, _counter(pk))
        case "payments":
            return ids.payment_id(row.received_date.year, _counter(pk))
        case "payment_allocations":
            return ids.payment_allocation_id(row.payment_id, _counter(pk))
        case "refunds":
            return ids.refund_id(row.requested_date.year, _counter(pk))
        case "snapshots":
            return ids.balance_snapshot_id(row.as_of_date.year, _counter(pk))
    return None


def _student_of(name: str, row, ds) -> str | None:
    if name == "students":
        return row.pk
    sid = getattr(row, "student_id", None)
    if sid is not None:
        return sid
    parent = {
        "enrollment_events": ("enrollments", "enrollment_id"),
        "lines": ("invoices", "invoice_id"),
        "invoice_events": ("invoices", "invoice_id"),
        "scholarship_allocations": ("scholarships", "scholarship_id"),
        "scholarship_events": ("scholarships", "scholarship_id"),
        "payment_allocations": ("payments", "payment_id"),
        "refund_events": ("refunds", "refund_id"),
    }.get(name)
    if parent is None:
        return None
    owner = ds.by_id[parent[0]].get(getattr(row, parent[1], None))
    return None if owner is None else owner.student_id


def _tables(student_owned: bool):
    return [n for n, (_, _, owned) in TABLES.items() if owned == student_owned]


def _ids(ds, student_owned: bool):
    findings, checked = [], 0
    for name in _tables(student_owned):
        entity, kind, _ = TABLES[name]
        for row in getattr(ds, name):
            checked += 1
            pk, problem, expected = row.pk, None, None
            if not ids.is_valid_id(kind, pk):
                problem = "PATTERN"
            else:
                try:
                    expected = _expected_id(name, row, ds)
                except (ids.IdFormatError, AttributeError, ValueError):
                    problem = "NOT_DERIVABLE"
                else:
                    if expected is not None and expected != pk:
                        problem = "PREFIX_OR_YEAR"
            if problem:
                findings.append(
                    Finding(
                        policy.INVALID_ID_FORMAT,
                        R_ID,
                        entity,
                        str(pk),
                        _student_of(name, row, ds),
                        details={"kind": kind, "problem": problem, "expected_id": expected},
                    )
                )
    return checked, findings


@check("S-ID-DOMAIN", family="STRUCTURAL", rule_id=R_ID)
def ids_of_student_records(ds):
    """Student-owned IDs match their core.ids pattern, parents and business year."""
    return _ids(ds, True)


@check("S-ID-MASTER", family="STRUCTURAL", rule_id=R_ID, scope=GLOBAL)
def ids_of_master_data(ds):
    """Master-data IDs (programs, terms, fee schedules, scholarship types) are well formed."""
    return _ids(ds, False)


def _enums(ds, student_owned: bool):
    findings, checked = [], 0
    for name in _tables(student_owned):
        entity = TABLES[name][0]
        fields = ds.enum_fields.get(entity, {})
        for row in getattr(ds, name):
            for attname, (allowed, nullable) in sorted(fields.items()):
                checked += 1
                value = getattr(row, attname)
                if value is None and nullable:
                    continue
                if value not in allowed:
                    findings.append(
                        Finding(
                            policy.INVALID_ENUM,
                            R_STRUCT,
                            entity,
                            str(row.pk),
                            _student_of(name, row, ds),
                            details={"field": attname, "value": value},
                        )
                    )
    return checked, findings


@check("S-ENUM-DOMAIN", family="STRUCTURAL", rule_id=R_STRUCT)
def enums_of_student_records(ds):
    """Every enum column of a student-owned row holds a documented value."""
    return _enums(ds, True)


@check("S-ENUM-MASTER", family="STRUCTURAL", rule_id=R_STRUCT, scope=GLOBAL)
def enums_of_master_data(ds):
    """Every enum column of a master-data row holds a documented value."""
    return _enums(ds, False)


@check("S-FK-ORPHAN", family="STRUCTURAL", rule_id=R_ORPHAN, scope=GLOBAL)
def orphan_foreign_keys(ds):
    """No foreign key points at a missing row (SQLite foreign_key_check)."""
    findings = [
        Finding(
            policy.ORPHAN_FK,
            R_ORPHAN,
            o.entity_type,
            str(o.record_id),
            details={"field": o.field, "missing_parent_table": o.target},
        )
        for o in ds.orphans
    ]
    return len(ds.orphans), findings


# DATABASE_DESIGN §4 (the invoice rule is DUPLICATE_INVOICE, a required type, not here)
UNIQUE_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "students": (("email",),),
    "enrollments": (("student_id", "term_id"),),
    "scholarships": (("eligibility_id",),),
    "payments": (("external_reference",),),
    "ledger": (
        ("invoice_line_id",),
        ("scholarship_allocation_id",),
        ("payment_id",),
        ("refund_id",),
    ),
    "snapshots": (("student_id", "as_of_date"),),
    "programs": (("code",),),
    "terms": (("year", "season"),),
    "fee_schedules": (
        ("program_id", "term_id", "fee_type", "residency", "version"),
        ("program_id", "term_id", "fee_type", "residency", "effective_from"),
        ("supersedes_id",),
    ),
    "type_programs": (("scholarship_type_id", "program_id"),),
}


def _duplicates(ds, student_owned: bool):
    findings, checked = [], 0
    for name in _tables(student_owned):
        entity = TABLES[name][0]
        rows = getattr(ds, name)
        for key in (("pk",), *UNIQUE_KEYS.get(name, ())):
            groups = defaultdict(list)
            for row in rows:
                value = tuple(getattr(row, f) for f in key)
                if any(v is None for v in value):
                    continue  # NULLs never collide (supersedes of V1, ledger sources)
                groups[value].append(row)
            checked += len(rows)
            for value, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
                if len(members) < 2:
                    continue
                first, *later = sorted(members, key=lambda r: str(r.pk))
                for row in later:
                    findings.append(
                        Finding(
                            policy.DUPLICATE_RECORD,
                            R_STRUCT,
                            entity,
                            str(row.pk),
                            _student_of(name, row, ds),
                            details={
                                "key": list(key),
                                "value": [str(v) for v in value],
                                "first_record_id": str(first.pk),
                            },
                        )
                    )
    return checked, findings


@check("S-DUP-DOMAIN", family="STRUCTURAL", rule_id=R_STRUCT)
def duplicate_student_records(ds):
    """No two student-owned rows share a PK or a DATABASE_DESIGN §4 unique key."""
    return _duplicates(ds, True)


@check("S-DUP-MASTER", family="STRUCTURAL", rule_id=R_STRUCT, scope=GLOBAL)
def duplicate_master_records(ds):
    """No two master-data rows share a PK or a DATABASE_DESIGN §4 unique key."""
    return _duplicates(ds, False)


# --- counter gaps (DATABASE_DESIGN §2 "Reconciliation checks format, counter gaps and
# duplicates"; ARCHITECTURE §12.1 "ID counter gaps/duplicates"; D-R13 as revised) ------------
#
# Each sequence is exactly a core.ids sequence key (``sequence_key``), so a counter is only
# compared with counters of the same kind, year and parent. Every service allocates IDs
# inside its own transaction and a rollback releases the counter, so valid data numbers
# every sequence 1, 2, 3, … with no hole; a hole means a row is missing. Only holes between
# 1 and the highest counter present are reported: IdSequence (internal, not exported) is
# never read, so a reserved-but-unused trailing counter is never a finding. Natural keys
# (Term, FeeSchedule, ScholarshipTypeProgram) have no counter; the detector's own runs and
# findings, and Phase 7's generation runs and labels, are not domain data.

# table → (core.ids kind, ID regex with the counter last, scope from the match, width)
GAP_SEQUENCES: dict[str, tuple[str, str, object]] = {
    "students": ("student", r"STU-(?P<n>\d{6})", lambda m: {}),
    "programs": ("program", r"PRG-(?P<n>\d{3})", lambda m: {}),
    "scholarship_types": ("scholarship_type", r"SCT-(?P<n>\d{3})", lambda m: {}),
    "enrollments": (
        "enrollment",
        r"ENR-(?P<y>\d{4})-(?P<n>\d{6})",
        lambda m: {"year": int(m["y"])},
    ),
    "eligibilities": (
        "eligibility",
        r"ELG-(?P<y>\d{4})-(?P<n>\d{6})",
        lambda m: {"year": int(m["y"])},
    ),
    "scholarships": (
        "scholarship",
        r"SCH-(?P<y>\d{4})-(?P<n>\d{6})",
        lambda m: {"year": int(m["y"])},
    ),
    "payments": ("payment", r"PAY-(?P<y>\d{4})-(?P<n>\d{6})", lambda m: {"year": int(m["y"])}),
    "refunds": ("refund", r"REF-(?P<y>\d{4})-(?P<n>\d{6})", lambda m: {"year": int(m["y"])}),
    "ledger": ("ledger_entry", r"LED-(?P<y>\d{4})-(?P<n>\d{7})", lambda m: {"year": int(m["y"])}),
    "snapshots": (
        "balance_snapshot",
        r"SNAP-(?P<y>\d{4})-(?P<n>\d{6})",
        lambda m: {"year": int(m["y"])},
    ),
    **{
        name: (
            "status_event",
            r"EVT-(?P<e>[A-Z]{3})-(?P<y>\d{4})-(?P<n>\d{7})",
            lambda m: {"entity": m["e"], "year": int(m["y"])},
        )
        for name in EVENT_ENTITY
    },
    "invoices": (
        "invoice",
        r"INV-(?P<p>STU-\d{6})-(?P<y>\d{4})-(?P<n>\d{6})",
        lambda m: {"parent": m["p"], "year": int(m["y"])},
    ),
    "lines": (
        "invoice_line",
        r"(?P<p>INV-STU-\d{6}-\d{4}-\d{6})-L(?P<n>\d{2})",
        lambda m: {"parent": m["p"]},
    ),
    "scholarship_allocations": (
        "scholarship_allocation",
        r"SCHA-(?P<p>SCH-\d{4}-\d{6})-(?P<n>\d{2})",
        lambda m: {"parent": m["p"]},
    ),
    "payment_allocations": (
        "payment_allocation",
        r"ALLOC-(?P<p>PAY-\d{4}-\d{6})-(?P<n>\d{2})",
        lambda m: {"parent": m["p"]},
    ),
}
# sequences scoped to one student's own parent rows can run in a STUDENT-scope run
PARENT_SCOPED = ("invoices", "lines", "scholarship_allocations", "payment_allocations")
_GAP_RE = {name: re.compile(rf"\A{rx}\Z") for name, (_, rx, _) in GAP_SEQUENCES.items()}


def _slots(name: str, row, ds) -> tuple[list[str], bool]:
    """The IDs whose sequence slot this row fills, and whether its ID is misnumbered.

    A well-numbered row fills its own slot. A row whose ID S-ID-* reports (wrong year or
    parent) fills both its own slot and the one core.ids would have given it, so one bad ID
    is reported once (INVALID_ID_FORMAT, PREFIX_OR_YEAR), not again as gaps; and a sequence
    holding only misnumbered rows is not checked (its "gap" is that same problem)."""
    if not ids.is_valid_id(GAP_SEQUENCES[name][0], row.pk):
        return [], True  # a malformed ID is S-ID-*'s finding (PATTERN) and fills no slot
    own = row.pk
    try:
        expected = _expected_id(name, row, ds)
    except (ids.IdFormatError, AttributeError, ValueError):
        expected = None
    if expected is None or expected == own:
        return ([own] if own else []), False
    return [x for x in (own, expected) if x], True


def _missing_runs(counters: list[int]) -> list[tuple[int, int]]:
    """[(first, last)] of the counters absent between 1 and max(counters), ascending."""
    present, runs, start = set(counters), [], None
    for n in range(1, max(counters) + 1):
        if n not in present and start is None:
            start = n
        if n in present and start is not None:
            runs.append((start, n - 1))
            start = None
    return runs


def _gaps(ds, tables):
    findings, checked = [], 0
    for name in tables:
        entity = TABLES[name][0]
        kind, _, scope_of = GAP_SEQUENCES[name]
        groups: dict[str, list] = defaultdict(list)
        scopes: dict[str, dict] = {}
        well_numbered: set[str] = set()
        for row in getattr(ds, name):
            slots, misnumbered = _slots(name, row, ds)
            for slot in slots:
                match = _GAP_RE[name].match(slot)
                if not match:
                    continue  # an unparseable ID is S-ID-*'s finding
                scope = scope_of(match)
                key = ids.sequence_key(kind, **scope)
                groups[key].append((int(match["n"]), row))
                scopes[key] = scope
                if not misnumbered:
                    well_numbered.add(key)
            checked += 1
        for key in sorted(well_numbered):
            counters = [n for n, _ in groups[key]]
            members = sorted(groups[key], key=lambda x: x[0])
            for first, last in _missing_runs(counters):
                after = next(row for n, row in members if n > last)
                scope = scopes[key]
                missing = [_format(kind, n, scope) for n in (first, last)]
                findings.append(
                    Finding(
                        policy.INVALID_ID_FORMAT,
                        R_ID,
                        entity,
                        missing[0],
                        _student_of(name, after, ds),
                        details={
                            "problem": "COUNTER_GAP",
                            "sequence": key,
                            "missing_from": missing[0],
                            "missing_to": missing[1],
                            "missing_count": last - first + 1,
                            "next_present_id": str(after.pk),
                        },
                    )
                )
    return checked, findings


def _format(kind: str, n: int, scope: dict) -> str:
    """The ID core.ids formats for counter ``n`` in ``scope`` (public formatters only)."""
    fmt = {
        "student": lambda: ids.student_id(n),
        "program": lambda: ids.program_id(n),
        "scholarship_type": lambda: ids.scholarship_type_id(n),
        "enrollment": lambda: ids.enrollment_id(scope["year"], n),
        "eligibility": lambda: ids.eligibility_id(scope["year"], n),
        "scholarship": lambda: ids.scholarship_id(scope["year"], n),
        "payment": lambda: ids.payment_id(scope["year"], n),
        "refund": lambda: ids.refund_id(scope["year"], n),
        "ledger_entry": lambda: ids.ledger_entry_id(scope["year"], n),
        "balance_snapshot": lambda: ids.balance_snapshot_id(scope["year"], n),
        "status_event": lambda: ids.status_event_id(scope["entity"], scope["year"], n),
        "invoice": lambda: ids.invoice_id(scope["parent"], scope["year"], n),
        "invoice_line": lambda: ids.invoice_line_id(scope["parent"], n),
        "scholarship_allocation": lambda: ids.scholarship_allocation_id(scope["parent"], n),
        "payment_allocation": lambda: ids.payment_allocation_id(scope["parent"], n),
    }
    return fmt[kind]()


@check("S-ID-GAPS-PARENT", family="STRUCTURAL", rule_id=R_ID)
def counter_gaps_within_a_parent(ds):
    """No counter is missing within a student's invoices of a year, an invoice's lines, an
    award's allocations or a payment's allocations (DATABASE_DESIGN §2 counter scopes)."""
    return _gaps(ds, PARENT_SCOPED)


@check("S-ID-GAPS-YEAR", family="STRUCTURAL", rule_id=R_ID, scope=GLOBAL)
def counter_gaps_within_a_year_or_globally(ds):
    """No counter is missing in a global sequence (STU, PRG, SCT) or a per-year one (ENR, ELG,
    SCH, PAY, REF, LED, SNAP, EVT per entity) (DATABASE_DESIGN §2 counter scopes)."""
    return _gaps(ds, [n for n in GAP_SEQUENCES if n not in PARENT_SCOPED])
