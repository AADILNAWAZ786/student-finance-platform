"""Pure billing calculators added in Phase 2 (no database).

price_lines(enrollment, fee_rows)      frozen two-argument contract (D4), BR-FEE-02/04
apply_waivers(lines, waivers)          BR-WAV-01, separate from price_lines (D4)
late_fee(open, due, as_of, policy)     BR-LATE-01
ledger_balance(entries, as_of)         BR-BAL-01, the single implementation (D13)
line_open_amounts / void_adjustments   BR-ALC-02 / BR-INV-08
"""

import inspect
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from billing import calculators as calc
from billing.policy import LateFeePolicy

ENR = NS(credits=16, program=NS(has_lab=True), student=NS(residency="DOMESTIC"))


def row(fee_type, residency="ALL", basis="FLAT", amount="1000.00", n=1):
    return NS(
        fee_schedule_id=f"FEE-{fee_type}-{residency}-V{n}",
        fee_type=fee_type,
        residency=residency,
        charge_basis=basis,
        amount=D(amount),
    )


ROWS = [
    row("STUDENT_ACTIVITY", amount="3000.00"),
    row("TUITION", "DOMESTIC", "PER_CREDIT", "9000.00"),
    row("LAB", amount="6000.00"),
    row("REGISTRATION", amount="5000.00"),
    row("INTERNATIONAL_SERVICE", "INTERNATIONAL", amount="8000.00"),
]


def test_D4_price_lines_keeps_the_frozen_two_argument_signature():
    assert list(inspect.signature(calc.price_lines).parameters) == ["enrollment", "fee_rows"]


def test_price_lines_quantities_gross_order_and_flags():
    lines = calc.price_lines(ENR, ROWS)
    assert [(x.fee_type, x.quantity, x.gross_amount, x.allocation_priority) for x in lines] == [
        ("TUITION", D("16"), D("144000.00"), 10),
        ("LAB", D("1"), D("6000.00"), 20),
        ("REGISTRATION", D("1"), D("5000.00"), 30),
        ("STUDENT_ACTIVITY", D("1"), D("3000.00"), 60),
    ]
    assert all(x.waiver_amount == 0 and x.net_amount == x.gross_amount for x in lines)
    assert [x.is_waivable for x in lines] == [False, False, False, True]


def test_BR_FEE_04_lab_and_international_service_only_when_applicable():
    no_lab = NS(credits=12, program=NS(has_lab=False), student=NS(residency="INTERNATIONAL"))
    lines = calc.price_lines(
        no_lab, [row("TUITION", "INTERNATIONAL", "PER_CREDIT", "14400.00"), *ROWS[2:]]
    )
    assert [x.fee_type for x in lines] == ["TUITION", "REGISTRATION", "INTERNATIONAL_SERVICE"]
    assert lines[0].gross_amount == D("172800.00")


def test_price_lines_rejects_wrong_residency_or_duplicate_types():
    with pytest.raises(ValueError, match="does not apply"):
        calc.price_lines(ENR, [row("TUITION", "INTERNATIONAL", "PER_CREDIT")])
    with pytest.raises(ValueError, match="more than one"):
        calc.price_lines(ENR, [row("LAB"), row("LAB", n=2)])


def test_per_credit_non_tuition_fee_uses_credits():
    (line,) = calc.price_lines(ENR, [row("LIBRARY", basis="PER_CREDIT", amount="125.50")])
    assert (line.quantity, line.gross_amount, line.description) == (
        D("16"),
        D("2008.00"),
        "Library fee (16 credits)",
    )


def test_inputs_are_not_modified():
    rows = list(ROWS)
    before = [vars(r).copy() for r in rows]
    lines = calc.price_lines(ENR, rows)
    calc.apply_waivers(
        lines, [{"fee_type": "STUDENT_ACTIVITY", "amount": D("1"), "reason": "HARDSHIP"}]
    )
    assert [vars(r) for r in rows] == before
    assert all(x.waiver_amount == 0 for x in lines)


# --- apply_waivers (BR-WAV-01) ---------------------------------------------------------------


def test_apply_waivers_partial_and_full():
    lines = calc.price_lines(ENR, ROWS)
    out = calc.apply_waivers(
        lines, [NS(fee_type="STUDENT_ACTIVITY", amount=D("3000.00"), reason="PG_ACTIVITY_EXEMPT")]
    )
    act = out[-1]
    assert (act.waiver_amount, act.waiver_reason, act.net_amount) == (
        D("3000.00"),
        "PG_ACTIVITY_EXEMPT",
        D("0.00"),
    )
    assert out[:-1] == lines[:-1]


def test_apply_waivers_reports_every_problem_at_once():
    lines = calc.price_lines(ENR, ROWS)
    with pytest.raises(calc.WaiverError) as exc:
        calc.apply_waivers(
            lines,
            [
                {"fee_type": "TUITION", "amount": D("1"), "reason": "HARDSHIP"},
                {"fee_type": "LIBRARY", "amount": D("1"), "reason": "HARDSHIP"},
                {"fee_type": "STUDENT_ACTIVITY", "amount": D("3000.01"), "reason": "HARDSHIP"},
            ],
        )
    assert exc.value.errors == [
        "waivers[0] (TUITION): TUITION is not a waivable fee type.",
        "waivers[1] (LIBRARY): this fee type is not charged on the invoice.",
        "waivers[2] (STUDENT_ACTIVITY): amount 3000.01 exceeds the line gross 3000.00.",
    ]


def test_waiver_equal_to_gross_is_allowed():
    lines = calc.price_lines(ENR, [row("TECHNOLOGY", amount="4000.00")])
    (line,) = calc.apply_waivers(
        lines, [{"fee_type": "TECHNOLOGY", "amount": D("4000.00"), "reason": "SPONSOR_AGREEMENT"}]
    )
    assert line.net_amount == 0


# --- late_fee (BR-LATE-01) -------------------------------------------------------------------

DUE = date(2026, 9, 14)
POLICY = LateFeePolicy(grace_days=14)


@pytest.mark.parametrize(
    ("as_of", "outstanding", "expected"),
    [
        (DUE + timedelta(days=14), "100000.00", "0.00"),  # last grace day
        (DUE + timedelta(days=15), "100000.00", "2000.00"),
        (DUE + timedelta(days=15), "0.00", "0.00"),
        (DUE + timedelta(days=15), "-5.00", "0.00"),
        (DUE + timedelta(days=15), "24999.99", "500.00"),  # 499.9998 → minimum
        (DUE + timedelta(days=15), "25000.00", "500.00"),
        (DUE + timedelta(days=15), "25025.00", "500.50"),
        (DUE + timedelta(days=15), "250000.00", "5000.00"),
        (DUE + timedelta(days=15), "250025.00", "5000.00"),  # 5,000.50 → maximum
        (DUE + timedelta(days=15), "33333.33", "666.67"),  # 666.6666 half-up
    ],
)
def test_late_fee(as_of, outstanding, expected):
    assert calc.late_fee(D(outstanding), DUE, as_of, POLICY) == D(expected)


def test_late_fee_date_is_the_day_after_grace():
    assert calc.late_fee_date(DUE, 14) == date(2026, 9, 29)


# --- ledger_balance (BR-BAL-01, D13) -----------------------------------------------------------


def test_ledger_balance_by_date_and_all():
    entries = [
        NS(entry_date=date(2026, 8, 17), amount=D("144000.00")),
        NS(entry_date=date(2026, 9, 29), amount=D("3280.00")),
        NS(entry_date=date(2026, 10, 1), amount=D("-147280.00")),
    ]
    assert calc.ledger_balance(entries, date(2026, 8, 16)) == D("0.00")
    assert calc.ledger_balance(entries, date(2026, 8, 17)) == D("144000.00")
    assert calc.ledger_balance(entries, date(2026, 9, 30)) == D("147280.00")
    assert calc.ledger_balance(entries, None) == D("0.00")
    assert calc.ledger_balance([], None) == D("0.00")


def test_ledger_balance_is_exact_in_decimal():
    entries = [NS(entry_date=date(2026, 1, 1), amount=D("0.10")) for _ in range(3)]
    assert calc.ledger_balance(entries, None) == D("0.30")


def test_ledger_amount_signs():
    assert calc.ledger_amount("CHARGE", D("10")) == D("10.00")
    assert calc.ledger_amount("ADJUSTMENT", D("10")) == D("-10.00")
    with pytest.raises(ValueError):
        calc.ledger_amount("CHARGE", D("-1"))


# --- open amounts and the void plan ---------------------------------------------------------------


def _line(line_id, line_type, net, adjusts=None, fee_type="TUITION", priority=10):
    return NS(
        line_id=line_id,
        line_type=line_type,
        net_amount=D(net),
        adjusts_line_id=adjusts,
        fee_type=fee_type,
        allocation_priority=priority,
    )


LINES = [
    _line("L01", "FEE", "160000.00"),
    _line("L02", "FEE", "0.00", fee_type="STUDENT_ACTIVITY", priority=60),
    _line("L03", "LATE_FEE", "3280.00", fee_type="LATE_FEE", priority=90),
    _line("L04", "ADJUSTMENT", "-96000.00", adjusts="L01"),
]


def test_line_open_amounts():
    opens = calc.line_open_amounts(
        LINES,
        [NS(invoice_line_id="L01", amount=D("10000.00"))],
        [NS(invoice_line_id="L03", amount=D("3280.00"))],
    )
    assert opens == {"L01": D("54000.00"), "L02": D("0.00"), "L03": D("0.00")}


def test_void_adjustments_cover_the_remaining_net_of_each_line():
    plan = calc.void_adjustments(LINES)
    assert [(a.adjusts_line_id, a.amount, a.allocation_priority) for a in plan] == [
        ("L01", D("-64000.00"), 10),  # 160,000 − 96,000 already adjusted
        ("L03", D("-3280.00"), 90),
    ]
    remaining = calc.line_open_amounts(
        [
            *LINES,
            *[
                _line(f"V{i}", "ADJUSTMENT", a.amount, a.adjusts_line_id)
                for i, a in enumerate(plan)
            ],
        ]
    )
    assert set(remaining.values()) == {D("0.00")}


def test_release_for_adjustment_is_not_implemented_yet():
    """D12: BR-WDR-03 belongs to Phase 5."""
    assert not hasattr(calc, "release_for_adjustment")
