"""Pure reconciliation calculators (ARCHITECTURE.md §5).

They hold no financial rule of their own (safeguard 4): each one composes the domain apps'
calculators, so the detector and the services can never disagree about a rule.

* ``balance_from_ledger``: BR-BAL-01, a thin wrapper around
  ``billing.calculators.ledger_balance`` (decision D-W2; parity tested).
* ``snapshot_totals``: what a BalanceSnapshot reports (DATABASE_DESIGN §3).
* ``balance_identity``: BR-BAL-02 with billing's and payments' own calculators, exactly as
  ``billing.services.student_balance`` evaluates it (parity tested).
* ``refund_net_paid_breaches``: BR-REF-01 replayed with ``payments.calculators.net_paid``
  (decision D-R4).
* ``scholarship_reservation_breaches``: BR-SCH-03/04/08 with
  ``scholarships.calculators.eligible_fee_base`` and the Phase 3 reservation rule of
  ``scholarships.services.reserved_on_invoice`` (decision D-R5; parity tested).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from billing.calculators import invoice_amounts, ledger_balance
from core.money import dsum, round_money
from payments import calculators as pay_calc
from payments.policy import REJECTED
from scholarships import calculators as sch_calc
from scholarships.lifecycle import REVOKED, UNAPPLIED


def balance_from_ledger(entries: Iterable, as_of: date | None = None) -> Decimal:
    """BR-BAL-01: Σ amount of the entries dated on or before ``as_of`` (all if None)."""
    return ledger_balance(entries, as_of)


@dataclass(frozen=True)
class SnapshotTotals:
    balance: Decimal
    debits: Decimal  # Σ positive entries (≥ 0)
    credits: Decimal  # Σ negative entries (≤ 0)


def snapshot_totals(entries: Iterable, as_of: date) -> SnapshotTotals:
    included = [e for e in entries if e.entry_date <= as_of]
    return SnapshotTotals(
        balance=balance_from_ledger(included, as_of),
        debits=dsum(e.amount for e in included if e.amount > 0),
        credits=dsum(e.amount for e in included if e.amount < 0),
    )


@dataclass(frozen=True)
class Identity:
    ledger_balance: Decimal
    invoice_outstanding: Decimal
    unallocated_credit: Decimal

    @property
    def expected(self) -> Decimal:
        return round_money(self.invoice_outstanding - self.unallocated_credit)

    @property
    def holds(self) -> bool:
        return self.ledger_balance == self.expected


def balance_identity(
    *, entries, invoices, payments, payment_allocations, refunds, as_of: date
) -> Identity:
    """BR-BAL-02 for one student at ``as_of``.

    ``invoices`` is ``[(invoice, lines, scholarship_allocations, payment_allocations)]``.
    Exactly the as-of rules of ``billing.services.student_balance``: invoices issued on or
    before ``as_of``; their lines posted and allocations dated on or before it; unallocated
    credit from ``payments.calculators.unallocated_credit``.
    """

    def dated(rows, attr):
        return [r for r in rows if getattr(r, attr, None) is None or getattr(r, attr) <= as_of]

    outstanding = dsum(
        invoice_amounts(
            [x for x in lines if x.posted_date <= as_of],
            dated(sch, "allocated_on"),
            dated(pay, "allocated_on"),
        ).outstanding
        for inv, lines, sch, pay in invoices
        if inv.issue_date <= as_of
    )
    return Identity(
        ledger_balance=ledger_balance(entries, as_of),
        invoice_outstanding=outstanding,
        unallocated_credit=pay_calc.unallocated_credit(
            payments, payment_allocations, refunds, as_of
        ),
    )


@dataclass(frozen=True)
class Breach:
    record_id: str
    expected: Decimal  # the cap
    actual: Decimal  # the amount over it
    details: dict


def refund_net_paid_breaches(payments: Iterable, refunds: Iterable) -> list[Breach]:
    """BR-REF-01 for one student, replayed in request order (decision D-R4).

    Each refund not REJECTED must be ≤ net paid computed as Phase 4 does at request time
    (``payments.calculators.net_paid``): the student's payments less every refund not
    REJECTED that precedes it, by (requested_date, refund_id). REJECTED refunds reserve
    nothing (Phase 4 semantics). Refunds on one payment beyond that payment's own available
    amount are BR-ALC-01's finding (PAYMENT_OVER_ALLOCATED on the payment), so another
    payment never makes them look valid.
    """
    amounts = [p.amount for p in payments]
    ordered = sorted(refunds, key=lambda r: (r.requested_date, r.refund_id))
    out, earlier = [], []
    for r in ordered:
        if r.status == REJECTED:
            continue
        cap = pay_calc.net_paid(amounts, earlier)
        if r.amount > cap:
            out.append(
                Breach(
                    r.refund_id,
                    cap,
                    r.amount,
                    {
                        "payments_total": str(dsum(amounts)),
                        "earlier_refunds_not_rejected": [x.refund_id for x in earlier],
                        "net_paid_before_refund": str(cap),
                    },
                )
            )
        earlier.append(r)
    return out


def award_reservation(award, allocations: Iterable) -> Decimal:
    """What one award holds against its invoice: Phase 3's reservation rule
    (``scholarships.services.reserved_on_invoice``): the awarded amount until it is
    applied, then the net applied (APPLY − REVERSE); nothing once REVOKED."""
    if award.status == REVOKED:
        return Decimal("0.00")
    if award.status in UNAPPLIED:
        return round_money(award.awarded_amount)
    return round_money(dsum(a.amount for a in allocations))


def scholarship_reservation_breaches(awards_with_allocations, scholarship_types, lines):
    """BR-SCH-03/04/08 for one invoice (decision D-R5), with Phase 3's own calculations.

    ``awards_with_allocations`` is ``[(award, its allocations)]``. In (award_date,
    scholarship_id) order, as Phase 3 checks each award when it is made: an award breaches
    when its awarded amount exceeds the eligible fee base of its type
    (``eligible_fee_base``), or when the reservations of the awards up to and including it
    exceed that base (the award-stage guardrail). Revoked awards hold nothing.
    """
    out, held = [], Decimal("0.00")
    ordered = sorted(awards_with_allocations, key=lambda p: (p[0].award_date, p[0].scholarship_id))
    for award, allocations in ordered:
        if award.status == REVOKED:
            continue
        stype = scholarship_types[award.scholarship_type_id]
        base = sch_calc.eligible_fee_base(stype, lines)
        held = round_money(held + award_reservation(award, allocations))
        if award.awarded_amount > base or held > base:
            out.append(
                Breach(
                    award.scholarship_id,
                    base,
                    max(award.awarded_amount, held),
                    {
                        "eligible_fee_base": str(base),
                        "awarded_amount": str(award.awarded_amount),
                        "reserved_on_invoice_through_this_award": str(held),
                        "problem": "AWARD_ABOVE_BASE"
                        if award.awarded_amount > base
                        else "RESERVATIONS_ABOVE_BASE",
                    },
                )
            )
    return out
