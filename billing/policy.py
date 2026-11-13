"""Billing policy constants (DATABASE_DESIGN.md §3 billing, ARCHITECTURE.md §6.1).

Everything here is fixed by the frozen design and set by the server. Clients never send
these values (Phase 2 decision D5): a fee schedule's flags and allocation priority are
derived from its ``fee_type``, and every invoice line snapshots them at issue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# --- fee types --------------------------------------------------------------------------
TUITION = "TUITION"
LAB = "LAB"
REGISTRATION = "REGISTRATION"
TECHNOLOGY = "TECHNOLOGY"
LIBRARY = "LIBRARY"
STUDENT_ACTIVITY = "STUDENT_ACTIVITY"
INTERNATIONAL_SERVICE = "INTERNATIONAL_SERVICE"
LATE_FEE = "LATE_FEE"  # line fee_type of a late-fee line; never a FeeSchedule fee_type

FEE_TYPES = (
    TUITION,
    LAB,
    REGISTRATION,
    TECHNOLOGY,
    LIBRARY,
    STUDENT_ACTIVITY,
    INTERNATIONAL_SERVICE,
)

DOMESTIC, INTERNATIONAL, ALL = "DOMESTIC", "INTERNATIONAL", "ALL"

# D3: the exact residency mapping of DATABASE_DESIGN §3 (FeeSchedule.residency).
#   TUITION priced per residency; INTERNATIONAL_SERVICE = INTERNATIONAL; all others ALL.
ALLOWED_RESIDENCIES: dict[str, tuple[str, ...]] = {
    TUITION: (DOMESTIC, INTERNATIONAL),
    INTERNATIONAL_SERVICE: (INTERNATIONAL,),
    LAB: (ALL,),
    REGISTRATION: (ALL,),
    TECHNOLOGY: (ALL,),
    LIBRARY: (ALL,),
    STUDENT_ACTIVITY: (ALL,),
}

# D5: server-controlled flags and allocation priority (DATABASE_DESIGN §3 FeeSchedule).
SCHOLARSHIP_ELIGIBLE_TYPES = frozenset({TUITION, LAB})
REFUNDABLE_TYPES = frozenset({TUITION, LAB})
WAIVABLE_TYPES = frozenset({TECHNOLOGY, LIBRARY, STUDENT_ACTIVITY})
ALLOCATION_PRIORITY: dict[str, int] = {
    TUITION: 10,
    LAB: 20,
    REGISTRATION: 30,
    TECHNOLOGY: 40,
    LIBRARY: 50,
    STUDENT_ACTIVITY: 60,
    INTERNATIONAL_SERVICE: 70,
}
LATE_FEE_PRIORITY = 90


@dataclass(frozen=True)
class FeeFlags:
    is_scholarship_eligible: bool
    is_refundable: bool
    is_waivable: bool
    allocation_priority: int


def fee_flags(fee_type: str) -> FeeFlags:
    """The fixed flags for a fee type (KeyError for an unknown type)."""
    return FeeFlags(
        is_scholarship_eligible=fee_type in SCHOLARSHIP_ELIGIBLE_TYPES,
        is_refundable=fee_type in REFUNDABLE_TYPES,
        is_waivable=fee_type in WAIVABLE_TYPES,
        allocation_priority=ALLOCATION_PRIORITY[fee_type],
    )


# D2 (Phase 2 policy decision): fee types that must have a version in force.
#   TUITION and REGISTRATION always; LAB when program.has_lab; INTERNATIONAL_SERVICE for
#   international students. TECHNOLOGY, LIBRARY and STUDENT_ACTIVITY are charged only when
#   a version is in force.
ALWAYS_REQUIRED = (TUITION, REGISTRATION)
OPTIONAL_TYPES = (TECHNOLOGY, LIBRARY, STUDENT_ACTIVITY)


def required_fee_types(*, has_lab: bool, residency: str) -> tuple[str, ...]:
    required = list(ALWAYS_REQUIRED)
    if has_lab:
        required.append(LAB)
    if residency == INTERNATIONAL:
        required.append(INTERNATIONAL_SERVICE)
    return tuple(sorted(required, key=ALLOCATION_PRIORITY.__getitem__))


def applicable_fee_types(*, has_lab: bool, residency: str) -> tuple[str, ...]:
    """Every fee type that may appear on the invoice (BR-FEE-04)."""
    types = [*required_fee_types(has_lab=has_lab, residency=residency), *OPTIONAL_TYPES]
    return tuple(sorted(types, key=ALLOCATION_PRIORITY.__getitem__))


# --- waivers (BR-WAV-01) ---------------------------------------------------------------
WAIVER_REASONS = ("STAFF_DEPENDENT", "HARDSHIP", "PG_ACTIVITY_EXEMPT", "SPONSOR_AGREEMENT")

# --- adjustments ---------------------------------------------------------------------------
ADJUSTMENT_REASONS = ("WITHDRAWAL", "CANCELLATION", "VOID")


# --- late fee (BR-LATE-01) -----------------------------------------------------------------
@dataclass(frozen=True)
class LateFeePolicy:
    """2% of outstanding, at least ₹500, at most ₹5,000, after ``grace_days`` past due.

    ``grace_days`` comes from ``Term.late_fee_grace_days`` (14 for every documented term).
    """

    rate: Decimal = Decimal("0.02")
    minimum: Decimal = Decimal("500.00")
    maximum: Decimal = Decimal("5000.00")
    grace_days: int = 14


LATE_FEE_POLICY = LateFeePolicy()

# --- business dates (ARCHITECTURE D1: every business date falls in 2026) ---------------------
BUSINESS_DATE_MIN = date(2026, 1, 1)
BUSINESS_DATE_MAX = date(2026, 12, 31)

CURRENCY = "INR"
