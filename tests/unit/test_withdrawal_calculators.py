"""Phase 5 pure pieces beyond the three frozen files: the D-W4 reversal split, the
withdrawal / cancellation adjustment builders (BR-WDR-01/02, D-W3), release-plan details,
the balance_from_ledger parity (D-W2 / D13) and the academics exit registry (D-W1)."""

import itertools
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from academics import exits
from academics.calculators import withdrawal_day, withdrawal_refund_pct
from billing import calculators as calc
from reconciliation.calculators import balance_from_ledger
from scholarships.calculators import split_reversal

TERM = NS(start_date=date(2026, 9, 7), end_date=date(2026, 12, 19))


# --- D-W4: largest-remainder split ----------------------------------------------------------------


def test_split_is_proportional_and_exact():
    assert split_reversal({"A": D("75000.00"), "B": D("20000.00")}, D("57000.00")) == {
        "A": D("45000.00"),
        "B": D("12000.00"),
    }


def test_leftover_cents_go_to_the_largest_remainders_then_allocation_id():
    # 0.10 over three equal holders: 0.033.. each → 0.03 each + 1 leftover cent to the
    # smallest allocation_id (all fractional parts equal)
    assert split_reversal({"C": D("1.00"), "A": D("1.00"), "B": D("1.00")}, D("0.10")) == {
        "A": D("0.04"),
        "B": D("0.03"),
        "C": D("0.03"),
    }
    # 1.00 over 1 : 2: exact 0.333.. / 0.666.. → 0.33 + 0.67 (larger remainder wins)
    assert split_reversal({"A": D("1.00"), "B": D("2.00")}, D("1.00")) == {
        "A": D("0.33"),
        "B": D("0.67"),
    }


def test_split_sweep_sums_exactly_and_never_exceeds_a_holding():
    values = [D(c) / 100 for c in (1, 2, 3, 7, 33, 99, 101, 333, 1001)]
    for a, b, c in itertools.product(values, repeat=3):
        held = {"ALLOC-1": a, "ALLOC-2": b, "ALLOC-3": c}
        total_held = a + b + c
        for total in {D("0.01"), total_held, (total_held / 3).quantize(D("0.01"))}:
            if total > total_held:
                continue
            shares = split_reversal(held, total)
            assert sum(shares.values()) == total
            assert all(0 < v <= held[k] for k, v in shares.items())


@pytest.mark.parametrize("bad", ["-0.01", "3.01"])
def test_split_rejects_impossible_totals(bad):
    with pytest.raises(ValueError):
        split_reversal({"A": D("1.00"), "B": D("2.00")}, D(bad))


def test_split_of_zero_is_empty():
    assert split_reversal({"A": D("1.00")}, D("0.00")) == {}


# --- adjustment builders (BR-WDR-01 / 02, D-W3) ---------------------------------------------------


def _line(n, fee_type, net, *, refundable, line_type="FEE", adjusts=None):
    return NS(
        line_id=f"INV-L{n:02d}",
        line_type=line_type,
        fee_type=fee_type,
        net_amount=D(net),
        is_refundable=refundable,
        allocation_priority=n * 10,
        adjusts_line_id=adjusts,
    )


LINES = [
    _line(1, "TUITION", "144000.00", refundable=True),
    _line(2, "LAB", "6000.00", refundable=True),
    _line(3, "REGISTRATION", "5000.00", refundable=False),
    _line(4, "TECHNOLOGY", "4000.00", refundable=False),
    _line(5, "STUDENT_ACTIVITY", "0.00", refundable=False),  # fully waived: no adjustment
]


def test_withdrawal_adjustments_round_half_up_on_refundable_lines_only():
    odd = [_line(1, "TUITION", "0.05", refundable=True), *LINES[2:]]
    assert [(a.adjusts_line_id, a.amount) for a in calc.withdrawal_adjustments(odd, D("0.50"))] == [
        ("INV-L01", D("-0.03"))  # 0.025 → 0.03, never banker's rounding
    ]
    adj = calc.withdrawal_adjustments(LINES, D("0.60"))
    assert [(a.adjusts_line_id, a.line_net, a.amount) for a in adj] == [
        ("INV-L01", D("144000.00"), D("-86400.00")),
        ("INV-L02", D("6000.00"), D("-3600.00")),
    ]
    assert calc.withdrawal_adjustments(LINES, D("0.00")) == ()


def test_cancellation_adjusts_everything_but_registration_and_zero_lines():
    adj = calc.cancellation_adjustments(LINES)
    assert [(a.adjusts_line_id, a.amount) for a in adj] == [
        ("INV-L01", D("-144000.00")),
        ("INV-L02", D("-6000.00")),
        ("INV-L04", D("-4000.00")),
    ]


def test_D_W3_base_is_the_current_net_after_earlier_adjustments():
    lines = [
        *LINES,
        _line(
            9, "TUITION", "-44000.00", refundable=False, line_type="ADJUSTMENT", adjusts="INV-L01"
        ),
    ]
    (tuition, lab) = calc.withdrawal_adjustments(lines, D("0.80"))
    assert (tuition.line_net, tuition.amount) == (D("100000.00"), D("-80000.00"))
    assert calc.cancellation_adjustments(lines)[0].amount == D("-100000.00")


def test_release_plan_reports_its_inputs():
    plan = calc.release_for_adjustment(
        line_net=D("6000.00"),
        adjustment=D("-3600.00"),
        scholarship_allocated=D("0"),
        payment_allocations=[
            NS(allocation_id="A1", allocated_on=date(2026, 8, 20), amount=D("6000.00"))
        ],
    )
    assert (plan.reduced_net, plan.payments_allocated, plan.excess, plan.deallocations) == (
        D("2400.00"),
        D("6000.00"),
        D("3600.00"),
        (("A1", D("3600.00")),),
    )


def test_release_ignores_fully_released_allocations():
    plan = calc.release_for_adjustment(
        line_net=D("100.00"),
        adjustment=D("-100.00"),
        scholarship_allocated=D("0"),
        payment_allocations=[
            NS(allocation_id="A1", allocated_on=date(2026, 8, 20), amount=D("0.00")),
            NS(allocation_id="A2", allocated_on=date(2026, 8, 19), amount=D("40.00")),
        ],
    )
    assert plan.deallocations == (("A2", D("40.00")),)


def test_withdrawal_day_and_pct_before_the_start():
    assert withdrawal_day(TERM, date(2026, 9, 7)) == 0
    with pytest.raises(ValueError, match="cancellation"):
        withdrawal_refund_pct(TERM, date(2026, 9, 6))


# --- D-W2 / D13: balance_from_ledger parity ------------------------------------------------------


def test_balance_from_ledger_is_the_billing_ledger_balance():
    entries = [
        NS(entry_date=date(2026, 8, 17), amount=D("164000.00")),
        NS(entry_date=date(2026, 8, 25), amount=D("-60000.00")),
        NS(entry_date=date(2026, 9, 17), amount=D("-90000.00")),
    ]
    for as_of in (None, date(2026, 8, 16), date(2026, 8, 25), date(2026, 12, 31)):
        assert balance_from_ledger(entries, as_of) == calc.ledger_balance(entries, as_of)
    assert balance_from_ledger(entries) == D("14000.00")
    assert balance_from_ledger([]) == D("0")


# --- D-W1: the exit registry ---------------------------------------------------------------------


def test_exit_registry_is_idempotent_and_billing_is_registered():
    from billing.withdrawals import apply_enrollment_exit

    assert exits.registered() == ["billing"]
    exits.register_exit_handler("billing", apply_enrollment_exit)  # same function: no-op
    with pytest.raises(ValueError, match="different function"):
        exits.register_exit_handler("billing", lambda **kw: None)
    with pytest.raises(ValueError, match="kind"):
        exits.run_exit_handlers(
            kind="PAUSE", enrollment=None, on_date=None, refund_pct=None, actor="t"
        )
