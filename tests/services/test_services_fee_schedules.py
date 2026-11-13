"""billing.services fee schedules: BR-FEE-01..04, decisions D3 (residency mapping) and D5
(server-controlled flags)."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict

from billing import calculators as calc
from billing import services
from billing.models import FeeSchedule
from core.exceptions import BusinessRuleViolation

pytestmark = pytest.mark.django_db


def _create(program, term, fee_type="TUITION", residency="DOMESTIC", basis="PER_CREDIT", **kw):
    return services.create_fee_schedule(
        program=program,
        term=term,
        fee_type=fee_type,
        residency=residency,
        charge_basis=basis,
        amount=kw.get("amount", Decimal("9000.00")),
        effective_from=kw.get("effective_from", date(2026, 1, 1)),
    )


def test_v1_id_and_server_derived_fields(program, terms):
    fs = _create(program, terms["TERM-2026-FALL"])
    assert fs.fee_schedule_id == "FEE-PRG-001-2026-FALL-TUITION-DOM-V1"
    assert (fs.version, fs.supersedes) == (1, None)
    assert (fs.is_scholarship_eligible, fs.is_refundable, fs.is_waivable) == (True, True, False)
    assert fs.allocation_priority == 10


@pytest.mark.parametrize(
    ("fee_type", "residency", "basis", "flags", "priority", "suffix"),
    [
        ("TUITION", "INTERNATIONAL", "PER_CREDIT", (True, True, False), 10, "TUITION-INTL-V1"),
        ("LAB", "ALL", "FLAT", (True, True, False), 20, "LAB-ALL-V1"),
        ("REGISTRATION", "ALL", "FLAT", (False, False, False), 30, "REGISTRATION-ALL-V1"),
        ("TECHNOLOGY", "ALL", "FLAT", (False, False, True), 40, "TECHNOLOGY-ALL-V1"),
        ("LIBRARY", "ALL", "PER_CREDIT", (False, False, True), 50, "LIBRARY-ALL-V1"),
        ("STUDENT_ACTIVITY", "ALL", "FLAT", (False, False, True), 60, "STUDENT_ACTIVITY-ALL-V1"),
        (
            "INTERNATIONAL_SERVICE",
            "INTERNATIONAL",
            "FLAT",
            (False, False, False),
            70,
            "INTERNATIONAL_SERVICE-INTL-V1",
        ),
    ],
)
def test_flags_and_priority_by_fee_type(
    program, terms, fee_type, residency, basis, flags, priority, suffix
):
    fs = _create(program, terms["TERM-2026-FALL"], fee_type, residency, basis)
    assert (fs.is_scholarship_eligible, fs.is_refundable, fs.is_waivable) == flags
    assert fs.allocation_priority == priority
    assert fs.fee_schedule_id.endswith(suffix)


@pytest.mark.parametrize(
    ("fee_type", "residency"),
    [
        ("TUITION", "ALL"),
        ("INTERNATIONAL_SERVICE", "DOMESTIC"),
        ("INTERNATIONAL_SERVICE", "ALL"),
        ("LAB", "DOMESTIC"),
        ("REGISTRATION", "INTERNATIONAL"),
        ("TECHNOLOGY", "DOMESTIC"),
        ("LIBRARY", "INTERNATIONAL"),
        ("STUDENT_ACTIVITY", "DOMESTIC"),
    ],
)
def test_D3_exact_residency_mapping(program, terms, fee_type, residency):
    basis = "PER_CREDIT" if fee_type == "TUITION" else "FLAT"
    with pytest.raises(ValidationError) as exc:
        _create(program, terms["TERM-2026-FALL"], fee_type, residency, basis)
    assert "residency" in exc.value.message_dict
    assert FeeSchedule.objects.count() == 0


def test_tuition_must_be_per_credit(program, terms):
    with pytest.raises(ValidationError) as exc:
        _create(program, terms["TERM-2026-FALL"], basis="FLAT")
    assert exc.value.message_dict["charge_basis"] == ["TUITION must be charged PER_CREDIT."]


def test_term_must_be_billable(program, terms):
    with pytest.raises(ValidationError) as exc:
        _create(program, terms["TERM-2025-FALL"])
    assert "term" in exc.value.message_dict


@pytest.mark.parametrize("bad", [date(2025, 12, 31), date(2027, 1, 1)])
def test_effective_from_must_be_a_2026_business_date(program, terms, bad):
    with pytest.raises(ValidationError) as exc:
        _create(program, terms["TERM-2026-FALL"], effective_from=bad)
    assert "effective_from" in exc.value.message_dict


def test_negative_amount_rejected(program, terms):
    with pytest.raises(ValidationError):
        _create(program, terms["TERM-2026-FALL"], amount=Decimal("-1.00"))


def test_second_v1_for_same_key_is_version_invalid(program, terms):
    first = _create(program, terms["TERM-2026-FALL"])
    with pytest.raises(BusinessRuleViolation) as exc:
        _create(program, terms["TERM-2026-FALL"], amount=Decimal("1.00"))
    assert exc.value.code == "FEE_SCHEDULE_VERSION_INVALID"
    assert exc.value.rule_id == "BR-FEE-03"
    assert exc.value.details == {"latest_fee_schedule_id": first.fee_schedule_id}


def test_new_version_inserts_v2_and_never_changes_v1(program, terms):
    v1 = _create(program, terms["TERM-2026-FALL"])
    before = model_to_dict(FeeSchedule.objects.get(pk=v1.pk))
    v2 = services.new_fee_schedule_version(
        v1, amount=Decimal("9360.00"), effective_from=date(2026, 8, 1)
    )
    assert v2.fee_schedule_id == "FEE-PRG-001-2026-FALL-TUITION-DOM-V2"
    assert (v2.version, v2.supersedes_id, v2.amount) == (2, v1.pk, Decimal("9360.00"))
    assert (v2.fee_type, v2.residency, v2.charge_basis) == ("TUITION", "DOMESTIC", "PER_CREDIT")
    assert v2.allocation_priority == v1.allocation_priority
    assert model_to_dict(FeeSchedule.objects.get(pk=v1.pk)) == before
    windows = calc.effective_windows(services.chain_of(v1))
    assert windows == {
        v1.pk: (date(2026, 1, 1), date(2026, 7, 31)),
        v2.pk: (date(2026, 8, 1), None),
    }


def test_new_version_must_supersede_the_latest(program, terms):
    v1 = _create(program, terms["TERM-2026-FALL"])
    v2 = services.new_fee_schedule_version(v1, amount=Decimal("1"), effective_from=date(2026, 3, 1))
    with pytest.raises(BusinessRuleViolation) as exc:
        services.new_fee_schedule_version(v1, amount=Decimal("2"), effective_from=date(2026, 4, 1))
    assert exc.value.code == "FEE_SCHEDULE_VERSION_INVALID"
    assert exc.value.details["latest_fee_schedule_id"] == v2.pk
    assert FeeSchedule.objects.count() == 2


@pytest.mark.parametrize("delta", [0, -1])
def test_new_version_must_start_later(program, terms, delta):
    v1 = _create(program, terms["TERM-2026-FALL"], effective_from=date(2026, 3, 1))
    with pytest.raises(BusinessRuleViolation) as exc:
        services.new_fee_schedule_version(
            v1, amount=Decimal("1"), effective_from=v1.effective_from + timedelta(days=delta)
        )
    assert exc.value.code == "FEE_SCHEDULE_VERSION_INVALID"
    assert exc.value.details["predecessor_effective_from"] == "2026-03-01"


def test_new_version_one_day_later_is_accepted(program, terms):
    v1 = _create(program, terms["TERM-2026-FALL"], effective_from=date(2026, 3, 1))
    v2 = services.new_fee_schedule_version(v1, amount=Decimal("1"), effective_from=date(2026, 3, 2))
    assert calc.effective_windows([v1, v2])[v1.pk] == (date(2026, 3, 1), date(2026, 3, 1))


def test_new_version_effective_from_outside_2026_is_400(program, terms):
    v1 = _create(program, terms["TERM-2026-FALL"])
    with pytest.raises(ValidationError):
        services.new_fee_schedule_version(v1, amount=Decimal("1"), effective_from=date(2027, 1, 1))


def test_chain_is_linear_and_per_key(program, terms):
    dom = _create(program, terms["TERM-2026-FALL"])
    intl = _create(program, terms["TERM-2026-FALL"], residency="INTERNATIONAL")
    v2 = services.new_fee_schedule_version(
        dom, amount=Decimal("1"), effective_from=date(2026, 5, 1)
    )
    v3 = services.new_fee_schedule_version(v2, amount=Decimal("2"), effective_from=date(2026, 6, 1))
    assert [f.pk for f in services.chain_of(dom)] == [dom.pk, v2.pk, v3.pk]
    assert [f.pk for f in services.chain_of(intl)] == [intl.pk]
    assert dom.superseded_by == v2 and v2.superseded_by == v3
