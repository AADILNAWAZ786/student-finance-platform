"""payments.calculators beyond the frozen plan_allocation file: refund caps with pending
reservations (BR-REF-01/02, D-P7/D-P8), unallocated credit (BR-BAL-02), LIFO release
(BR-ALC-06), payment available / allocation state. Pure functions: no database."""

import inspect
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from payments import calculators as calc
from payments.calculators import max_refundable


def ref(rid, amount, status, *, payment="PAY-2026-000001", processed=None):
    return NS(
        refund_id=rid, amount=D(amount), status=status, payment_id=payment, processed_date=processed
    )


def alloc(aid, amount, *, kind="ALLOCATE", on=date(2026, 8, 20), reverses=None):
    return NS(
        allocation_id=aid,
        amount=D(amount),
        allocation_type=kind,
        allocated_on=on,
        reverses_id=reverses,
    )


# --- frozen contract ----------------------------------------------------------------------------


def test_max_refundable_keeps_the_frozen_signature():
    assert list(inspect.signature(max_refundable).parameters) == [
        "net_paid",
        "credit_balance",
        "payment_available",
    ]


@pytest.mark.parametrize(
    ("net", "credit", "available", "expected"),
    [
        ("100.00", "80.00", "90.00", "80.00"),
        ("50.00", "80.00", "90.00", "50.00"),
        ("100.00", "80.00", "10.00", "10.00"),
        ("100.00", "-20.00", "90.00", "0.00"),  # never negative
        ("110000.00", "64600.00", "30400.00", "30400.00"),  # §7 Stage C, PAY-1
    ],
)
def test_max_refundable_is_the_smallest_cap(net, credit, available, expected):
    assert max_refundable(D(net), D(credit), D(available)) == D(expected)


# --- BR-REF-01: net paid subtracts every refund not REJECTED --------------------------------------


def test_BR_REF_01_net_paid_subtracts_requested_approved_and_processed_refunds():
    refunds = [
        ref("REF-2026-000001", "100.00", "REQUESTED"),
        ref("REF-2026-000002", "200.00", "APPROVED"),
        ref("REF-2026-000003", "300.00", "PROCESSED"),
        ref("REF-2026-000004", "400.00", "REJECTED"),  # the only one not subtracted
    ]
    assert calc.net_paid([D("1000.00"), D("500.00")], refunds) == D("900.00")


def test_net_paid_can_leave_out_the_refund_being_checked():
    refunds = [
        ref("REF-2026-000001", "100.00", "APPROVED"),
        ref("REF-2026-000002", "50.00", "REQUESTED"),
    ]
    assert calc.net_paid([D("1000.00")], refunds, exclude_refund_id="REF-2026-000001") == D(
        "950.00"
    )


# --- BR-REF-02: credit balance and pending reservations -------------------------------------------


def test_ledger_credit_balance_is_minus_a_negative_balance_else_zero():
    assert calc.ledger_credit_balance(D("-64600.00")) == D("64600.00")
    assert calc.ledger_credit_balance(D("0.00")) == D("0.00")
    assert calc.ledger_credit_balance(D("1500.00")) == D("0.00")


def test_pending_reservations_are_requested_and_approved_only():
    refunds = [
        ref("REF-2026-000001", "100.00", "REQUESTED"),
        ref("REF-2026-000002", "200.00", "APPROVED"),
        ref("REF-2026-000003", "300.00", "PROCESSED"),  # already on the ledger
        ref("REF-2026-000004", "400.00", "REJECTED"),
    ]
    assert calc.pending_reservations(refunds) == D("300.00")
    assert calc.pending_reservations(refunds, exclude_refund_id="REF-2026-000002") == D("100.00")


def test_credit_available_subtracts_pending_reservations_and_is_never_negative():
    assert calc.credit_available(D("-10000.00"), D("4000.00")) == D("6000.00")
    assert calc.credit_available(D("-10000.00"), D("12000.00")) == D("0.00")
    assert calc.credit_available(D("500.00"), D("0.00")) == D("0.00")


def test_payment_available_reserves_every_refund_not_rejected():
    allocs = [alloc("A1", "600.00"), alloc("A2", "-100.00", kind="DEALLOCATE", reverses="A1")]
    refunds = [
        ref("REF-2026-000001", "100.00", "REQUESTED"),
        ref("REF-2026-000002", "50.00", "PROCESSED"),
        ref("REF-2026-000003", "999.00", "REJECTED"),
    ]
    # 1000 − (600 − 100) − (100 + 50)
    assert calc.payment_available(D("1000.00"), allocs, refunds) == D("350.00")
    assert calc.payment_available(
        D("1000.00"), allocs, refunds, exclude_refund_id="REF-2026-000001"
    ) == D("450.00")


def test_refund_caps_two_pending_requests_cannot_spend_the_same_credit():
    """Ledger credit 10,000; one pending refund of 6,000 leaves 4,000 for a second one."""
    pending = [ref("REF-2026-000001", "6000.00", "REQUESTED")]
    caps = calc.refund_caps(
        payment_amounts=[D("50000.00")],
        student_refunds=pending,
        ledger_balance=D("-10000.00"),
        source_amount=D("50000.00"),
        source_allocations=[alloc("A1", "40000.00")],
        source_refunds=pending,
    )
    assert caps.as_dict() == {
        "net_paid": D("44000.00"),
        "ledger_balance": D("-10000.00"),
        "credit_balance": D("10000.00"),
        "pending_refunds": D("6000.00"),
        "credit_available": D("4000.00"),
        "payment_available": D("4000.00"),
        "max_refundable": D("4000.00"),
    }


def test_refund_caps_leave_out_the_refund_being_approved_so_it_never_counts_twice():
    """D-P8: approving the 6,000 refund checks it against 10,000 of credit, not 4,000."""
    pending = [ref("REF-2026-000001", "6000.00", "REQUESTED")]
    caps = calc.refund_caps(
        payment_amounts=[D("50000.00")],
        student_refunds=pending,
        ledger_balance=D("-10000.00"),
        source_amount=D("50000.00"),
        source_allocations=[alloc("A1", "40000.00")],
        source_refunds=pending,
        exclude_refund_id="REF-2026-000001",
    )
    assert (caps.net_paid, caps.pending_refunds, caps.credit_available) == (
        D("50000.00"),
        D("0.00"),
        D("10000.00"),
    )
    assert (caps.payment_available, caps.max_refundable) == (D("10000.00"), D("10000.00"))


# --- BR-BAL-02: unallocated credit subtracts PROCESSED refunds only ------------------------------


def test_unallocated_credit_subtracts_only_processed_refunds_and_respects_as_of():
    payments = [
        NS(amount=D("1000.00"), received_date=date(2026, 8, 20)),
        NS(amount=D("500.00"), received_date=date(2026, 9, 20)),
    ]
    allocations = [alloc("A1", "600.00", on=date(2026, 8, 21))]
    refunds = [
        ref("REF-2026-000001", "100.00", "PROCESSED", processed=date(2026, 9, 1)),
        ref("REF-2026-000002", "200.00", "APPROVED"),  # not on the ledger: not subtracted
    ]
    assert calc.unallocated_credit(payments, allocations, refunds) == D("800.00")
    assert calc.unallocated_credit(payments, allocations, refunds, date(2026, 8, 31)) == D("400.00")
    assert calc.unallocated_credit(payments, allocations, refunds, date(2026, 8, 20)) == D(
        "1000.00"
    )


# --- BR-ALC-06: LIFO release ---------------------------------------------------------------------


def test_release_is_last_in_first_out_by_date_then_allocation_id():
    rows = [
        alloc("ALLOC-PAY-2026-000001-01", "60000.00", on=date(2026, 8, 25)),
        alloc("ALLOC-PAY-2026-000002-01", "14000.00", on=date(2026, 9, 10)),
    ]
    plan = calc.plan_release_lifo(rows, D("44400.00"))
    assert [(p.allocation_id, p.amount) for p in plan] == [
        ("ALLOC-PAY-2026-000002-01", D("14000.00")),  # newest first (§7 Stage B)
        ("ALLOC-PAY-2026-000001-01", D("30400.00")),
    ]


def test_release_counts_earlier_deallocations_and_breaks_same_day_ties_by_id():
    rows = [
        alloc("ALLOC-PAY-2026-000001-01", "100.00"),
        alloc("ALLOC-PAY-2026-000001-02", "100.00"),
        alloc(
            "ALLOC-PAY-2026-000001-03",
            "-70.00",
            kind="DEALLOCATE",
            reverses="ALLOC-PAY-2026-000001-02",
        ),
    ]
    plan = calc.plan_release_lifo(rows, D("50.00"))
    assert [(p.allocation_id, p.amount) for p in plan] == [
        ("ALLOC-PAY-2026-000001-02", D("30.00")),  # only 30 left on -02
        ("ALLOC-PAY-2026-000001-01", D("20.00")),
    ]


def test_release_more_than_held_raises():
    with pytest.raises(calc.InsufficientAllocations) as exc:
        calc.plan_release_lifo([alloc("A1", "10.00")], D("10.01"))
    assert (exc.value.needed, exc.value.held) == (D("10.01"), D("10.00"))


@pytest.mark.parametrize("bad", ["0.00", "-1.00"])
def test_release_amount_must_be_positive(bad):
    with pytest.raises(ValueError):
        calc.plan_release_lifo([alloc("A1", "10.00")], D(bad))


def test_allocation_state():
    assert calc.allocation_state(D("100.00"), D("0.00")) == "UNALLOCATED"
    assert calc.allocation_state(D("100.00"), D("40.00")) == "PARTIAL"
    assert calc.allocation_state(D("100.00"), D("100.00")) == "FULL"


def test_plan_allocation_never_rounds_away_money():
    lines = [
        NS(line_id="L2", due_date=date(2026, 9, 14), allocation_priority=20, open_amount=D("0.01")),
        NS(line_id="L1", due_date=date(2026, 9, 14), allocation_priority=10, open_amount=D("0.02")),
    ]
    plan = calc.plan_allocation(D("0.03"), lines)
    assert [(p.line_id, p.amount) for p in plan] == [("L1", D("0.02")), ("L2", D("0.01"))]
