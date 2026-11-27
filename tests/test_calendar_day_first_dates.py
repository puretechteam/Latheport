"""Calendar CSVs with day-first dates keep their day and month, and say what was left out."""

import pytest

from latheport import registry
from latheport.converters import contacts
from latheport.errors import ConversionError


def _ics(records):
    found: list = []
    tokens = registry.running({}, found, [])
    try:
        return contacts.write_events(records), found
    finally:
        registry.reset(tokens)


def _starts(ics: str) -> list[str]:
    return [line.split(":", 1)[1] for line in ics.splitlines()
            if line.startswith("DTSTART") and ":" in line]


def test_day_first_table_reads_both_dates():
    ics, found = _ics([
        {"Start Date": "05/10/2026", "Subject": "early"},
        {"Start Date": "25/10/2026", "Subject": "late"},
    ])
    assert _starts(ics) == ["20261005", "20261025"]
    assert found == []


def test_dashed_day_first_date_is_read():
    ics, found = _ics([{"Start Date": "25-10-2026", "Subject": "one"}])
    assert _starts(ics) == ["20261025"]
    assert found == []


def test_month_first_table_is_unchanged():
    ics, found = _ics([
        {"Start Date": "10/25/2026", "Subject": "late"},
        {"Start Date": "10/05/2026", "Subject": "early"},
    ])
    assert _starts(ics) == ["20261025", "20261005"]
    assert found == []


def test_undecided_table_notes_the_month_first_choice():
    ics, found = _ics([{"Start Date": "05/10/2026", "Subject": "one"}])
    assert _starts(ics) == ["20260510"]
    assert any("05/10/2026" in n and "month" in n for n in found), found


def test_unreadable_rows_are_counted_and_reported():
    ics, found = _ics([
        {"Start Date": "2026-10-05", "Subject": "good"},
        {"Start Date": "not a date", "Subject": "bad"},
        {"Start Date": "", "Subject": "empty"},
    ])
    assert _starts(ics) == ["20261005"]
    assert any("2" in n and "left out" in n for n in found), found


def test_mixed_day_and_month_first_is_an_error():
    with pytest.raises(ConversionError) as exc:
        _ics([
            {"Start Date": "25/10/2026", "Subject": "day first"},
            {"Start Date": "10/25/2026", "Subject": "month first"},
        ])
    assert "25/10/2026" in str(exc.value) and "10/25/2026" in str(exc.value)
