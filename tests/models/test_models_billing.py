"""FeeSchedule, Invoice, InvoiceLine, InvoiceEvent, LedgerEntry: DB constraints
(DATABASE_DESIGN §3 billing, §4 uniqueness).

Rows are inserted directly with the ORM (bypassing services) to prove the database itself
rejects single-row violations. Arithmetic equalities are deliberately *not* DB constraints
(SQLite stores decimals as REAL): they are service rules, tested in tests/services.
"""

from datetime import date
from decimal import Decimal as D

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry

pytestmark = pytest.mark.django_db


def _rejects(obj):
    with pytest.raises(IntegrityError), transaction.atomic():
        obj.save(force_insert=True)
        connection.check_constraints()  # FKs are deferred until commit on SQLite


def _fee(program, term, **kw):
    fields = dict(
        fee_schedule_id="FEE-PRG-001-2026-FALL-TUITION-DOM-V1",
        program=program,
        term=term,
        fee_type="TUITION",
        residency="DOMESTIC",
        charge_basis="PER_CREDIT",
        amount=D("9000.00"),
        is_scholarship_eligible=True,
        is_refundable=True,
        is_waivable=False,
        allocation_priority=10,
        version=1,
        effective_from=date(2026, 1, 1),
    )
    fields.update(kw)
    return FeeSchedule(**fields)


@pytest.fixture
def fall(terms):
    return terms["TERM-2026-FALL"]


# --- FeeSchedule -------------------------------------------------------------------------


def test_fee_schedule_valid_row_saves(program, fall):
    _fee(program, fall).save(force_insert=True)
    assert FeeSchedule.objects.count() == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": D("-0.01")},
        {"fee_type": "PARKING"},
        {"residency": "LOCAL"},
        {"charge_basis": "HOURLY"},
        {"charge_basis": "FLAT"},  # TUITION must be PER_CREDIT
        {"residency": "ALL"},  # D3: TUITION is per residency
        {"version": 0},
        {"version": 2},  # version > 1 needs supersedes
        {"is_waivable": True},  # D5: TUITION is not waivable
        {"is_scholarship_eligible": False},
        {"is_refundable": False},
        {"allocation_priority": 30},
    ],
)
def test_fee_schedule_check_constraints(program, fall, overrides):
    _rejects(_fee(program, fall, **overrides))


@pytest.mark.parametrize(
    ("fee_type", "residency", "flags", "priority", "ok"),
    [
        ("INTERNATIONAL_SERVICE", "INTERNATIONAL", (False, False, False), 70, True),
        ("INTERNATIONAL_SERVICE", "DOMESTIC", (False, False, False), 70, False),
        ("LAB", "ALL", (True, True, False), 20, True),
        ("LAB", "INTERNATIONAL", (True, True, False), 20, False),
        ("TECHNOLOGY", "ALL", (False, False, True), 40, True),
        ("TECHNOLOGY", "ALL", (False, False, False), 40, False),
        ("REGISTRATION", "ALL", (False, False, False), 30, True),
        ("REGISTRATION", "ALL", (False, False, False), 10, False),
    ],
)
def test_fee_schedule_residency_flags_priority_by_type(
    program, fall, fee_type, residency, flags, priority, ok
):
    from core.ids import fee_schedule_id

    row = _fee(
        program,
        fall,
        fee_schedule_id=fee_schedule_id("PRG-001", "TERM-2026-FALL", fee_type, residency, 1),
        fee_type=fee_type,
        residency=residency,
        charge_basis="FLAT",
        is_scholarship_eligible=flags[0],
        is_refundable=flags[1],
        is_waivable=flags[2],
        allocation_priority=priority,
    )
    if ok:
        row.save(force_insert=True)
    else:
        _rejects(row)


def test_fee_schedule_unique_version_effective_from_and_linear_chain(program, fall):
    v1 = _fee(program, fall)
    v1.save(force_insert=True)
    _rejects(
        _fee(
            program,
            fall,
            fee_schedule_id="FEE-PRG-001-2026-FALL-TUITION-DOM-V9",
            effective_from=date(2026, 2, 1),
        )
    )
    _rejects(
        _fee(
            program,
            fall,
            fee_schedule_id="FEE-PRG-001-2026-FALL-TUITION-DOM-V8",
            version=8,
            supersedes=v1,
            effective_from=date(2026, 1, 1),
        )
    )
    _fee(
        program,
        fall,
        fee_schedule_id="FEE-PRG-001-2026-FALL-TUITION-DOM-V2",
        version=2,
        supersedes=v1,
        effective_from=date(2026, 8, 1),
    ).save(force_insert=True)
    # supersedes is unique: a second successor of V1 would fork the chain
    _rejects(
        _fee(
            program,
            fall,
            fee_schedule_id="FEE-PRG-001-2026-FALL-TUITION-DOM-V3",
            version=3,
            supersedes=v1,
            effective_from=date(2026, 9, 1),
        )
    )


def test_fee_schedule_and_its_program_are_protected(program, fall):
    fs = _fee(program, fall)
    fs.save(force_insert=True)
    with pytest.raises(ProtectedError):
        program.delete()


# --- Invoice ---------------------------------------------------------------------------------


def _invoice_row(enrollment, **kw):
    fields = dict(
        invoice_id="INV-STU-000001-2026-000009",
        student_id=enrollment.student_id,
        enrollment=enrollment,
        term_id=enrollment.term_id,
        issue_date=date(2026, 8, 17),
        due_date=date(2026, 9, 14),
        total_amount=D("100.00"),
        lifecycle_status="ISSUED",
        collection_status="UNPAID",
        collection_status_as_of=date(2026, 8, 17),
    )
    fields.update(kw)
    return Invoice(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"due_date": date(2026, 8, 16)},
        {"total_amount": D("-1.00")},
        {"lifecycle_status": "PAID"},
        {"collection_status": "ISSUED"},
        {"currency": "USD"},
        {"invoice_id": "INV-STU-000001-2026-000009", "student_id": "STU-999999"},
    ],
)
def test_invoice_check_constraints(fall_enrollment, overrides):
    _rejects(_invoice_row(fall_enrollment, **overrides))


def test_invoice_has_no_db_unique_on_enrollment(invoice, fall_enrollment):
    """BR-INV-01 is a service rule; a duplicate invoice must be representable (anomaly)."""
    _invoice_row(fall_enrollment).save(force_insert=True)
    assert Invoice.objects.filter(enrollment=fall_enrollment).count() == 2


# --- InvoiceLine -----------------------------------------------------------------------------


def _line(inv, fs, **kw):
    fields = dict(
        line_id=f"{inv.invoice_id}-L90",
        invoice=inv,
        line_type="FEE",
        fee_schedule=fs,
        fee_type="TECHNOLOGY",
        description="x",
        quantity=D("1"),
        unit_amount=D("4000.00"),
        gross_amount=D("4000.00"),
        waiver_amount=D("0"),
        net_amount=D("4000.00"),
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=True,
        allocation_priority=40,
        posted_date=date(2026, 8, 17),
    )
    fields.update(kw)
    return InvoiceLine(**fields)


@pytest.fixture
def tech(fee_setup):
    return fee_setup[("TECHNOLOGY", "ALL")]


def test_invoice_line_valid_rows_save(invoice, tech):
    target = invoice.lines.get(fee_type="TECHNOLOGY")
    _line(
        invoice, tech, waiver_amount=D("100"), waiver_reason="HARDSHIP", net_amount=D("3900")
    ).save(force_insert=True)
    _line(
        invoice,
        None,
        line_id=f"{invoice.invoice_id}-L91",
        line_type="LATE_FEE",
        fee_type="LATE_FEE",
        is_waivable=False,
        allocation_priority=90,
    ).save(force_insert=True)
    _line(
        invoice,
        None,
        line_id=f"{invoice.invoice_id}-L92",
        line_type="ADJUSTMENT",
        unit_amount=D("-4000.00"),
        gross_amount=D("-4000.00"),
        net_amount=D("-4000.00"),
        adjusts_line=target,
        adjustment_reason="VOID",
        is_waivable=False,
    ).save(force_insert=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"line_type": "CREDIT"},
        {"fee_type": "PARKING"},
        {"fee_schedule": None},  # required iff FEE
        {"quantity": D("0")},
        {"unit_amount": D("-1")},
        {"waiver_amount": D("-1")},
        {"waiver_amount": D("10")},  # reason required iff waiver > 0
        {"waiver_reason": "HARDSHIP"},  # reason without a waiver
        {"waiver_amount": D("10"), "waiver_reason": "FRIEND"},
        {"net_amount": D("-0.01")},  # FEE net ≥ 0
        {"adjustment_reason": "VOID"},  # only on ADJUSTMENT lines
        {"fee_type": "LATE_FEE"},  # LATE_FEE fee type only on LATE_FEE lines
    ],
)
def test_invoice_line_fee_constraints(invoice, tech, overrides):
    _rejects(_line(invoice, tech, **overrides))


def test_waiver_only_on_waivable_fee_lines(invoice, fee_setup):
    tuition = fee_setup[("TUITION", "DOMESTIC")]
    _rejects(
        _line(
            invoice,
            tuition,
            fee_type="TUITION",
            is_waivable=False,
            is_scholarship_eligible=True,
            is_refundable=True,
            allocation_priority=10,
            waiver_amount=D("10"),
            waiver_reason="HARDSHIP",
            net_amount=D("3990"),
        )
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"fee_schedule": "fee"},  # LATE_FEE has no schedule
        {"allocation_priority": 40},  # LATE_FEE priority is 90
        {"is_refundable": True},  # flags false unless FEE
        {"fee_type": "TECHNOLOGY"},
        {"waiver_amount": D("1"), "waiver_reason": "HARDSHIP"},
    ],
)
def test_late_fee_line_constraints(invoice, tech, overrides):
    if overrides.get("fee_schedule") == "fee":
        overrides = {**overrides, "fee_schedule": tech}
    base = dict(
        line_type="LATE_FEE",
        fee_type="LATE_FEE",
        is_waivable=False,
        allocation_priority=90,
        fee_schedule=None,
    )
    _rejects(_line(invoice, None, **{**base, **overrides}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"net_amount": D("0")},  # ADJUSTMENT net < 0
        {"net_amount": D("1")},
        {"adjusts_line": None},
        {"adjustment_reason": None},
        {"adjustment_reason": "GOODWILL"},
    ],
)
def test_adjustment_line_constraints(invoice, overrides):
    target = invoice.lines.get(fee_type="TECHNOLOGY")
    base = dict(
        line_type="ADJUSTMENT",
        fee_schedule=None,
        unit_amount=D("-1"),
        gross_amount=D("-1"),
        net_amount=D("-1"),
        adjusts_line=target,
        adjustment_reason="VOID",
        is_waivable=False,
    )
    _rejects(_line(invoice, None, **{**base, **overrides}))


def test_invoice_lines_and_schedules_are_protected(invoice):
    with pytest.raises(ProtectedError):
        invoice.lines.first().fee_schedule.delete()
    with pytest.raises(ProtectedError):
        invoice.delete()


# --- InvoiceEvent ----------------------------------------------------------------------------


def test_invoice_event_dimension_enum(invoice):
    _rejects(
        InvoiceEvent(
            event_id="EVT-INV-2026-0009999",
            invoice=invoice,
            dimension="PAYMENT",
            to_status="UNPAID",
            event_date=date(2026, 8, 17),
            reason="X",
            actor="t",
        )
    )


# --- LedgerEntry -----------------------------------------------------------------------------


def _entry(line, **kw):
    fields = dict(
        entry_id="LED-2026-0099999",
        student_id=line.invoice.student_id,
        entry_date=line.posted_date,
        entry_type="CHARGE",
        amount=D("1.00"),
        invoice_line=line,
    )
    fields.update(kw)
    return LedgerEntry(**fields)


@pytest.fixture
def free_line(invoice, tech):
    line = _line(invoice, tech)
    line.save(force_insert=True)
    return line


def test_ledger_entry_valid_and_zero_charge(free_line):
    _entry(free_line, amount=D("0.00")).save(force_insert=True)  # fully waived line: CHARGE 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"entry_type": "CHARGE", "amount": D("-0.01")},
        {"entry_type": "LATE_FEE", "amount": D("0")},
        {"entry_type": "ADJUSTMENT", "amount": D("0")},
        {"entry_type": "ADJUSTMENT", "amount": D("1")},
        {"entry_type": "PAYMENT", "amount": D("-1")},  # Phase 4 type: not allowed yet (D1)
        # Phase 3: a scholarship type needs a scholarship-allocation source, not a line
        {"entry_type": "SCHOLARSHIP_CREDIT", "amount": D("-1")},
        {"invoice_line": None},  # exactly one source
    ],
)
def test_ledger_entry_constraints(free_line, overrides):
    _rejects(_entry(free_line, **overrides))


def test_ledger_entry_source_is_unique(invoice):
    line = invoice.lines.first()  # already posted by generate_invoice
    _rejects(_entry(line))
