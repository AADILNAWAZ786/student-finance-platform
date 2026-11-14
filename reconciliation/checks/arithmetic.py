"""Arithmetic checks (ARCHITECTURE §12.1), all in Python Decimal with the domain apps' own
calculators (ARCHITECTURE §8.3; safeguard 4): net = gross − waiver, gross = qty × unit,
invoice total = Σ FEE lines, line open ≥ 0, adjustment ≤ line, BR-INV-04 amounts never
negative, releases ≤ what they release, fee lines priced from their schedule, snapshot
totals, and BR-LED-01 ledger completeness (exactly one matching entry per source row).

Amount rules use the rows dated on or before the run's as-of date (REC-08); ledger
completeness and line arithmetic are timeless.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from billing import calculators as billing_calc
from billing.services import ENTRY_TYPE_FOR_LINE, SCHOLARSHIP_ENTRY_TYPE
from core.money import dsum, round_money, to_decimal
from reconciliation import calculators as rec_calc
from reconciliation import policy
from reconciliation.checks.base import Finding, check

ARITH = policy.ARITHMETIC_MISMATCH


def _f(rule, entity, record_id, student_id, expected=None, actual=None, **details):
    return Finding(
        ARITH,
        rule,
        entity,
        str(record_id),
        student_id,
        expected=None if expected is None else round_money(expected),
        actual=None if actual is None else round_money(actual),
        details=details,
    )


def _student_of_invoice(ds, invoice_id):
    inv = ds.by_id["invoices"].get(invoice_id)
    return inv and inv.student_id


@check("A-LINE-AMOUNTS", family="ARITHMETIC", rule_id="BR-WAV-01")
def line_amounts(ds):
    """gross = quantity × unit (BR-FEE-02); net = gross − waiver with 0 ≤ waiver ≤ gross
    (BR-WAV-01)."""
    out = []
    for line in ds.lines:
        sid = _student_of_invoice(ds, line.invoice_id)
        gross = round_money(to_decimal(line.quantity) * to_decimal(line.unit_amount))
        if gross != line.gross_amount:
            out.append(
                _f(
                    "BR-FEE-02",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    gross,
                    line.gross_amount,
                    field="gross_amount",
                )
            )
        if round_money(line.gross_amount - line.waiver_amount) != line.net_amount:
            out.append(
                _f(
                    "BR-WAV-01",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    line.gross_amount - line.waiver_amount,
                    line.net_amount,
                    field="net_amount",
                )
            )
        if line.line_type == "FEE" and not (0 <= line.waiver_amount <= line.gross_amount):
            out.append(
                _f(
                    "BR-WAV-01",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    None,
                    line.waiver_amount,
                    field="waiver_amount",
                    gross_amount=str(line.gross_amount),
                )
            )
    return len(ds.lines), out


@check("A-FEE-PRICE", family="ARITHMETIC", rule_id="BR-FEE-01")
def fee_line_priced_from_its_schedule(ds):
    """A FEE line's schedule was in force by the term's invoice_date (BR-FEE-01), its unit is
    the schedule amount and its quantity follows the charge basis (BR-FEE-02; R-45)."""
    out, checked = [], 0
    for line in ds.lines:
        if line.line_type != "FEE" or line.fee_schedule_id is None:
            continue
        fs = ds.by_id["fee_schedules"].get(line.fee_schedule_id)
        inv = ds.by_id["invoices"].get(line.invoice_id)
        term = inv and ds.by_id["terms"].get(inv.term_id)
        enr = inv and ds.by_id["enrollments"].get(inv.enrollment_id)
        if not (fs and term and enr):
            continue
        checked += 1
        sid = inv.student_id
        if fs.effective_from > term.invoice_date:
            out.append(
                _f(
                    "BR-FEE-01",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    field="fee_schedule",
                    fee_schedule_id=fs.pk,
                    effective_from=str(fs.effective_from),
                    invoice_date=str(term.invoice_date),
                )
            )
        if line.unit_amount != fs.amount:
            out.append(
                _f(
                    "BR-FEE-01",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    fs.amount,
                    line.unit_amount,
                    field="unit_amount",
                    fee_schedule_id=fs.pk,
                )
            )
        quantity = Decimal(enr.credits) if fs.charge_basis == "PER_CREDIT" else Decimal(1)
        if to_decimal(line.quantity) != quantity:
            out.append(
                _f(
                    "BR-FEE-02",
                    "InvoiceLine",
                    line.pk,
                    sid,
                    None,
                    None,
                    field="quantity",
                    expected_quantity=str(quantity),
                    quantity=str(line.quantity),
                )
            )
    return checked, out


@check("A-INVOICE-TOTAL", family="ARITHMETIC", rule_id="BR-INV-02")
def invoice_total_is_fee_lines(ds):
    """invoice.total_amount = Σ net of its FEE lines (BR-INV-02)."""
    out = []
    for inv in ds.invoices:
        total = dsum(
            line.net_amount
            for line in ds.lines_by_invoice.get(inv.pk, [])
            if line.line_type == "FEE"
        )
        if total != inv.total_amount:
            out.append(_f("BR-INV-02", "Invoice", inv.pk, inv.student_id, total, inv.total_amount))
    return len(ds.invoices), out


def _as_of_rows(ds, invoice_id):
    lines = [x for x in ds.lines_by_invoice.get(invoice_id, []) if ds.on_or_before(x.posted_date)]
    ids = {x.line_id for x in lines}
    sch = [
        a
        for a in ds.scholarship_allocations_by_invoice.get(invoice_id, [])
        if ds.on_or_before(a.allocated_on) and a.invoice_line_id in ids
    ]
    pay = [
        a
        for a in ds.payment_allocations_by_invoice.get(invoice_id, [])
        if ds.on_or_before(a.allocated_on) and a.invoice_line_id in ids
    ]
    return lines, sch, pay


@check("A-LINE-OPEN", family="ARITHMETIC", rule_id="BR-ALC-02")
def line_open_never_negative(ds):
    """Every FEE/LATE_FEE line's open amount ≥ 0, and its adjustments never exceed it
    (BR-ALC-02), as of the run date."""
    out, checked = [], 0
    for inv in ds.invoices:
        lines, sch, pay = _as_of_rows(ds, inv.pk)
        charge_ids = {x.line_id for x in lines if x.line_type in ("FEE", "LATE_FEE")}
        # rows pointing at anything but a charge line are RL-* findings; keep them out here
        lines = [x for x in lines if x.line_type != "ADJUSTMENT" or x.adjusts_line_id in charge_ids]
        sch = [a for a in sch if a.invoice_line_id in charge_ids]
        pay = [a for a in pay if a.invoice_line_id in charge_ids]
        opens = billing_calc.line_open_amounts(lines, sch, pay)
        adjusted = billing_calc.line_open_amounts(lines)  # net + adjustments only
        for line_id in sorted(opens):
            checked += 1
            if adjusted[line_id] < 0:
                out.append(
                    _f(
                        "BR-ALC-02",
                        "InvoiceLine",
                        line_id,
                        inv.student_id,
                        Decimal("0.00"),
                        adjusted[line_id],
                        problem="ADJUSTMENT_EXCEEDS_LINE",
                    )
                )
            elif opens[line_id] < 0:
                out.append(
                    _f(
                        "BR-ALC-02",
                        "InvoiceLine",
                        line_id,
                        inv.student_id,
                        Decimal("0.00"),
                        opens[line_id],
                        problem="LINE_OPEN_NEGATIVE",
                    )
                )
    return checked, out


@check("A-INVOICE-AMOUNTS", family="ARITHMETIC", rule_id="BR-INV-04")
def invoice_amounts_never_negative(ds):
    """scholarship_applied, amount_paid and outstanding are never negative (BR-INV-04)."""
    out, checked = [], 0
    for inv in ds.invoices:
        if not ds.on_or_before(inv.issue_date):
            continue
        checked += 1
        amounts = billing_calc.invoice_amounts(*_as_of_rows(ds, inv.pk))
        try:
            billing_calc.check_amounts(amounts)
        except ValueError as exc:
            out.append(
                _f(
                    "BR-INV-04",
                    "Invoice",
                    inv.pk,
                    inv.student_id,
                    Decimal("0.00"),
                    None,
                    message=str(exc),
                    scholarship_applied=str(amounts.scholarship_applied),
                    amount_paid=str(amounts.amount_paid),
                    outstanding=str(amounts.outstanding),
                )
            )
    return checked, out


@check("A-RELEASE-BOUNDS", family="ARITHMETIC", rule_id="BR-ALC-05")
def releases_never_exceed_the_original(ds):
    """Σ deallocations of an ALLOCATE row ≤ its amount (BR-ALC-05); Σ reversals of an APPLY
    row ≤ its amount (BR-SCH-07)."""
    out, checked = [], 0
    for table, original, release, rule, entity in (
        ("payment_allocations", "ALLOCATE", "DEALLOCATE", "BR-ALC-05", "PaymentAllocation"),
        ("scholarship_allocations", "APPLY", "REVERSE", "BR-SCH-07", "ScholarshipAllocation"),
    ):
        released = defaultdict(list)
        rows = getattr(ds, table)
        for r in rows:
            if r.allocation_type == release:
                released[r.reverses_id].append(r.amount)
        for r in rows:
            if r.allocation_type != original:
                continue
            checked += 1
            total = -dsum(released.get(r.pk, []))
            if total > r.amount:
                owner = (
                    ds.by_id["payments"].get(r.payment_id)
                    if table == "payment_allocations"
                    else ds.by_id["scholarships"].get(r.scholarship_id)
                )
                out.append(_f(rule, entity, r.pk, owner and owner.student_id, r.amount, total))
    return checked, out


def _expected_entries(ds):
    """{(source field, source id): (entity, student, entry_type, amount, date)} per the
    BR-LED-01 posting table, using billing's own posting constants and ledger_amount."""
    out = {}
    for line in ds.lines:
        t = ENTRY_TYPE_FOR_LINE[line.line_type]
        out[("invoice_line_id", line.pk)] = (
            "InvoiceLine",
            _student_of_invoice(ds, line.invoice_id),
            t,
            billing_calc.ledger_amount(t, abs(line.net_amount)),
            line.posted_date,
        )
    for a in ds.scholarship_allocations:
        t = SCHOLARSHIP_ENTRY_TYPE[a.allocation_type]
        s = ds.by_id["scholarships"].get(a.scholarship_id)
        out[("scholarship_allocation_id", a.pk)] = (
            "ScholarshipAllocation",
            s and s.student_id,
            t,
            billing_calc.ledger_amount(t, abs(a.amount)),
            a.allocated_on,
        )
    for p in ds.payments:
        out[("payment_id", p.pk)] = (
            "Payment",
            p.student_id,
            "PAYMENT",
            billing_calc.ledger_amount("PAYMENT", p.amount),
            p.received_date,
        )
    for r in ds.refunds:
        if r.status == "PROCESSED":
            out[("refund_id", r.pk)] = (
                "Refund",
                r.student_id,
                "REFUND",
                billing_calc.ledger_amount("REFUND", r.amount),
                r.processed_date,
            )
    return out


SOURCE_FIELDS = ("invoice_line_id", "scholarship_allocation_id", "payment_id", "refund_id")


@check("A-LEDGER-COMPLETENESS", family="ARITHMETIC", rule_id="BR-LED-01")
def one_matching_ledger_entry_per_source(ds):
    """Every source row that posts has exactly one ledger entry with the right type, amount
    (sign included), date and student; rows that post nothing (allocations, unprocessed
    refunds) have none (BR-LED-01)."""
    expected = _expected_entries(ds)
    actual = defaultdict(list)
    for e in ds.ledger:
        for f in SOURCE_FIELDS:
            if getattr(e, f) is not None:
                actual[(f, getattr(e, f))].append(e)
    out = []
    for key in sorted(expected, key=str):
        entity, sid, etype, amount, when = expected[key]
        entries = actual.get(key, [])
        if len(entries) != 1:
            out.append(
                _f(
                    "BR-LED-01",
                    entity,
                    key[1],
                    sid,
                    amount,
                    dsum(e.amount for e in entries),
                    problem="MISSING_ENTRY" if not entries else "SEVERAL_ENTRIES",
                    entry_ids=sorted(e.pk for e in entries),
                )
            )
            continue
        e = entries[0]
        wrong = [
            name
            for name, ok in (
                ("entry_type", e.entry_type == etype),
                ("amount", e.amount == amount),
                ("entry_date", e.entry_date == when),
                ("student", e.student_id == sid),
            )
            if not ok
        ]
        if wrong:
            out.append(
                _f(
                    "BR-LED-01",
                    entity,
                    key[1],
                    sid,
                    amount,
                    e.amount,
                    problem="ENTRY_MISMATCH",
                    entry_id=e.pk,
                    fields=wrong,
                    expected_entry={"entry_type": etype, "entry_date": str(when), "student": sid},
                    actual_entry={
                        "entry_type": e.entry_type,
                        "entry_date": str(e.entry_date),
                        "student": e.student_id,
                    },
                )
            )
    for key in sorted(set(actual) - set(expected), key=str):
        for e in actual[key]:
            out.append(
                _f(
                    "BR-LED-01",
                    "LedgerEntry",
                    e.pk,
                    e.student_id,
                    None,
                    e.amount,
                    problem="ENTRY_WITHOUT_POSTING_SOURCE",
                    source=[key[0], key[1]],
                )
            )
    return len(expected) + len(ds.ledger), out


@check("A-SNAPSHOT-TOTALS", family="ARITHMETIC", rule_id="BR-BAL-03")
def snapshot_totals(ds):
    """A snapshot's total_debits / total_credits are Σ positive / Σ negative ledger entries
    dated on or before its as_of_date (DATABASE_DESIGN §3).

    Checked only on snapshots whose reported_balance still equals the ledger: one whose
    balance no longer does (a later posting dated on or before it, or a modified balance) is
    reported once, as BALANCE_LEDGER_MISMATCH (BR-BAL-03), not again for its totals, which
    DATABASE_DESIGN §3 defines "at snapshot time"."""
    out, checked = [], 0
    entries = ds.per_student("ledger")
    for snap in ds.snapshots:
        if not ds.on_or_before(snap.as_of_date):
            continue
        totals = rec_calc.snapshot_totals(entries.get(snap.student_id, []), snap.as_of_date)
        if totals.balance != snap.reported_balance:
            continue  # Q-BALANCE-LEDGER-MISMATCH reports this snapshot
        checked += 1
        for name, value in (("total_debits", totals.debits), ("total_credits", totals.credits)):
            if getattr(snap, name) != value:
                out.append(
                    _f(
                        "BR-BAL-03",
                        "BalanceSnapshot",
                        snap.pk,
                        snap.student_id,
                        value,
                        getattr(snap, name),
                        field=name,
                    )
                )
    return checked, out
