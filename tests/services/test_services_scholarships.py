"""scholarships.services: awards (BR-SCH-02/03/05/08), application (BR-SCH-04/06/08,
BR-ALC-02/06), reversal (BR-SCH-07 building block), ledger (BR-LED-01) and invoice refresh.

The four money limits are tested separately, each one proved to bite on its own:
  1. per-term cap on *awards* (D-S5)              → test_D_S5_*
  2. award-stage reservation guardrail (not BR-SCH-04) → test_reservation_guardrail_*
  3. BR-SCH-04 on *actual allocations* at apply    → test_BR_SCH_04_*
  4. each line's remaining room, with adjustments  → test_line_room_*
"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest
from django.core.exceptions import ValidationError

from billing import allocations as registry
from billing import calculators as billing_calc
from billing import services as billing
from billing.allocations import PaymentReleaseUnavailable
from billing.models import Invoice, InvoiceLine, LedgerEntry
from core.exceptions import BusinessRuleViolation, StateConflict
from core.models import IdSequence
from scholarships import services
from scholarships.models import Scholarship, ScholarshipAllocation, ScholarshipEvent
from tests.billing_fixtures import make_fees

pytestmark = pytest.mark.django_db

ISSUE = date(2026, 8, 17)


def _counts():
    return tuple(
        m.objects.count()
        for m in (Scholarship, ScholarshipAllocation, ScholarshipEvent, LedgerEntry)
    )


def _award(elig, inv, **kw):
    return services.award_scholarship(eligibility=elig, invoice=inv, actor="t", **kw)


def _events(s):
    return [
        (e.from_status, e.to_status, e.event_date, e.reason)
        for e in s.events.order_by("event_date", "event_id")
    ]


# --- awards -------------------------------------------------------------------------------------


def test_award_merit_on_the_eligible_base(merit_award, invoice):
    a = merit_award
    assert (a.scholarship_id, a.status, a.awarded_amount) == (
        "SCH-2026-000001",
        "OFFERED",
        D("75000.00"),
    )
    assert (a.invoice_id, a.term_id, a.student_id) == (invoice.pk, "TERM-2026-FALL", "STU-000001")
    assert a.award_date == ISSUE  # max(evaluated_on, issue_date)
    assert _events(a) == [(None, "OFFERED", ISSUE, "AWARDED")]
    assert not a.allocations.exists()  # an award is not an allocation


def test_award_date_defaults_to_a_later_issue_date(
    scholarship_types, student_with_invoice, evaluate
):
    s, inv = student_with_invoice(enrolled_on=date(2026, 9, 1))
    a = _award(evaluate(s)["SCT-001"], inv)
    assert a.award_date == date(2026, 9, 1)


@pytest.mark.parametrize(
    ("award_date", "message"),
    [
        (date(2026, 8, 16), "on or after 2026-08-17 (evaluated_on)"),
        (date(2027, 1, 1), "business date between 2026-01-01 and 2026-12-31"),
    ],
)
def test_award_date_rules(scholarship_types, invoice, evaluate, award_date, message):
    with pytest.raises(ValidationError) as exc:
        _award(evaluate(invoice.student)["SCT-001"], invoice, award_date=award_date)
    assert message in exc.value.message_dict["award_date"][0]


def test_BR_SCH_02_ineligible_superseded_and_used_eligibilities(
    scholarship_types, invoice, evaluate
):
    first = evaluate(invoice.student)
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(first["SCT-002"], invoice)
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_NOT_ELIGIBLE", "BR-SCH-02")
    assert exc.value.details["reason_codes"] == ["GPA_AT_OR_ABOVE_MAX"]

    second = evaluate(invoice.student)
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(first["SCT-001"], invoice)
    assert exc.value.details["reason"] == "SUPERSEDED"
    assert exc.value.details["latest_eligibility_id"] == second["SCT-001"].pk

    used = _award(second["SCT-004"], invoice)
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(second["SCT-004"], invoice)
    assert exc.value.details == {
        "eligibility_id": second["SCT-004"].pk,
        "reason": "ELIGIBILITY_ALREADY_USED",
        "existing_scholarship_id": used.pk,
    }


def test_invoice_must_be_the_students_issued_invoice_for_the_term(
    scholarship_types, invoice, evaluate, student_with_invoice
):
    other, other_inv = student_with_invoice()
    elig = evaluate(invoice.student)["SCT-001"]
    with pytest.raises(ValidationError) as exc:
        _award(elig, other_inv)
    assert (
        "is not STU-000001's invoice for TERM-2026-FALL" in exc.value.message_dict["invoice_id"][0]
    )
    billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    with pytest.raises(StateConflict) as exc:
        _award(elig, invoice)
    assert exc.value.code == "ILLEGAL_TRANSITION" and exc.value.details["status"] == "VOID"


def test_BR_SCH_05_one_award_per_exclusive_group(scholarship_types, invoice, evaluate):
    services.create_scholarship_type(
        name="Merit Special",
        category="MERIT",
        exclusive_group="MERIT",
        min_gpa=D("3.00"),
        residency="ALL",
        first_term_only=False,
        program_restricted=False,
        coverage_scope="ELIGIBLE_LINES_ONLY",
        award_basis="FIXED",
        award_value=D("1000.00"),
        max_amount_per_term=D("1000.00"),
        priority=8,
        effective_from=date(2026, 1, 1),
    )
    rows = evaluate(invoice.student)
    first = _award(rows["SCT-001"], invoice)
    with pytest.raises(StateConflict) as exc:
        _award(rows["SCT-008"], invoice)
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_GROUP_CONFLICT", "BR-SCH-05")
    assert exc.value.details == {
        "scholarship_type_id": "SCT-008",
        "exclusive_group": "MERIT",
        "conflicting_scholarship_ids": [first.pk],
    }
    _award(rows["SCT-004"], invoice)  # no group: allowed alongside merit


def test_BR_SCH_08_full_ride_excludes_every_other_award_in_both_directions(
    scholarship_types, student_with_invoice, evaluate
):
    s, inv = student_with_invoice(gpa="3.90", need_index="0.90")
    rows = evaluate(s)
    full = _award(rows["SCT-007"], inv)
    assert full.awarded_amount == D("164000.00")  # 100% of every FEE line
    with pytest.raises(StateConflict) as exc:
        _award(rows["SCT-001"], inv)
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_GROUP_CONFLICT", "BR-SCH-08")

    s2, inv2 = student_with_invoice(gpa="3.90", need_index="0.90")
    rows2 = evaluate(s2)
    other = _award(rows2["SCT-003"], inv2)
    with pytest.raises(StateConflict) as exc:
        _award(rows2["SCT-007"], inv2)
    assert exc.value.details["conflicting_scholarship_ids"] == [other.pk]


# --- (1) per-term cap on awards (D-S5) ------------------------------------------------------------


def test_D_S5_per_term_cap_is_cumulative_over_awards_not_allocations(
    scholarship_types, student_with_invoice, evaluate
):
    s, inv = student_with_invoice(gpa="3.40", need_index="0.65")  # SCT-003 need grant: fixed 40,000
    a1 = _award(evaluate(s)["SCT-003"], inv, requested_amount=D("15000.00"))
    assert a1.allocations.count() == 0  # not applied: still counts toward the per-term cap
    a2 = _award(evaluate(s)["SCT-003"], inv)
    assert a2.awarded_amount == D("25000.00")  # 40,000 − 15,000 already awarded
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(evaluate(s)["SCT-003"], inv)
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", "BR-SCH-03")
    assert exc.value.details["already_awarded_this_term"] == "40000.00"
    assert exc.value.details["computed_award"] == "0.00"


def test_D_S4_requested_amount_may_only_reduce(merit_award, scholarship_types, invoice, evaluate):
    elig = evaluate(invoice.student)["SCT-004"]  # STEM: fixed 20,000
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(elig, invoice, requested_amount=D("20000.01"))
    assert exc.value.rule_id == "BR-SCH-03" and exc.value.details["maximum_award"] == "20000.00"
    for bad in (D("0"), D("-1.00")):
        with pytest.raises(ValidationError):
            _award(elig, invoice, requested_amount=bad)
    assert _award(elig, invoice, requested_amount=D("12000.00")).awarded_amount == D("12000.00")


# --- (2) award-stage reservation guardrail (NOT BR-SCH-04) ----------------------------------------


@pytest.fixture
def cheap_invoice(scholarship_types, program, terms, make_student, enroll_fall):
    """Eligible base 16,000 (TUITION 1,000 × 16, LAB 0) for a needy merit student."""
    make_fees(
        program,
        terms["TERM-2026-FALL"],
        [
            ("TUITION", "DOMESTIC", "PER_CREDIT", "1000.00"),
            ("LAB", "ALL", "FLAT", "0.00"),
            ("REGISTRATION", "ALL", "FLAT", "5000.00"),
        ],
    )
    s = make_student(gpa=D("3.75"), need_index=D("0.65"))
    return billing.generate_invoice(enrollment=enroll_fall(s), actor="t")


def test_reservation_guardrail_caps_awards_at_unreserved_eligible_fees(cheap_invoice, evaluate):
    rows = evaluate(cheap_invoice.student)
    merit = _award(rows["SCT-001"], cheap_invoice)  # 50% of 16,000
    assert merit.awarded_amount == D("8000.00")
    need = _award(rows["SCT-003"], cheap_invoice)  # computed 40,000; only 8,000 unreserved
    assert need.awarded_amount == D("8000.00")
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(rows["SCT-004"], cheap_invoice)  # nothing left to reserve
    assert exc.value.code == "SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES"
    assert exc.value.rule_id is None  # the guardrail is not claimed to be BR-SCH-04
    assert exc.value.details["reserved_on_invoice"] == "16000.00"
    assert exc.value.details["maximum_award"] == "0.00"


def test_reservation_guardrail_rejects_an_over_reserving_request(cheap_invoice, evaluate):
    rows = evaluate(cheap_invoice.student)
    _award(rows["SCT-001"], cheap_invoice)
    with pytest.raises(BusinessRuleViolation) as exc:
        _award(rows["SCT-003"], cheap_invoice, requested_amount=D("8000.01"))
    assert exc.value.rule_id is None and exc.value.details["maximum_award"] == "8000.00"


def test_reservation_uses_applied_amount_once_applied(cheap_invoice, evaluate):
    rows = evaluate(cheap_invoice.student)
    merit = _award(rows["SCT-001"], cheap_invoice, requested_amount=D("6000.00"))
    services.apply_scholarship(merit, actor="t")
    assert services.reserved_on_invoice(cheap_invoice) == D("6000.00")
    assert _award(rows["SCT-003"], cheap_invoice).awarded_amount == D("10000.00")


# --- application ----------------------------------------------------------------------------------


def test_apply_allocates_posts_credits_moves_status_and_refreshes_invoice(merit_award, invoice):
    services.apply_scholarship(merit_award, actor="ops")
    merit_award.refresh_from_db()
    assert merit_award.status == "APPLIED"
    allocs = list(merit_award.allocations.order_by("allocation_id"))
    assert [(a.allocation_id, a.invoice_line_id[-3:], a.amount, a.reason) for a in allocs] == [
        ("SCHA-SCH-2026-000001-01", "L01", D("75000.00"), "AWARD")  # tuition first
    ]
    e = allocs[0].ledger_entry
    assert (e.entry_type, e.amount, e.entry_date, e.student_id) == (
        "SCHOLARSHIP_CREDIT",
        D("-75000.00"),
        ISSUE,
        "STU-000001",
    )
    assert _events(merit_award) == [
        (None, "OFFERED", ISSUE, "AWARDED"),
        ("OFFERED", "ACCEPTED", ISSUE, "ACCEPTED_ON_APPLY"),
        ("ACCEPTED", "APPLIED", ISSUE, "APPLIED"),
    ]
    a = billing.current_amounts(invoice)
    assert (a.scholarship_applied, a.amount_paid, a.outstanding) == (
        D("75000.00"),
        0,
        D("89000.00"),
    )
    invoice.refresh_from_db()
    assert invoice.collection_status == "UNPAID"  # partial scholarship, no payment


def test_full_ride_apply_covers_every_fee_line_and_is_scholarship_covered(
    scholarship_types, student_with_invoice, evaluate
):
    s, inv = student_with_invoice(gpa="3.90", need_index="0.90")
    award = _award(evaluate(s)["SCT-007"], inv)
    services.apply_scholarship(award, actor="t")
    lines = [a.invoice_line.fee_type for a in award.allocations.order_by("allocation_id")]
    assert lines == ["TUITION", "LAB", "REGISTRATION", "TECHNOLOGY", "LIBRARY", "STUDENT_ACTIVITY"]
    inv.refresh_from_db()
    assert inv.collection_status == "SCHOLARSHIP_COVERED"
    assert inv.events.filter(dimension="COLLECTION").last().reason == "SCHOLARSHIP_APPLIED"
    assert billing.student_balance(s, None)["ledger_balance"] == 0


def test_partial_scholarship_after_due_date_stays_overdue(merit_award, invoice):
    billing.refresh_collection_status(as_of=date(2026, 9, 20), actor="t")
    services.apply_scholarship(merit_award, actor="t")
    invoice.refresh_from_db()
    assert invoice.collection_status == "OVERDUE"
    assert merit_award.allocations.get().allocated_on == date(2026, 9, 20)  # deterministic default


def test_two_awards_share_line_room(scholarship_types, invoice, evaluate):
    rows = evaluate(invoice.student)
    merit, stem = _award(rows["SCT-001"], invoice), _award(rows["SCT-004"], invoice)
    services.apply_scholarship(merit, actor="t")
    services.apply_scholarship(stem, actor="t")
    by_line = billing_calc.line_open_amounts(
        list(invoice.lines.all()), services.invoice_allocations(invoice)
    )
    assert by_line[f"{invoice.pk}-L01"] == D("49000.00")  # 144,000 − 75,000 − 20,000


@pytest.mark.parametrize(
    ("allocated_on", "message"),
    [
        (date(2026, 8, 16), "on or after 2026-08-17"),
        (date(2027, 1, 1), "business date between"),
    ],
)
def test_allocated_on_rules(merit_award, allocated_on, message):
    with pytest.raises(ValidationError) as exc:
        services.apply_scholarship(merit_award, allocated_on=allocated_on, actor="t")
    assert message in exc.value.message_dict["allocated_on"][0]


def test_apply_twice_is_illegal_transition(merit_award):
    services.apply_scholarship(merit_award, actor="t")
    before = _counts()
    with pytest.raises(StateConflict) as exc:
        services.apply_scholarship(merit_award, actor="t")
    assert exc.value.code == "ILLEGAL_TRANSITION" and exc.value.details["from_status"] == "APPLIED"
    assert _counts() == before


def test_apply_to_a_void_invoice_is_409(merit_award, invoice):
    billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    with pytest.raises(StateConflict) as exc:
        services.apply_scholarship(merit_award, actor="t")
    assert exc.value.details["status"] == "VOID"


# --- (3) BR-SCH-04 on actual allocations, at apply ------------------------------------------------


def _insert_award(elig, inv, amount):
    """An award inserted directly, bypassing the award-stage guardrail (as an injected anomaly
    would be), to prove that BR-SCH-04 is enforced independently when applying."""
    return Scholarship.objects.create(
        scholarship_id="SCH-2026-000900",
        student=inv.student,
        scholarship_type=elig.scholarship_type,
        term=inv.term,
        eligibility=elig,
        invoice=inv,
        awarded_amount=amount,
        award_date=ISSUE,
        status="OFFERED",
    )


def test_BR_SCH_04_apply_rejects_allocations_above_the_eligible_fee_base(
    scholarship_types, invoice, evaluate
):
    award = _insert_award(evaluate(invoice.student)["SCT-001"], invoice, D("150000.01"))
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        services.apply_scholarship(award, actor="t")
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", "BR-SCH-04")
    assert exc.value.details["eligible_fee_base"] == "150000.00"
    assert _counts() == before


def test_BR_SCH_04_counts_allocations_already_applied_by_other_awards(
    scholarship_types, invoice, evaluate
):
    rows = evaluate(invoice.student)
    merit = _award(rows["SCT-001"], invoice)  # 75,000
    services.apply_scholarship(merit, actor="t")
    extra = _insert_award(rows["SCT-004"], invoice, D("75000.01"))
    with pytest.raises(BusinessRuleViolation) as exc:
        services.apply_scholarship(extra, actor="t")
    assert exc.value.rule_id == "BR-SCH-04" and exc.value.details["already_applied"] == "75000.00"


def test_BR_SCH_04_exactly_at_the_base_is_allowed(scholarship_types, invoice, evaluate):
    award = _insert_award(evaluate(invoice.student)["SCT-001"], invoice, D("150000.00"))
    services.apply_scholarship(award, actor="t")
    assert billing.current_amounts(invoice).scholarship_applied == D("150000.00")


# --- (4) each line's remaining room, with adjustments ---------------------------------------------


def _adjust_tuition(inv, amount):
    """An ADJUSTMENT line on tuition, as a Phase 5 withdrawal credit would post it."""
    tuition = inv.lines.get(fee_type="TUITION")
    line = InvoiceLine.objects.create(
        line_id=f"{inv.pk}-L20",
        invoice=inv,
        line_type="ADJUSTMENT",
        fee_type="TUITION",
        description="credit",
        quantity=1,
        unit_amount=amount,
        gross_amount=amount,
        net_amount=amount,
        adjusts_line=tuition,
        adjustment_reason="WITHDRAWAL",
        is_scholarship_eligible=False,
        is_refundable=False,
        is_waivable=False,
        allocation_priority=10,
        posted_date=date(2026, 9, 10),
    )
    billing.post_ledger(line)


def test_line_room_rejects_apply_even_when_the_base_would_allow_it(merit_award, invoice):
    _adjust_tuition(invoice, D("-140000.00"))  # tuition room 4,000 + lab 6,000 = 10,000
    assert services.reserved_on_invoice(invoice) == D("75000.00") <= D("150000.00")  # base fine
    before = _counts()
    with pytest.raises(BusinessRuleViolation) as exc:
        services.apply_scholarship(merit_award, actor="t")
    assert (exc.value.code, exc.value.rule_id) == ("SCHOLARSHIP_EXCEEDS_ELIGIBLE_FEES", "BR-ALC-02")
    assert exc.value.details["remaining_room"] == "10000.00"
    assert _counts() == before


def test_line_room_after_adjustment_allows_what_fits(scholarship_types, invoice, evaluate):
    _adjust_tuition(invoice, D("-140000.00"))
    award = _award(evaluate(invoice.student)["SCT-001"], invoice, requested_amount=D("10000.00"))
    services.apply_scholarship(award, actor="t")
    assert [
        (a.invoice_line.fee_type, a.amount) for a in award.allocations.order_by("allocation_id")
    ] == [
        ("TUITION", D("4000.00")),
        ("LAB", D("6000.00")),
    ]
    assert award.allocations.first().allocated_on == date(2026, 9, 10)  # after the adjustment


# --- BR-ALC-06 payment-release hook ---------------------------------------------------------------
# Phase 4 registers the real handler ("payments"). The first test removes it for the duration of
# the test, to keep proving that a missing handler fails loudly and rolls back (D-P17).


def test_payment_release_without_handler_fails_loudly_and_rolls_back(
    merit_award, invoice, monkeypatch
):
    tuition = f"{invoice.pk}-L01"
    fake = [
        NS(invoice_line_id=tuition, amount=D("100000.00"), payer_type="SELF", allocated_on=ISSUE)
    ]
    monkeypatch.setitem(registry._providers["payment"], "test-only", lambda inv: fake)
    monkeypatch.delitem(registry._release_handlers, "payments")  # as if payments were absent
    before, seq = _counts(), list(IdSequence.objects.values_list("key", "last_value"))
    with pytest.raises(PaymentReleaseUnavailable, match="SCH-2026-000001"):
        services.apply_scholarship(merit_award, actor="t")
    assert _counts() == before
    assert list(IdSequence.objects.values_list("key", "last_value")) == seq
    merit_award.refresh_from_db()
    assert merit_award.status == "OFFERED"


def test_payment_release_handler_receives_the_amounts_when_registered(
    merit_award, invoice, monkeypatch
):
    """Phase 4: a real payment (AUTO → tuition 100,000) and the real "payments" handler,
    wrapped only to record the call it receives."""
    from payments import services as payments
    from payments.models import PaymentAllocation

    tuition = f"{invoice.pk}-L01"
    payment, _ = payments.record_payment(
        student=invoice.student,
        amount=D("100000.00"),
        received_date=ISSUE,
        method="UPI",
        payer_type="SELF",
        external_reference="UTR-SCH-PRECEDENCE",
        auto_allocate=True,
        actor="t",
    )
    calls, real = [], registry._release_handlers["payments"]

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setitem(registry._release_handlers, "payments", spy)
    services.apply_scholarship(merit_award, actor="t")
    assert calls[0]["releases"] == {tuition: D("31000.00")}  # 100,000 + 75,000 − 144,000
    assert calls[0]["source_ref"] == "SCH-2026-000001"
    a = billing.current_amounts(invoice)
    assert (a.scholarship_applied, a.amount_paid) == (D("75000.00"), D("69000.00"))
    released = PaymentAllocation.objects.get(allocation_type="DEALLOCATE")
    assert (released.amount, released.rule_applied, released.reverses.payment_id) == (
        D("-31000.00"),
        "SCHOLARSHIP_PRECEDENCE",
        payment.payment_id,
    )


def test_zero_releases_never_need_a_handler(merit_award):
    services.apply_scholarship(merit_award, actor="t")  # no payments → all releases are 0


# --- atomicity ------------------------------------------------------------------------------------


def test_apply_rolls_back_when_a_ledger_post_fails(
    scholarship_types, student_with_invoice, evaluate, monkeypatch
):
    s, inv = student_with_invoice(gpa="3.90", need_index="0.90")
    award = _award(evaluate(s)["SCT-007"], inv)
    real, calls = billing.post_scholarship_ledger, []

    def flaky(allocation, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(allocation, **kw)

    monkeypatch.setattr(billing, "post_scholarship_ledger", flaky)
    before = _counts()
    with pytest.raises(RuntimeError):
        services.apply_scholarship(award, actor="t")
    assert _counts() == before
    inv.refresh_from_db()
    assert inv.collection_status == "UNPAID"


# --- void and billing views see real scholarships -------------------------------------------------


def test_void_is_blocked_by_an_applied_scholarship(merit_award, invoice):
    services.apply_scholarship(merit_award, actor="t")
    with pytest.raises(StateConflict) as exc:
        billing.void_invoice(invoice, void_date=date(2026, 8, 20), actor="t")
    assert exc.value.code == "INVOICE_HAS_ALLOCATIONS"
    assert exc.value.details["scholarship_applied"] == "75000.00"


def test_void_is_allowed_with_an_unapplied_award(merit_award, invoice):
    billing.void_invoice(
        invoice, void_date=date(2026, 8, 20), actor="t"
    )  # awards are not allocations
    invoice.refresh_from_db()
    assert invoice.lifecycle_status == "VOID"


def test_balance_breakdown_and_identity_with_scholarship(merit_award, invoice):
    services.apply_scholarship(merit_award, actor="t")
    b = billing.student_balance(invoice.student, None)
    assert b["breakdown"]["scholarship_credits"] == D("-75000.00")
    assert b["ledger_balance"] == b["invoice_outstanding"] == D("89000.00")
    assert b["identity"]["holds"] is True
    before = billing.student_balance(invoice.student, date(2026, 8, 16))
    assert before["ledger_balance"] == 0 and before["identity"]["holds"] is True


def test_late_fee_uses_outstanding_after_scholarship(merit_award, invoice):
    services.apply_scholarship(merit_award, actor="t")
    r = billing.assess_late_fees(as_of=date(2026, 9, 29), dry_run=True, actor="t")
    assert r["assessed"][0].outstanding == D("89000.00")
    assert r["assessed"][0].late_fee == D("1780.00")


# --- reversal (BR-SCH-07 building block, no endpoint) ---------------------------------------------


def test_reverse_allocation_posts_reversal_and_partially_reverses(merit_award, invoice):
    services.apply_scholarship(merit_award, actor="t")
    alloc = merit_award.allocations.get()
    rev = services.reverse_allocation(
        alloc, amount=D("45000.00"), reason="WITHDRAWAL", on_date=date(2026, 9, 17), actor="t"
    )
    assert (rev.allocation_type, rev.amount, rev.reverses_id, rev.reason) == (
        "REVERSE",
        D("-45000.00"),
        alloc.pk,
        "WITHDRAWAL",
    )
    assert (rev.ledger_entry.entry_type, rev.ledger_entry.amount) == (
        "SCHOLARSHIP_REVERSAL",
        D("45000.00"),
    )
    merit_award.refresh_from_db()
    assert merit_award.status == "PARTIALLY_REVERSED"
    assert billing.current_amounts(invoice).scholarship_applied == D("30000.00")
    # a second reversal keeps PARTIALLY_REVERSED and writes no status event (D-S16)
    n = merit_award.events.count()
    services.reverse_allocation(
        alloc, amount=D("30000.00"), reason="WITHDRAWAL", on_date=date(2026, 9, 18), actor="t"
    )
    merit_award.refresh_from_db()
    assert merit_award.status == "PARTIALLY_REVERSED" and merit_award.events.count() == n
    assert billing.current_amounts(invoice).scholarship_applied == 0
    invoice.refresh_from_db()
    assert invoice.collection_status == "OVERDUE"  # 164,000 owed again after the due date


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"amount": D("75000.01")}, "amount"),
        ({"amount": D("0")}, "amount"),
        ({"reason": "AWARD"}, "reason"),
        ({"on_date": date(2026, 8, 16)}, "on_date"),
    ],
)
def test_reverse_allocation_rules(merit_award, kw, field):
    services.apply_scholarship(merit_award, actor="t")
    args = dict(amount=D("1.00"), reason="CANCELLATION", on_date=date(2026, 9, 1), actor="t", **{})
    args.update(kw)
    with pytest.raises(ValidationError) as exc:
        services.reverse_allocation(merit_award.allocations.get(), **args)
    assert field in exc.value.message_dict


def test_cannot_reverse_a_reversal(merit_award):
    services.apply_scholarship(merit_award, actor="t")
    alloc = merit_award.allocations.get()
    rev = services.reverse_allocation(
        alloc, amount=D("1.00"), reason="WITHDRAWAL", on_date=date(2026, 9, 1), actor="t"
    )
    with pytest.raises(ValidationError):
        services.reverse_allocation(
            rev, amount=D("1.00"), reason="WITHDRAWAL", on_date=date(2026, 9, 1), actor="t"
        )


def test_D_S12_no_reversal_without_a_withdrawal_or_cancellation_reason():
    from scholarships import policy

    assert policy.REVERSAL_REASONS == ("WITHDRAWAL", "CANCELLATION")


def test_registry_registration_is_idempotent_and_conflicts_raise():
    assert registry.registered()["scholarship"] == ["scholarships"]
    registry.register_allocation_provider(
        "scholarship", "scholarships", services.invoice_allocations
    )
    assert registry.registered()["scholarship"] == ["scholarships"]
    with pytest.raises(ValueError, match="different function"):
        registry.register_allocation_provider("scholarship", "scholarships", lambda inv: [])
    with pytest.raises(ValueError):
        registry.register_allocation_provider("refund", "x", lambda inv: [])
    assert registry.registered()["payment_release"] == ["payments"]  # registered in Phase 4


def test_invoice_queries_never_see_other_invoices_allocations(
    scholarship_types, invoice, evaluate, student_with_invoice
):
    award = _award(evaluate(invoice.student)["SCT-001"], invoice)
    services.apply_scholarship(award, actor="t")
    _, other = student_with_invoice()
    assert services.invoice_allocations(other) == []
    assert billing.current_amounts(Invoice.objects.get(pk=other.pk)).scholarship_applied == 0
