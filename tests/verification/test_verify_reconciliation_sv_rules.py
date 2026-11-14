"""P6-O7 resolution: every newly re-checked service-layer rule (docs/phase6_sv_coverage.md) is
found on a real corruption of the clean world, written below the services (QuerySet.update or
plain ORM inserts), with its documented contract: issue type, rule ID, severity, recommended
action, entity, record_id and student. Where one corruption necessarily breaks two rules, both
findings are asserted and the reason is given. Rules whose corruption cannot be stored in
isolation are covered on pure rows in tests/unit/test_reconciliation_sv_rules.py."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from academics import services as academics
from academics.models import Enrollment, Term
from billing import calculators as billing_calc
from billing.models import FeeSchedule, Invoice, InvoiceLine, LedgerEntry
from core.ids import fee_schedule_id, next_id
from payments.models import Refund
from reconciliation import services
from reconciliation.models import AnomalyFinding
from scholarships import services as scholarships
from scholarships.models import Eligibility, Scholarship, ScholarshipEvent
from students.models import Student

pytestmark = pytest.mark.django_db

END = date(2026, 12, 31)


def _rows():
    run = services.run_reconciliation(scope="ALL", as_of_date=END, triggered_by="API")
    assert run.status == "COMPLETED", run.summary
    return [
        (
            f.issue_type,
            f.rule_id,
            f.severity,
            f.recommended_action,
            f.entity_type,
            f.record_id,
            f.student_id,
            f.details.get("check_id"),
        )
        for f in AnomalyFinding.objects.filter(run=run).order_by("finding_id")
    ]


def _one(issue, rule, sev, entity, record, student, check_id):
    return (issue, rule, sev, "INVESTIGATE", entity, record, student, check_id)


def test_student_age(clean_world):
    s = clean_world["S15"]
    Student.objects.filter(pk=s.pk).update(date_of_birth=date(2015, 1, 1))
    assert _rows() == [_one("DATE_ORDER", "R-39", "MEDIUM", "Student", s.pk, s.pk, "T-STUDENT-AGE")]


def test_enrollment_before_the_admit_term(clean_world, terms):
    s = clean_world["S15"]
    enr = academics.enroll(
        student=s,
        term=terms["TERM-2026-SPRING"],
        credits=16,
        enrolled_on=date(2026, 1, 20),
        actor="t",
    )
    Student.objects.filter(pk=s.pk).update(admit_term=terms["TERM-2026-FALL"])
    assert _rows() == [
        _one("DATE_ORDER", "R-39", "MEDIUM", "Enrollment", enr.pk, s.pk, "T-ENROLLMENT-DATES")
    ]


def test_enrolled_after_add_drop_also_breaks_the_invoice_date_order(clean_world):
    """enrolled_on after start + 7 is necessarily after the (earlier) issue date as well."""
    enr = Enrollment.objects.get(student=clean_world["S16"])
    Enrollment.objects.filter(pk=enr.pk).update(enrolled_on=date(2026, 9, 20))
    inv = Invoice.objects.get(enrollment=enr)
    assert sorted(_rows()) == sorted(
        [
            _one(
                "DATE_ORDER",
                "BR-INV-03",
                "MEDIUM",
                "Invoice",
                inv.pk,
                enr.student_id,
                "T-INVOICE-DATES",
            ),
            _one(
                "DATE_ORDER",
                "R-39",
                "MEDIUM",
                "Enrollment",
                enr.pk,
                enr.student_id,
                "T-ENROLLMENT-DATES",
            ),
        ]
    )


def test_credits_out_of_range(clean_world, terms):
    s = clean_world["S15"]
    enr = academics.enroll(
        student=s,
        term=terms["TERM-2026-SPRING"],
        credits=16,
        enrolled_on=date(2026, 1, 20),
        actor="t",
    )
    Enrollment.objects.filter(pk=enr.pk).update(credits=40)
    assert _rows() == [
        _one(
            "RELATIONSHIP_MISMATCH",
            "R-26",
            "MEDIUM",
            "Enrollment",
            enr.pk,
            s.pk,
            "RL-ENROLLMENT-RULES",
        )
    ]


def test_enrollment_in_a_non_billable_term(clean_world, terms):
    s = clean_world["S15"]
    Student.objects.filter(pk=s.pk).update(admit_term=terms["TERM-2025-FALL"])
    enr = academics.enroll(
        student=s,
        term=terms["TERM-2026-SPRING"],
        credits=16,
        enrolled_on=date(2026, 1, 20),
        actor="t",
    )
    Enrollment.objects.filter(pk=enr.pk).update(term=terms["TERM-2025-FALL"])
    rows = _rows()
    assert (
        "RELATIONSHIP_MISMATCH",
        "R-26",
        "MEDIUM",
        "INVESTIGATE",
        "Enrollment",
        enr.pk,
        s.pk,
        "RL-ENROLLMENT-RULES",
    ) in rows
    # moved into a 2025 term, the enrollment also breaks two other rules: its enrolled_on
    # (2026) is after that term's add/drop window, and its ID year is no longer the term's
    assert {(r[0], r[7]) for r in rows} == {
        ("RELATIONSHIP_MISMATCH", "RL-ENROLLMENT-RULES"),
        ("DATE_ORDER", "T-ENROLLMENT-DATES"),
        ("INVALID_ID_FORMAT", "S-ID-DOMAIN"),
    }
    assert {r[5] for r in rows} == {enr.pk}


def test_census_date(clean_world, terms):
    summer = terms["TERM-2026-SUMMER"]
    Term.objects.filter(pk=summer.pk).update(census_date=summer.census_date + timedelta(days=1))
    assert _rows() == [
        _one("DATE_ORDER", "BR-WDR-02", "MEDIUM", "Term", summer.pk, None, "T-TERM-CENSUS")
    ]


def test_fee_schedule_for_a_non_billable_term(clean_world, program, terms):
    old = terms["TERM-2025-FALL"]
    fs = FeeSchedule.objects.create(
        fee_schedule_id=fee_schedule_id(program.pk, old.pk, "LIBRARY", "ALL", 1),
        program=program,
        term=old,
        fee_type="LIBRARY",
        residency="ALL",
        charge_basis="FLAT",
        amount=D("100.00"),
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=True,
        allocation_priority=50,
        version=1,
        effective_from=date(2025, 1, 1),
    )
    assert _rows() == [
        _one(
            "RELATIONSHIP_MISMATCH",
            "R-26",
            "MEDIUM",
            "FeeSchedule",
            fs.pk,
            None,
            "RL-BILLABLE-TERM-MASTER",
        )
    ]


def test_invoice_due_date(clean_world):
    inv = Invoice.objects.get(student=clean_world["S2"])  # scholarship-covered: no late fee
    Invoice.objects.filter(pk=inv.pk).update(due_date=inv.due_date + timedelta(days=1))
    assert _rows() == [
        _one(
            "DATE_ORDER",
            "BR-INV-03",
            "MEDIUM",
            "Invoice",
            inv.pk,
            inv.student_id,
            "T-INVOICE-CALENDAR",
        )
    ]


def test_fee_type_not_applicable_also_mismatches_the_schedule_residency(clean_world):
    """A domestic student cannot have INTERNATIONAL_SERVICE (BR-FEE-04), and neither the
    international tuition schedule nor the INTERNATIONAL service schedule matches the student
    any more (BR-FEE-01): one corruption, three rules."""
    s = clean_world["S18"]
    Student.objects.filter(pk=s.pk).update(residency="DOMESTIC")
    inv = Invoice.objects.get(student=s)
    tuition = inv.lines.get(fee_type="TUITION").pk
    service = inv.lines.get(fee_type="INTERNATIONAL_SERVICE").pk
    assert sorted(_rows()) == sorted(
        [
            _one(
                "RELATIONSHIP_MISMATCH",
                "BR-FEE-04",
                "MEDIUM",
                "InvoiceLine",
                service,
                s.pk,
                "RL-FEE-APPLICABLE",
            ),
            _one(
                "RELATIONSHIP_MISMATCH",
                "BR-FEE-01",
                "MEDIUM",
                "InvoiceLine",
                tuition,
                s.pk,
                "RL-LINE-SCHEDULE",
            ),
            _one(
                "RELATIONSHIP_MISMATCH",
                "BR-FEE-01",
                "MEDIUM",
                "InvoiceLine",
                service,
                s.pk,
                "RL-LINE-SCHEDULE",
            ),
        ]
    )


def test_cancelled_invoice_without_a_cancelled_enrollment_also_breaks_its_stored_status(
    clean_world,
):
    """Re-labelling S7's invoice ISSUED contradicts its lifecycle events too."""
    inv = Invoice.objects.get(student=clean_world["S7"])
    Invoice.objects.filter(pk=inv.pk).update(lifecycle_status="ISSUED")
    assert sorted(_rows()) == sorted(
        [
            (
                "INVOICE_STATUS_MISMATCH",
                "BR-INV-08",
                "MEDIUM",
                "INVESTIGATE",
                "Invoice",
                inv.pk,
                inv.student_id,
                "L-STORED-STATUS",
            ),
            (
                "RELATIONSHIP_MISMATCH",
                "BR-WDR-01",
                "MEDIUM",
                "INVESTIGATE",
                "Invoice",
                inv.pk,
                inv.student_id,
                "RL-CANCELLED-INVOICE",
            ),
        ]
    )


def test_exit_adjustments_that_do_not_match_the_schedule(clean_world):
    """D08 withdrew on day 8 (60 %); a recorded day 7 means 80 %, so the posted adjustments
    and their date no longer match BR-WDR-02."""
    s = clean_world["D08"]
    enr = Enrollment.objects.get(student=s)
    Enrollment.objects.filter(pk=enr.pk).update(withdrawal_date=enr.withdrawal_date - timedelta(1))
    inv = Invoice.objects.get(enrollment=enr)
    rows = _rows()
    assert rows == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-WDR-02",
            "HIGH",
            "INVESTIGATE",
            "Invoice",
            inv.pk,
            s.pk,
            "A-EXIT-ADJUSTMENTS",
        )
    ]


def test_a_second_late_fee(clean_world):
    s = clean_world["S6"]
    inv = Invoice.objects.get(student=s)
    first = inv.lines.get(line_type="LATE_FEE")
    second = InvoiceLine.objects.create(
        line_id=next_id("invoice_line", parent=inv.pk),
        invoice=inv,
        line_type="LATE_FEE",
        fee_type="LATE_FEE",
        description="Late fee",
        quantity=1,
        unit_amount=D("500.00"),
        gross_amount=D("500.00"),
        net_amount=D("500.00"),
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=False,
        allocation_priority=90,
        posted_date=first.posted_date,
    )
    LedgerEntry.objects.create(
        entry_id=next_id("ledger_entry", year=2026),
        student=s,
        entry_date=second.posted_date,
        entry_type="LATE_FEE",
        amount=billing_calc.ledger_amount("LATE_FEE", D("500.00")),
        invoice_line=second,
    )
    rows = _rows()
    assert [r[:8] for r in rows] == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-LATE-01",
            "HIGH",
            "INVESTIGATE",
            "InvoiceLine",
            second.pk,
            s.pk,
            "A-LATE-FEE",
        )
    ]
    f = AnomalyFinding.objects.get(record_id=second.pk)
    assert f.details["problems"] == ["MORE_THAN_ONE"]


def test_award_above_the_per_term_cap(clean_world):
    """S3's second need award: the cap (40,000) less the first award (15,000) leaves 25,000;
    30,000 breaks BR-SCH-03 while the invoice's reservations stay within the base (D-R5)."""
    s = clean_world["S3"]
    b3 = Scholarship.objects.filter(student=s, status="OFFERED").get()
    Scholarship.objects.filter(pk=b3.pk).update(awarded_amount=D("30000.00"))
    rows = _rows()
    assert rows == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-SCH-03",
            "HIGH",
            "INVESTIGATE",
            "Scholarship",
            b3.pk,
            s.pk,
            "A-SCHOLARSHIP-CAP",
        )
    ]
    f = AnomalyFinding.objects.get(record_id=b3.pk)
    assert (f.expected_value, f.actual_value) == (D("25000.00"), D("30000.00"))


def _award(student, stype, invoice, amount, on):
    elg = next(
        e
        for e in scholarships.evaluate_eligibility(student=student, term=invoice.term)
        if e.scholarship_type_id == stype and e.is_eligible
    )
    s = Scholarship.objects.create(
        scholarship_id=next_id("scholarship", year=on.year),
        student=student,
        scholarship_type_id=stype,
        term=invoice.term,
        eligibility=elg,
        invoice=invoice,
        awarded_amount=D(amount),
        award_date=on,
        status="ACCEPTED",
    )
    for frm, to in ((None, "OFFERED"), ("OFFERED", "ACCEPTED")):
        ScholarshipEvent.objects.create(
            event_id=next_id("status_event", entity="SCH", year=on.year),
            scholarship=s,
            from_status=frm,
            to_status=to,
            event_date=on,
            reason="IMPORTED",
            actor="legacy",
        )
    return s


def test_second_award_in_an_exclusive_group(clean_world):
    s = clean_world["S1"]
    inv = Invoice.objects.get(student=s)
    dup = _award(s, "SCT-001", inv, "100.00", inv.issue_date)
    assert _rows() == [
        (
            "DUPLICATE_RECORD",
            "BR-SCH-05",
            "HIGH",
            "INVESTIGATE",
            "Scholarship",
            dup.pk,
            s.pk,
            "S-SCHOLARSHIP-GROUP",
        )
    ]


def test_award_on_an_invoice_already_cancelled(clean_world):
    s = clean_world["S7"]
    inv = Invoice.objects.get(student=s)
    late = _award(s, "SCT-001", inv, "100.00", date(2026, 8, 26))  # cancelled 08-25
    assert _rows() == [
        (
            "RELATIONSHIP_MISMATCH",
            "BR-INV-08",
            "MEDIUM",
            "INVESTIGATE",
            "Scholarship",
            late.pk,
            s.pk,
            "RL-AWARD-ON-ISSUED-INVOICE",
        )
    ]


def test_applied_above_awarded(clean_world):
    s = clean_world["S3"]
    a3 = Scholarship.objects.filter(student=s, status="PARTIALLY_REVERSED").get()  # net 10,000
    Scholarship.objects.filter(pk=a3.pk).update(awarded_amount=D("9000.00"))
    assert _rows() == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-SCH-03",
            "HIGH",
            "INVESTIGATE",
            "Scholarship",
            a3.pk,
            s.pk,
            "A-AWARD-APPLIED",
        )
    ]


def test_award_dated_before_its_evaluation(clean_world):
    s = clean_world["S3"]
    b3 = Scholarship.objects.filter(student=s, status="OFFERED").get()
    Scholarship.objects.filter(pk=b3.pk).update(award_date=date(2026, 8, 16))
    assert _rows() == [
        _one("DATE_ORDER", "R-39", "MEDIUM", "Scholarship", b3.pk, s.pk, "T-SCHOLARSHIP-DATES")
    ]


def test_eligibility_that_the_calculator_does_not_produce(clean_world):
    s = clean_world["S16"]
    e = Eligibility.objects.filter(student=s, is_eligible=False).order_by("pk").first()
    Eligibility.objects.filter(pk=e.pk).update(is_eligible=True, reason_codes=["ALL_CRITERIA_MET"])
    assert _rows() == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-SCH-01",
            "HIGH",
            "INVESTIGATE",
            "Eligibility",
            e.pk,
            s.pk,
            "A-ELIGIBILITY",
        )
    ]
    assert AnomalyFinding.objects.get(record_id=e.pk).details["problems"] == ["RESULT_DIFFERS"]


def test_refund_requested_before_its_payment(clean_world):
    r = Refund.objects.filter(student=clean_world["S1"], status="REQUESTED").order_by("pk").first()
    pay = r.payment
    Refund.objects.filter(pk=r.pk).update(requested_date=pay.received_date - timedelta(days=1))
    assert _rows() == [
        _one("DATE_ORDER", "R-39", "MEDIUM", "Refund", r.pk, r.student_id, "T-REFUND-DATES")
    ]


def test_refund_above_its_recorded_credit_cap(clean_world):
    r = Refund.objects.filter(
        student=clean_world["S1"], status="REQUESTED", amount=D("300.00")
    ).get()
    Refund.objects.filter(pk=r.pk).update(
        calculation={**r.calculation, "credit_available": "100.00"}
    )
    assert _rows() == [
        (
            "ARITHMETIC_MISMATCH",
            "BR-REF-02",
            "HIGH",
            "INVESTIGATE",
            "Refund",
            r.pk,
            r.student_id,
            "A-REFUND-RECORDED-CAPS",
        )
    ]


def test_d_r4_and_d_r5_findings_are_not_reported_twice(clean_world):
    """The injected anomalies keep exactly their D-R4 / D-R5 findings (and the one documented
    consequence): the new BR-SCH-03 and recorded-cap checks stay silent on those records."""
    from tests.reconciliation_fixtures import (
        inject_refund_over_net_paid,
        inject_scholarship_over_award,
    )

    sid = inject_scholarship_over_award(clean_world["S16"])
    rid = inject_refund_over_net_paid(clean_world["S17"])
    rows = _rows()
    assert {(r[0], r[5]) for r in rows if r[5] in (sid, rid)} == {
        ("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", sid),
        ("REFUND_EXCEEDS_NET_PAID", rid),
    }


def test_withdrawal_refund_relabelled_as_a_cancellation(clean_world):
    r = Refund.objects.get(student=clean_world["S1"], reason="WITHDRAWAL")
    Refund.objects.filter(pk=r.pk).update(reason="CANCELLATION")
    assert _rows() == [
        (
            "RELATIONSHIP_MISMATCH",
            "R-26",
            "MEDIUM",
            "INVESTIGATE",
            "Refund",
            r.pk,
            r.student_id,
            "RL-REFUND-ENROLLMENT",
        )
    ]
