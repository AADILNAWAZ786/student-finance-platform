"""Generation validation: exact baseline counts, coverage bands and boundary cases
(DATABASE_DESIGN §7, ARCHITECTURE §11.2). Measured on the baseline, before injection.

Bands are the frozen full-scale targets; count-based targets scale with the factor
(full-ride awards 300–600 × f, SCHOLARSHIP_COVERED ≥ 300 × f, each boundary case ≥ 5 × f,
never below 1). Generation fails when any check fails.
"""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_UP, Decimal

from core.money import dsum
from scholarships import policy as spolicy

BOUNDARY_CASES = (
    "payment_equals_outstanding",
    "overpayment_one_paisa",
    "withdrawal_day_0",
    "withdrawal_day_7",
    "withdrawal_day_8",
    "withdrawal_day_14",
    "withdrawal_day_15",
    "withdrawal_day_27",
    "withdrawal_day_28",
    "late_fee_minimum",
    "late_fee_maximum",
    "scholarship_at_cap",
    "full_ride_covers_registration",
    "fully_waived_line",
    "cancellation_registration_owed",
)


def _scaled(n, factor: Decimal) -> int:
    return max(1, int((Decimal(n) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP)))


def _pct(part, whole) -> str:
    return f"{Decimal(part) * 100 / Decimal(whole):.2f}" if whole else "0.00"


def _stats(values) -> dict:
    values = sorted(float(v) for v in values)
    q = statistics.quantiles(values, n=10, method="inclusive")
    return {
        "mean": f"{statistics.fmean(values):.3f}",
        "p10": f"{q[0]:.3f}",
        "p50": f"{statistics.median(values):.3f}",
        "p90": f"{q[8]:.3f}",
    }


def validate(world, stats, counts: dict, factor: Decimal) -> dict:
    w = world
    n_students = counts["students"]
    invoices = list(w.invoices)
    n_inv, n_enr, n_pay = len(invoices), len(w.enrollments), len(w.payments)

    late = [x for x in w.lines if x.line_type == "LATE_FEE"]
    if any(x.net_amount == Decimal("500.00") for x in late):
        stats.hit("late_fee_minimum")
    if any(x.net_amount == Decimal("5000.00") for x in late):
        stats.hit("late_fee_maximum")

    exact = {
        "programs": (len(w.programs), 20),
        "terms": (len(w.terms), 12),
        "scholarship_types": (len(w.types), 7),
        "scholarship_type_programs": (len(w.bridges), 8),
        "students": (len(w.students), n_students),
        "enrollments": (n_enr, 3 * n_students),
        "invoices": (n_inv, 3 * n_students),
        "eligibility": (len(w.eligibility), 21 * n_students),
        "scholarships": (len(w.scholarships), 3 * counts["awards_per_term"]),
        "payments": (n_pay, counts["payments"]),
    }

    def has_event_to(invoice, status):
        return any(e.dimension == "COLLECTION" and e.to_status == status for e in invoice.events)

    partial = sum(
        1
        for i in invoices
        if has_event_to(i, "PARTIALLY_PAID")
        or (dsum(a.amount for a in i.pay_allocs) > 0 and w.current_amounts(i).outstanding > 0)
    )
    payer = {p: sum(1 for x in w.payments if x.payer_type == p) for p in ("GUARDIAN", "SPONSOR")}
    waived = sum(1 for i in invoices if any(x.waiver_amount > 0 for x in i.lines))
    withdrawn = sum(1 for e in w.enrollments if e.status == "WITHDRAWN")
    cancelled = sum(1 for e in w.enrollments if e.status == "CANCELLED")
    late_invoices = len({x.invoice.key for x in late})
    full_ride = sum(
        1 for s in w.scholarships if s.scholarship_type.coverage_scope == spolicy.ALL_FEE_LINES
    )
    covered = sum(1 for i in invoices if i.collection_status == "SCHOLARSHIP_COVERED")

    bands = {
        # name: (actual %, low %, high %)  — high None = no upper bound
        "partial_payments_pct_of_invoices": (_pct(partial, n_inv), "20.00", None),
        "overpayments_pct_of_invoices": (_pct(stats.overpayment_slots, n_inv), "2.00", "5.00"),
        "guardian_pct_of_payments": (_pct(payer["GUARDIAN"], n_pay), "15.00", "25.00"),
        "sponsor_pct_of_payments": (_pct(payer["SPONSOR"], n_pay), "5.00", "15.00"),
        "waived_pct_of_invoices": (_pct(waived, n_inv), "5.00", "10.00"),
        "withdrawals_pct_of_enrollments": (_pct(withdrawn, n_enr), "3.00", "8.00"),
        "cancellations_pct_of_enrollments": (_pct(cancelled, n_enr), "0.50", "1.50"),
        "late_fees_pct_of_invoices": (_pct(late_invoices, n_inv), "8.00", "15.00"),
    }
    count_targets = {
        "full_ride_awards": (full_ride, _scaled(300, factor), _scaled(600, factor)),
        "scholarship_covered_invoices": (covered, _scaled(300, factor), None),
    }
    boundary_min = _scaled(5, factor)
    problems = []
    for name, (actual, expected) in exact.items():
        if actual != expected:
            problems.append(f"{name}: {actual} != {expected}")
    for name, (actual, lo, hi) in bands.items():
        if Decimal(actual) < Decimal(lo) or (hi is not None and Decimal(actual) > Decimal(hi)):
            problems.append(f"{name}: {actual}% outside [{lo}, {hi or '∞'}]")
    for name, (actual, lo, hi) in count_targets.items():
        if actual < lo or (hi is not None and actual > hi):
            problems.append(f"{name}: {actual} outside [{lo}, {hi if hi is not None else '∞'}]")
    boundary = {name: stats.boundary.get(name, 0) for name in BOUNDARY_CASES}
    for name, n in boundary.items():
        if n < boundary_min:
            problems.append(f"boundary case {name}: {n} < {boundary_min}")
    return {
        "passed": not problems,
        "problems": problems,
        "exact_counts": {k: {"actual": a, "expected": e} for k, (a, e) in exact.items()},
        "coverage": {
            k: {"actual_pct": a, "low_pct": lo, "high_pct": hi} for k, (a, lo, hi) in bands.items()
        },
        "count_targets": {
            k: {"actual": a, "low": lo, "high": hi} for k, (a, lo, hi) in count_targets.items()
        },
        "boundary_cases": {"minimum_each": boundary_min, "counts": boundary},
        "payment_profiles": dict(stats.profiles),
        "rejected_refunds": stats.rejected_refunds,
        "advance_payments": stats.advance_payments,
        "eligible_not_awarded": sum(
            1 for e in w.eligibility if e.is_eligible and e.scholarship is None
        ),
        "distributions": {
            "gpa": _stats(s.gpa for s in w.students),
            "need_index": _stats(s.need_index for s in w.students),
            "credits": _stats(e.credits for e in w.enrollments),
            "international_pct": _pct(
                sum(1 for s in w.students if s.residency == "INTERNATIONAL"), len(w.students)
            ),
        },
    }
