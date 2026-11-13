"""Serializer fields shared by all apps."""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from core.money import MAX_DIGITS


class DecimalStringField(serializers.DecimalField):
    """A decimal sent as a JSON **string** (ARCHITECTURE §9.1).

    JSON numbers are rejected so binary floating point never touches the value.
    Used for every non-money decimal too (GPA, need index), so all decimals in the
    API behave the same way. Output is always a string.
    """

    default_error_messages = {
        "must_be_string": 'Send this decimal as a string, e.g. "3.70".',
    }
    string_error_key = "must_be_string"

    def __init__(self, **kwargs):
        kwargs["coerce_to_string"] = True
        super().__init__(**kwargs)

    def to_internal_value(self, data) -> Decimal:
        if not isinstance(data, str):
            self.fail(self.string_error_key)
        return super().to_internal_value(data) + 0  # "+ 0" turns -0.00 into 0.00


class MoneyField(DecimalStringField):
    """Money: string, at most 2 decimal places, Decimal(12, 2), e.g. ``"12500.00"``."""

    default_error_messages = {
        "money_must_be_string": 'Money must be sent as a string, e.g. "1250.00".',
    }
    string_error_key = "money_must_be_string"

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", MAX_DIGITS)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(**kwargs)
