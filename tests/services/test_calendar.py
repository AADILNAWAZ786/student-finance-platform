"""academics.calendar reproduces the frozen term table (DATABASE_DESIGN §3, ARCHITECTURE D1)."""

from datetime import date

import pytest

from academics.calendar import standard_calendar, term_spec


@pytest.mark.parametrize(
    ("season", "invoice", "start", "due", "census", "end"),
    [
        (
            "SPRING",
            date(2026, 1, 5),
            date(2026, 1, 26),
            date(2026, 2, 2),
            date(2026, 2, 23),
            date(2026, 5, 9),
        ),
        (
            "SUMMER",
            date(2026, 5, 4),
            date(2026, 5, 25),
            date(2026, 6, 1),
            date(2026, 6, 22),
            date(2026, 8, 15),
        ),
        (
            "FALL",
            date(2026, 8, 17),
            date(2026, 9, 7),
            date(2026, 9, 14),
            date(2026, 10, 5),
            date(2026, 12, 19),
        ),
    ],
)
def test_2026_matches_frozen_table(season, invoice, start, due, census, end):
    t = term_spec(2026, season)
    assert (t.invoice_date, t.start_date, t.due_date, t.census_date, t.end_date) == (
        invoice,
        start,
        due,
        census,
        end,
    )
    assert t.start_date.weekday() == 0  # Monday
    assert t.is_billable and t.late_fee_grace_days == 14


def test_late_fee_posting_day_matches_table():
    # "Late fee posted" column = due + grace + 1
    from datetime import timedelta

    posted = [
        t.due_date + timedelta(days=t.late_fee_grace_days + 1)
        for t in standard_calendar()
        if t.is_billable
    ]
    assert posted == [date(2026, 2, 17), date(2026, 6, 16), date(2026, 9, 29)]


def test_twelve_terms_nine_historical():
    cal = standard_calendar()
    assert len(cal) == 12
    assert [t.term_id for t in cal if t.is_billable] == [
        "TERM-2026-SPRING",
        "TERM-2026-SUMMER",
        "TERM-2026-FALL",
    ]
    assert cal[0].term_id == "TERM-2023-SPRING" and cal[8].term_id == "TERM-2025-FALL"
    assert all(t.start_date.weekday() == 0 for t in cal)
    starts = [t.start_date for t in cal]
    assert starts == sorted(starts)
    # every business date of the billable terms falls inside 2026 (ID year segment, D1)
    assert all(
        d.year == 2026
        for t in cal
        if t.is_billable
        for d in (t.invoice_date, t.due_date, t.start_date, t.census_date, t.end_date)
    )
