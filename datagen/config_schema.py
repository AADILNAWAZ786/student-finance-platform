"""Validation of ``config/generation.yaml`` (Phase 8, decision P8-D6).

``check(raw)`` returns every problem at once, each naming its path (``payments.profiles``:
"shares sum to 0.98, must be 1"), so a bad config is fixed in one pass. ``resolve`` refuses a
config with any problem. Unknown keys are problems too: a misspelt key would otherwise be
silently ignored and the default behaviour used.

Rules marked FROZEN restate the frozen design (DATABASE_DESIGN §3/§7, ARCHITECTURE §11):
20 programs with 8 STEM and 12 with a lab, credits 6 ≤ min ≤ max ≤ 24, tuition ₹3,000–₹12,000
per credit, every business date in 2026 and the generation as-of date 2026-12-31 (D7).
Everything else is configurable within sane ranges.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from billing import policy as bpolicy
from payments import policy as ppolicy

YEAR = 2026
AS_OF = "2026-12-31"  # FROZEN: ARCHITECTURE D7

SCHEMA = {
    "seed": int,
    "as_of_date": str,
    "actor": str,
    "scales": {"starter": "share", "full": "share"},
    "volumes": {
        "students": "count",
        "payments": "count",
        "awards_per_term": "count",
        "anomalies_per_type": "count",
        "anomalies_min_per_type": "count",
    },
    "programs": list,
    "fees": {
        "LAB": "money",
        "REGISTRATION": "money",
        "TECHNOLOGY": "money",
        "LIBRARY": "money",
        "STUDENT_ACTIVITY": "money",
        "INTERNATIONAL_SERVICE": "money",
        "effective_from": "date",
    },
    "pricing": {
        "international_tuition_multiplier": "factor",
        "season_tuition_multiplier": {"SPRING": "factor", "SUMMER": "factor", "FALL": "factor"},
        "fall_tuition_v2": {"count": "count0", "effective_from": "date", "increase": "factor"},
    },
    "students": {
        "international_share": "share",
        "full_ride_share": "share",
        "first_term_admit_share": "share",
        "hold_share": "share",
        "age_at_admit": "int_range",
        "gpa": {"mean": "number", "sd": "number", "min": "number", "max": "number"},
        "need_index_beta": "int_range",
        "created_on": "date",
        "first_names": "names",
        "last_names": "names",
    },
    "enrollment": {"days_before_invoice": "int_range"},
    "waivers": {"invoice_share": "share", "reasons": "waiver_reasons"},
    "exits": {
        "withdrawal_share": "share",
        "cancellation_share": "share",
        "boundary_days": "days",
        "max_withdrawal_day": "count",
    },
    "payments": {
        "profiles": {
            "FULL": "share",
            "INSTALMENTS": "share",
            "PARTIAL": "share",
            "LATE": "share",
            "PARTIAL_SMALL": "share",
            "LATE_LARGE": "share",
        },
        "overpayment_invoice_share": "share",
        "partial_fraction": "share_range",
        "overpay_extra": "int_range",
        "payer_mix": {"SELF": "share", "GUARDIAN": "share", "SPONSOR": "share"},
        "methods": {"SELF": "methods", "GUARDIAN": "methods", "SPONSOR": "methods"},
        "instalments": "int_range",
        "late_window_days": "int_range",
    },
    "refunds": {
        "overpayment_refund_share": "share",
        "request_after_days": "int_range",
        "approve_after_days": "int_range",
        "process_after_days": "int_range",
    },
    "snapshots": "dates",
    "anomalies": {
        "over_allocation_amount": "int_range",
        "scholarship_overaward_factor": "share_range_open",
        "refund_excess": "int_range",
        "balance_delta": "int_range",
    },
}
PROGRAM_KEYS = {"code", "name", "level", "department", "stem", "lab", "min", "max", "tuition"}


def _dec(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:  # a YAML float is read through its shortest repr ("0.01"): deterministic
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _leaf(path: str, kind: str, value, problems: list[str]) -> None:
    def bad(msg):
        problems.append(f"{path}: {msg} (got {value!r})")

    if kind == "share":
        d = _dec(value)
        if d is None or not Decimal(0) <= d <= Decimal(1):
            bad("must be a quoted decimal between 0 and 1")
    elif kind in ("count", "count0"):
        low = 0 if kind == "count0" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            bad(f"must be an integer ≥ {low}")
    elif kind == "money":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            bad("must be a whole number of rupees ≥ 0")
    elif kind == "factor":
        d = _dec(value)
        if d is None or d <= 0:
            bad("must be a quoted decimal > 0")
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            bad("must be a number")
    elif kind == "date":
        d = _date(value)
        if d is None or d.year != YEAR:
            bad(f"must be an ISO date in {YEAR} (ARCHITECTURE D1)")
    elif kind == "dates":
        ds = [_date(x) for x in value] if isinstance(value, list) else [None]
        if not value or any(d is None or d.year != YEAR for d in ds):
            bad(f"must be a non-empty list of ISO dates in {YEAR}")
        elif len(set(ds)) != len(ds):
            bad("must not repeat a date")
        elif max(ds) > date.fromisoformat(AS_OF):
            bad(f"must be on or before {AS_OF}")
    elif kind in ("int_range", "share_range", "share_range_open"):
        ok = isinstance(value, list) and len(value) == 2
        if ok and kind == "int_range":
            ok = all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in value)
            lo, hi = value if ok else (0, 0)
        elif ok:
            lo, hi = (_dec(x) for x in value)
            ok = lo is not None and hi is not None and lo > 0
            if ok and kind == "share_range":
                ok = hi <= 1
        if not ok:
            bad(
                "must be [low, high] of non-negative integers"
                if kind == "int_range"
                else "must be [low, high] of quoted decimals > 0"
                + (" and ≤ 1" if kind == "share_range" else "")
            )
        elif lo > hi:
            bad("low must not exceed high")
    elif kind == "names":
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(x, str) and x.strip() for x in value)
        ):
            bad("must be a non-empty list of names")
    elif kind == "waiver_reasons":
        if (
            not isinstance(value, list)
            or not value
            or not set(value) <= set(bpolicy.WAIVER_REASONS)
        ):
            bad(f"must be a non-empty subset of {', '.join(bpolicy.WAIVER_REASONS)}")
    elif kind == "methods":
        if not isinstance(value, list) or not value or not set(value) <= set(ppolicy.METHODS):
            bad(f"must be a non-empty subset of {', '.join(ppolicy.METHODS)}")
    elif kind == "days":
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in value)
        ):
            bad("must be a non-empty list of day numbers ≥ 0")
    else:  # pragma: no cover - a schema kind without a rule
        raise AssertionError(kind)


def _walk(path: str, schema, value, problems: list[str]) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            problems.append(f"{path or 'config'}: must be a mapping (got {type(value).__name__})")
            return
        for key in sorted(set(schema) - set(value)):
            if path == "" and key == "seed":
                continue  # the seed may come from the command line only
            problems.append(f"{(path + '.') if path else ''}{key}: required")
        for key in sorted(set(value) - set(schema)):
            problems.append(f"{(path + '.') if path else ''}{key}: unknown key")
        for key in sorted(set(schema) & set(value)):
            _walk(f"{(path + '.') if path else ''}{key}", schema[key], value[key], problems)
    elif schema is int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"{path}: must be an integer ≥ 0 (got {value!r})")
    elif schema is str:
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{path}: must be a non-empty string (got {value!r})")
    elif schema is list:
        if not isinstance(value, list):
            problems.append(f"{path}: must be a list")
    else:
        _leaf(path, schema, value, problems)


def _sum_is_one(path, mapping, problems):
    if isinstance(mapping, dict):
        values = [_dec(v) for v in mapping.values()]
        if all(v is not None for v in values) and sum(values) != 1:
            problems.append(f"{path}: shares sum to {sum(values)}, must be 1")


def check(raw) -> list[str]:
    """Every problem of a raw (unresolved) config; [] when it is valid."""
    problems: list[str] = []
    _walk("", SCHEMA, raw, problems)
    if not isinstance(raw, dict):
        return problems
    if raw.get("as_of_date") not in (None, AS_OF):
        problems.append(f"as_of_date: must be {AS_OF} (ARCHITECTURE D7; FROZEN)")
    programs = raw.get("programs")
    if isinstance(programs, list):
        if len(programs) != 20:
            problems.append(
                f"programs: must list exactly 20 programs (FROZEN), got {len(programs)}"
            )
        codes = []
        for i, p in enumerate(programs):
            where = f"programs[{i}]"
            if not isinstance(p, dict) or set(p) != PROGRAM_KEYS:
                problems.append(
                    f"{where}: must have exactly the keys {', '.join(sorted(PROGRAM_KEYS))}"
                )
                continue
            codes.append(p["code"])
            if not isinstance(p["code"], str) or not 0 < len(p["code"]) <= 12:
                problems.append(f"{where}.code: 1–12 characters (got {p['code']!r})")
            if p["level"] not in ("UG", "PG", "DIPLOMA"):
                problems.append(f"{where}.level: must be UG, PG or DIPLOMA (got {p['level']!r})")
            if not (isinstance(p["min"], int) and isinstance(p["max"], int)
                    and 6 <= p["min"] <= p["max"] <= 24):  # fmt: skip
                problems.append(f"{where}: credits must satisfy 6 ≤ min ≤ max ≤ 24 (FROZEN)")
            if not (isinstance(p["tuition"], int) and 3000 <= p["tuition"] <= 12000):
                problems.append(f"{where}.tuition: ₹3,000–₹12,000 per credit (FROZEN)")
            for flag in ("stem", "lab"):
                if not isinstance(p[flag], bool):
                    problems.append(f"{where}.{flag}: must be true or false")
        if len(set(codes)) != len(codes):
            problems.append("programs: codes must be unique")
        good = [p for p in programs if isinstance(p, dict) and set(p) == PROGRAM_KEYS]
        if len(good) == 20:
            if sum(1 for p in good if p["stem"] is True) != 8:
                problems.append("programs: exactly 8 must be STEM (FROZEN)")
            if sum(1 for p in good if p["lab"] is True) != 12:
                problems.append("programs: exactly 12 must have a lab (FROZEN)")
    payments = raw.get("payments") if isinstance(raw.get("payments"), dict) else {}
    _sum_is_one("payments.profiles", payments.get("profiles"), problems)
    _sum_is_one("payments.payer_mix", payments.get("payer_mix"), problems)
    volumes = raw.get("volumes") if isinstance(raw.get("volumes"), dict) else {}
    if all(isinstance(volumes.get(k), int) for k in ("students", "awards_per_term")):
        if volumes["awards_per_term"] > 7 * volumes["students"]:
            problems.append("volumes.awards_per_term: cannot exceed 7 × students (one per type)")
    exits = raw.get("exits") if isinstance(raw.get("exits"), dict) else {}
    if isinstance(exits.get("boundary_days"), list) and isinstance(
        exits.get("max_withdrawal_day"), int
    ):
        if any(
            isinstance(d, int) and d > exits["max_withdrawal_day"] for d in exits["boundary_days"]
        ):
            problems.append("exits.boundary_days: must not exceed exits.max_withdrawal_day")
    students = raw.get("students") if isinstance(raw.get("students"), dict) else {}
    age = students.get("age_at_admit")
    if isinstance(age, list) and len(age) == 2 and all(isinstance(a, int) for a in age):
        if not 16 <= age[0] <= age[1] <= 59:
            problems.append(
                "students.age_at_admit: must lie within 16–59 (students.services: 16–60)"
            )
    gpa = students.get("gpa")
    if isinstance(gpa, dict) and all(
        isinstance(gpa.get(k), int | float) for k in ("min", "max", "mean", "sd")
    ):
        if not 0 <= gpa["min"] < gpa["max"] <= 4 or gpa["sd"] <= 0:
            problems.append("students.gpa: need 0 ≤ min < max ≤ 4 and sd > 0")
    beta = students.get("need_index_beta")
    if (
        isinstance(beta, list)
        and len(beta) == 2
        and any(isinstance(b, int) and b < 1 for b in beta)
    ):
        problems.append("students.need_index_beta: both parameters must be ≥ 1")
    return problems
