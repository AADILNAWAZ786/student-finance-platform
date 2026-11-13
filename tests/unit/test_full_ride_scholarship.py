"""BR-SCH-06 / BR-SCH-08: which invoice lines a scholarship may cover.

Pure calculator tests (no database).

Contract under test (scholarships.calculators):
    coverable_lines(scholarship_type, lines) -> list   (allocation_priority, then line_id)
    eligible_fee_base(scholarship_type, lines) -> Decimal
    award_amount(scholarship_type, eligible_base, already_awarded) -> Decimal
"""

from decimal import Decimal as D
from types import SimpleNamespace as NS

from scholarships.calculators import award_amount, coverable_lines, eligible_fee_base

INV = "INV-STU-000001-2026-000003"


def line(n, line_type, fee_type, net, eligible, priority):
    return NS(
        line_id=f"{INV}-L{n:02d}",
        line_type=line_type,
        fee_type=fee_type,
        net_amount=D(net),
        is_scholarship_eligible=eligible,
        allocation_priority=priority,
    )


LINES = [
    line(1, "FEE", "TECHNOLOGY", "4000.00", False, 40),
    line(2, "FEE", "TUITION", "160000.00", True, 10),
    line(3, "FEE", "REGISTRATION", "5000.00", False, 30),
    line(4, "FEE", "LAB", "12000.00", True, 20),
    line(5, "FEE", "LIBRARY", "2000.00", False, 50),
    line(6, "LATE_FEE", "LATE_FEE", "1500.00", False, 90),
    line(7, "ADJUSTMENT", "TUITION", "-64000.00", False, 10),
]

MERIT = NS(
    scholarship_type_id="SCT-001",
    coverage_scope="ELIGIBLE_LINES_ONLY",
    award_basis="PERCENT",
    award_value=D("50"),
    max_amount_per_term=D("100000.00"),
)
FULL_RIDE = NS(
    scholarship_type_id="SCT-007",
    coverage_scope="ALL_FEE_LINES",
    award_basis="PERCENT",
    award_value=D("100"),
    max_amount_per_term=D("600000.00"),
)


def fee_types(lines):
    return [ln.fee_type for ln in lines]


def test_standard_type_unchanged_covers_only_eligible_lines():
    assert fee_types(coverable_lines(MERIT, LINES)) == ["TUITION", "LAB"]
    assert eligible_fee_base(MERIT, LINES) == D("172000.00")


def test_full_ride_covers_registration_and_all_other_fee_lines_in_priority_order():
    assert fee_types(coverable_lines(FULL_RIDE, LINES)) == [
        "TUITION",
        "LAB",
        "REGISTRATION",
        "TECHNOLOGY",
        "LIBRARY",
    ]
    assert eligible_fee_base(FULL_RIDE, LINES) == D("183000.00")


def test_no_type_covers_late_fees_or_adjustments():
    for t in (MERIT, FULL_RIDE):
        covered = {ln.line_type for ln in coverable_lines(t, LINES)}
        assert covered == {"FEE"}


def test_full_ride_award_equals_full_base():
    assert award_amount(FULL_RIDE, D("183000.00"), D("0")) == D("183000.00")


def test_full_ride_award_capped_at_max_per_term():
    assert award_amount(FULL_RIDE, D("700000.00"), D("0")) == D("600000.00")


def test_standard_award_unchanged():
    assert award_amount(MERIT, D("172000.00"), D("0")) == D("86000.00")
