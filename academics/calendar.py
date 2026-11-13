"""The documented term calendar (DATABASE_DESIGN.md §3 Term table; ARCHITECTURE D1, D11).

One rule produces all 12 terms:
  start   = first Monday on or after the season anchor (26 Jan, 25 May, 07 Sep)
  invoice = start − 21 days, due = start + 7, census = start + 28,
  end     = start + 103 days (SPRING, FALL) or + 82 days (SUMMER), grace = 14 days.
For 2026 this reproduces the frozen table exactly (a test asserts every date). The 2023–2025
historical admit terms use the same rule; the design gives no dates for them (assumption).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from core.ids import term_id

BILLABLE_YEAR = 2026
HISTORICAL_YEARS = (2023, 2024, 2025)
SEASONS = ("SPRING", "SUMMER", "FALL")
_ANCHOR = {"SPRING": (1, 26), "SUMMER": (5, 25), "FALL": (9, 7)}
_LENGTH_DAYS = {"SPRING": 103, "SUMMER": 82, "FALL": 103}
CENSUS_OFFSET_DAYS = 28
INVOICE_LEAD_DAYS = 21
DUE_AFTER_START_DAYS = 7
LATE_FEE_GRACE_DAYS = 14


@dataclass(frozen=True)
class TermSpec:
    term_id: str
    year: int
    season: str
    is_billable: bool
    invoice_date: date
    due_date: date
    start_date: date
    census_date: date
    end_date: date
    late_fee_grace_days: int = LATE_FEE_GRACE_DAYS

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


def _first_monday_on_or_after(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7)


def term_spec(year: int, season: str) -> TermSpec:
    month, day = _ANCHOR[season]
    start = _first_monday_on_or_after(date(year, month, day))
    return TermSpec(
        term_id=term_id(year, season),
        year=year,
        season=season,
        is_billable=year == BILLABLE_YEAR,
        invoice_date=start - timedelta(days=INVOICE_LEAD_DAYS),
        due_date=start + timedelta(days=DUE_AFTER_START_DAYS),
        start_date=start,
        census_date=start + timedelta(days=CENSUS_OFFSET_DAYS),
        end_date=start + timedelta(days=_LENGTH_DAYS[season]),
    )


def standard_calendar() -> list[TermSpec]:
    """9 historical (non-billable) terms followed by the 3 billable 2026 terms."""
    return [term_spec(y, s) for y in (*HISTORICAL_YEARS, BILLABLE_YEAR) for s in SEASONS]
