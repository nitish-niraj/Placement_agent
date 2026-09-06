"""F-020 date parsing — Asia/Kolkata resolution + FR-EVT-005 hallucination
guard (nothing is ever resolved from a value not printed in the text)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pia_worker.events.dateparse import (
    IST,
    DateHit,
    deadline_from_hit,
    parse_date_mentions,
)

NOW = datetime(2026, 9, 6, 19, 0, tzinfo=ZoneInfo("Asia/Kolkata"))  # Sunday evening


def first(text: str) -> DateHit:
    hits = parse_date_mentions(text, NOW)
    assert hits, f"no date parsed from {text!r}"
    return hits[0]


class TestAbsoluteDates:
    def test_day_month_name_year_with_time(self) -> None:
        hit = first("Reporting Date: 7 Sep 2026\nReporting Time: 9:00 AM")
        assert hit.phrase == "7 Sep 2026"
        assert hit.resolved == datetime(2026, 9, 7, 9, 0, tzinfo=IST)
        assert hit.time_inferred is False

    def test_day_first_numeric_indian(self) -> None:
        hit = first("Last date: 25/09/2026")
        assert hit.resolved == datetime(2026, 9, 25, 0, 0, tzinfo=IST)
        assert hit.time_inferred is True

    def test_two_digit_year(self) -> None:
        assert first("drive on 7-9-26").resolved.year == 2026

    def test_month_first(self) -> None:
        assert first("OA on Sep 25 2026").resolved.month == 9

    def test_missing_year_rolls_forward(self) -> None:
        # "5 Sep" said on 6 Sep 2026 means NEXT year's 5 Sep, never a past date
        hit = parse_date_mentions("results on 5 Sep", NOW)[0]
        assert hit.resolved.year == 2027

    def test_iso_date(self) -> None:
        assert first("closes 2026-09-30").resolved.day == 30

    def test_malformed_date_is_never_invented(self) -> None:
        assert parse_date_mentions("due 31/02/2026", NOW) == []


class TestRelativeDates:
    def test_today_with_time(self) -> None:
        # PRD acceptance example: "today 6 PM"
        hit = first("register by today 6 PM")
        assert hit.resolved == datetime(2026, 9, 6, 18, 0, tzinfo=IST)

    def test_tomorrow(self) -> None:
        hit = first("OA tomorrow at 5 pm")
        assert hit.resolved == datetime(2026, 9, 7, 17, 0, tzinfo=IST)

    def test_weekday(self) -> None:
        # Sunday 6 Sep -> next Friday is 11 Sep
        assert first("submit by Friday").resolved.day == 11

    def test_next_weekday_skips_a_week(self) -> None:
        assert first("see you next Friday").resolved.day == 18

    def test_within_hours_is_precise(self) -> None:
        hit = first("complete within 24 hours")
        assert hit.resolved == NOW + timedelta(hours=24)
        assert hit.time_inferred is False


class TestDeadlineSemantics:
    def test_date_only_deadline_is_end_of_day(self) -> None:
        hit = first("Last date: 25/09/2026")
        assert deadline_from_hit(hit).hour == 23
        assert deadline_from_hit(hit).minute == 59

    def test_explicit_time_stands(self) -> None:
        hit = first("OA tomorrow at 5 pm")
        assert deadline_from_hit(hit).hour == 17


class TestHallucinationGuard:
    def test_every_phrase_is_a_literal_substring(self) -> None:
        text = "Reporting Date: 7 Sep 2026, OA tomorrow 5 pm, by Friday, within 24 hours"
        for hit in parse_date_mentions(text, NOW):
            assert hit.phrase in text

    def test_no_date_text_yields_nothing(self) -> None:
        assert parse_date_mentions("form submission is mandatory", NOW) == []
        assert parse_date_mentions("", NOW) == []
