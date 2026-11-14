"""Pure reconciliation calculators and checks (no database): D-R4 refund replay, D-R5
scholarship reservations, snapshot totals, the two issue types the database cannot store
(DUPLICATE_RECORD, INVALID_ENUM), the D-R9 / action tables, the run lifecycle, and
deterministic ordering."""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from reconciliation import calculators as calc
from reconciliation import engine, policy
from reconciliation.checks import REGISTRY
from reconciliation.dataset import Dataset
from reconciliation.lifecycle import RUN_TRANSITIONS, run_transition_allowed

AS_OF = date(2026, 12, 31)


def row(pk, **kw):
    return SimpleNamespace(pk=pk, **kw)


def payment(pk, amount, student="STU-000001", on=date(2026, 8, 20), **kw):
    return row(
        pk,
        payment_id=pk,
        student_id=student,
        amount=D(amount),
        received_date=on,
        payer_type="SELF",
        external_reference=kw.pop("ref", f"R-{pk}"),
        method=kw.pop("method", "UPI"),
        status="POSTED",
        currency="INR",
        **kw,
    )


def refund(
    pk, amount, payment_id, *, status="PROCESSED", on=date(2026, 9, 1), student="STU-000001"
):
    return row(
        pk,
        refund_id=pk,
        payment_id=payment_id,
        student_id=student,
        amount=D(amount),
        status=status,
        requested_date=on,
        processed_date=on if status == "PROCESSED" else None,
        payer_type="SELF",
        enrollment_id=None,
        reason="OVERPAYMENT",
    )


def alloc(pk, payment_id, amount, line="INV-STU-000001-2026-000001-L01", on=date(2026, 8, 21)):
    return row(
        pk,
        allocation_id=pk,
        payment_id=payment_id,
        invoice_line_id=line,
        amount=D(amount),
        allocation_type="ALLOCATE" if D(amount) > 0 else "DEALLOCATE",
        reverses_id=None,
        allocated_on=on,
        rule_applied="MANUAL",
        payer_type="SELF",
    )


# --- D-R4: BR-REF-01 replayed with payments.calculators.net_paid ----------------------------------


def test_refunds_within_net_paid_across_several_payments_are_clean():
    pays = [payment("PAY-2026-000001", "1000.00"), payment("PAY-2026-000002", "500.00")]
    refs = [
        refund("REF-2026-000001", "900.00", "PAY-2026-000001"),
        refund("REF-2026-000002", "600.00", "PAY-2026-000002", status="REQUESTED"),
    ]
    assert calc.refund_net_paid_breaches(pays, refs) == []  # 1,500 ≤ 1,500: the boundary


def test_the_first_refund_over_net_paid_and_every_later_one_breach():
    pays = [payment("PAY-2026-000001", "1000.00"), payment("PAY-2026-000002", "500.00")]
    refs = [
        refund("REF-2026-000003", "100.00", "PAY-2026-000002", on=date(2026, 9, 5)),
        refund("REF-2026-000001", "1400.00", "PAY-2026-000001", on=date(2026, 9, 1)),
        refund("REF-2026-000002", "0.01", "PAY-2026-000001", on=date(2026, 9, 1)),
    ]
    breaches = calc.refund_net_paid_breaches(pays, refs)
    # request order: REF-1 (1,400 ≤ 1,500), REF-2 (0.01 ≤ 100), REF-3 (100 > 99.99)
    assert [(b.record_id, b.expected, b.actual) for b in breaches] == [
        ("REF-2026-000003", D("99.99"), D("100.00"))
    ]
    assert breaches[0].details["earlier_refunds_not_rejected"] == [
        "REF-2026-000001",
        "REF-2026-000002",
    ]


def test_rejected_refunds_reserve_nothing_and_are_never_flagged():
    pays = [payment("PAY-2026-000001", "1000.00")]
    refs = [
        refund("REF-2026-000001", "5000.00", "PAY-2026-000001", status="REJECTED"),
        refund("REF-2026-000002", "1000.00", "PAY-2026-000001", status="APPROVED"),
    ]
    assert calc.refund_net_paid_breaches(pays, refs) == []


def _payment_ds(pays, allocs=(), refs=()):
    return Dataset(
        as_of=AS_OF, payments=list(pays), payment_allocations=list(allocs), refunds=list(refs)
    )


def test_a_refund_is_not_justified_by_an_unrelated_payment():
    """D-R4: student-level net paid covers the refund, but the source payment (fully
    allocated) has nothing left: BR-ALC-01 flags that payment (PAYMENT_OVER_ALLOCATED)."""
    pays = [payment("PAY-2026-000001", "1000.00"), payment("PAY-2026-000002", "5000.00")]
    allocs = [alloc("ALLOC-PAY-2026-000001-01", "PAY-2026-000001", "1000.00")]
    refs = [refund("REF-2026-000001", "800.00", "PAY-2026-000001")]
    ds = _payment_ds(pays, allocs, refs)
    assert REGISTRY["Q-REFUND-EXCEEDS-NET-PAID"].fn(ds)[1] == []
    _, found = REGISTRY["Q-PAYMENT-OVER-ALLOCATED"].fn(ds)
    assert [(f.record_id, f.expected, f.actual) for f in found] == [
        ("PAY-2026-000001", D("200.00"), D("1000.00"))
    ]


def test_payment_exactly_used_is_clean_and_one_cent_over_is_not():
    pays = [payment("PAY-2026-000001", "1000.00")]
    exact = [alloc("ALLOC-PAY-2026-000001-01", "PAY-2026-000001", "600.00")]
    refs = [refund("REF-2026-000001", "400.00", "PAY-2026-000001", status="REQUESTED")]
    assert REGISTRY["Q-PAYMENT-OVER-ALLOCATED"].fn(_payment_ds(pays, exact, refs))[1] == []
    over = [*exact, alloc("ALLOC-PAY-2026-000001-02", "PAY-2026-000001", "0.01")]
    found = REGISTRY["Q-PAYMENT-OVER-ALLOCATED"].fn(_payment_ds(pays, over, refs))[1]
    assert [f.details["over_by"] for f in found] == ["0.01"]


def test_rows_after_the_as_of_date_are_ignored():
    pays = [payment("PAY-2026-000001", "1000.00")]
    late = [alloc("ALLOC-PAY-2026-000001-01", "PAY-2026-000001", "1500.00", on=date(2026, 12, 1))]
    ds = Dataset(as_of=date(2026, 11, 30), payments=pays, payment_allocations=late)
    assert REGISTRY["Q-PAYMENT-OVER-ALLOCATED"].fn(ds)[1] == []


# --- D-R5: Phase 3 eligible base and reservations -------------------------------------------------

ELIGIBLE_ONLY = row("SCT-001", scholarship_type_id="SCT-001", coverage_scope="ELIGIBLE_LINES_ONLY")
FULL_RIDE = row("SCT-007", scholarship_type_id="SCT-007", coverage_scope="ALL_FEE_LINES")
NEED = row("SCT-003", scholarship_type_id="SCT-003", coverage_scope="ELIGIBLE_LINES_ONLY")
TYPES = {"SCT-001": ELIGIBLE_ONLY, "SCT-003": NEED, "SCT-007": FULL_RIDE}


def fee(n, amount, eligible):
    return row(
        f"INV-L{n:02d}",
        line_id=f"INV-L{n:02d}",
        line_type="FEE",
        net_amount=D(amount),
        is_scholarship_eligible=eligible,
        allocation_priority=n,
    )


LINES = [fee(1, "144000.00", True), fee(2, "6000.00", True), fee(3, "5000.00", False)]


def award(pk, stype, amount, status="OFFERED", on=date(2026, 8, 17)):
    return row(
        pk,
        scholarship_id=pk,
        scholarship_type_id=stype,
        awarded_amount=D(amount),
        status=status,
        award_date=on,
    )


def sa(amount):
    return row("SCHA", amount=D(amount))


def test_several_awards_within_the_base_are_clean():
    awards = [
        (award("SCH-2026-000001", "SCT-001", "75000.00", "APPLIED"), [sa("75000.00")]),
        (award("SCH-2026-000002", "SCT-003", "75000.00"), []),  # 150,000 = base: boundary
    ]
    assert calc.scholarship_reservation_breaches(awards, TYPES, LINES) == []


def test_applied_awards_reserve_only_what_is_applied_net_of_reversals():
    awards = [
        (
            award("SCH-2026-000001", "SCT-001", "100000.00", "PARTIALLY_REVERSED"),
            [sa("100000.00"), sa("-40000.00")],
        ),
        (award("SCH-2026-000002", "SCT-003", "90000.00"), []),  # 60,000 + 90,000 = 150,000
    ]
    assert calc.scholarship_reservation_breaches(awards, TYPES, LINES) == []


def test_reservations_over_the_base_flag_the_award_that_crosses_it_only():
    awards = [
        (award("SCH-2026-000002", "SCT-003", "80000.00", on=date(2026, 8, 20)), []),
        (award("SCH-2026-000001", "SCT-001", "75000.00", "APPLIED"), [sa("75000.00")]),
        (award("SCH-2026-000003", "SCT-003", "1.00", "REVOKED"), []),
    ]
    breaches = calc.scholarship_reservation_breaches(awards, TYPES, LINES)
    assert [(b.record_id, b.details["problem"], b.actual) for b in breaches] == [
        ("SCH-2026-000002", "RESERVATIONS_ABOVE_BASE", D("155000.00"))
    ]


def test_full_ride_base_is_every_fee_line_and_never_late_fees():
    late = row(
        "INV-L04",
        line_id="INV-L04",
        line_type="LATE_FEE",
        net_amount=D("500.00"),
        is_scholarship_eligible=False,
        allocation_priority=90,
    )
    ok = [(award("SCH-2026-000001", "SCT-007", "155000.00"), [])]
    assert calc.scholarship_reservation_breaches(ok, TYPES, [*LINES, late]) == []
    over = [(award("SCH-2026-000001", "SCT-007", "155000.01"), [])]
    b = calc.scholarship_reservation_breaches(over, TYPES, [*LINES, late])
    assert [(x.expected, x.details["problem"]) for x in b] == [(D("155000.00"), "AWARD_ABOVE_BASE")]


def test_awards_on_different_invoices_are_checked_separately():
    def ds_for(awards_by_invoice):
        invoices = [row(i, invoice_id=i, student_id="STU-000001") for i in awards_by_invoice]
        lines = [
            SimpleNamespace(
                **{**vars(ln), "pk": f"{i}-{ln.pk}", "line_id": f"{i}-{ln.pk}", "invoice_id": i}
            )
            for i in awards_by_invoice
            for ln in LINES
        ]
        awards_ = [
            SimpleNamespace(**{**vars(a), "invoice_id": i, "student_id": "STU-000001"})
            for i, rows_ in awards_by_invoice.items()
            for a in rows_
        ]
        return Dataset(
            as_of=AS_OF,
            invoices=invoices,
            lines=lines,
            scholarships=awards_,
            scholarship_types=list(TYPES.values()),
        )

    a1 = award("SCH-2026-000001", "SCT-001", "100000.00")
    a2 = award("SCH-2026-000002", "SCT-003", "100000.00")
    separate = ds_for({"INV-A": [a1], "INV-B": [a2]})
    assert REGISTRY["Q-SCHOLARSHIP-EXCEEDS-FEES"].fn(separate)[1] == []
    same = ds_for({"INV-A": [a1, a2]})
    found = REGISTRY["Q-SCHOLARSHIP-EXCEEDS-FEES"].fn(same)[1]
    assert [(f.record_id, f.rule_id) for f in found] == [("SCH-2026-000002", "BR-SCH-04")]


# --- BR-BAL-01 / snapshot totals ------------------------------------------------------------------


def entry(amount, on):
    return row("LED", amount=D(amount), entry_date=on)


def test_snapshot_totals_split_by_sign_up_to_the_date():
    entries = [
        entry("100.00", date(2026, 8, 1)),
        entry("-30.00", date(2026, 8, 2)),
        entry("-50.00", date(2026, 8, 2)),
        entry("999.00", date(2026, 8, 3)),
    ]
    t = calc.snapshot_totals(entries, date(2026, 8, 2))
    assert (t.balance, t.debits, t.credits) == (D("20.00"), D("100.00"), D("-80.00"))
    assert t.balance == calc.balance_from_ledger(entries, date(2026, 8, 2))  # BR-BAL-01


# --- DUPLICATE_RECORD and INVALID_ENUM (unstorable in the database) -------------------------------


def _engine_findings(ds, check_id):
    return [rf for rf in engine.run_checks(ds).findings if rf.check_id == check_id]


def test_duplicate_record_contract():
    pays = [
        payment("PAY-2026-000001", "10.00", ref="UTR-1"),
        payment("PAY-2026-000002", "10.00", ref="UTR-1"),
    ]
    found = _engine_findings(_payment_ds(pays), "S-DUP-DOMAIN")
    assert [
        (
            rf.finding.issue_type,
            rf.finding.rule_id,
            rf.severity,
            rf.recommended_action,
            rf.finding.entity_type,
            rf.finding.record_id,
            rf.finding.student_id,
        )
        for rf in found
    ] == [
        (
            "DUPLICATE_RECORD",
            "R-39",
            "HIGH",
            "INVESTIGATE",
            "Payment",
            "PAY-2026-000002",
            "STU-000001",
        )
    ]
    assert found[0].finding.details["first_record_id"] == "PAY-2026-000001"


def test_invalid_enum_contract():
    ds = _payment_ds([payment("PAY-2026-000001", "10.00", method="BITCOIN")])
    ds.enum_fields = {"Payment": {"method": (frozenset({"UPI", "CARD"}), False)}}
    found = _engine_findings(ds, "S-ENUM-DOMAIN")
    assert [
        (
            rf.finding.issue_type,
            rf.finding.rule_id,
            rf.severity,
            rf.recommended_action,
            rf.finding.record_id,
            rf.finding.details,
        )
        for rf in found
    ] == [
        (
            "INVALID_ENUM",
            "R-39",
            "MEDIUM",
            "INVESTIGATE",
            "PAY-2026-000001",
            {"field": "method", "value": "BITCOIN"},
        )
    ]


def test_invalid_id_pattern():
    ds = _payment_ds([payment("PAY-26-1", "10.00")])
    found = REGISTRY["S-ID-DOMAIN"].fn(ds)[1]
    assert [(f.issue_type, f.record_id, f.details["problem"]) for f in found] == [
        ("INVALID_ID_FORMAT", "PAY-26-1", "PATTERN")
    ]


# --- policy tables, run lifecycle, ordering -------------------------------------------------------


def test_every_issue_type_has_a_frozen_severity_and_action():
    assert set(policy.ISSUE_TYPES) == set(policy.SEVERITY) == set(policy.ACTION)
    assert set(policy.SEVERITY.values()) <= set(policy.SEVERITIES)
    assert set(policy.ACTION.values()) <= set(policy.RECOMMENDED_ACTIONS)
    assert len(policy.ISSUE_TYPES) == 14 and len(policy.REQUIRED_ISSUE_TYPES) == 5
    assert {policy.ACTION[t] for t in policy.REQUIRED_ISSUE_TYPES} == set(
        policy.RECOMMENDED_ACTIONS[:5]
    )


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        (None, "RUNNING", True),
        ("RUNNING", "COMPLETED", True),
        ("RUNNING", "FAILED", True),
        (None, "COMPLETED", False),
        ("COMPLETED", "FAILED", False),
        ("FAILED", "RUNNING", False),
        ("COMPLETED", "RUNNING", False),
    ],
)
def test_run_lifecycle(current, target, ok):
    assert run_transition_allowed(current, target) is ok
    assert RUN_TRANSITIONS["COMPLETED"] == RUN_TRANSITIONS["FAILED"] == frozenset()


def test_every_check_cites_a_short_rule_and_has_a_family():
    for c in REGISTRY.values():
        assert len(c.rule_id) <= 12 and (c.rule_id.startswith(("BR-", "R-")))
        assert c.description


def test_findings_are_ordered_deterministically_whatever_the_row_order():
    pays = [payment(f"PAY-2026-00000{i}", "10.00", ref="SAME") for i in (3, 1, 2)]
    a = [
        (rf.check_id, rf.finding.issue_type, rf.finding.record_id)
        for rf in engine.run_checks(_payment_ds(pays)).findings
    ]
    b = [
        (rf.check_id, rf.finding.issue_type, rf.finding.record_id)
        for rf in engine.run_checks(_payment_ds(reversed(pays))).findings
    ]
    assert a == b and a
