"""Service-layer rules re-checked by reconciliation (DATABASE_DESIGN conventions: "Every SV rule
is also REC"; ARCHITECTURE §10, Service layer; P6-O7 resolution). Each check reuses the domain
apps' own calculators, policies and constants (safeguard 4) and uses only the 14 frozen issue
types. The coverage matrix is docs/phase6_sv_coverage.md.

Where a later, more specific finding already reports the same record, these checks stay
silent, so every problem is reported once with its most specific type:

* BR-SCH-03 skips awards that breach D-R5 (SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES);
* BR-REF-02 (recorded caps) skips refunds that breach D-R4 (REFUND_EXCEEDS_NET_PAID);
* the cancelled-invoice and exit-adjustment checks skip duplicate invoices
  (DUPLICATE_INVOICE).

Rules whose exact re-computation depends on history the data does not record are re-checked
only on their stable parts (documented in docs/phase6_sv_coverage.md and KNOWN_LIMITATIONS
P6-O1 and P6-O13): BR-LATE-01's 2 % base, BR-FEE-01's "latest version in force".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from academics.calculators import withdrawal_refund_pct
from academics.calendar import CENSUS_OFFSET_DAYS
from academics.services import ADD_DROP_DAYS
from billing import calculators as billing_calc
from billing import policy as billing_policy
from core.money import dsum, round_money, to_decimal
from payments.policy import REJECTED
from payments.services import REFUND_ENROLLMENT_STATUS
from reconciliation import policy
from reconciliation.checks.base import GLOBAL, Finding, check
from scholarships import calculators as sch_calc
from scholarships.lifecycle import REVOKED
from students.calculators import age_on
from students.services import MAX_AGE, MIN_AGE

TERMINAL_LIFECYCLE = ("CANCELLED", "VOID")


def _f(issue, rule, entity, record_id, student_id, expected=None, actual=None, **details):
    # details are keyword-only, so a details key named like a parameter would silently become
    # that parameter (the P6-B1 defect class): such a call fails here, in tests, instead
    return Finding(
        issue,
        rule,
        entity,
        str(record_id),
        student_id,
        expected=None if expected is None else round_money(expected),
        actual=None if actual is None else round_money(actual),
        details={k: (str(v) if hasattr(v, "isoformat") else v) for k, v in details.items()},
    )


def _first_live_invoices(ds) -> dict[str, object]:
    """{enrollment_id: its first non-VOID invoice}; later ones are DUPLICATE_INVOICE's."""
    out = {}
    for inv in sorted(ds.invoices, key=lambda i: (i.issue_date, i.pk)):
        if inv.lifecycle_status != "VOID":
            out.setdefault(inv.enrollment_id, inv)
    return out


def _terminal_since(ds) -> dict[str, object]:
    """{invoice_id: date of its first CANCELLED / VOID lifecycle event}."""
    out = {}
    for e in sorted(ds.invoice_events, key=lambda e: e.pk):
        if getattr(e, "dimension", None) == "LIFECYCLE" and e.to_status in TERMINAL_LIFECYCLE:
            out.setdefault(e.invoice_id, e.event_date)
    return out


# --- students and enrollments ---------------------------------------------------------------------


@check("T-STUDENT-AGE", family="TEMPORAL", rule_id="R-39")
def student_age_at_admit_term(ds):
    """A student is 16–60 years old on the admit term's start date (DATABASE_DESIGN SV)."""
    out = []
    for s in ds.students:
        term = ds.by_id["terms"].get(s.admit_term_id)
        if term is None:
            continue
        age = age_on(s.date_of_birth, term.start_date)
        if not MIN_AGE <= age <= MAX_AGE:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "Student",
                    s.pk,
                    s.pk,
                    age=age,
                    date_of_birth=s.date_of_birth,
                    admit_term_start=term.start_date,
                )
            )
    return len(ds.students), out


@check("RL-STUDENT-PROGRAM", family="RELATIONSHIP", rule_id="R-26")
def student_in_an_active_program(ds):
    """A student's program is active (students.services.create_student; programs are never
    updated, so this holds for life)."""
    out = []
    for s in ds.students:
        prog = ds.by_id["programs"].get(s.program_id)
        if prog is not None and not prog.is_active:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "Student",
                    s.pk,
                    s.pk,
                    problem="PROGRAM_NOT_ACTIVE",
                    program=prog.pk,
                )
            )
    return len(ds.students), out


@check("T-ENROLLMENT-DATES", family="TEMPORAL", rule_id="R-39")
def enrollment_dates(ds):
    """The admit term starts no later than the enrolled term (SV), and enrolled_on is no later
    than term start + 7 days (DATABASE_DESIGN Enrollment)."""
    out = []
    for enr in ds.enrollments:
        term = ds.by_id["terms"].get(enr.term_id)
        stu = ds.by_id["students"].get(enr.student_id)
        admit = stu and ds.by_id["terms"].get(stu.admit_term_id)
        if term is None:
            continue
        if admit is not None and admit.start_date > term.start_date:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "Enrollment",
                    enr.pk,
                    enr.student_id,
                    problem="BEFORE_ADMIT_TERM",
                    admit_term=admit.pk,
                    term=term.pk,
                )
            )
        last_day = term.start_date + timedelta(days=ADD_DROP_DAYS)
        if enr.enrolled_on > last_day:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "Enrollment",
                    enr.pk,
                    enr.student_id,
                    problem="AFTER_ADD_DROP",
                    enrolled_on=enr.enrolled_on,
                    last_day=last_day,
                )
            )
    return len(ds.enrollments), out


@check("RL-ENROLLMENT-RULES", family="RELATIONSHIP", rule_id="R-26")
def enrollment_term_program_credits(ds):
    """An enrollment is in a billable term, in an active program, with credits within the
    program's range (ARCHITECTURE §10: non-billable term, credits outside program range)."""
    out = []
    for enr in ds.enrollments:
        term = ds.by_id["terms"].get(enr.term_id)
        prog = ds.by_id["programs"].get(enr.program_id)
        if term is not None and not term.is_billable:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "Enrollment",
                    enr.pk,
                    enr.student_id,
                    problem="TERM_NOT_BILLABLE",
                    term=term.pk,
                )
            )
        if prog is not None and not prog.is_active:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "Enrollment",
                    enr.pk,
                    enr.student_id,
                    problem="PROGRAM_NOT_ACTIVE",
                    program=prog.pk,
                )
            )
        if prog is not None and not prog.min_credits <= enr.credits <= prog.max_credits:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "Enrollment",
                    enr.pk,
                    enr.student_id,
                    problem="CREDITS_OUT_OF_RANGE",
                    credits=enr.credits,
                    min_credits=prog.min_credits,
                    max_credits=prog.max_credits,
                )
            )
    return len(ds.enrollments), out


@check("T-TERM-CENSUS", family="TEMPORAL", rule_id="BR-WDR-02", scope=GLOBAL)
def census_is_start_plus_28(ds):
    """census_date = start_date + 28 days (DATABASE_DESIGN SV; BR-WDR-02)."""
    out = []
    for t in ds.terms:
        expected = t.start_date + timedelta(days=CENSUS_OFFSET_DAYS)
        if t.census_date != expected:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "BR-WDR-02",
                    "Term",
                    t.pk,
                    None,
                    census_date=t.census_date,
                    expected_census_date=expected,
                )
            )
    return len(ds.terms), out


@check("RL-BILLABLE-TERM-MASTER", family="RELATIONSHIP", rule_id="R-26", scope=GLOBAL)
def fee_schedules_are_for_billable_terms(ds):
    """A fee schedule is for a billable term (DATABASE_DESIGN FeeSchedule)."""
    out = []
    for fs in ds.fee_schedules:
        term = ds.by_id["terms"].get(fs.term_id)
        if term is not None and not term.is_billable:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "FeeSchedule",
                    fs.pk,
                    None,
                    problem="TERM_NOT_BILLABLE",
                    term=term.pk,
                )
            )
    return len(ds.fee_schedules), out


# --- invoices -------------------------------------------------------------------------------------


@check("T-INVOICE-CALENDAR", family="TEMPORAL", rule_id="BR-INV-03")
def invoice_calendar(ds):
    """issue_date = max(term invoice_date, enrolled_on), due_date = term due_date (BR-INV-03);
    every line is posted on or after the invoice's issue_date (DATABASE_DESIGN). An invoice
    whose dates are out of order is T-INVOICE-DATES's finding and is not reported twice."""
    out, checked = [], 0
    for inv in ds.invoices:
        term = ds.by_id["terms"].get(inv.term_id)
        enr = ds.by_id["enrollments"].get(inv.enrollment_id)
        if term is None or enr is None:
            continue
        checked += 1
        expected = (max(term.invoice_date, enr.enrolled_on), term.due_date)
        in_order = enr.enrolled_on <= inv.issue_date <= inv.due_date  # else T-INVOICE-DATES
        if in_order and (inv.issue_date, inv.due_date) != expected:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "BR-INV-03",
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    issue_date=inv.issue_date,
                    due_date=inv.due_date,
                    expected_issue_date=expected[0],
                    expected_due_date=expected[1],
                )
            )
        for line in ds.lines_by_invoice.get(inv.pk, []):
            checked += 1
            if line.posted_date < inv.issue_date:
                out.append(
                    _f(
                        policy.DATE_ORDER,
                        "R-39",
                        "InvoiceLine",
                        line.pk,
                        inv.student_id,
                        posted_date=line.posted_date,
                        issue_date=inv.issue_date,
                    )
                )
    return checked, out


@check("RL-FEE-APPLICABLE", family="RELATIONSHIP", rule_id="BR-FEE-04")
def fee_line_applies_to_the_student(ds):
    """A FEE line's fee type applies to the enrollment (LAB only with program.has_lab,
    INTERNATIONAL_SERVICE only for international students: BR-FEE-04)."""
    out, checked = [], 0
    for line in ds.lines:
        if line.line_type != "FEE":
            continue
        inv = ds.by_id["invoices"].get(line.invoice_id)
        enr = inv and ds.by_id["enrollments"].get(inv.enrollment_id)
        prog = enr and ds.by_id["programs"].get(enr.program_id)
        stu = inv and ds.by_id["students"].get(inv.student_id)
        if not (prog and stu):
            continue
        checked += 1
        allowed = billing_policy.applicable_fee_types(has_lab=prog.has_lab, residency=stu.residency)
        if line.fee_type not in allowed:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "BR-FEE-04",
                    "InvoiceLine",
                    line.pk,
                    stu.pk,
                    fee_type=line.fee_type,
                    has_lab=prog.has_lab,
                    residency=stu.residency,
                )
            )
    return checked, out


@check("A-WAIVER-WAIVABLE", family="ARITHMETIC", rule_id="BR-WAV-01")
def waivers_only_on_waivable_lines(ds):
    """A waiver (> 0) is only on a waivable line (BR-WAV-01)."""
    out = []
    for line in ds.lines:
        if line.waiver_amount > 0 and not line.is_waivable:
            inv = ds.by_id["invoices"].get(line.invoice_id)
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-WAV-01",
                    "InvoiceLine",
                    line.pk,
                    inv and inv.student_id,
                    Decimal("0.00"),
                    line.waiver_amount,
                    fee_type=line.fee_type,
                )
            )
    return len(ds.lines), out


@check("A-VOID-INVOICE", family="ARITHMETIC", rule_id="BR-INV-08")
def void_invoices_hold_no_money(ds):
    """A VOID invoice has no net scholarship and no net payment on it (BR-INV-08: void only
    without allocations; nothing is allocated to it afterwards)."""
    out, checked = [], 0
    for inv in ds.invoices:
        if inv.lifecycle_status != "VOID":
            continue
        checked += 1
        sch = dsum(a.amount for a in ds.scholarship_allocations_by_invoice.get(inv.pk, []))
        pay = dsum(a.amount for a in ds.payment_allocations_by_invoice.get(inv.pk, []))
        if sch != 0 or pay != 0:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-INV-08",
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    Decimal("0.00"),
                    sch + pay,
                    scholarship_applied=str(sch),
                    amount_paid=str(pay),
                )
            )
    return checked, out


@check("RL-CANCELLED-INVOICE", family="RELATIONSHIP", rule_id="BR-WDR-01")
def cancelled_invoice_iff_cancelled_enrollment(ds):
    """An invoice is CANCELLED exactly when its enrollment is CANCELLED (a VOID invoice stays
    VOID; decision D-W7) (BR-WDR-01, BR-INV-08)."""
    out = []
    live = _first_live_invoices(ds)
    for enrollment_id, inv in sorted(live.items()):
        enr = ds.by_id["enrollments"].get(enrollment_id)
        if enr is None:
            continue
        if (inv.lifecycle_status == "CANCELLED") != (enr.status == "CANCELLED"):
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "BR-WDR-01",
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    lifecycle_status=inv.lifecycle_status,
                    enrollment_status=enr.status,
                    enrollment_id=enr.pk,
                )
            )
    return len(live), out


@check("A-EXIT-ADJUSTMENTS", family="ARITHMETIC", rule_id="BR-WDR-02")
def exit_adjustments(ds):
    """A withdrawn enrollment's live invoice carries exactly the BR-WDR-02 adjustments, a
    cancelled one exactly the BR-WDR-01 adjustments (Phase 5's own calculators, on the lines
    posted by the exit date), dated the exit date; no other invoice carries exit adjustments."""
    out, checked = [], 0
    live = _first_live_invoices(ds)
    for inv in ds.invoices:
        enr = ds.by_id["enrollments"].get(inv.enrollment_id)
        term = ds.by_id["terms"].get(inv.term_id)
        if enr is None or term is None:
            continue
        lines = ds.lines_by_invoice.get(inv.pk, [])
        actual = sorted(
            (
                x
                for x in lines
                if x.line_type == "ADJUSTMENT"
                and x.adjustment_reason in ("WITHDRAWAL", "CANCELLATION")
            ),
            key=lambda x: x.adjusts_line_id,
        )
        if live.get(inv.enrollment_id) is not inv:
            continue  # VOID (never adjusted on exit, D-W7) or a DUPLICATE_INVOICE
        kind = {"WITHDRAWN": "WITHDRAWAL", "CANCELLED": "CANCELLATION"}.get(enr.status)
        if kind is None:
            if actual:
                out.append(
                    _f(
                        policy.ARITHMETIC_MISMATCH,
                        "BR-WDR-02",
                        "Invoice",
                        inv.pk,
                        inv.student_id,
                        problem="EXIT_ADJUSTMENT_WITHOUT_EXIT",
                        enrollment_status=enr.status,
                    )
                )
            continue
        checked += 1
        on = enr.withdrawal_date if kind == "WITHDRAWAL" else enr.cancellation_date
        if on is None:
            continue  # T-EXIT-DATES / the CHECK constraints report it
        before = [
            x
            for x in lines
            if x.posted_date <= on and not (x.line_type == "ADJUSTMENT" and x in actual)
        ]
        if kind == "WITHDRAWAL":
            try:
                pct = withdrawal_refund_pct(term, on)
            except ValueError:
                continue  # before the start: T-EXIT-DATES reports it
            expected = billing_calc.withdrawal_adjustments(before, pct)
            rule = "BR-WDR-02"
        else:
            expected = billing_calc.cancellation_adjustments(before)
            rule = "BR-WDR-01"
        want = {(a.adjusts_line_id, a.amount) for a in expected}
        got = {(x.adjusts_line_id, x.net_amount) for x in actual}
        reasons_ok = all(x.adjustment_reason == kind for x in actual)
        dates_ok = all(x.posted_date == on for x in actual)
        if want != got or not reasons_ok or not dates_ok:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    rule,
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    dsum(a for _, a in want),
                    dsum(a for _, a in got),
                    kind=kind,
                    exit_date=on,
                    expected_adjustments=sorted([lid, str(a)] for lid, a in want),
                    actual_adjustments=sorted([lid, str(a)] for lid, a in got),
                    reasons_ok=reasons_ok,
                    dates_ok=dates_ok,
                )
            )
    return checked, out


@check("A-LATE-FEE", family="ARITHMETIC", rule_id="BR-LATE-01")
def late_fees(ds):
    """BR-LATE-01, on what the data can re-check: at most one late fee per invoice, posted the
    day after due_date + grace_days, on an invoice still ISSUED then, within [₹500, ₹5,000]
    and no more than 2 % (clamped) of the charges posted by the end of the grace period.

    The exact 2 % base (the outstanding amount at the end of the grace day) is not
    re-computed: Phase 5 exits may be dated before that day after the fee was posted
    (decision D-W8), which legitimately changes the as-of outstanding later (P6-O13)."""
    out, checked = [], 0
    terminal = _terminal_since(ds)
    for inv in ds.invoices:
        lines = ds.lines_by_invoice.get(inv.pk, [])
        fees = sorted((x for x in lines if x.line_type == "LATE_FEE"), key=lambda x: x.pk)
        if not fees:
            continue
        term = ds.by_id["terms"].get(inv.term_id)
        if term is None:
            continue
        rules = billing_policy.LateFeePolicy(grace_days=term.late_fee_grace_days)
        posted = billing_calc.late_fee_date(inv.due_date, rules.grace_days)
        cutoff = posted - timedelta(days=1)
        charges = dsum(
            x.net_amount for x in lines if x.line_type == "FEE" and x.posted_date <= cutoff
        )
        ceiling = billing_calc.late_fee(charges, inv.due_date, posted, rules)
        for n, fee in enumerate(fees):
            checked += 1
            problems = []
            if n > 0:
                problems.append("MORE_THAN_ONE")
            if fee.posted_date != posted:
                problems.append("POSTED_DATE")
            if inv.pk in terminal and terminal[inv.pk] < fee.posted_date:
                problems.append("INVOICE_NOT_ISSUED")
            if not rules.minimum <= fee.net_amount <= rules.maximum:
                problems.append("OUTSIDE_MIN_MAX")
            if fee.net_amount > ceiling:
                problems.append("ABOVE_2_PERCENT_OF_CHARGES")
            if problems:
                out.append(
                    _f(
                        policy.ARITHMETIC_MISMATCH,
                        "BR-LATE-01",
                        "InvoiceLine",
                        fee.pk,
                        inv.student_id,
                        ceiling,
                        fee.net_amount,
                        problems=problems,
                        expected_posted_date=posted,
                        posted_date=fee.posted_date,
                    )
                )
    return checked, out


# --- scholarships ---------------------------------------------------------------------------------


def _active_awards_in_order(ds):
    return sorted(
        (s for s in ds.scholarships if s.status != REVOKED),
        key=lambda s: (s.award_date, s.scholarship_id),
    )


def _d_r5_breaches(ds) -> set[str]:
    """Awards the D-R5 check reports (SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES), recomputed with the
    same calculator so the two checks never disagree about which award that is."""
    from reconciliation.checks.required import scholarship_exceeds_eligible_fees

    return {f.record_id for f in scholarship_exceeds_eligible_fees(ds)[1]}


@check("A-SCHOLARSHIP-CAP", family="ARITHMETIC", rule_id="BR-SCH-03")
def scholarship_per_term_cap(ds):
    """An award is at most what Phase 3's award_amount allows: PERCENT × eligible base or
    FIXED, capped by the type's max_amount_per_term less the same type's earlier awards to the
    student that term (BR-SCH-03), in (award_date, scholarship_id) order."""
    types = ds.by_id["scholarship_types"]
    skip = _d_r5_breaches(ds)
    awarded = defaultdict(lambda: Decimal("0.00"))
    out, checked = [], 0
    for s in _active_awards_in_order(ds):
        stype = types.get(s.scholarship_type_id)
        if stype is None:
            continue
        key = (s.student_id, s.term_id, s.scholarship_type_id)
        base = sch_calc.eligible_fee_base(stype, ds.lines_by_invoice.get(s.invoice_id, []))
        maximum = sch_calc.award_amount(stype, base, awarded[key])
        checked += 1
        if s.awarded_amount > maximum and s.pk not in skip:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-SCH-03",
                    "Scholarship",
                    s.pk,
                    s.student_id,
                    maximum,
                    s.awarded_amount,
                    already_awarded_this_term=str(awarded[key]),
                    max_amount_per_term=str(stype.max_amount_per_term),
                    eligible_fee_base=str(base),
                )
            )
        awarded[key] = round_money(awarded[key] + s.awarded_amount)
    return checked, out


@check("S-SCHOLARSHIP-GROUP", family="STRUCTURAL", rule_id="BR-SCH-05")
def one_award_per_exclusive_group(ds):
    """At most one award per exclusive_group per student and term (BR-SCH-05), and a full-ride
    award with no other award for the student and term (BR-SCH-08); revoked awards do not
    count. The later award (award_date, scholarship_id) is the duplicate."""
    types = ds.by_id["scholarship_types"]
    seen: dict[tuple, list] = defaultdict(list)
    out, checked = [], 0
    for s in _active_awards_in_order(ds):
        stype = types.get(s.scholarship_type_id)
        if stype is None:
            continue
        checked += 1
        earlier = seen[(s.student_id, s.term_id)]
        full_ride = [
            x for x in earlier if types[x.scholarship_type_id].coverage_scope == "ALL_FEE_LINES"
        ]
        is_full = stype.coverage_scope == "ALL_FEE_LINES"
        same_group = [
            x
            for x in earlier
            if stype.exclusive_group
            and types[x.scholarship_type_id].exclusive_group == stype.exclusive_group
        ]
        if full_ride or (is_full and earlier):
            conflict, rule = (full_ride or earlier)[0], "BR-SCH-08"
        elif same_group:
            conflict, rule = same_group[0], "BR-SCH-05"
        else:
            conflict = None
        if conflict is not None:
            out.append(
                _f(
                    policy.DUPLICATE_RECORD,
                    rule,
                    "Scholarship",
                    s.pk,
                    s.student_id,
                    first_record_id=conflict.pk,
                    exclusive_group=stype.exclusive_group,
                    term=s.term_id,
                )
            )
        earlier.append(s)
    return checked, out


@check("A-AWARD-APPLIED", family="ARITHMETIC", rule_id="BR-SCH-03")
def applied_within_awarded(ds):
    """Σ net allocations of an award ≤ its awarded_amount (DATABASE_DESIGN SV)."""
    out = []
    for s in ds.scholarships:
        applied = dsum(a.amount for a in ds.allocations_by_scholarship.get(s.pk, []))
        if applied > s.awarded_amount:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-SCH-03",
                    "Scholarship",
                    s.pk,
                    s.student_id,
                    s.awarded_amount,
                    applied,
                    problem="APPLIED_ABOVE_AWARD",
                )
            )
    return len(ds.scholarships), out


@check("RL-SCHOLARSHIP-COVERAGE", family="RELATIONSHIP", rule_id="BR-SCH-06")
def applied_only_to_coverable_lines(ds):
    """An APPLY row is on a line its type may cover (scholarships.calculators.coverable_lines:
    eligible lines, or every FEE line for the full ride) (BR-SCH-06/08)."""
    types = ds.by_id["scholarship_types"]
    out, checked = [], 0
    for a in ds.scholarship_allocations:
        if a.allocation_type != "APPLY":
            continue
        s = ds.by_id["scholarships"].get(a.scholarship_id)
        stype = s and types.get(s.scholarship_type_id)
        if not stype:
            continue
        checked += 1
        coverable = {
            x.line_id
            for x in sch_calc.coverable_lines(stype, ds.lines_by_invoice.get(s.invoice_id, []))
        }
        if a.invoice_line_id not in coverable:
            rule = "BR-SCH-08" if stype.coverage_scope == "ALL_FEE_LINES" else "BR-SCH-06"
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    rule,
                    "ScholarshipAllocation",
                    a.pk,
                    s.student_id,
                    invoice_line_id=a.invoice_line_id,
                    coverage_scope=stype.coverage_scope,
                )
            )
    return checked, out


@check("T-SCHOLARSHIP-DATES", family="TEMPORAL", rule_id="R-39")
def scholarship_dates(ds):
    """award_date ≥ the eligibility's evaluated_on; an APPLY row on or after the award date; a
    REVERSE row on or after the row it reverses (DATABASE_DESIGN Scholarship rules)."""
    out, checked = [], 0
    for s in ds.scholarships:
        checked += 1
        elg = ds.by_id["eligibilities"].get(s.eligibility_id)
        if elg is not None and s.award_date < elg.evaluated_on:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "Scholarship",
                    s.pk,
                    s.student_id,
                    award_date=s.award_date,
                    evaluated_on=elg.evaluated_on,
                )
            )
    for a in ds.scholarship_allocations:
        checked += 1
        s = ds.by_id["scholarships"].get(a.scholarship_id)
        if s is None:
            continue
        if a.allocation_type == "APPLY" and a.allocated_on < s.award_date:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "ScholarshipAllocation",
                    a.pk,
                    s.student_id,
                    allocated_on=a.allocated_on,
                    award_date=s.award_date,
                )
            )
        if a.allocation_type == "REVERSE":
            orig = ds.by_id["scholarship_allocations"].get(a.reverses_id)
            if orig is not None and a.allocated_on < orig.allocated_on:
                out.append(
                    _f(
                        policy.DATE_ORDER,
                        "R-39",
                        "ScholarshipAllocation",
                        a.pk,
                        s.student_id,
                        allocated_on=a.allocated_on,
                        reversed_allocated_on=orig.allocated_on,
                    )
                )
    return checked, out


@check("RL-AWARD-ON-ISSUED-INVOICE", family="RELATIONSHIP", rule_id="BR-INV-08")
def awards_and_applications_on_issued_invoices(ds):
    """An award is made, and applied, while its invoice is ISSUED (DATABASE_DESIGN SV:
    "lifecycle ISSUED"): no CANCELLED / VOID lifecycle event is dated before the award date
    or an APPLY row's date. Same-day events are not ordered by the data, so they pass."""
    terminal = _terminal_since(ds)
    out, checked = [], 0
    for s in ds.scholarships:
        checked += 1
        since = terminal.get(s.invoice_id)
        if since is not None and since < s.award_date:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "BR-INV-08",
                    "Scholarship",
                    s.pk,
                    s.student_id,
                    invoice_id=s.invoice_id,
                    invoice_terminal_since=since,
                    award_date=s.award_date,
                )
            )
    for a in ds.scholarship_allocations:
        if a.allocation_type != "APPLY":
            continue
        s = ds.by_id["scholarships"].get(a.scholarship_id)
        since = s and terminal.get(s.invoice_id)
        checked += 1
        if since is not None and since < a.allocated_on:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "BR-INV-08",
                    "ScholarshipAllocation",
                    a.pk,
                    s.student_id,
                    invoice_id=s.invoice_id,
                    invoice_terminal_since=since,
                    allocated_on=a.allocated_on,
                )
            )
    return checked, out


@check("A-ELIGIBILITY", family="ARITHMETIC", rule_id="BR-SCH-01")
def eligibility_matches_the_calculator(ds):
    """Every stored eligibility equals scholarships.calculators.evaluate on its own input
    snapshot (BR-SCH-01: "calculator output, never random"); the snapshot is for the row's
    term, the student's program and admit term; the term is billable."""
    types = ds.by_id["scholarship_types"]
    bridge = defaultdict(list)
    for tp in ds.type_programs:
        bridge[tp.scholarship_type_id].append(tp.program_id)
    out, checked = [], 0
    for e in ds.eligibilities:
        stype = types.get(e.scholarship_type_id)
        term = ds.by_id["terms"].get(e.term_id)
        stu = ds.by_id["students"].get(e.student_id)
        if not (stype and term and stu):
            continue
        checked += 1
        snap = e.inputs_snapshot or {}
        problems = []
        if not term.is_billable:
            problems.append("TERM_NOT_BILLABLE")
        if (snap.get("term"), snap.get("program"), snap.get("admit_term")) != (
            e.term_id,
            stu.program_id,
            stu.admit_term_id,
        ):
            problems.append("SNAPSHOT_NOT_THIS_STUDENT_OR_TERM")
        try:
            student_in = _NS(
                gpa=snap["gpa"],
                need_index=snap["need_index"],
                residency=snap["residency"],
                program_id=snap["program"],
                admit_term_id=snap["admit_term"],
            )
            type_in = _NS(
                **{f: getattr(stype, f) for f in _TYPE_FIELDS},
                program_ids=tuple(bridge.get(stype.pk, ())),
            )
            result = sch_calc.evaluate(student_in, term, type_in)
        except (KeyError, TypeError, ArithmeticError):
            problems.append("SNAPSHOT_INCOMPLETE")
        else:
            if (result.is_eligible, list(result.reason_codes)) != (
                e.is_eligible,
                list(e.reason_codes),
            ):
                problems.append("RESULT_DIFFERS")
        if problems:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-SCH-01",
                    "Eligibility",
                    e.pk,
                    e.student_id,
                    problems=problems,
                    is_eligible=e.is_eligible,
                    reason_codes=list(e.reason_codes or []),
                )
            )
    return checked, out


_TYPE_FIELDS = (
    "scholarship_type_id",
    "min_gpa",
    "max_gpa",
    "min_need_index",
    "residency",
    "first_term_only",
    "program_restricted",
    "effective_from",
    "effective_to",
)


class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- refunds --------------------------------------------------------------------------------------


@check("T-REFUND-DATES", family="TEMPORAL", rule_id="R-39")
def refund_dates(ds):
    """requested_date ≥ the source payment's received_date; processed_date ≥ requested_date
    (DATABASE_DESIGN Refund)."""
    out = []
    for r in ds.refunds:
        pay = ds.by_id["payments"].get(r.payment_id)
        if pay is not None and r.requested_date < pay.received_date:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "R-39",
                    "Refund",
                    r.pk,
                    r.student_id,
                    requested_date=r.requested_date,
                    received_date=pay.received_date,
                )
            )
        if r.processed_date is not None and r.processed_date < r.requested_date:
            out.append(
                _f(
                    policy.DATE_ORDER,
                    "BR-REF-03",
                    "Refund",
                    r.pk,
                    r.student_id,
                    requested_date=r.requested_date,
                    processed_date=r.processed_date,
                )
            )
    return len(ds.refunds), out


@check("RL-REFUND-ENROLLMENT", family="RELATIONSHIP", rule_id="R-26")
def refund_reason_matches_the_enrollment(ds):
    """A WITHDRAWAL refund names a WITHDRAWN enrollment, a CANCELLATION refund a CANCELLED one
    (payments.services.REFUND_ENROLLMENT_STATUS, decision D-W6; both statuses are terminal)."""
    out, checked = [], 0
    for r in ds.refunds:
        needed = REFUND_ENROLLMENT_STATUS.get(r.reason)
        if needed is None or r.enrollment_id is None:
            continue
        checked += 1
        enr = ds.by_id["enrollments"].get(r.enrollment_id)
        if enr is not None and enr.status != needed:
            out.append(
                _f(
                    policy.RELATIONSHIP_MISMATCH,
                    "R-26",
                    "Refund",
                    r.pk,
                    r.student_id,
                    reason=r.reason,
                    enrollment_id=enr.pk,
                    enrollment_status=enr.status,
                    required_status=needed,
                )
            )
    return checked, out


CAP_KEYS = ("net_paid", "credit_available", "payment_available")


@check("A-REFUND-RECORDED-CAPS", family="ARITHMETIC", rule_id="BR-REF-02")
def refund_within_its_recorded_caps(ds):
    """A refund not REJECTED is within the three caps recorded in its request-time
    ``calculation`` (BR-REF-01/02: net paid, credit available, source payment available).
    The credit-balance cap depends on the ledger at request time, which only that audit
    snapshot records; the per-payment cap is also re-checked live by BR-ALC-01 (P6-O2).
    Refunds already reported by D-R4 (REFUND_EXCEEDS_NET_PAID) are not reported again."""
    from reconciliation.checks.required import refund_exceeds_net_paid

    skip = {f.record_id for f in refund_exceeds_net_paid(ds)[1]}
    out, checked = [], 0
    for r in ds.refunds:
        if r.status == REJECTED or r.pk in skip:
            continue
        checked += 1
        calc = r.calculation or {}
        missing = [k for k in CAP_KEYS if k not in calc]
        if missing:
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    "BR-REF-02",
                    "Refund",
                    r.pk,
                    r.student_id,
                    None,
                    r.amount,
                    problem="CALCULATION_INCOMPLETE",
                    missing=missing,
                )
            )
            continue
        caps = {k: to_decimal(calc[k]) for k in CAP_KEYS}
        over = [k for k in CAP_KEYS if r.amount > caps[k]]
        if over:
            rule = "BR-REF-01" if over == ["net_paid"] else "BR-REF-02"
            out.append(
                _f(
                    policy.ARITHMETIC_MISMATCH,
                    rule,
                    "Refund",
                    r.pk,
                    r.student_id,
                    min(caps.values()),
                    r.amount,
                    problem="ABOVE_RECORDED_CAP",
                    caps_exceeded=over,
                )
            )
    return checked, out
