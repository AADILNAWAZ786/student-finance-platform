"""seed_scholarship_types (D-S10): opt-in, deterministic, idempotent; refuses conflicts.
Runs only against the test database."""

from datetime import date
from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from scholarships import services
from scholarships.management.commands.seed_scholarship_types import SPECS
from scholarships.models import ScholarshipType, ScholarshipTypeProgram

pytestmark = pytest.mark.django_db


def _run():
    out = StringIO()
    call_command("seed_scholarship_types", stdout=out)
    return out.getvalue()


def _dump():
    return [
        tuple(
            str(getattr(t, f.attname)) for f in t._meta.concrete_fields if f.name != "recorded_at"
        )
        for t in ScholarshipType.objects.order_by("pk")
    ]


def test_creates_exactly_the_documented_types(program, make_program):
    make_program(code="BA-ECO", is_stem=False)
    assert "created: 7 (SCT-004 linked to PRG-001)" in _run()
    rows = {t.pk: t for t in ScholarshipType.objects.all()}
    assert sorted(rows) == [f"SCT-00{n}" for n in range(1, 8)]
    for spec in SPECS:
        t = rows[spec.scholarship_type_id]
        assert (t.name, t.award_basis, t.award_value, t.max_amount_per_term, t.priority) == (
            spec.name,
            spec.award_basis,
            spec.award_value,
            spec.max_amount_per_term,
            spec.priority,
        )
        assert (t.effective_from, t.effective_to) == (date(2026, 1, 1), None)
    assert list(ScholarshipTypeProgram.objects.values_list("id", flat=True)) == ["SCT-004-PRG-001"]
    full = rows["SCT-007"]
    assert (full.coverage_scope, full.min_gpa, full.min_need_index) == (
        "ALL_FEE_LINES",
        D("3.85"),
        D("0.85"),
    )


def test_second_run_changes_nothing(program):
    _run()
    before = _dump()
    assert "nothing changed" in _run()
    assert _dump() == before


def test_refuses_when_other_types_exist(program):
    services.create_scholarship_type(
        name="Custom", category="NEED", residency="ALL", first_term_only=False,
        program_restricted=False, coverage_scope="ELIGIBLE_LINES_ONLY", award_basis="FIXED",
        award_value=D("1.00"), max_amount_per_term=D("1.00"), priority=9,
        effective_from=date(2026, 1, 1),
    )  # fmt: skip
    with pytest.raises(CommandError, match="Refusing to seed"):
        _run()
    assert ScholarshipType.objects.count() == 1


def test_needs_a_stem_program_and_writes_nothing_otherwise(make_program):
    make_program(code="BA-ECO", is_stem=False)
    with pytest.raises(CommandError, match="needs at least one STEM program"):
        _run()
    assert ScholarshipType.objects.count() == 0
