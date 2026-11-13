"""BR-FEE-01 / BR-FEE-03: immutable fee-schedule versions with derived windows.

Contract under test (billing.calculators), ARCHITECTURE.md §5:
    resolve_fee_version(versions, on_date) -> version in force (latest effective_from <= on_date)
        raises LookupError if none is in force (FEE_SCHEDULE_NOT_FOUND)
    effective_windows(versions) -> {fee_schedule_id: (effective_from, effective_to_or_None)}
        effective_to = next version's effective_from - 1 day; None for the latest
Neither function may modify its inputs.
"""

from dataclasses import FrozenInstanceError, dataclass
from datetime import date
from decimal import Decimal

import pytest

from billing.calculators import effective_windows, resolve_fee_version


@dataclass(frozen=True)
class Version:
    fee_schedule_id: str
    version: int
    effective_from: date
    amount: Decimal


KEY = "FEE-PRG-004-2026-FALL-TUITION-DOM"
V1 = Version(f"{KEY}-V1", 1, date(2026, 1, 1), Decimal("10000.00"))
V2 = Version(f"{KEY}-V2", 2, date(2026, 8, 1), Decimal("10400.00"))
VERSIONS = (V2, V1)  # deliberately unordered


def test_version_in_force_on_fall_invoice_date_is_v2():
    assert resolve_fee_version(VERSIONS, date(2026, 8, 17)) == V2


def test_day_before_new_version_still_uses_v1():
    assert resolve_fee_version(VERSIONS, date(2026, 7, 31)) == V1


def test_new_version_applies_from_its_effective_from():
    assert resolve_fee_version(VERSIONS, date(2026, 8, 1)) == V2


def test_no_version_in_force_raises():
    with pytest.raises(LookupError):
        resolve_fee_version(VERSIONS, date(2025, 12, 31))


def test_windows_are_derived_without_storing_effective_to():
    assert effective_windows(VERSIONS) == {
        V1.fee_schedule_id: (date(2026, 1, 1), date(2026, 7, 31)),
        V2.fee_schedule_id: (date(2026, 8, 1), None),
    }


def test_versions_are_not_modified():
    before = tuple(VERSIONS)
    effective_windows(VERSIONS)
    resolve_fee_version(VERSIONS, date(2026, 8, 17))
    assert tuple(VERSIONS) == before
    with pytest.raises(FrozenInstanceError):
        V1.amount = Decimal("1")  # type: ignore[misc]
