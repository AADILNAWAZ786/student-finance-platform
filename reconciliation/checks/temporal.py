"""Temporal checks (ARCHITECTURE §12.1): enroll ≤ issue ≤ due; payment ≥ first issue;
allocation ≥ payment date; withdrawal within term; fee-version chain order; and the
StatusEventBase rule that an event is not dated before the previous event of its record
(DATABASE_DESIGN §3). Each re-checks a date rule the services already enforce."""

from __future__ import annotations

from collections import defaultdict

from reconciliation import policy
from reconciliation.checks.base import GLOBAL, Finding, check

R_DATES = "R-39"
DATE_ORDER = policy.DATE_ORDER


def _f(rule, entity, row, student_id, **details):
    return Finding(
        DATE_ORDER,
        rule,
        entity,
        str(row.pk),
        student_id,
        details={k: (str(v) if v is not None else None) for k, v in details.items()},
    )


@check("T-INVOICE-DATES", family="TEMPORAL", rule_id="BR-INV-03")
def enroll_issue_due_order(ds):
    """enrolled_on ≤ issue_date ≤ due_date."""
    out = []
    for inv in ds.invoices:
        enr = ds.by_id["enrollments"].get(inv.enrollment_id)
        if enr is None:
            continue
        if not (enr.enrolled_on <= inv.issue_date <= inv.due_date):
            out.append(
                _f(
                    "BR-INV-03",
                    "Invoice",
                    inv,
                    inv.student_id,
                    enrolled_on=enr.enrolled_on,
                    issue_date=inv.issue_date,
                    due_date=inv.due_date,
                )
            )
    return len(ds.invoices), out


@check("T-PAYMENT-FIRST-ISSUE", family="TEMPORAL", rule_id=R_DATES)
def payment_after_first_invoice(ds):
    """A payment is received on or after its student's first invoice issue date (D-P9)."""
    first_issue = {}
    for inv in ds.invoices:
        d = first_issue.get(inv.student_id)
        first_issue[inv.student_id] = inv.issue_date if d is None else min(d, inv.issue_date)
    out = []
    for p in ds.payments:
        first = first_issue.get(p.student_id)
        if first is None or p.received_date < first:
            out.append(
                _f(
                    R_DATES,
                    "Payment",
                    p,
                    p.student_id,
                    received_date=p.received_date,
                    first_invoice_issue_date=first,
                )
            )
    return len(ds.payments), out


@check("T-ALLOCATION-DATES", family="TEMPORAL", rule_id=R_DATES)
def allocation_dates(ds):
    """An allocation is dated on or after its payment's received_date and its line's
    posted_date (D-P6); a release on or after the allocation it releases."""
    out = []
    for a in ds.payment_allocations:
        pay = ds.by_id["payments"].get(a.payment_id)
        line = ds.by_id["lines"].get(a.invoice_line_id)
        sid = pay and pay.student_id
        if pay and a.allocated_on < pay.received_date:
            out.append(
                _f(
                    R_DATES,
                    "PaymentAllocation",
                    a,
                    sid,
                    allocated_on=a.allocated_on,
                    received_date=pay.received_date,
                )
            )
        if a.allocation_type == "ALLOCATE" and line and a.allocated_on < line.posted_date:
            out.append(
                _f(
                    R_DATES,
                    "PaymentAllocation",
                    a,
                    sid,
                    allocated_on=a.allocated_on,
                    line_posted_date=line.posted_date,
                )
            )
        if a.allocation_type == "DEALLOCATE":
            orig = ds.by_id["payment_allocations"].get(a.reverses_id)
            if orig and a.allocated_on < orig.allocated_on:
                out.append(
                    _f(
                        "BR-ALC-05",
                        "PaymentAllocation",
                        a,
                        sid,
                        allocated_on=a.allocated_on,
                        released_allocation_on=orig.allocated_on,
                    )
                )
    return len(ds.payment_allocations), out


@check("T-EXIT-DATES", family="TEMPORAL", rule_id="BR-WDR-02")
def exit_dates_within_term(ds):
    """A withdrawal is within the term (start ≤ date ≤ end); a cancellation is before the
    start; both on or after enrolled_on (BR-WDR-01/02)."""
    out = []
    for enr in ds.enrollments:
        term = ds.by_id["terms"].get(enr.term_id)
        if term is None:
            continue
        wd, cd = enr.withdrawal_date, enr.cancellation_date
        if wd is not None and not (max(term.start_date, enr.enrolled_on) <= wd <= term.end_date):
            out.append(
                _f(
                    "BR-WDR-02",
                    "Enrollment",
                    enr,
                    enr.student_id,
                    withdrawal_date=wd,
                    start_date=term.start_date,
                    end_date=term.end_date,
                    enrolled_on=enr.enrolled_on,
                )
            )
        if cd is not None and not (enr.enrolled_on <= cd < term.start_date):
            out.append(
                _f(
                    "BR-WDR-01",
                    "Enrollment",
                    enr,
                    enr.student_id,
                    cancellation_date=cd,
                    start_date=term.start_date,
                    enrolled_on=enr.enrolled_on,
                )
            )
    return len(ds.enrollments), out


@check("T-FEE-CHAIN", family="TEMPORAL", rule_id="BR-FEE-03", scope=GLOBAL)
def fee_version_chain(ds):
    """Each fee version supersedes the previous version of the same program, term, fee type
    and residency, with version + 1 and a later effective_from (BR-FEE-03)."""
    out = []
    for fs in ds.fee_schedules:
        prev = ds.by_id["fee_schedules"].get(fs.supersedes_id) if fs.supersedes_id else None
        if prev is None:
            if fs.supersedes_id is None and fs.version != 1:
                out.append(_f("BR-FEE-03", "FeeSchedule", fs, None, version=fs.version))
            continue
        same_chain = (prev.program_id, prev.term_id, prev.fee_type, prev.residency) == (
            fs.program_id,
            fs.term_id,
            fs.fee_type,
            fs.residency,
        )
        if (
            not same_chain
            or fs.version != prev.version + 1
            or not fs.effective_from > prev.effective_from
        ):
            out.append(
                _f(
                    "BR-FEE-03",
                    "FeeSchedule",
                    fs,
                    None,
                    supersedes=prev.pk,
                    version=fs.version,
                    previous_version=prev.version,
                    effective_from=fs.effective_from,
                    previous_effective_from=prev.effective_from,
                )
            )
    return len(ds.fee_schedules), out


EVENT_TABLES = {
    "student_events": ("StudentStatusEvent", "student_id", "students"),
    "enrollment_events": ("EnrollmentEvent", "enrollment_id", "enrollments"),
    "invoice_events": ("InvoiceEvent", "invoice_id", "invoices"),
    "scholarship_events": ("ScholarshipEvent", "scholarship_id", "scholarships"),
    "refund_events": ("RefundEvent", "refund_id", "refunds"),
}


def event_chains(ds, name):
    """{(record id, dimension): [events in creation (event_id) order]}. Invoices have two
    independent chains, LIFECYCLE and COLLECTION (decision D13)."""
    _, fk, _ = EVENT_TABLES[name]
    chains = defaultdict(list)
    for e in getattr(ds, name):
        chains[(getattr(e, fk), getattr(e, "dimension", None))].append(e)
    return {k: sorted(v, key=lambda e: e.pk) for k, v in sorted(chains.items(), key=str)}


def owner_student(ds, name, record_id):
    owner = ds.by_id[EVENT_TABLES[name][2]].get(record_id)
    if owner is None:
        return None
    return owner.pk if name == "student_events" else owner.student_id


@check("T-EVENT-ORDER", family="TEMPORAL", rule_id=R_DATES)
def events_never_go_back_in_time(ds):
    """Within one record's chain, no event is dated before the previous one."""
    out, checked = [], 0
    for name, (entity, _, _) in EVENT_TABLES.items():
        for (record_id, dimension), chain in event_chains(ds, name).items():
            for prev, e in zip(chain, chain[1:], strict=False):
                checked += 1
                if e.event_date < prev.event_date:
                    out.append(
                        _f(
                            R_DATES,
                            entity,
                            e,
                            owner_student(ds, name, record_id),
                            record=record_id,
                            dimension=dimension,
                            event_date=e.event_date,
                            previous_event_id=prev.pk,
                            previous_event_date=prev.event_date,
                        )
                    )
    return checked, out
