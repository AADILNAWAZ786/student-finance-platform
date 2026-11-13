"""billing.services invoices: generation (BR-FEE, BR-WAV-01, BR-INV-01..03/07, D2, D11),
void (BR-INV-08, D15), late fees (BR-LATE-01, D8), collection refresh (BR-INV-07),
ledger posting (BR-LED-01), student balance (BR-BAL-01/02, D13) and atomicity (§8.4)."""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from django.core.exceptions import ValidationError

from academics import services as academics
from billing import calculators as calc
from billing import services
from billing.models import FeeSchedule, Invoice, InvoiceEvent, InvoiceLine, LedgerEntry
from core.exceptions import BusinessRuleViolation, StateConflict
from core.models import IdSequence
from tests.billing_fixtures import DOMESTIC_16_TOTAL, FALL_PRICES, make_fees

pytestmark = pytest.mark.django_db

FALL_INVOICE, FALL_DUE = date(2026, 8, 17), date(2026, 9, 14)
LATE_FEE_DAY = date(2026, 9, 29)  # due 14 Sep + 14 grace days, posted the next day


def _counts():
    return tuple(m.objects.count() for m in (Invoice, InvoiceLine, InvoiceEvent, LedgerEntry))


def _events(inv, dimension):
    return [
        (e.from_status, e.to_status, e.event_date, e.reason)
        for e in inv.events.filter(dimension=dimension).order_by("event_date", "event_id")
    ]


# --- generation ------------------------------------------------------------------------------


def test_generate_domestic_lab_program_invoice(invoice):
    inv = invoice
    assert inv.invoice_id == "INV-STU-000001-2026-000001"
    assert (inv.issue_date, inv.due_date) == (FALL_INVOICE, FALL_DUE)
    assert (inv.lifecycle_status, inv.collection_status) == ("ISSUED", "UNPAID")
    assert inv.collection_status_as_of == FALL_INVOICE
    assert inv.total_amount == DOMESTIC_16_TOTAL
    lines = list(inv.lines.order_by("line_id"))
    assert [
        (x.line_id[-3:], x.fee_type, x.quantity, x.unit_amount, x.gross_amount) for x in lines
    ] == [
        ("L01", "TUITION", D("16"), D("9000.00"), D("144000.00")),
        ("L02", "LAB", D("1"), D("6000.00"), D("6000.00")),
        ("L03", "REGISTRATION", D("1"), D("5000.00"), D("5000.00")),
        ("L04", "TECHNOLOGY", D("1"), D("4000.00"), D("4000.00")),
        ("L05", "LIBRARY", D("1"), D("2000.00"), D("2000.00")),
        ("L06", "STUDENT_ACTIVITY", D("1"), D("3000.00"), D("3000.00")),
    ]
    assert all(x.line_type == "FEE" and x.posted_date == FALL_INVOICE for x in lines)
    assert all(x.net_amount == x.gross_amount and x.waiver_amount == 0 for x in lines)
    assert lines[0].fee_schedule_id == "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
    assert [x.allocation_priority for x in lines] == [10, 20, 30, 40, 50, 60]
    assert [(x.is_scholarship_eligible, x.is_refundable, x.is_waivable) for x in lines[:4]] == [
        (True, True, False),
        (True, True, False),
        (False, False, False),
        (False, False, True),
    ]
    assert _events(inv, "LIFECYCLE") == [(None, "ISSUED", FALL_INVOICE, "CREATED")]
    assert _events(inv, "COLLECTION") == [(None, "UNPAID", FALL_INVOICE, "CREATED")]


def test_BR_LED_01_one_charge_entry_per_line(invoice):
    entries = list(LedgerEntry.objects.order_by("entry_id"))
    assert [e.entry_id for e in entries] == [f"LED-2026-{n:07d}" for n in range(1, 7)]
    by_line = {e.invoice_line_id: e for e in entries}
    for line in invoice.lines.all():
        e = by_line[line.line_id]
        assert (e.entry_type, e.amount, e.entry_date) == (
            "CHARGE",
            line.net_amount,
            line.posted_date,
        )
        assert e.student_id == invoice.student_id
    assert calc.ledger_balance(entries, None) == DOMESTIC_16_TOTAL


def test_international_student_gets_intl_tuition_and_service_fee(
    make_student, fee_setup, enroll_fall
):
    s = make_student(residency="INTERNATIONAL")
    inv = services.generate_invoice(enrollment=enroll_fall(s, credits=12), actor="t")
    lines = {x.fee_type: x for x in inv.lines.all()}
    assert lines["TUITION"].fee_schedule_id.endswith("TUITION-INTL-V1")
    assert lines["TUITION"].gross_amount == D("172800.00")  # 12 × 14,400
    assert lines["INTERNATIONAL_SERVICE"].gross_amount == D("8000.00")
    assert lines["INTERNATIONAL_SERVICE"].allocation_priority == 70


def test_domestic_student_never_gets_international_service_fee(invoice):
    assert not invoice.lines.filter(fee_type="INTERNATIONAL_SERVICE").exists()


def test_BR_FEE_04_no_lab_line_when_program_has_no_lab(
    make_program, make_student, terms, enroll_fall
):
    prog = make_program(code="BA-ECO", has_lab=False)
    make_fees(prog, terms["TERM-2026-FALL"])  # a LAB schedule exists but must not be charged
    inv = services.generate_invoice(enrollment=enroll_fall(make_student(program=prog)), actor="t")
    assert not inv.lines.filter(fee_type="LAB").exists()


def test_D2_optional_fee_types_charged_only_when_a_version_exists(
    program, terms, student, enroll_fall
):
    make_fees(
        program,
        terms["TERM-2026-FALL"],
        [
            ("TUITION", "DOMESTIC", "PER_CREDIT", "9000.00"),
            ("LAB", "ALL", "FLAT", "6000.00"),
            ("REGISTRATION", "ALL", "FLAT", "5000.00"),
        ],
    )
    inv = services.generate_invoice(enrollment=enroll_fall(student), actor="t")
    assert sorted(inv.lines.values_list("fee_type", flat=True)) == [
        "LAB",
        "REGISTRATION",
        "TUITION",
    ]
    assert inv.total_amount == D("155000.00")


@pytest.mark.parametrize(
    ("residency", "drop", "missing"),
    [
        ("DOMESTIC", "TUITION", ["TUITION"]),
        ("DOMESTIC", "REGISTRATION", ["REGISTRATION"]),
        ("DOMESTIC", "LAB", ["LAB"]),
        ("INTERNATIONAL", "INTERNATIONAL_SERVICE", ["INTERNATIONAL_SERVICE"]),
    ],
)
def test_D2_missing_required_fee_type_is_422(
    make_student, program, terms, enroll_fall, residency, drop, missing
):
    make_fees(program, terms["TERM-2026-FALL"], [p for p in FALL_PRICES if p[0] != drop])
    enr = enroll_fall(make_student(residency=residency))
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        services.generate_invoice(enrollment=enr, actor="t")
    assert exc.value.code == "FEE_SCHEDULE_NOT_FOUND" and exc.value.rule_id == "BR-FEE-01"
    assert exc.value.details["missing_fee_types"] == missing
    assert exc.value.details["on_date"] == "2026-08-17"
    assert _counts() == before


def test_version_not_yet_in_force_on_invoice_date_is_422(program, terms, student, enroll_fall):
    make_fees(program, terms["TERM-2026-FALL"], effective_from=date(2026, 8, 18))
    with pytest.raises(BusinessRuleViolation) as exc:
        services.generate_invoice(enrollment=enroll_fall(student), actor="t")
    assert exc.value.details["missing_fee_types"] == ["TUITION", "LAB", "REGISTRATION"]


def test_BR_FEE_01_v2_in_force_on_invoice_date_is_used(fee_setup, fall_enrollment):
    v1 = fee_setup[("TUITION", "DOMESTIC")]
    v2 = services.new_fee_schedule_version(v1, amount=D("9360.00"), effective_from=date(2026, 8, 1))
    inv = services.generate_invoice(enrollment=fall_enrollment, actor="t")
    tuition = inv.lines.get(fee_type="TUITION")
    assert tuition.fee_schedule_id == v2.pk and tuition.gross_amount == D("149760.00")


def test_BR_FEE_01_version_starting_after_invoice_date_is_not_used(fee_setup, student, enroll_fall):
    v1 = fee_setup[("TUITION", "DOMESTIC")]
    services.new_fee_schedule_version(v1, amount=D("9999.00"), effective_from=date(2026, 8, 18))
    enr = enroll_fall(student, enrolled_on=date(2026, 9, 1))  # late enrolment
    inv = services.generate_invoice(enrollment=enr, actor="t")
    assert inv.issue_date == date(2026, 9, 1)  # BR-INV-03: enrolled_on is later
    assert inv.lines.get(fee_type="TUITION").fee_schedule_id == v1.pk


def test_BR_INV_03_issue_date_is_enrolled_on_when_later(fee_setup, student, enroll_fall):
    inv = services.generate_invoice(
        enrollment=enroll_fall(student, enrolled_on=date(2026, 9, 14)), actor="t"
    )
    assert (inv.issue_date, inv.due_date) == (date(2026, 9, 14), FALL_DUE)
    assert set(inv.lines.values_list("posted_date", flat=True)) == {date(2026, 9, 14)}


def test_BR_INV_01_second_generation_is_409_and_writes_nothing(invoice, fall_enrollment):
    before = _counts()
    seq = list(IdSequence.objects.values_list("key", "last_value"))
    with pytest.raises(StateConflict) as exc:
        services.generate_invoice(enrollment=fall_enrollment, actor="t")
    assert exc.value.code == "DUPLICATE_INVOICE" and exc.value.rule_id == "BR-INV-01"
    assert exc.value.details == {
        "enrollment_id": fall_enrollment.enrollment_id,
        "existing_invoice_id": invoice.invoice_id,
    }
    assert _counts() == before
    assert list(IdSequence.objects.values_list("key", "last_value")) == seq


def test_BR_INV_01_regeneration_allowed_after_void(invoice, fall_enrollment):
    services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    again = services.generate_invoice(enrollment=fall_enrollment, actor="t")
    assert again.invoice_id == "INV-STU-000001-2026-000002"
    assert again.lifecycle_status == "ISSUED"


def test_D11_only_enrolled_enrollments_can_be_invoiced(fee_setup, fall_enrollment):
    academics.complete_enrollment(fall_enrollment, on_date=date(2026, 12, 20), actor="t")
    with pytest.raises(StateConflict) as exc:
        services.generate_invoice(enrollment=fall_enrollment, actor="t")
    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert exc.value.details == {
        "entity": "Enrollment",
        "record_id": fall_enrollment.enrollment_id,
        "status": "COMPLETED",
        "required_status": "ENROLLED",
    }
    assert Invoice.objects.count() == 0


# --- waivers (BR-WAV-01) -----------------------------------------------------------------------


def test_waiver_reduces_one_line(fee_setup, fall_enrollment):
    inv = services.generate_invoice(
        enrollment=fall_enrollment,
        waivers=[{"fee_type": "TECHNOLOGY", "amount": D("1500.00"), "reason": "HARDSHIP"}],
        actor="t",
    )
    tech = inv.lines.get(fee_type="TECHNOLOGY")
    assert (tech.gross_amount, tech.waiver_amount, tech.waiver_reason, tech.net_amount) == (
        D("4000.00"),
        D("1500.00"),
        "HARDSHIP",
        D("2500.00"),
    )
    assert inv.total_amount == DOMESTIC_16_TOTAL - D("1500.00")
    assert tech.ledger_entry.amount == D("2500.00")


def test_fully_waived_line_has_net_zero_and_a_zero_charge(fee_setup, fall_enrollment):
    inv = services.generate_invoice(
        enrollment=fall_enrollment,
        waivers=[
            {"fee_type": "STUDENT_ACTIVITY", "amount": D("3000.00"), "reason": "PG_ACTIVITY_EXEMPT"}
        ],
        actor="t",
    )
    line = inv.lines.get(fee_type="STUDENT_ACTIVITY")
    assert line.net_amount == 0
    assert (line.ledger_entry.entry_type, line.ledger_entry.amount) == ("CHARGE", D("0.00"))


@pytest.mark.parametrize(
    ("waiver", "message"),
    [
        (
            {"fee_type": "TUITION", "amount": D("1"), "reason": "HARDSHIP"},
            "not a waivable fee type",
        ),
        (
            {"fee_type": "REGISTRATION", "amount": D("1"), "reason": "HARDSHIP"},
            "not a waivable fee type",
        ),
        (
            {"fee_type": "TECHNOLOGY", "amount": D("4000.01"), "reason": "HARDSHIP"},
            "exceeds the line gross",
        ),
        ({"fee_type": "TECHNOLOGY", "amount": D("100")}, "reason is required"),
        ({"fee_type": "TECHNOLOGY", "amount": D("0"), "reason": "HARDSHIP"}, "must be omitted"),
        (
            {"fee_type": "TECHNOLOGY", "amount": D("100"), "reason": "FRIENDLY"},
            "reason must be one of",
        ),
        (
            {"fee_type": "TECHNOLOGY", "amount": D("-1"), "reason": "HARDSHIP"},
            "must not be negative",
        ),
    ],
)
def test_invalid_waiver_is_400_and_writes_nothing(fee_setup, fall_enrollment, waiver, message):
    before = _counts()
    with pytest.raises(ValidationError) as exc:
        services.generate_invoice(enrollment=fall_enrollment, waivers=[waiver], actor="t")
    assert message in exc.value.message_dict["waivers"][0]
    assert _counts() == before


def test_waiver_for_a_fee_type_not_on_the_invoice(program, terms, student, enroll_fall):
    make_fees(program, terms["TERM-2026-FALL"], [p for p in FALL_PRICES if p[0] != "LIBRARY"])
    with pytest.raises(ValidationError) as exc:
        services.generate_invoice(
            enrollment=enroll_fall(student),
            waivers=[{"fee_type": "LIBRARY", "amount": D("1"), "reason": "HARDSHIP"}],
            actor="t",
        )
    assert "not charged on the invoice" in exc.value.message_dict["waivers"][0]


def test_one_waiver_per_fee_type(fee_setup, fall_enrollment):
    w = {"fee_type": "LIBRARY", "amount": D("1"), "reason": "HARDSHIP"}
    with pytest.raises(ValidationError) as exc:
        services.generate_invoice(enrollment=fall_enrollment, waivers=[w, w], actor="t")
    assert exc.value.message_dict["waivers"] == [
        "waivers[1] (LIBRARY): only one waiver per fee type."
    ]


def test_a_zero_waiver_without_reason_changes_nothing(fee_setup, fall_enrollment):
    inv = services.generate_invoice(
        enrollment=fall_enrollment, waivers=[{"fee_type": "LIBRARY", "amount": D("0")}], actor="t"
    )
    assert inv.total_amount == DOMESTIC_16_TOTAL
    assert not inv.lines.filter(waiver_amount__gt=0).exists()


def test_zero_priced_invoice_is_adjusted_to_zero_at_issue(program, terms, student, enroll_fall):
    make_fees(
        program,
        terms["TERM-2026-FALL"],
        [
            ("TUITION", "DOMESTIC", "PER_CREDIT", "0.00"),
            ("LAB", "ALL", "FLAT", "0.00"),
            ("REGISTRATION", "ALL", "FLAT", "0.00"),
        ],
    )
    inv = services.generate_invoice(enrollment=enroll_fall(student), actor="t")
    assert inv.total_amount == 0 and inv.collection_status == "ADJUSTED_TO_ZERO"
    assert _events(inv, "COLLECTION") == [
        (None, "UNPAID", FALL_INVOICE, "CREATED"),
        ("UNPAID", "ADJUSTED_TO_ZERO", FALL_INVOICE, "RECALCULATED"),
    ]


# --- atomicity (ARCHITECTURE §8.4) ---------------------------------------------------------------


def test_generation_rolls_back_completely_when_a_ledger_post_fails(
    fee_setup, fall_enrollment, monkeypatch
):
    real, calls = services.post_ledger, []

    def flaky(line):
        calls.append(line.line_id)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(line)

    monkeypatch.setattr(services, "post_ledger", flaky)
    seq_before = list(IdSequence.objects.values_list("key", "last_value"))
    with pytest.raises(RuntimeError):
        services.generate_invoice(enrollment=fall_enrollment, actor="t")
    assert _counts() == (0, 0, 0, 0)
    assert list(IdSequence.objects.values_list("key", "last_value")) == seq_before
    monkeypatch.setattr(services, "post_ledger", real)
    assert services.generate_invoice(enrollment=fall_enrollment, actor="t").invoice_id.endswith(
        "000001"
    )


def test_void_rolls_back_when_a_ledger_post_fails(invoice, monkeypatch):
    before = _counts()

    def boom(line):
        raise RuntimeError("fail")

    monkeypatch.setattr(services, "post_ledger", boom)
    with pytest.raises(RuntimeError):
        services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "ISSUED" and _counts() == before


# --- void (BR-INV-08, D15) ---------------------------------------------------------------------


def test_void_posts_minus_net_adjustments_and_zeroes_the_ledger(fee_setup, fall_enrollment):
    inv = services.generate_invoice(
        enrollment=fall_enrollment,
        waivers=[{"fee_type": "STUDENT_ACTIVITY", "amount": D("3000.00"), "reason": "HARDSHIP"}],
        actor="t",
    )
    services.void_invoice(inv, void_date=date(2026, 9, 1), actor="ops")
    inv.refresh_from_db()
    assert (inv.lifecycle_status, inv.collection_status) == ("VOID", "ADJUSTED_TO_ZERO")
    assert inv.collection_status_as_of == date(2026, 9, 1)
    adjustments = list(inv.lines.filter(line_type="ADJUSTMENT").order_by("line_id"))
    # the fully waived STUDENT_ACTIVITY line (net 0) gets no adjustment
    assert [(a.line_id[-3:], a.adjusts_line_id[-3:], a.net_amount) for a in adjustments] == [
        ("L07", "L01", D("-144000.00")),
        ("L08", "L02", D("-6000.00")),
        ("L09", "L03", D("-5000.00")),
        ("L10", "L04", D("-4000.00")),
        ("L11", "L05", D("-2000.00")),
    ]
    assert all(
        a.adjustment_reason == "VOID" and a.posted_date == date(2026, 9, 1) for a in adjustments
    )
    assert all(a.ledger_entry.entry_type == "ADJUSTMENT" for a in adjustments)
    assert calc.ledger_balance(LedgerEntry.objects.all(), None) == 0
    assert inv.total_amount == D("161000.00")  # immutable (BR-INV-02)
    assert _events(inv, "LIFECYCLE")[-1] == ("ISSUED", "VOID", date(2026, 9, 1), "VOIDED")
    assert _events(inv, "COLLECTION")[-1] == (
        "UNPAID",
        "ADJUSTED_TO_ZERO",
        date(2026, 9, 1),
        "INVOICE_VOIDED",
    )
    assert set(inv.events.values_list("actor", flat=True)) == {"t", "ops"}


def test_void_twice_is_illegal_transition(invoice):
    services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        services.void_invoice(invoice, void_date=date(2026, 8, 21), actor="t")
    assert exc.value.code == "ILLEGAL_TRANSITION"
    assert exc.value.details["from_status"] == "VOID" and exc.value.rule_id == "BR-INV-08"
    assert _counts() == before


def test_cancelled_invoice_cannot_be_voided(invoice):
    Invoice.objects.filter(pk=invoice.pk).update(lifecycle_status="CANCELLED")  # Phase 5 path
    with pytest.raises(StateConflict) as exc:
        services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    assert exc.value.code == "ILLEGAL_TRANSITION"


@pytest.mark.parametrize(
    ("void_date", "message"),
    [
        (date(2026, 8, 16), "on or after the invoice issue date (2026-08-17)"),
        (date(2027, 1, 1), "business date between 2026-01-01 and 2026-12-31"),
        (date(2025, 12, 31), "business date between 2026-01-01 and 2026-12-31"),
    ],
)
def test_D15_void_date_rules(invoice, void_date, message):
    with pytest.raises(ValidationError) as exc:
        services.void_invoice(invoice, void_date=void_date, actor="t")
    assert message in exc.value.message_dict["void_date"][0]
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "ISSUED"


def test_D15_void_on_the_issue_date_is_allowed(invoice):
    services.void_invoice(invoice, void_date=FALL_INVOICE, actor="t")
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "VOID"


def test_void_date_cannot_precede_a_later_late_fee(invoice):
    services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    with pytest.raises(ValidationError) as exc:
        services.void_invoice(invoice, void_date=date(2026, 9, 20), actor="t")
    assert "on or after 2026-09-29" in exc.value.message_dict["void_date"][0]


def test_void_after_late_fee_also_reverses_the_late_fee(invoice):
    services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    services.void_invoice(invoice, void_date=date(2026, 10, 1), actor="t")
    late = invoice.lines.get(line_type="LATE_FEE")
    assert late.adjustments.get().net_amount == -late.net_amount
    invoice.refresh_from_db()
    assert invoice.collection_status == "ADJUSTED_TO_ZERO"
    assert calc.ledger_balance(LedgerEntry.objects.all(), None) == 0


def test_BR_INV_08_current_allocations_block_void(invoice, monkeypatch):
    # Phases 3–4 supply real allocations through this single seam
    monkeypatch.setattr(services, "allocations_for", lambda inv: ([NS(amount=D("1000.00"))], []))
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    assert exc.value.code == "INVOICE_HAS_ALLOCATIONS" and exc.value.rule_id == "BR-INV-08"
    assert exc.value.details == {
        "invoice_id": invoice.invoice_id,
        "scholarship_applied": "1000.00",
        "amount_paid": "0.00",
    }
    assert _counts() == before


def test_BR_INV_08_payment_applied_blocks_void(invoice, monkeypatch):
    monkeypatch.setattr(
        services, "allocations_for", lambda inv: ([], [NS(amount=D("0.01"), payer_type="SPONSOR")])
    )
    with pytest.raises(StateConflict) as exc:
        services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    assert exc.value.details["amount_paid"] == "0.01"


def test_BR_INV_08_fully_reversed_allocations_do_not_block_void(invoice, monkeypatch):
    """Not a permanent "ever allocated" prohibition: the current signed totals decide."""
    monkeypatch.setattr(
        services,
        "allocations_for",
        lambda inv: (
            [NS(amount=D("5000.00")), NS(amount=D("-5000.00"))],
            [NS(amount=D("200.00"), payer_type="SELF"), NS(amount=D("-200.00"), payer_type="SELF")],
        ),
    )
    services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "VOID"


# --- late fees (BR-LATE-01, D8) -------------------------------------------------------------------


def test_no_late_fee_on_the_last_grace_day(invoice):
    r = services.assess_late_fees(as_of=date(2026, 9, 28), dry_run=False, actor="t")
    assert (r["examined"], r["assessed_count"]) == (0, 0)
    assert not InvoiceLine.objects.filter(line_type="LATE_FEE").exists()


def test_late_fee_posted_the_day_after_grace(invoice):
    r = services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="ops")
    assert (r["examined"], r["assessed_count"], r["total_late_fees"]) == (1, 1, D("3280.00"))
    line = invoice.lines.get(line_type="LATE_FEE")
    assert (line.fee_type, line.net_amount, line.posted_date) == (
        "LATE_FEE",
        D("3280.00"),
        LATE_FEE_DAY,
    )
    assert (line.allocation_priority, line.fee_schedule_id) == (90, None)
    assert (line.is_scholarship_eligible, line.is_refundable, line.is_waivable) == (
        False,
        False,
        False,
    )
    assert (line.ledger_entry.entry_type, line.ledger_entry.amount) == ("LATE_FEE", D("3280.00"))
    assert r["assessed"][0].line_id == line.line_id
    invoice.refresh_from_db()
    assert (invoice.collection_status, invoice.collection_status_as_of) == ("OVERDUE", LATE_FEE_DAY)
    assert _events(invoice, "COLLECTION")[-1] == (
        "UNPAID",
        "OVERDUE",
        LATE_FEE_DAY,
        "LATE_FEE_POSTED",
    )


def test_late_fee_dry_run_writes_nothing(invoice):
    before = _counts()
    seq = list(IdSequence.objects.values_list("key", "last_value"))
    r = services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=True, actor="t")
    assert r["dry_run"] is True and r["assessed_count"] == 1
    assert r["assessed"][0].late_fee == D("3280.00") and r["assessed"][0].line_id is None
    assert _counts() == before
    assert list(IdSequence.objects.values_list("key", "last_value")) == seq


def test_late_fee_once_per_invoice(invoice):
    services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    r = services.assess_late_fees(as_of=date(2026, 12, 31), dry_run=False, actor="t")
    assert r["assessed_count"] == 0
    assert invoice.lines.filter(line_type="LATE_FEE").count() == 1


def test_late_fee_does_not_depend_on_when_the_batch_runs(invoice):
    services.refresh_collection_status(as_of=date(2026, 11, 1), actor="t")
    services.assess_late_fees(as_of=date(2026, 12, 31), dry_run=False, actor="t")
    line = invoice.lines.get(line_type="LATE_FEE")
    assert (line.posted_date, line.net_amount) == (LATE_FEE_DAY, D("3280.00"))
    invoice.refresh_from_db()
    assert invoice.collection_status_as_of == date(2026, 11, 1)  # never moved back


def _invoice_with_tuition(
    make_student, program, terms, enroll_fall, tuition_per_credit, credits=12
):
    term = terms["TERM-2026-FALL"]
    if not FeeSchedule.objects.filter(fee_type="REGISTRATION").exists():
        make_fees(
            program,
            term,
            [("LAB", "ALL", "FLAT", "0.00"), ("REGISTRATION", "ALL", "FLAT", "0.00")],
        )
    fs = FeeSchedule.objects.filter(fee_type="TUITION", residency="DOMESTIC").first()
    if fs is None:
        make_fees(program, term, [("TUITION", "DOMESTIC", "PER_CREDIT", tuition_per_credit)])
    return services.generate_invoice(
        enrollment=enroll_fall(make_student(), credits=credits), actor="t"
    )


@pytest.mark.parametrize(
    ("per_credit", "expected"),
    [
        ("100.00", "500.00"),  # 1,200 outstanding → 24 → minimum 500
        ("2083.33", "500.00"),  # 24,999.96 → 499.9992 → 500 (just under)
        ("2083.34", "500.00"),  # 25,000.08 → 500.0016 → 500.00 exactly at the minimum
        ("2500.00", "600.00"),  # 30,000 → 600 (between the limits)
        ("20833.33", "5000.00"),  # 249,999.96 → 4,999.9992 → 5,000.00
        ("20900.00", "5000.00"),  # 250,800 → 5,016 → maximum 5,000
    ],
)
def test_late_fee_minimum_and_maximum(
    make_student, program, terms, enroll_fall, per_credit, expected
):
    inv = _invoice_with_tuition(make_student, program, terms, enroll_fall, per_credit)
    services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    assert inv.lines.get(line_type="LATE_FEE").net_amount == D(expected)


def test_no_late_fee_for_void_or_fully_settled_invoices(
    invoice, program, terms, make_student, enroll_fall
):
    services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    r = services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    assert (r["examined"], r["assessed_count"]) == (0, 0)


def test_no_late_fee_when_nothing_is_outstanding(program, terms, student, enroll_fall):
    make_fees(
        program,
        terms["TERM-2026-FALL"],
        [
            ("TUITION", "DOMESTIC", "PER_CREDIT", "0.00"),
            ("LAB", "ALL", "FLAT", "0.00"),
            ("REGISTRATION", "ALL", "FLAT", "0.00"),
        ],
    )
    services.generate_invoice(enrollment=enroll_fall(student), actor="t")
    r = services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    assert (r["examined"], r["assessed_count"]) == (1, 0)


@pytest.mark.parametrize("bad", [date(2025, 12, 31), date(2027, 1, 1)])
def test_late_fee_as_of_must_be_a_business_date(bad):
    with pytest.raises(ValidationError) as exc:
        services.assess_late_fees(as_of=bad, dry_run=True, actor="t")
    assert "as_of_date" in exc.value.message_dict


# --- collection status refresh (BR-INV-07) --------------------------------------------------------


def test_refresh_on_due_date_keeps_unpaid_then_overdue_the_day_after(invoice):
    r = services.refresh_collection_status(as_of=FALL_DUE, actor="t")
    assert (r["examined"], r["changed"], r["unchanged"]) == (1, 0, 1)
    invoice.refresh_from_db()
    assert (invoice.collection_status, invoice.collection_status_as_of) == ("UNPAID", FALL_DUE)
    r = services.refresh_collection_status(as_of=date(2026, 9, 15), actor="t")
    assert r["changes"] == [
        {"invoice_id": invoice.invoice_id, "from_status": "UNPAID", "to_status": "OVERDUE"}
    ]
    assert _events(invoice, "COLLECTION")[-1] == (
        "UNPAID",
        "OVERDUE",
        date(2026, 9, 15),
        "DUE_DATE_PASSED",
    )


def test_refresh_never_moves_a_status_back_in_time(invoice):
    services.refresh_collection_status(as_of=date(2026, 10, 1), actor="t")
    r = services.refresh_collection_status(as_of=date(2026, 9, 1), actor="t")
    assert (r["examined"], r["skipped_later_as_of"]) == (0, 1)
    invoice.refresh_from_db()
    assert (invoice.collection_status, invoice.collection_status_as_of) == (
        "OVERDUE",
        date(2026, 10, 1),
    )


def test_refresh_skips_void_invoices(invoice):
    services.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    r = services.refresh_collection_status(as_of=date(2026, 12, 31), actor="t")
    assert r["examined"] == 0
    invoice.refresh_from_db()
    assert (invoice.collection_status, invoice.collection_status_as_of) == (
        "ADJUSTED_TO_ZERO",
        date(2026, 8, 20),
    )


def test_refresh_without_change_writes_no_event(invoice):
    n = invoice.events.count()
    services.refresh_collection_status(as_of=date(2026, 9, 1), actor="t")
    assert invoice.events.count() == n


# --- student balance (BR-BAL-01/02, D13) ----------------------------------------------------------


def test_balance_is_the_ledger_sum_and_identity_holds(invoice):
    b = services.student_balance(invoice.student, None)
    assert b["ledger_balance"] == DOMESTIC_16_TOTAL == b["invoice_outstanding"]
    assert b["breakdown"]["charges"] == DOMESTIC_16_TOTAL
    assert b["identity"]["holds"] is True
    services.assess_late_fees(as_of=LATE_FEE_DAY, dry_run=False, actor="t")
    b = services.student_balance(invoice.student, date(2026, 9, 28))
    assert b["ledger_balance"] == DOMESTIC_16_TOTAL and b["breakdown"]["late_fees"] == 0
    b = services.student_balance(invoice.student, LATE_FEE_DAY)
    assert b["ledger_balance"] == D("167280.00") and b["identity"]["holds"] is True


def test_balance_before_issue_date_is_zero(invoice):
    b = services.student_balance(invoice.student, date(2026, 8, 16))
    assert (b["ledger_balance"], b["invoice_outstanding"], b["identity"]["holds"]) == (0, 0, True)


def test_balance_after_void_is_zero_and_identity_holds(invoice):
    services.void_invoice(invoice, void_date=date(2026, 9, 1), actor="t")
    b = services.student_balance(invoice.student, None)
    assert b["ledger_balance"] == 0 and b["breakdown"]["adjustments"] == -DOMESTIC_16_TOTAL
    assert b["identity"]["holds"] is True
    mid = services.student_balance(invoice.student, date(2026, 8, 31))
    assert mid["ledger_balance"] == DOMESTIC_16_TOTAL and mid["identity"]["holds"] is True


def test_D13_api_balance_uses_the_single_ledger_balance_function(invoice, monkeypatch):
    seen = []
    real = calc.ledger_balance

    def spy(entries, as_of):
        seen.append(as_of)
        return real(entries, as_of)

    monkeypatch.setattr(calc, "ledger_balance", spy)
    services.student_balance(invoice.student, date(2026, 9, 1))
    assert seen == [date(2026, 9, 1)]
