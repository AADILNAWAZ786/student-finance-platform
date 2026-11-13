"""core.money and core.fields.MoneyField (ARCHITECTURE.md §2, §8.3, §9.1)."""

from decimal import Decimal as D

import pytest
from rest_framework import serializers

from core.fields import MoneyField
from core.money import (
    ZERO,
    MoneyError,
    apply_rate,
    dsum,
    format_money,
    parse_money,
    round_money,
    to_decimal,
)

pytestmark = pytest.mark.phase0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.025", "0.03"),
        ("0.024", "0.02"),
        ("-0.025", "-0.03"),
        ("1.005", "1.01"),
        ("2.675", "2.68"),
        ("-0.001", "0.00"),
        (5, "5.00"),
    ],
)
def test_round_money_is_half_up(value, expected):
    assert round_money(value) == D(expected)


def test_negative_zero_is_normalised():
    assert str(round_money("-0.004")) == "0.00"
    assert format_money(D("-0.00")) == "0.00"


@pytest.mark.parametrize("bad", [0.1, 1.0, True, None, [1]])
def test_floats_and_non_numbers_are_refused(bad):
    with pytest.raises(MoneyError):
        to_decimal(bad)


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", ""])
def test_non_finite_or_garbage_strings_are_refused(bad):
    with pytest.raises(MoneyError):
        to_decimal(bad)


def test_parse_money_is_strict():
    assert parse_money("12500.5") == D("12500.50")
    assert parse_money(" 7 ") == D("7.00")
    for bad in ["1.001", "1.500", "-1.00", "99999999999.00"]:
        with pytest.raises(MoneyError):
            parse_money(bad)
    assert parse_money("-96000.00", allow_negative=True) == D("-96000.00")


def test_dsum_is_exact_where_float_is_not():
    values = ["0.10", "0.20"]
    assert sum(float(v) for v in values) != 0.3  # 0.30000000000000004 (SQLite REAL / float)
    assert dsum(values) == D("0.30")
    assert dsum(["1234.56"] * 1000) == D("1234560.00")
    assert dsum([]) == ZERO
    with pytest.raises(MoneyError):
        dsum([D("1.00"), 0.5])


def test_apply_rate_matches_business_examples():
    assert apply_rate("160000.00", "0.60") == D("96000.00")  # withdrawal credit
    assert apply_rate("12000.00", "0.60") == D("7200.00")
    assert apply_rate("24999.99", "0.02") == D("500.00")  # late fee 2%
    assert apply_rate("86000.00", "0.6") == D("51600.00")  # scholarship reversal


def test_format_money():
    assert format_money(D("1234.5")) == "1234.50"
    assert format_money("1E+3") == "1000.00"


class _Payment(serializers.Serializer):
    amount = MoneyField()


@pytest.mark.parametrize(
    ("raw", "ok", "expected"),
    [
        ("12500.00", True, D("12500.00")),
        ("12500.5", True, D("12500.50")),
        (12500.5, False, None),
        (12500, False, None),
        ("1.005", False, None),
        ("abc", False, None),
        ("10000000000.00", False, None),
    ],
)
def test_money_field_input(raw, ok, expected):
    s = _Payment(data={"amount": raw})
    assert s.is_valid() is ok, s.errors
    if ok:
        assert s.validated_data["amount"] == expected


def test_money_field_output_is_string():
    assert _Payment({"amount": D("7200")}).data == {"amount": "7200.00"}
