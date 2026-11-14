"""Newly re-checked service-layer rules on pure rows (no database): the variants a database
corruption cannot produce in isolation (a DB CHECK forbids it, or the corruption would
break other rules first) and every problem branch of the multi-part checks."""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from reconciliation.checks import REGISTRY
from reconciliation.dataset import Dataset

AS_OF = date(2026, 12, 31)
FALL = SimpleNamespace(
    pk="TERM-2026-FALL",
    term_id="TERM-2026-FALL",
    year=2026,
    is_billable=True,
    invoice_date=date(2026, 8, 17),
    due_date=date(2026, 9, 14),
    start_date=date(2026, 9, 7),
    census_date=date(2026, 10, 5),
    end_date=date(2026, 12, 19),
    late_fee_grace_days=14,
)
INV = "INV-STU-000001-2026-000001"


def row(pk, **kw):
    return SimpleNamespace(pk=pk, **kw)


def line(n, line_type="FEE", net="1000.00", **kw):
    fields = dict(
        line_id=f"{INV}-L{n:02d}",
        invoice_id=INV,
        line_type=line_type,
        fee_type="TUITION",
        net_amount=D(net),
        gross_amount=D(net),
        waiver_amount=D("0.00"),
        quantity=D("1"),
        unit_amount=D(net),
        posted_date=date(2026, 8, 17),
        is_waivable=False,
        is_scholarship_eligible=True,
        is_refundable=True,
        allocation_priority=10,
        adjusts_line_id=None,
        adjustment_reason=None,
    )
    fields.update(kw)
    return row(fields["line_id"], **fields)


def invoice(**kw):
    fields = dict(
        invoice_id=INV,
        student_id="STU-000001",
        enrollment_id="ENR-2026-000001",
        term_id=FALL.pk,
        issue_date=date(2026, 8, 17),
        due_date=date(2026, 9, 14),
        lifecycle_status="ISSUED",
    )
    fields.update(kw)
    return row(INV, **fields)


def enrollment(**kw):
    fields = dict(
        enrollment_id="ENR-2026-000001",
        student_id="STU-000001",
        term_id=FALL.pk,
        program_id="PRG-001",
        status="ENROLLED",
        enrolled_on=date(2026, 8, 10),
        withdrawal_date=None,
        cancellation_date=None,
        credits=16,
    )
    fields.update(kw)
    return row("ENR-2026-000001", **fields)


def run(check_id, ds):
    return REGISTRY[check_id].fn(ds)[1]


def ds_with(**tables):
    return Dataset(as_of=AS_OF, terms=[FALL], **tables)


# --- invoices -------------------------------------------------------------------------------------


def test_a_line_posted_before_its_invoice_was_issued():
    early = line(1, posted_date=date(2026, 8, 16))
    found = run(
        "T-INVOICE-CALENDAR",
        ds_with(invoices=[invoice()], enrollments=[enrollment()], lines=[early]),
    )
    assert [(f.issue_type, f.rule_id, f.entity_type, f.record_id) for f in found] == [
        ("DATE_ORDER", "R-39", "InvoiceLine", early.pk)
    ]


def test_waiver_on_a_line_that_is_not_waivable():
    waived = line(1, waiver_amount=D("100.00"), is_waivable=False)
    ok = line(2, waiver_amount=D("100.00"), is_waivable=True)
    found = run("A-WAIVER-WAIVABLE", ds_with(invoices=[invoice()], lines=[waived, ok]))
    assert [(f.issue_type, f.rule_id, f.record_id, f.actual) for f in found] == [
        ("ARITHMETIC_MISMATCH", "BR-WAV-01", waived.pk, D("100.00"))
    ]


def test_void_invoice_with_money_on_it():
    inv = invoice(lifecycle_status="VOID")
    l1 = line(1)
    pay = row("ALLOC-PAY-2026-000001-01", invoice_line_id=l1.pk, amount=D("10.00"))
    ds = ds_with(invoices=[inv], lines=[l1], payment_allocations=[pay])
    found = run("A-VOID-INVOICE", ds)
    assert [(f.issue_type, f.rule_id, f.record_id, f.actual) for f in found] == [
        ("ARITHMETIC_MISMATCH", "BR-INV-08", INV, D("10.00"))
    ]
    clean = ds_with(invoices=[inv], lines=[l1])
    assert run("A-VOID-INVOICE", clean) == []


@pytest.mark.parametrize(
    ("lifecycle", "enrollment_status", "flagged"),
    [
        ("CANCELLED", "CANCELLED", False),
        ("ISSUED", "ENROLLED", False),
        ("ISSUED", "CANCELLED", True),  # enrollment cancelled, invoice not
        ("CANCELLED", "WITHDRAWN", True),
        ("VOID", "CANCELLED", False),  # a VOID invoice stays VOID (D-W7)
    ],
)
def test_cancelled_invoice_iff_cancelled_enrollment(lifecycle, enrollment_status, flagged):
    ds = ds_with(
        invoices=[invoice(lifecycle_status=lifecycle)],
        enrollments=[enrollment(status=enrollment_status)],
    )
    found = run("RL-CANCELLED-INVOICE", ds)
    assert [(f.issue_type, f.rule_id) for f in found] == (
        [("RELATIONSHIP_MISMATCH", "BR-WDR-01")] if flagged else []
    )


def test_exit_adjustment_on_an_enrollment_that_never_exited():
    l1 = line(1)
    adj = line(2, "ADJUSTMENT", "-600.00", adjusts_line_id=l1.pk, adjustment_reason="WITHDRAWAL")
    found = run(
        "A-EXIT-ADJUSTMENTS",
        ds_with(invoices=[invoice()], enrollments=[enrollment()], lines=[l1, adj]),
    )
    assert [(f.rule_id, f.details["problem"]) for f in found] == [
        ("BR-WDR-02", "EXIT_ADJUSTMENT_WITHOUT_EXIT")
    ]


def test_withdrawal_without_its_adjustments_and_a_correct_one():
    l1 = line(1, net="144000.00")
    wd = date(2026, 9, 17)  # day 10: 60 %
    enr = enrollment(status="WITHDRAWN", withdrawal_date=wd)
    missing = run(
        "A-EXIT-ADJUSTMENTS", ds_with(invoices=[invoice()], enrollments=[enr], lines=[l1])
    )
    assert [(f.rule_id, f.expected, f.actual) for f in missing] == [
        ("BR-WDR-02", D("-86400.00"), D("0.00"))
    ]
    adj = line(
        2,
        "ADJUSTMENT",
        "-86400.00",
        adjusts_line_id=l1.pk,
        adjustment_reason="WITHDRAWAL",
        posted_date=wd,
    )
    assert (
        run("A-EXIT-ADJUSTMENTS", ds_with(invoices=[invoice()], enrollments=[enr], lines=[l1, adj]))
        == []
    )
    wrong_date = line(
        2,
        "ADJUSTMENT",
        "-86400.00",
        adjusts_line_id=l1.pk,
        adjustment_reason="WITHDRAWAL",
        posted_date=date(2026, 9, 18),
    )
    [f] = run(
        "A-EXIT-ADJUSTMENTS",
        ds_with(invoices=[invoice()], enrollments=[enr], lines=[l1, wrong_date]),
    )
    assert f.details["dates_ok"] is False


def test_cancellation_adjusts_everything_but_registration():
    l1 = line(1, net="144000.00")
    reg = line(3, net="5000.00", fee_type="REGISTRATION", is_refundable=False)
    cd = date(2026, 8, 25)
    enr = enrollment(status="CANCELLED", cancellation_date=cd)
    adj = line(
        7,
        "ADJUSTMENT",
        "-144000.00",
        adjusts_line_id=l1.pk,
        adjustment_reason="CANCELLATION",
        posted_date=cd,
    )
    ok = ds_with(
        invoices=[invoice(lifecycle_status="CANCELLED")], enrollments=[enr], lines=[l1, reg, adj]
    )
    assert run("A-EXIT-ADJUSTMENTS", ok) == []
    reg_adj = line(
        8,
        "ADJUSTMENT",
        "-5000.00",
        adjusts_line_id=reg.pk,
        adjustment_reason="CANCELLATION",
        posted_date=cd,
    )
    bad = ds_with(
        invoices=[invoice(lifecycle_status="CANCELLED")],
        enrollments=[enr],
        lines=[l1, reg, adj, reg_adj],
    )
    assert [f.rule_id for f in run("A-EXIT-ADJUSTMENTS", bad)] == ["BR-WDR-01"]


# --- late fees ------------------------------------------------------------------------------------

POSTED = date(2026, 9, 29)  # due 09-14 + 14 + 1


def _late(amount, posted=POSTED, n=9):
    return line(
        n,
        "LATE_FEE",
        amount,
        fee_type="LATE_FEE",
        posted_date=posted,
        is_refundable=False,
        is_scholarship_eligible=False,
        allocation_priority=90,
    )


def _late_ds(*fees, charges="164000.00", events=()):
    return ds_with(
        invoices=[invoice()], lines=[line(1, net=charges), *fees], invoice_events=list(events)
    )


@pytest.mark.parametrize(
    ("fees", "charges", "problems"),
    [
        ((_late("3280.00"),), "164000.00", None),  # 2 % of 164,000: at the ceiling
        ((_late("500.00"),), "1000.00", None),  # the minimum, though 2 % is 20
        ((_late("5000.00"),), "900000.00", None),  # the maximum
        ((_late("5000.01"),), "900000.00", ["OUTSIDE_MIN_MAX", "ABOVE_2_PERCENT_OF_CHARGES"]),
        ((_late("499.99"),), "1000.00", ["OUTSIDE_MIN_MAX"]),
        ((_late("3280.01"),), "164000.00", ["ABOVE_2_PERCENT_OF_CHARGES"]),
        ((_late("600.00", posted=date(2026, 9, 28)),), "164000.00", ["POSTED_DATE"]),
    ],
)
def test_late_fee_bounds_and_date(fees, charges, problems):
    found = run("A-LATE-FEE", _late_ds(*fees, charges=charges))
    assert [f.details["problems"] for f in found] == ([problems] if problems else [])
    assert all((f.issue_type, f.rule_id) == ("ARITHMETIC_MISMATCH", "BR-LATE-01") for f in found)


def test_late_fee_twice_and_on_an_invoice_no_longer_issued():
    twice = run("A-LATE-FEE", _late_ds(_late("600.00", n=8), _late("600.00", n=9)))
    assert [(f.record_id, f.details["problems"]) for f in twice] == [
        (f"{INV}-L09", ["MORE_THAN_ONE"])
    ]
    cancelled = row(
        "EVT-INV-2026-0000003",
        invoice_id=INV,
        dimension="LIFECYCLE",
        to_status="CANCELLED",
        event_date=date(2026, 9, 20),
    )
    [f] = run("A-LATE-FEE", _late_ds(_late("600.00"), events=[cancelled]))
    assert f.details["problems"] == ["INVOICE_NOT_ISSUED"]


# --- scholarships ---------------------------------------------------------------------------------


def stype(pk, scope="ELIGIBLE_LINES_ONLY", group=None, **kw):
    fields = dict(
        scholarship_type_id=pk,
        coverage_scope=scope,
        exclusive_group=group,
        award_basis="PERCENT",
        award_value=D("50.00"),
        max_amount_per_term=D("100000.00"),
    )
    fields.update(kw)
    return row(pk, **fields)


def award(pk, t, amount="100.00", status="ACCEPTED", on=date(2026, 8, 17)):
    return row(
        pk,
        scholarship_id=pk,
        scholarship_type_id=t,
        student_id="STU-000001",
        term_id=FALL.pk,
        invoice_id=INV,
        awarded_amount=D(amount),
        status=status,
        award_date=on,
        eligibility_id=None,
    )


def test_full_ride_with_any_other_award_and_merit_groups():
    types = [
        stype("SCT-007", "ALL_FEE_LINES", "FULL_RIDE"),
        stype("SCT-001", group="MERIT"),
        stype("SCT-002", group="MERIT"),
        stype("SCT-003"),
    ]
    lines = [line(1, net="144000.00")]

    def found(*awards):
        ds = ds_with(
            scholarship_types=types, scholarships=list(awards), lines=lines, invoices=[invoice()]
        )
        return [(f.record_id, f.rule_id) for f in run("S-SCHOLARSHIP-GROUP", ds)]

    assert found(award("SCH-2026-000001", "SCT-001"), award("SCH-2026-000002", "SCT-003")) == []
    assert found(award("SCH-2026-000001", "SCT-001"), award("SCH-2026-000002", "SCT-002")) == [
        ("SCH-2026-000002", "BR-SCH-05")
    ]
    assert found(award("SCH-2026-000001", "SCT-007"), award("SCH-2026-000002", "SCT-003")) == [
        ("SCH-2026-000002", "BR-SCH-08")
    ]
    assert found(award("SCH-2026-000001", "SCT-003"), award("SCH-2026-000002", "SCT-007")) == [
        ("SCH-2026-000002", "BR-SCH-08")
    ]
    revoked = award("SCH-2026-000001", "SCT-001", status="REVOKED")
    assert found(revoked, award("SCH-2026-000002", "SCT-002")) == []


def test_scholarship_applied_to_a_line_its_type_cannot_cover():
    types = [stype("SCT-001")]
    tuition = line(1, net="144000.00")
    registration = line(3, net="5000.00", fee_type="REGISTRATION", is_scholarship_eligible=False)
    a = award("SCH-2026-000001", "SCT-001")
    apply_ok = row(
        "SCHA-SCH-2026-000001-01",
        scholarship_id=a.pk,
        invoice_line_id=tuition.pk,
        allocation_type="APPLY",
        amount=D("10.00"),
        allocated_on=date(2026, 8, 17),
    )
    apply_bad = row(
        "SCHA-SCH-2026-000001-02",
        scholarship_id=a.pk,
        invoice_line_id=registration.pk,
        allocation_type="APPLY",
        amount=D("10.00"),
        allocated_on=date(2026, 8, 17),
    )
    ds = ds_with(
        scholarship_types=types,
        scholarships=[a],
        lines=[tuition, registration],
        scholarship_allocations=[apply_ok, apply_bad],
        invoices=[invoice()],
    )
    assert [(f.issue_type, f.rule_id, f.record_id) for f in run("RL-SCHOLARSHIP-COVERAGE", ds)] == [
        ("RELATIONSHIP_MISMATCH", "BR-SCH-06", apply_bad.pk)
    ]


# --- refunds --------------------------------------------------------------------------------------


def _refund(**kw):
    fields = dict(
        refund_id="REF-2026-000001",
        payment_id="PAY-2026-000001",
        student_id="STU-000001",
        amount=D("100.00"),
        status="REQUESTED",
        requested_date=date(2026, 9, 2),
        processed_date=None,
        calculation={
            "net_paid": "500.00",
            "credit_available": "500.00",
            "payment_available": "500.00",
        },
    )
    fields.update(kw)
    return row(fields["refund_id"], **fields)


PAY = row(
    "PAY-2026-000001",
    payment_id="PAY-2026-000001",
    student_id="STU-000001",
    amount=D("500.00"),
    received_date=date(2026, 8, 25),
)


def test_refund_processed_before_it_was_requested():
    r = _refund(status="PROCESSED", processed_date=date(2026, 9, 1))
    found = run("T-REFUND-DATES", ds_with(refunds=[r], payments=[PAY]))
    assert [(f.issue_type, f.rule_id) for f in found] == [("DATE_ORDER", "BR-REF-03")]


@pytest.mark.parametrize(
    ("calculation", "rule", "problem"),
    [
        ({}, "BR-REF-02", "CALCULATION_INCOMPLETE"),
        (
            {"net_paid": "50.00", "credit_available": "500.00", "payment_available": "500.00"},
            "BR-REF-01",
            "ABOVE_RECORDED_CAP",
        ),
        (
            {"net_paid": "500.00", "credit_available": "500.00", "payment_available": "99.99"},
            "BR-REF-02",
            "ABOVE_RECORDED_CAP",
        ),
    ],
)
def test_refund_recorded_caps(calculation, rule, problem):
    found = run(
        "A-REFUND-RECORDED-CAPS",
        ds_with(refunds=[_refund(calculation=calculation)], payments=[PAY]),
    )
    assert [(f.rule_id, f.details["problem"]) for f in found] == [(rule, problem)]
    rejected = _refund(calculation=calculation, status="REJECTED")
    assert run("A-REFUND-RECORDED-CAPS", ds_with(refunds=[rejected], payments=[PAY])) == []


def test_every_new_check_runs_on_an_empty_dataset():
    from reconciliation.checks import service_rules

    names = {c.check_id for c in REGISTRY.values() if c.fn.__module__ == service_rules.__name__}
    assert len(names) == 23
    for check_id in names:
        assert REGISTRY[check_id].fn(Dataset(as_of=AS_OF)) == (0, [])


def test_student_in_an_inactive_program():
    prog = row("PRG-001", program_id="PRG-001", is_active=False)
    stu = row("STU-000001", student_id="STU-000001", program_id="PRG-001")
    found = run("RL-STUDENT-PROGRAM", ds_with(programs=[prog], students=[stu]))
    assert [(f.issue_type, f.rule_id, f.entity_type, f.record_id) for f in found] == [
        ("RELATIONSHIP_MISMATCH", "R-26", "Student", "STU-000001")
    ]


@pytest.mark.parametrize(
    ("reason", "status", "flagged"),
    [
        ("WITHDRAWAL", "WITHDRAWN", False),
        ("CANCELLATION", "CANCELLED", False),
        ("WITHDRAWAL", "ENROLLED", True),
        ("CANCELLATION", "WITHDRAWN", True),
    ],
)
def test_refund_reason_matches_its_enrollment(reason, status, flagged):
    r = _refund(reason=reason, enrollment_id="ENR-2026-000001")
    ds = ds_with(refunds=[r], enrollments=[enrollment(status=status)], payments=[PAY])
    found = run("RL-REFUND-ENROLLMENT", ds)
    assert [(f.issue_type, f.rule_id) for f in found] == (
        [("RELATIONSHIP_MISMATCH", "R-26")] if flagged else []
    )


# --- rules whose checks shipped in the first Phase 6 delta: one test per rule -------------------
STU2 = "STU-000002"


def _student(pk="STU-000001", **kw):
    fields = dict(
        student_id=pk,
        program_id="PRG-001",
        residency="DOMESTIC",
        admit_term_id=FALL.pk,
        status="ACTIVE",
    )
    fields.update(kw)
    return row(pk, **fields)


def _found(check_id, **tables):
    return [(f.issue_type, f.rule_id, f.record_id) for f in run(check_id, ds_with(**tables))]


def test_enrollment_program_is_the_students():
    assert _found(
        "RL-ENROLLMENT-PROGRAM",
        enrollments=[enrollment(program_id="PRG-002")],
        students=[_student()],
    ) == [("RELATIONSHIP_MISMATCH", "R-26", "ENR-2026-000001")]


def test_invoice_student_and_term_are_the_enrollments():
    assert _found(
        "RL-INVOICE-ENROLLMENT", invoices=[invoice(student_id=STU2)], enrollments=[enrollment()]
    ) == [("RELATIONSHIP_MISMATCH", "R-26", INV)]


def test_adjustment_targets_a_charge_on_its_own_invoice():
    other = line(1, invoice_id="INV-STU-000001-2026-000009")
    adj = line(2, "ADJUSTMENT", "-1.00", adjusts_line_id=other.pk, adjustment_reason="VOID")
    assert _found("RL-ADJUSTMENT-TARGET", lines=[other, adj], invoices=[invoice()]) == [
        ("RELATIONSHIP_MISMATCH", "BR-ALC-02", adj.pk)
    ]


def _pa(pk, payment, line_id, amount, kind="ALLOCATE", reverses=None, on=date(2026, 8, 25)):
    return row(
        pk,
        allocation_id=pk,
        payment_id=payment,
        invoice_line_id=line_id,
        amount=D(amount),
        allocation_type=kind,
        reverses_id=reverses,
        allocated_on=on,
    )


def test_payment_allocation_joins():
    l1 = line(1)
    adj = line(2, "ADJUSTMENT", "-1.00", adjusts_line_id=l1.pk, adjustment_reason="VOID")
    pay = row(
        "PAY-2026-000001",
        payment_id="PAY-2026-000001",
        student_id=STU2,
        amount=D("100.00"),
        received_date=date(2026, 8, 25),
    )
    a1 = _pa("ALLOC-PAY-2026-000001-01", pay.pk, l1.pk, "10.00")  # another student's line
    a2 = _pa("ALLOC-PAY-2026-000001-02", pay.pk, adj.pk, "10.00")  # not a charge line
    a3 = _pa("ALLOC-PAY-2026-000001-03", pay.pk, l1.pk, "-5.00", "DEALLOCATE", "missing")
    found = _found(
        "RL-PAYMENT-ALLOCATION",
        payments=[pay],
        lines=[l1, adj],
        payment_allocations=[a1, a2, a3],
        invoices=[invoice()],
    )
    assert sorted(found) == sorted(
        [
            ("RELATIONSHIP_MISMATCH", "R-26", a1.pk),
            ("RELATIONSHIP_MISMATCH", "R-26", a2.pk),
            ("RELATIONSHIP_MISMATCH", "R-26", a3.pk),
            ("RELATIONSHIP_MISMATCH", "BR-ALC-02", a2.pk),
            ("RELATIONSHIP_MISMATCH", "BR-ALC-05", a3.pk),
        ]
    )


def test_award_joins_invoice_and_eligibility():
    a = award("SCH-2026-000001", "SCT-001")
    a.eligibility_id = "ELG-2026-000001"
    elg = row(
        "ELG-2026-000001",
        student_id="STU-000001",
        scholarship_type_id="SCT-002",
        term_id=FALL.pk,
        is_eligible=True,
    )
    assert _found(
        "RL-AWARD", scholarships=[a], eligibilities=[elg], invoices=[invoice(student_id=STU2)]
    ) == [("RELATIONSHIP_MISMATCH", "R-26", a.pk), ("RELATIONSHIP_MISMATCH", "BR-SCH-02", a.pk)]


def test_scholarship_allocation_on_another_invoice():
    a = award("SCH-2026-000001", "SCT-001")
    other = line(1, invoice_id="INV-STU-000001-2026-000009")
    sa = row(
        "SCHA-SCH-2026-000001-01",
        scholarship_id=a.pk,
        invoice_line_id=other.pk,
        allocation_type="APPLY",
        amount=D("1.00"),
        reverses_id=None,
        allocated_on=date(2026, 8, 17),
    )
    assert _found(
        "RL-SCHOLARSHIP-ALLOCATION", scholarships=[a], lines=[other], scholarship_allocations=[sa]
    ) == [("RELATIONSHIP_MISMATCH", "R-26", sa.pk)]


def test_refund_belongs_to_its_payments_student():
    r = _refund(student_id=STU2, payer_type="SELF", enrollment_id=None)
    pay = row(
        PAY.pk,
        payment_id=PAY.pk,
        student_id="STU-000001",
        payer_type="SELF",
        amount=D("500.00"),
        received_date=date(2026, 8, 25),
    )
    assert _found("RL-REFUND", refunds=[r], payments=[pay]) == [
        ("RELATIONSHIP_MISMATCH", "R-26", r.pk)
    ]


def test_payment_before_the_first_invoice():
    early = row(
        "PAY-2026-000001",
        payment_id="PAY-2026-000001",
        student_id="STU-000001",
        amount=D("1.00"),
        received_date=date(2026, 8, 16),
    )
    assert _found("T-PAYMENT-FIRST-ISSUE", payments=[early], invoices=[invoice()]) == [
        ("DATE_ORDER", "R-39", early.pk)
    ]


def test_allocation_dated_before_its_payment_line_or_release_target():
    l1 = line(1)
    pay = row(
        PAY.pk,
        payment_id=PAY.pk,
        student_id="STU-000001",
        amount=D("500.00"),
        received_date=date(2026, 8, 25),
    )
    a1 = _pa("ALLOC-PAY-2026-000001-01", pay.pk, l1.pk, "10.00", on=date(2026, 8, 24))
    a2 = _pa("ALLOC-PAY-2026-000001-02", pay.pk, l1.pk, "10.00", on=date(2026, 8, 30))
    a3 = _pa(
        "ALLOC-PAY-2026-000001-03",
        pay.pk,
        l1.pk,
        "-1.00",
        "DEALLOCATE",
        a2.pk,
        on=date(2026, 8, 29),
    )
    found = _found(
        "T-ALLOCATION-DATES",
        payments=[pay],
        lines=[l1],
        payment_allocations=[a1, a2, a3],
        invoices=[invoice()],
    )
    assert sorted(found) == sorted(
        [("DATE_ORDER", "R-39", a1.pk), ("DATE_ORDER", "BR-ALC-05", a3.pk)]
    )


@pytest.mark.parametrize(
    ("fields", "rule"),
    [
        ({"status": "WITHDRAWN", "withdrawal_date": date(2026, 9, 6)}, "BR-WDR-02"),
        ({"status": "WITHDRAWN", "withdrawal_date": date(2026, 12, 20)}, "BR-WDR-02"),
        ({"status": "CANCELLED", "cancellation_date": date(2026, 9, 7)}, "BR-WDR-01"),
        ({"status": "CANCELLED", "cancellation_date": date(2026, 8, 9)}, "BR-WDR-01"),
    ],
)
def test_exit_dates_outside_their_window(fields, rule):
    assert _found("T-EXIT-DATES", enrollments=[enrollment(**fields)]) == [
        ("DATE_ORDER", rule, "ENR-2026-000001")
    ]


def _fs(version, eff, supersedes=None, **kw):
    pk = f"FEE-PRG-001-2026-FALL-LIBRARY-ALL-V{version}"
    fields = dict(
        program_id="PRG-001",
        term_id=FALL.pk,
        fee_type="LIBRARY",
        residency="ALL",
        version=version,
        effective_from=eff,
        supersedes_id=supersedes,
        amount=D("2000.00"),
        charge_basis="FLAT",
    )
    fields.update(kw)
    return row(pk, fee_schedule_id=pk, **fields)


def test_fee_version_chain():
    v1 = _fs(1, date(2026, 1, 1))
    ok = _fs(2, date(2026, 9, 1), v1.pk)
    assert _found("T-FEE-CHAIN", fee_schedules=[v1, ok]) == []
    not_later = _fs(2, date(2026, 1, 1), v1.pk)
    assert _found("T-FEE-CHAIN", fee_schedules=[v1, not_later]) == [
        ("DATE_ORDER", "BR-FEE-03", not_later.pk)
    ]
    skipped = _fs(3, date(2026, 9, 1), v1.pk)
    assert _found("T-FEE-CHAIN", fee_schedules=[v1, skipped]) == [
        ("DATE_ORDER", "BR-FEE-03", skipped.pk)
    ]


def _evt(n, frm, to, on, record="ENR-2026-000001"):
    return row(
        f"EVT-ENR-2026-{n:07d}", enrollment_id=record, from_status=frm, to_status=to, event_date=on
    )


def test_event_chains_dates_and_transitions():
    good = [
        _evt(1, None, "ENROLLED", date(2026, 8, 10)),
        _evt(2, "ENROLLED", "WITHDRAWN", date(2026, 9, 17)),
    ]
    enr = enrollment(status="WITHDRAWN", withdrawal_date=date(2026, 9, 17))
    assert _found("T-EVENT-ORDER", enrollment_events=good, enrollments=[enr]) == []
    assert _found("L-TRANSITIONS", enrollment_events=good, enrollments=[enr]) == []
    back = [good[0], _evt(2, "ENROLLED", "WITHDRAWN", date(2026, 8, 9))]
    assert _found("T-EVENT-ORDER", enrollment_events=back, enrollments=[enr]) == [
        ("DATE_ORDER", "R-39", back[1].pk)
    ]
    illegal = [
        good[0],
        _evt(2, "ENROLLED", "ENROLLED", date(2026, 9, 1)),
        _evt(3, "WITHDRAWN", "COMPLETED", date(2026, 12, 20)),
    ]
    assert _found("L-TRANSITIONS", enrollment_events=illegal, enrollments=[enr]) == [
        ("ILLEGAL_TRANSITION", "R-48", illegal[1].pk),
        ("ILLEGAL_TRANSITION", "R-48", illegal[2].pk),
    ]


def test_line_arithmetic():
    bad_gross = line(
        1, quantity=D("16"), unit_amount=D("9000.00"), gross_amount=D("1.00"), net_amount=D("1.00")
    )
    bad_net = line(
        2,
        gross_amount=D("1000.00"),
        waiver_amount=D("100.00"),
        net_amount=D("950.00"),
        is_waivable=True,
    )
    found = _found("A-LINE-AMOUNTS", lines=[bad_gross, bad_net], invoices=[invoice()])
    assert sorted(found) == sorted(
        [
            ("ARITHMETIC_MISMATCH", "BR-FEE-02", bad_gross.pk),
            ("ARITHMETIC_MISMATCH", "BR-WAV-01", bad_net.pk),
        ]
    )


def test_fee_line_priced_from_its_schedule():
    fs = _fs(
        1, date(2026, 1, 1), fee_type="TUITION", charge_basis="PER_CREDIT", amount=D("9000.00")
    )
    good = line(1, fee_schedule_id=fs.pk, quantity=D("16"), unit_amount=D("9000.00"))
    wrong_unit = line(2, fee_schedule_id=fs.pk, quantity=D("16"), unit_amount=D("9001.00"))
    wrong_qty = line(3, fee_schedule_id=fs.pk, quantity=D("15"), unit_amount=D("9000.00"))
    late_fs = _fs(
        2, date(2026, 9, 1), fee_type="TUITION", charge_basis="PER_CREDIT", amount=D("9000.00")
    )
    not_in_force = line(4, fee_schedule_id=late_fs.pk, quantity=D("16"), unit_amount=D("9000.00"))
    found = _found(
        "A-FEE-PRICE",
        fee_schedules=[fs, late_fs],
        lines=[good, wrong_unit, wrong_qty, not_in_force],
        invoices=[invoice()],
        enrollments=[enrollment()],
    )
    assert sorted(found) == sorted(
        [
            ("ARITHMETIC_MISMATCH", "BR-FEE-01", wrong_unit.pk),
            ("ARITHMETIC_MISMATCH", "BR-FEE-02", wrong_qty.pk),
            ("ARITHMETIC_MISMATCH", "BR-FEE-01", not_in_force.pk),
        ]
    )


def test_line_open_and_adjustment_bound():
    l1 = line(1, net="100.00")
    over_adjusted = line(
        2, "ADJUSTMENT", "-150.00", adjusts_line_id=l1.pk, adjustment_reason="VOID"
    )
    assert _found("A-LINE-OPEN", lines=[l1, over_adjusted], invoices=[invoice()]) == [
        ("ARITHMETIC_MISMATCH", "BR-ALC-02", l1.pk)
    ]
    pay = _pa("ALLOC-PAY-2026-000001-01", "PAY-2026-000001", l1.pk, "100.01")
    assert _found("A-LINE-OPEN", lines=[l1], payment_allocations=[pay], invoices=[invoice()]) == [
        ("ARITHMETIC_MISMATCH", "BR-ALC-02", l1.pk)
    ]
    found = run(
        "A-INVOICE-AMOUNTS",
        ds_with(
            lines=[l1],
            payment_allocations=[SimpleNamespace(**{**vars(pay), "payer_type": "SELF"})],
            invoices=[invoice()],
        ),
    )
    assert [(f.issue_type, f.rule_id, f.record_id) for f in found] == [
        ("ARITHMETIC_MISMATCH", "BR-INV-04", INV)
    ]


def test_releases_never_exceed_what_they_release():
    l1 = line(1)
    pay = row(
        PAY.pk,
        payment_id=PAY.pk,
        student_id="STU-000001",
        amount=D("500.00"),
        received_date=date(2026, 8, 25),
    )
    a1 = _pa("ALLOC-PAY-2026-000001-01", pay.pk, l1.pk, "10.00")
    r1 = _pa("ALLOC-PAY-2026-000001-02", pay.pk, l1.pk, "-6.00", "DEALLOCATE", a1.pk)
    r2 = _pa("ALLOC-PAY-2026-000001-03", pay.pk, l1.pk, "-5.00", "DEALLOCATE", a1.pk)
    assert _found("A-RELEASE-BOUNDS", payments=[pay], payment_allocations=[a1, r1, r2]) == [
        ("ARITHMETIC_MISMATCH", "BR-ALC-05", a1.pk)
    ]


def test_scholarship_reversals_bound_and_target():
    a = award("SCH-2026-000001", "SCT-001")
    l1 = line(1)

    def sa(n, kind, amount, reverses=None):
        return row(
            f"SCHA-SCH-2026-000001-{n:02d}",
            scholarship_id=a.pk,
            invoice_line_id=l1.pk,
            allocation_type=kind,
            amount=D(amount),
            reverses_id=reverses,
            allocated_on=date(2026, 9, 17),
        )

    apply_ = sa(1, "APPLY", "100.00")
    over = [apply_, sa(2, "REVERSE", "-60.00", apply_.pk), sa(3, "REVERSE", "-50.00", apply_.pk)]
    assert _found("A-RELEASE-BOUNDS", scholarships=[a], scholarship_allocations=over) == [
        ("ARITHMETIC_MISMATCH", "BR-SCH-07", apply_.pk)
    ]
    stray = sa(4, "REVERSE", "-1.00", "SCHA-SCH-2026-000009-01")
    found = _found(
        "RL-SCHOLARSHIP-ALLOCATION",
        scholarships=[a],
        lines=[l1],
        scholarship_allocations=[apply_, stray],
    )
    assert found == [("RELATIONSHIP_MISMATCH", "BR-SCH-07", stray.pk)]
