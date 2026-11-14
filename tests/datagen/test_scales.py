"""Configurable dataset sizes (Phase 8): the same generator validates at every scale.

Phase 8 fixes (P8-B1…B3): the ₹500 / ₹5,000 late-fee boundary cases are counted per
occurrence, and the ₹0.01 overpayment and the seven withdrawal boundary days are planted
max(1, round(5 × factor)) times each — exactly once at the starter scale, so the Phase 7
golden checksums are unchanged. In-memory planning only (no database); the full scale in a
database is opt-in (tests/datagen/test_fullscale.py, SF_FULLSCALE=1).
"""

import os
from decimal import Decimal

import pytest

from datagen import services
from datagen.pipeline.validation import BOUNDARY_CASES, boundary_quota

FULLSCALE = os.environ.get("SF_FULLSCALE") == "1"


@pytest.mark.parametrize(
    ("factor", "quota"), [("0.01", 1), ("0.1", 1), ("0.5", 3), ("1.0", 5), ("0.2", 1)]
)
def test_boundary_quota(factor, quota):
    assert boundary_quota(Decimal(factor)) == quota


@pytest.mark.parametrize("factor", ["0.02", "0.05", "0.25"])
def test_intermediate_scales_validate_with_exact_volumes(factor):
    p = services.plan(seed=20261022, scale_factor=factor)
    v = p.validation
    assert v["passed"], v["problems"]
    counts = p.settings.counts
    assert v["exact_counts"]["students"]["actual"] == counts["students"]
    assert v["exact_counts"]["payments"]["actual"] == counts["payments"]
    assert v["exact_counts"]["invoices"]["actual"] == 3 * counts["students"]
    assert len(p.labels) == 5 * counts["anomalies_per_type"]
    assert set(v["boundary_cases"]["counts"]) == set(BOUNDARY_CASES)


def test_late_fee_boundaries_are_counted_per_occurrence():
    p = services.plan(seed=20261022, scale_factor="0.25")
    late = [x for x in p.world.lines if x.line_type == "LATE_FEE"]
    counts = p.validation["boundary_cases"]["counts"]
    assert counts["late_fee_minimum"] == sum(x.net_amount == Decimal("500.00") for x in late) > 1
    assert counts["late_fee_maximum"] == sum(x.net_amount == Decimal("5000.00") for x in late)


@pytest.mark.skipif(not FULLSCALE, reason="full scale is opt-in: SF_FULLSCALE=1")
@pytest.mark.parametrize("factor", ["0.5", "1.0"])
def test_large_scales_plant_every_boundary_case_often_enough(factor):
    p = services.plan(seed=20261022, scale_factor=factor)
    v = p.validation
    assert v["passed"], v["problems"]
    quota = boundary_quota(Decimal(factor))
    assert min(v["boundary_cases"]["counts"].values()) >= quota
    counts = v["boundary_cases"]["counts"]
    assert counts["overpayment_one_paisa"] >= quota
    assert all(counts[f"withdrawal_day_{d}"] >= quota for d in (0, 7, 8, 14, 15, 27, 28))
