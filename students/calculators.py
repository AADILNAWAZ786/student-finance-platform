"""Pure student calculations (no ORM)."""

from datetime import date


def age_on(date_of_birth: date, on: date) -> int:
    """Completed years on ``on`` (a 29 Feb birthday counts on 1 Mar in non-leap years)."""
    before_birthday = (on.month, on.day) < (date_of_birth.month, date_of_birth.day)
    return on.year - date_of_birth.year - int(before_birthday)
