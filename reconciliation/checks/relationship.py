"""Relationship checks (ARCHITECTURE §12.1; R-26 "validate all joins"): the rows a record
points at must agree with it. Each finding is on the child row that disagrees."""

from __future__ import annotations

from reconciliation import policy
from reconciliation.checks.base import Finding, check

R_JOIN = "R-26"
MISMATCH = policy.RELATIONSHIP_MISMATCH


def _f(rule, entity, row, student_id, **details):
    return Finding(MISMATCH, rule, entity, str(row.pk), student_id, details=details)


@check("RL-INVOICE-ENROLLMENT", family="RELATIONSHIP", rule_id=R_JOIN)
def invoice_matches_enrollment(ds):
    """An invoice's student and term are its enrollment's."""
    out = []
    for inv in ds.invoices:
        enr = ds.by_id["enrollments"].get(inv.enrollment_id)
        if enr and (enr.student_id, enr.term_id) != (inv.student_id, inv.term_id):
            out.append(
                _f(
                    R_JOIN,
                    "Invoice",
                    inv,
                    inv.student_id,
                    enrollment_id=enr.pk,
                    invoice=[inv.student_id, inv.term_id],
                    enrollment=[enr.student_id, enr.term_id],
                )
            )
    return len(ds.invoices), out


@check("RL-ENROLLMENT-PROGRAM", family="RELATIONSHIP", rule_id=R_JOIN)
def enrollment_program_is_students(ds):
    """An enrollment's program is its student's program."""
    out = []
    for enr in ds.enrollments:
        stu = ds.by_id["students"].get(enr.student_id)
        if stu and stu.program_id != enr.program_id:
            out.append(
                _f(
                    R_JOIN,
                    "Enrollment",
                    enr,
                    enr.student_id,
                    enrollment_program=enr.program_id,
                    student_program=stu.program_id,
                )
            )
    return len(ds.enrollments), out


@check("RL-LINE-SCHEDULE", family="RELATIONSHIP", rule_id=R_JOIN)
def fee_line_matches_its_schedule(ds):
    """A FEE line's fee schedule is for the invoice's program, term, residency and fee type."""
    out, checked = [], 0
    for line in ds.lines:
        if line.line_type != "FEE" or line.fee_schedule_id is None:
            continue
        checked += 1
        inv = ds.by_id["invoices"].get(line.invoice_id)
        fs = ds.by_id["fee_schedules"].get(line.fee_schedule_id)
        enr = inv and ds.by_id["enrollments"].get(inv.enrollment_id)
        stu = inv and ds.by_id["students"].get(inv.student_id)
        if not (inv and fs and enr and stu):
            continue
        problems = []
        if fs.program_id != enr.program_id:
            problems.append("program")
        if fs.term_id != inv.term_id:
            problems.append("term")
        if fs.residency not in ("ALL", stu.residency):
            problems.append("residency")
        if fs.fee_type != line.fee_type:
            problems.append("fee_type")
        if problems:
            out.append(
                _f(
                    "BR-FEE-01",
                    "InvoiceLine",
                    line,
                    inv.student_id,
                    fee_schedule_id=fs.pk,
                    mismatched=problems,
                )
            )
    return checked, out


@check("RL-ADJUSTMENT-TARGET", family="RELATIONSHIP", rule_id="BR-ALC-02")
def adjustment_adjusts_a_charge_on_its_invoice(ds):
    """An ADJUSTMENT line adjusts a FEE or LATE_FEE line of the same invoice."""
    out, checked = [], 0
    for line in ds.lines:
        if line.line_type != "ADJUSTMENT":
            continue
        checked += 1
        target = ds.by_id["lines"].get(line.adjusts_line_id)
        inv = ds.by_id["invoices"].get(line.invoice_id)
        if target is None:
            continue  # an orphan is S-FK-ORPHAN's finding
        if target.invoice_id != line.invoice_id or target.line_type not in ("FEE", "LATE_FEE"):
            out.append(
                _f(
                    "BR-ALC-02",
                    "InvoiceLine",
                    line,
                    inv and inv.student_id,
                    adjusts_line_id=target.pk,
                    target_invoice_id=target.invoice_id,
                    target_line_type=target.line_type,
                )
            )
    return checked, out


@check("RL-PAYMENT-ALLOCATION", family="RELATIONSHIP", rule_id=R_JOIN)
def payment_allocation_joins(ds):
    """A payment allocation is on a FEE/LATE_FEE line of the payment's student, and a
    DEALLOCATE row releases an ALLOCATE row of the same payment and line (BR-ALC-05)."""
    out = []
    for a in ds.payment_allocations:
        pay = ds.by_id["payments"].get(a.payment_id)
        line = ds.by_id["lines"].get(a.invoice_line_id)
        inv = line and ds.by_id["invoices"].get(line.invoice_id)
        sid = pay and pay.student_id
        if pay and inv and inv.student_id != pay.student_id:
            out.append(_f(R_JOIN, "PaymentAllocation", a, sid, line_student_id=inv.student_id))
        if line and line.line_type not in ("FEE", "LATE_FEE"):
            out.append(_f("BR-ALC-02", "PaymentAllocation", a, sid, line_type=line.line_type))
        if a.allocation_type == "DEALLOCATE":
            orig = ds.by_id["payment_allocations"].get(a.reverses_id)
            if orig is None or (
                orig.allocation_type,
                orig.payment_id,
                orig.invoice_line_id,
            ) != ("ALLOCATE", a.payment_id, a.invoice_line_id):
                out.append(_f("BR-ALC-05", "PaymentAllocation", a, sid, reverses_id=a.reverses_id))
        elif a.reverses_id is not None:
            out.append(_f("BR-ALC-05", "PaymentAllocation", a, sid, reverses_id=a.reverses_id))
    return len(ds.payment_allocations), out


@check("RL-AWARD", family="RELATIONSHIP", rule_id=R_JOIN)
def award_joins(ds):
    """An award's invoice is its student's for its term; its eligibility is a positive row
    for the same student, type and term (BR-SCH-02)."""
    out = []
    for s in ds.scholarships:
        inv = ds.by_id["invoices"].get(s.invoice_id)
        if inv and (inv.student_id, inv.term_id) != (s.student_id, s.term_id):
            out.append(
                _f(
                    R_JOIN,
                    "Scholarship",
                    s,
                    s.student_id,
                    invoice_id=inv.pk,
                    invoice=[inv.student_id, inv.term_id],
                )
            )
        elg = ds.by_id["eligibilities"].get(s.eligibility_id)
        if elg and (
            (elg.student_id, elg.scholarship_type_id, elg.term_id)
            != (s.student_id, s.scholarship_type_id, s.term_id)
            or not elg.is_eligible
        ):
            out.append(
                _f(
                    "BR-SCH-02",
                    "Scholarship",
                    s,
                    s.student_id,
                    eligibility_id=elg.pk,
                    is_eligible=elg.is_eligible,
                )
            )
    return len(ds.scholarships), out


@check("RL-SCHOLARSHIP-ALLOCATION", family="RELATIONSHIP", rule_id=R_JOIN)
def scholarship_allocation_joins(ds):
    """A scholarship allocation is on a FEE line of its award's invoice; a REVERSE row
    reverses an APPLY row of the same award and line (BR-SCH-07)."""
    out = []
    for a in ds.scholarship_allocations:
        s = ds.by_id["scholarships"].get(a.scholarship_id)
        line = ds.by_id["lines"].get(a.invoice_line_id)
        sid = s and s.student_id
        if s and line and line.invoice_id != s.invoice_id:
            out.append(_f(R_JOIN, "ScholarshipAllocation", a, sid, line_invoice_id=line.invoice_id))
        if line and line.line_type != "FEE":
            out.append(_f("BR-SCH-08", "ScholarshipAllocation", a, sid, line_type=line.line_type))
        if a.allocation_type == "REVERSE":
            orig = ds.by_id["scholarship_allocations"].get(a.reverses_id)
            if orig is None or (
                orig.allocation_type,
                orig.scholarship_id,
                orig.invoice_line_id,
            ) != ("APPLY", a.scholarship_id, a.invoice_line_id):
                out.append(
                    _f("BR-SCH-07", "ScholarshipAllocation", a, sid, reverses_id=a.reverses_id)
                )
        elif a.reverses_id is not None:
            out.append(_f("BR-SCH-07", "ScholarshipAllocation", a, sid, reverses_id=a.reverses_id))
    return len(ds.scholarship_allocations), out


@check("RL-REFUND", family="RELATIONSHIP", rule_id=R_JOIN)
def refund_joins(ds):
    """A refund is its payment's student's, goes back to that payment's payer (BR-REF-04),
    and any enrollment it names is the same student's (decision D-W6)."""
    out = []
    for r in ds.refunds:
        pay = ds.by_id["payments"].get(r.payment_id)
        if pay and pay.student_id != r.student_id:
            out.append(_f(R_JOIN, "Refund", r, r.student_id, payment_student_id=pay.student_id))
        if pay and pay.payer_type != r.payer_type:
            out.append(
                _f(
                    "BR-REF-04",
                    "Refund",
                    r,
                    r.student_id,
                    refund_payer_type=r.payer_type,
                    payment_payer_type=pay.payer_type,
                )
            )
        if r.enrollment_id is not None:
            enr = ds.by_id["enrollments"].get(r.enrollment_id)
            if enr and enr.student_id != r.student_id:
                out.append(
                    _f(
                        R_JOIN,
                        "Refund",
                        r,
                        r.student_id,
                        enrollment_id=enr.pk,
                        enrollment_student_id=enr.student_id,
                    )
                )
    return len(ds.refunds), out
