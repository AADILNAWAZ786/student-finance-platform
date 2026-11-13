"""Payments policy constants (DATABASE_DESIGN.md §3 payments, ARCHITECTURE.md §6.3–6.4).

Everything here is fixed by the frozen design; clients never send these values except
where an endpoint documents the field (method, payer_type, reason).
"""

from __future__ import annotations

from billing import policy as billing_policy
from billing.calculators import PAYER_TYPES as _PAYER_TYPES

PAYER_TYPES = _PAYER_TYPES  # SELF, GUARDIAN, SPONSOR: all count as money paid (BR-PAY-03)
CURRENCY = billing_policy.CURRENCY  # "INR"
BUSINESS_DATE_MIN = billing_policy.BUSINESS_DATE_MIN
BUSINESS_DATE_MAX = billing_policy.BUSINESS_DATE_MAX  # also the latest processed_date

METHODS = ("UPI", "CARD", "BANK_TRANSFER", "CHEQUE", "CASH", "SPONSOR_TRANSFER")
# DATABASE_DESIGN gives Payment.method as Char(14), but SPONSOR_TRANSFER has 16 characters
# (decision D-P1, KNOWN_LIMITATIONS P4-O1): the column is Char(16).
METHOD_MAX_LENGTH = max(len(m) for m in METHODS)
PAYMENT_STATUSES = ("POSTED",)
POSTED = "POSTED"

ALLOCATE, DEALLOCATE = "ALLOCATE", "DEALLOCATE"
ALLOCATION_TYPES = (ALLOCATE, DEALLOCATE)
AUTO_OLDEST_FIRST = "AUTO_OLDEST_FIRST"
MANUAL = "MANUAL"
WITHDRAWAL_RELEASE = "WITHDRAWAL_RELEASE"
CANCELLATION_RELEASE = "CANCELLATION_RELEASE"
SCHOLARSHIP_PRECEDENCE = "SCHOLARSHIP_PRECEDENCE"
RULES = (
    AUTO_OLDEST_FIRST,
    MANUAL,
    WITHDRAWAL_RELEASE,
    CANCELLATION_RELEASE,
    SCHOLARSHIP_PRECEDENCE,
)
ALLOCATE_RULES = (AUTO_OLDEST_FIRST, MANUAL)
RELEASE_RULES = (WITHDRAWAL_RELEASE, CANCELLATION_RELEASE, SCHOLARSHIP_PRECEDENCE)

# lines a payment may be allocated to (BR-ALC-02)
ALLOCATABLE_LINE_TYPES = ("FEE", "LATE_FEE")
# invoices AUTO allocation looks at (D-P12): never VOID
AUTO_INVOICE_LIFECYCLES = ("ISSUED", "CANCELLED")

# ALLOC-{payment_id}-NN: at most 99 rows per payment, deallocations included (D-P13a)
MAX_ALLOCATIONS_PER_PAYMENT = 99

# AUTO outcomes (D-P13): never an error, always reported
ALLOCATED, NO_OPEN_LINES, NO_FUNDS_AVAILABLE = "ALLOCATED", "NO_OPEN_LINES", "NO_FUNDS_AVAILABLE"
OUTCOMES = (ALLOCATED, NO_OPEN_LINES, NO_FUNDS_AVAILABLE)

# --- refunds ---------------------------------------------------------------------------------
WITHDRAWAL, CANCELLATION, OVERPAYMENT = "WITHDRAWAL", "CANCELLATION", "OVERPAYMENT"
REFUND_REASONS = (WITHDRAWAL, CANCELLATION, OVERPAYMENT)
ENROLLMENT_REFUND_REASONS = (WITHDRAWAL, CANCELLATION)  # enrollment required (CK)
API_REFUND_REASONS = (OVERPAYMENT,)  # D-P14: the others are created by Phase 5 services

REQUESTED, APPROVED, PROCESSED, REJECTED = "REQUESTED", "APPROVED", "PROCESSED", "REJECTED"
REFUND_STATUSES = (REQUESTED, APPROVED, PROCESSED, REJECTED)
PENDING_REFUND_STATUSES = (REQUESTED, APPROVED)  # reserve their amount (BR-REF-02)
