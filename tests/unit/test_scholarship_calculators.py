"""Pure scholarship calculators added or pinned in Phase 3 (no database).

evaluate (BR-SCH-01): every criterion and boundary, every failing reason, fixed order.
award_amount (BR-SCH-03, D-S5): percent rounding, fixed, cumulative per-term cap.
plan_application (BR-SCH-06/08, BR-ALC-02/06, D-S6, D-S8): priority order, line room with
adjustments and existing scholarship, payment release, all-or-nothing.
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from scholarships import calculators as calc

FALL = NS(term_id="TERM-2026-FALL", invoice_date=date(2026, 8, 17))


def stype(**kw):
    base = dict(
        scholarship_type_id="SCT-900",
        min_gpa=None,
        max_gpa=None,
        min_need_index=None,
        residency="ALL",
        first_term_only=False,
        program_restricted=False,
        program_ids=frozenset(),
        coverage_scope="ELIGIBLE_LINES_ONLY",
        award_basis="PERCENT",
        award_value=D("50"),
        max_amount_per_term=D("100000.00"),
        effective_from=date(2026, 1, 1),
        effective_to=None,
    )
    base.update(kw)
    return NS(**base)


def student(
    gpa="3.72", need="0.35", residency="DOMESTIC", program="PRG-001", admit="TERM-2026-SPRING"
):
    return NS(
        gpa=D(gpa), need_index=D(need), residency=residency, program_id=program, admit_term_id=admit
    )


# --- evaluate --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gpa", "min_gpa", "max_gpa", "codes"),
    [
        ("3.70", "3.70", None, ["ALL_CRITERIA_MET"]),  # minimum is inclusive
        ("3.69", "3.70", None, ["GPA_BELOW_MIN"]),
        ("3.69", "3.30", "3.70", ["ALL_CRITERIA_MET"]),
        ("3.70", "3.30", "3.70", ["GPA_AT_OR_ABOVE_MAX"]),  # maximum is exclusive (tiered merit)
        ("3.30", "3.30", "3.70", ["ALL_CRITERIA_MET"]),
        ("3.29", "3.30", "3.70", ["GPA_BELOW_MIN"]),
    ],
)
def test_gpa_bounds(gpa, min_gpa, max_gpa, codes):
    t = stype(min_gpa=D(min_gpa), max_gpa=D(max_gpa) if max_gpa else None)
    r = calc.evaluate(student(gpa=gpa), FALL, t)
    assert list(r.reason_codes) == codes and r.is_eligible is (codes == ["ALL_CRITERIA_MET"])


@pytest.mark.parametrize(("need", "ok"), [("0.85", True), ("0.84", False), ("0.86", True)])
def test_need_minimum_is_inclusive(need, ok):
    r = calc.evaluate(student(need=need), FALL, stype(min_need_index=D("0.85")))
    assert r.is_eligible is ok and (ok or r.reason_codes == ("NEED_BELOW_MIN",))


def test_full_ride_needs_both_gpa_and_need():
    t = stype(min_gpa=D("3.85"), min_need_index=D("0.85"), coverage_scope="ALL_FEE_LINES")
    assert calc.evaluate(student(gpa="3.85", need="0.85"), FALL, t).is_eligible
    r = calc.evaluate(student(gpa="3.84", need="0.84"), FALL, t)
    assert r.reason_codes == ("GPA_BELOW_MIN", "NEED_BELOW_MIN")


def test_residency_first_term_program_and_window_with_every_reason_in_order():
    t = stype(
        min_gpa=D("3.90"),
        min_need_index=D("0.90"),
        residency="INTERNATIONAL",
        first_term_only=True,
        program_restricted=True,
        program_ids=frozenset({"PRG-002"}),
        effective_from=date(2026, 9, 1),
    )
    r = calc.evaluate(student(), FALL, t)
    assert r.reason_codes == (
        "GPA_BELOW_MIN",
        "NEED_BELOW_MIN",
        "RESIDENCY_MISMATCH",
        "NOT_FIRST_TERM",
        "PROGRAM_NOT_ELIGIBLE",
        "TYPE_NOT_IN_EFFECT",
    )
    assert r.is_eligible is False


def test_matching_residency_first_term_and_program():
    t = stype(
        residency="DOMESTIC",
        first_term_only=True,
        program_restricted=True,
        program_ids=frozenset({"PRG-001"}),
    )
    assert calc.evaluate(student(admit="TERM-2026-FALL"), FALL, t).is_eligible


def test_D_S7_window_is_checked_against_the_term_invoice_date():
    t = stype(effective_from=date(2026, 1, 1), effective_to=date(2026, 8, 16))
    assert calc.evaluate(student(), FALL, t).reason_codes == ("TYPE_NOT_IN_EFFECT",)
    t = stype(effective_from=date(2026, 8, 17), effective_to=date(2026, 8, 17))
    assert calc.evaluate(student(), FALL, t).is_eligible


def test_snapshot_records_the_inputs_used():
    r = calc.evaluate(student(), FALL, stype())
    assert r.inputs_snapshot == {
        "gpa": "3.72",
        "need_index": "0.35",
        "residency": "DOMESTIC",
        "program": "PRG-001",
        "admit_term": "TERM-2026-SPRING",
        "term": "TERM-2026-FALL",
        "first_term": False,
    }


def test_evaluate_is_deterministic_and_does_not_modify_inputs():
    s, t = student(), stype(min_gpa=D("3.00"))
    before = (vars(s).copy(), vars(t).copy())
    assert calc.evaluate(s, FALL, t) == calc.evaluate(s, FALL, t)
    assert (vars(s), vars(t)) == before


# --- award_amount (BR-SCH-03, D-S5) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("basis", "value", "cap", "base", "already", "expected"),
    [
        ("PERCENT", "50", "100000.00", "172000.00", "0", "86000.00"),
        ("PERCENT", "50", "100000.00", "250000.00", "0", "100000.00"),  # cap binds
        ("PERCENT", "25", "50000.00", "33333.33", "0", "8333.33"),  # 8333.3325 → half-up
        ("PERCENT", "15", "75000.00", "10000.10", "0", "1500.02"),  # 1500.015 → 1500.02
        ("FIXED", "40000.00", "40000.00", "150000.00", "0", "40000.00"),
        ("FIXED", "40000.00", "40000.00", "10.00", "0", "40000.00"),  # BR-SCH-04 is separate
        ("FIXED", "40000.00", "40000.00", "150000.00", "15000.00", "25000.00"),  # cumulative
        ("FIXED", "40000.00", "40000.00", "150000.00", "40000.00", "0.00"),
        ("PERCENT", "50", "100000.00", "172000.00", "90000.00", "10000.00"),
        ("PERCENT", "50", "100000.00", "172000.00", "120000.00", "0.00"),  # never negative
    ],
)
def test_award_amount(basis, value, cap, base, already, expected):
    t = stype(award_basis=basis, award_value=D(value), max_amount_per_term=D(cap))
    assert calc.award_amount(t, D(base), D(already)) == D(expected)


def test_award_amount_rejects_a_negative_base_and_unknown_basis():
    with pytest.raises(ValueError):
        calc.award_amount(stype(), D("-1"), D("0"))
    with pytest.raises(ValueError):
        calc.award_amount(stype(award_basis="TIERED"), D("1"), D("0"))


def test_coverable_lines_rejects_unknown_scope():
    with pytest.raises(ValueError):
        calc.coverable_lines(stype(coverage_scope="SOME_LINES"), [])


# --- plan_application ---------------------------------------------------------------------------

INV = "INV-STU-000001-2026-000001"


def line(n, fee_type, net, eligible, priority, line_type="FEE"):
    return NS(
        line_id=f"{INV}-L{n:02d}",
        line_type=line_type,
        fee_type=fee_type,
        net_amount=D(net),
        is_scholarship_eligible=eligible,
        allocation_priority=priority,
    )


LINES = [
    line(1, "TUITION", "144000.00", True, 10),
    line(2, "LAB", "6000.00", True, 20),
    line(3, "REGISTRATION", "5000.00", False, 30),
    line(4, "STUDENT_ACTIVITY", "0.00", False, 60),  # fully waived
    line(5, "LATE_FEE", "3280.00", False, 90, "LATE_FEE"),
]
L1, L2, L3 = (f"{INV}-L0{n}" for n in (1, 2, 3))
MERIT = stype()
FULL = stype(coverage_scope="ALL_FEE_LINES", award_value=D("100"), max_amount_per_term=D("600000"))


def plan(t, amount, **kw):
    return [
        (p.line_id, p.amount, p.payment_to_release)
        for p in calc.plan_application(t, LINES, D(amount), **kw)
    ]


def test_fills_lines_in_priority_order():
    assert plan(MERIT, "145000.00") == [
        (L1, D("144000.00"), D("0.00")),
        (L2, D("1000.00"), D("0.00")),
    ]


def test_full_ride_covers_every_fee_line_but_never_late_fees_and_skips_zero_room():
    assert plan(FULL, "155000.00") == [
        (L1, D("144000.00"), D("0.00")),
        (L2, D("6000.00"), D("0.00")),
        (L3, D("5000.00"), D("0.00")),
    ]


def test_D_S8_all_or_nothing_when_room_is_short():
    with pytest.raises(calc.InsufficientRoom) as exc:
        calc.plan_application(MERIT, LINES, D("150000.01"))
    assert exc.value.room == D("150000.00")


def test_D_S6_room_includes_adjustments_and_existing_scholarship():
    kw = dict(adjustments={L1: D("-140000.00")}, scholarship_allocated={L2: D("5000.00")})
    assert plan(MERIT, "5000.00", **kw) == [
        (L1, D("4000.00"), D("0.00")),
        (L2, D("1000.00"), D("0.00")),
    ]
    with pytest.raises(calc.InsufficientRoom) as exc:
        calc.plan_application(MERIT, LINES, D("5000.01"), **kw)
    assert exc.value.room == D("5000.00")


def test_BR_ALC_06_payments_do_not_reduce_room_but_must_be_released():
    got = plan(MERIT, "75000.00", payment_allocated={L1: D("100000.00")})
    # 100,000 paid + 75,000 scholarship on a 144,000 line → 31,000 of payment must be freed
    assert got == [(L1, D("75000.00"), D("31000.00"))]


def test_plan_rejects_non_positive_amounts():
    with pytest.raises(ValueError):
        calc.plan_application(MERIT, LINES, D("0"))


def test_net_by_line_sums_signed_amounts():
    rows = [
        NS(invoice_line_id=L1, amount=D("100")),
        NS(invoice_line_id=L1, amount=D("-40")),
        NS(invoice_line_id=L2, amount=D("5")),
    ]
    assert calc.net_by_line(rows) == {L1: D("60.00"), L2: D("5.00")}
