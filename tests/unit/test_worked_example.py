"""ARCHITECTURE.md §7 worked example: invoice, unallocated credit and ledger reconcile.

Uses only the calculator contracts in ARCHITECTURE.md §5. Every number here is the
number printed in §7; if one changes, both must change together.

Rules: BR-INV-04/06, BR-ALC-01..04, BR-WDR-02/03, BR-SCH-07, BR-REF-01/02, BR-LED-01, BR-BAL-01/02.
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

from academics.calculators import withdrawal_refund_pct
from billing.calculators import (
    LEDGER_SIGN,
    collection_status,
    invoice_amounts,
    release_for_adjustment,
)
from payments.calculators import max_refundable, plan_allocation
from reconciliation.calculators import balance_from_ledger
from scholarships.calculators import award_amount, eligible_fee_base

INV = "INV-STU-000042-2026-000003"
DUE = date(2026, 9, 14)
TERM = NS(
    term_id="TERM-2026-FALL",
    start_date=date(2026, 9, 7),
    census_date=date(2026, 10, 5),
    end_date=date(2026, 12, 19),
)
MERIT = NS(
    scholarship_type_id="SCT-001",
    coverage_scope="ELIGIBLE_LINES_ONLY",
    award_basis="PERCENT",
    award_value=D("50"),
    max_amount_per_term=D("100000.00"),
)

# (line_id, fee_type, net, scholarship_eligible, refundable, priority)
FEE_LINES = [
    (f"{INV}-L01", "TUITION", "160000.00", True, True, 10),
    (f"{INV}-L02", "LAB", "12000.00", True, True, 20),
    (f"{INV}-L03", "REGISTRATION", "5000.00", False, False, 30),
    (f"{INV}-L04", "TECHNOLOGY", "4000.00", False, False, 40),
    (f"{INV}-L05", "LIBRARY", "2000.00", False, False, 50),
    (f"{INV}-L06", "STUDENT_ACTIVITY", "0.00", False, False, 60),  # 3,000 fully waived
]
LINES = [
    NS(
        line_id=i,
        line_type="FEE",
        fee_type=t,
        net_amount=D(n),
        is_scholarship_eligible=e,
        is_refundable=r,
        allocation_priority=p,
    )
    for i, t, n, e, r, p in FEE_LINES
]


def entry(entry_type, magnitude):
    return NS(entry_type=entry_type, amount=LEDGER_SIGN[entry_type] * D(magnitude))


def test_BR_LED_01_ledger_signs():
    assert LEDGER_SIGN == {
        "CHARGE": 1,
        "LATE_FEE": 1,
        "SCHOLARSHIP_REVERSAL": 1,
        "REFUND": 1,
        "ADJUSTMENT": -1,
        "SCHOLARSHIP_CREDIT": -1,
        "PAYMENT": -1,
    }


def test_worked_example_end_to_end():
    total = sum(x.net_amount for x in LINES)
    assert total == D("183000.00")

    # --- Stage A: scholarship, then guardian and sponsor payments -------------
    base = eligible_fee_base(MERIT, LINES)
    assert base == D("172000.00")
    sch = award_amount(MERIT, base, D("0"))
    assert sch == D("86000.00")
    sch_by_line = {f"{INV}-L01": sch}

    pay_alloc = {x.line_id: [] for x in LINES}  # line -> [(allocation_id, date, amount, payer)]

    def open_lines():
        return [
            NS(
                line_id=x.line_id,
                due_date=DUE,
                allocation_priority=x.allocation_priority,
                open_amount=x.net_amount
                - sch_by_line.get(x.line_id, D("0"))
                - sum(a[2] for a in pay_alloc[x.line_id]),
            )
            for x in LINES
        ]

    def pay(pid, on, amount, payer):
        plan = plan_allocation(D(amount), open_lines())
        for n, a in enumerate(plan, start=1):
            pay_alloc[a.line_id].append((f"ALLOC-{pid}-{n:02d}", on, a.amount, payer))
        return sum(a.amount for a in plan)

    alloc1 = pay("PAY-2026-000101", date(2026, 8, 25), "60000.00", "GUARDIAN")
    assert alloc1 == D("60000.00")
    alloc2 = pay("PAY-2026-000102", date(2026, 9, 10), "50000.00", "SPONSOR")
    assert alloc2 == D("37000.00")

    def amounts(adjustments=()):
        return invoice_amounts(
            LINES + [NS(line_type="ADJUSTMENT", net_amount=a) for a in adjustments],
            [NS(amount=v) for v in sch_by_line.values()],
            [NS(amount=a[2], payer_type=a[3]) for allocs in pay_alloc.values() for a in allocs],
        )

    payments_total = D("110000.00")
    a = amounts()
    assert (a.net_charges, a.scholarship_applied, a.amount_paid) == (
        D("183000.00"),
        D("86000.00"),
        D("97000.00"),
    )
    assert a.payments_by_payer == {"GUARDIAN": D("60000.00"), "SPONSOR": D("37000.00")}
    assert a.outstanding == D("0.00")
    assert collection_status(a, due_date=DUE, as_of=date(2026, 9, 10)) == "PAID"

    ledger = [entry("CHARGE", x.net_amount) for x in LINES]
    ledger += [
        entry("SCHOLARSHIP_CREDIT", sch),
        entry("PAYMENT", "60000.00"),
        entry("PAYMENT", "50000.00"),
    ]
    unallocated = payments_total - a.amount_paid
    assert unallocated == D("13000.00")
    assert balance_from_ledger(ledger) == D("-13000.00")
    assert balance_from_ledger(ledger) == a.outstanding - unallocated  # BR-BAL-02

    # --- Stage B: withdrawal on 17 Sep (day 10 -> 60%) ------------------------
    pct = withdrawal_refund_pct(TERM, date(2026, 9, 17))
    assert pct == D("0.60")
    adjustments = []
    for x in LINES:
        if not x.is_refundable:
            continue
        adj = -(x.net_amount * pct).quantize(D("0.01"))
        allocs = [
            NS(allocation_id=i, allocated_on=d, amount=amt) for i, d, amt, _ in pay_alloc[x.line_id]
        ]
        plan = release_for_adjustment(
            line_net=x.net_amount,
            adjustment=adj,
            scholarship_allocated=sch_by_line.get(x.line_id, D("0")),
            payment_allocations=allocs,
        )
        adjustments.append(adj)
        ledger.append(entry("ADJUSTMENT", -adj))
        if plan.scholarship_reversal:
            sch_by_line[x.line_id] -= plan.scholarship_reversal
            ledger.append(entry("SCHOLARSHIP_REVERSAL", plan.scholarship_reversal))
        for alloc_id, amt in plan.deallocations:
            i = next(k for k, v in enumerate(pay_alloc[x.line_id]) if v[0] == alloc_id)
            aid, d, cur, payer = pay_alloc[x.line_id][i]
            pay_alloc[x.line_id][i] = (aid, d, cur - amt, payer)

    assert adjustments == [D("-96000.00"), D("-7200.00")]
    assert sch_by_line[f"{INV}-L01"] == D("34400.00")
    a = amounts(adjustments)
    assert (a.original_charges, a.adjustments, a.net_charges) == (
        D("183000.00"),
        D("-103200.00"),
        D("79800.00"),
    )
    assert (a.scholarship_applied, a.amount_paid) == (D("34400.00"), D("45400.00"))
    assert a.outstanding == D("0.00")
    assert collection_status(a, due_date=DUE, as_of=date(2026, 9, 17)) == "PAID"

    unallocated = payments_total - a.amount_paid
    assert unallocated == D("64600.00")
    assert balance_from_ledger(ledger) == D("-64600.00")
    assert balance_from_ledger(ledger) == a.outstanding - unallocated  # BR-BAL-02

    # --- Stage C: refunds back to each payer ----------------------------------
    def available(pid, amount):
        return D(amount) - sum(
            v[2]
            for allocs in pay_alloc.values()
            for v in allocs
            if v[0].startswith(f"ALLOC-{pid}-")
        )

    avail1 = available("PAY-2026-000101", "60000.00")
    avail2 = available("PAY-2026-000102", "50000.00")
    assert (avail1, avail2) == (D("30400.00"), D("34200.00"))
    credit_balance = -balance_from_ledger(ledger)
    assert max_refundable(payments_total, credit_balance, avail2) == D("34200.00")
    assert max_refundable(payments_total, credit_balance, avail1) == D("30400.00")
    assert D("35000.00") > max_refundable(payments_total, credit_balance, avail1)  # 422

    ledger += [entry("REFUND", "34200.00"), entry("REFUND", "30400.00")]
    refunded = D("64600.00")
    unallocated = payments_total - a.amount_paid - refunded
    assert balance_from_ledger(ledger) == D("0.00")
    assert unallocated == D("0.00")
    assert balance_from_ledger(ledger) == a.outstanding - unallocated  # BR-BAL-02
    assert payments_total - refunded == a.amount_paid  # net paid
    assert len(ledger) == 14


def test_max_refundable_is_the_smallest_cap_and_never_negative():
    assert max_refundable(D("100.00"), D("80.00"), D("90.00")) == D("80.00")
    assert max_refundable(D("50.00"), D("80.00"), D("90.00")) == D("50.00")
    assert max_refundable(D("100.00"), D("-20.00"), D("90.00")) == D("0.00")
