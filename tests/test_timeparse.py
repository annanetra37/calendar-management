"""T-13 / T-14: date, time and timezone resolution."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.nlu.schema import SpokenSlot
from app.nlu.timeparse import (
    TimeResolutionError,
    detect_timezone,
    parse_date_phrase,
    parse_time_phrase,
    resolve_slot,
)

MONDAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # 13:00 Yerevan


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("next Tuesday at 3", date(2026, 9, 22)),
        ("Tuesday 2pm", date(2026, 9, 15)),
        ("this Tuesday", date(2026, 9, 15)),
        ("Monday", date(2026, 9, 21)),
        ("this Monday", date(2026, 9, 14)),
        ("tomorrow", date(2026, 9, 15)),
        ("day after tomorrow", date(2026, 9, 16)),
        ("the 15th", date(2026, 9, 15)),
        ("the 3rd", date(2026, 10, 3)),
        ("15 September", date(2026, 9, 15)),
        ("September 21", date(2026, 9, 21)),
        ("2026-10-01", date(2026, 10, 1)),
        ("18.09", date(2026, 9, 18)),
        ("nächsten Dienstag", date(2026, 9, 22)),
        ("Donnerstag", date(2026, 9, 17)),
        ("в среду", date(2026, 9, 16)),
        ("չորեքշաբթի", date(2026, 9, 16)),
    ],
)
def test_date_phrases(phrase: str, expected: date) -> None:
    result = parse_date_phrase(phrase, MONDAY)
    assert result is not None, f"failed to parse {phrase!r}"
    assert result[0] == expected


def test_next_weekday_is_the_following_week_not_tomorrow() -> None:
    """T-13 acceptance criterion, stated explicitly."""
    parsed = parse_date_phrase("next Tuesday at 3", MONDAY)
    assert parsed is not None
    assert parsed[0] == date(2026, 9, 22)
    assert parsed[0] != MONDAY.replace(day=15)


@pytest.mark.parametrize(
    ("phrase", "hour", "minute"),
    [
        ("3pm", 15, 0),
        ("at 3", 15, 0),
        ("15:00", 15, 0),
        ("9am", 9, 0),
        ("10", 10, 0),
        ("Thursday 4", 16, 0),
        ("12", 12, 0),
        ("quarter past three", 15, 15),
        ("quarter to four", 15, 45),
        ("half past three", 15, 30),
        ("half three", 15, 30),
        ("ten to four", 15, 50),
        ("twenty past two", 14, 20),
        ("halb drei", 14, 30),
        ("viertel nach drei", 15, 15),
        ("viertel vor vier", 15, 45),
        ("15 uhr 30", 15, 30),
        ("11 утра", 11, 0),
        ("7 вечера", 19, 0),
    ],
)
def test_time_phrases(phrase: str, hour: int, minute: int) -> None:
    result = parse_time_phrase(phrase)
    assert result is not None, f"failed to parse {phrase!r}"
    assert (result[0].hour, result[0].minute) == (hour, minute)


def test_german_halb_is_before_the_hour_not_after() -> None:
    parsed = parse_time_phrase("halb drei")
    assert parsed is not None
    assert parsed[0].hour == 14 and parsed[0].minute == 30
    assert "halb" in parsed[1][0].lower()


def test_bare_afternoon_hour_is_flagged() -> None:
    parsed = parse_time_phrase("at 4")
    assert parsed is not None
    assert parsed[0].hour == 16
    assert parsed[1], "business-hours reading must be surfaced as a warning"


@pytest.mark.parametrize(
    ("raw", "explicit", "expected"),
    [
        ("3pm Vienna time", None, "Europe/Vienna"),
        ("at 10 Moscow time", None, "Europe/Moscow"),
        ("15:00 UTC", None, "UTC"),
        ("Tuesday at 3", None, None),
        ("Tuesday at 3", "Europe/Berlin", "Europe/Berlin"),
        ("Tuesday at 3", "Vienna", "Europe/Vienna"),
        ("meeting in Vienna on Tuesday", None, None),
    ],
)
def test_timezone_detection(raw: str, explicit: str | None, expected: str | None) -> None:
    zone, _ = detect_timezone(raw, explicit)
    assert zone == expected


def test_resolve_slot_uses_home_timezone_and_returns_utc() -> None:
    resolved = resolve_slot(
        SpokenSlot(raw="Tuesday 2pm", date="2026-09-15", time="14:00"),
        now_utc=NOW,
        home_timezone="Asia/Yerevan",
        duration_minutes=60,
    )
    assert resolved.timezone_name == "Asia/Yerevan"
    assert resolved.start_utc == datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert resolved.local_start().hour == 14
    assert (resolved.end_utc - resolved.start_utc).total_seconds() == 3600


def test_resolve_slot_honours_a_named_timezone() -> None:
    resolved = resolve_slot(
        SpokenSlot(raw="Wednesday at 3pm Vienna time", date="2026-09-16", time="15:00"),
        now_utc=NOW,
        home_timezone="Asia/Yerevan",
        duration_minutes=60,
    )
    assert resolved.timezone_name == "Europe/Vienna"
    assert resolved.local_start().astimezone(ZoneInfo("Europe/Vienna")).hour == 15


def test_phrase_overrides_a_wrong_llm_date_and_warns() -> None:
    """The LLM is not trusted with arithmetic; the phrase wins."""
    resolved = resolve_slot(
        # A model that fumbled "next Tuesday" and returned tomorrow.
        SpokenSlot(raw="next Tuesday at 3", date="2026-09-15", time="15:00"),
        now_utc=NOW,
        home_timezone="Asia/Yerevan",
        duration_minutes=60,
    )
    assert resolved.local_start().date() == date(2026, 9, 22)
    assert any("Extraction said" in w for w in resolved.warnings)


def test_llm_output_is_the_fallback_when_the_phrase_is_unparseable() -> None:
    resolved = resolve_slot(
        SpokenSlot(raw="the usual slot", date="2026-09-16", time="10:00"),
        now_utc=NOW,
        home_timezone="Asia/Yerevan",
        duration_minutes=45,
    )
    assert resolved.source == "llm"
    assert resolved.local_start().hour == 10


def test_past_dates_are_rejected() -> None:
    with pytest.raises(TimeResolutionError, match="in the past"):
        resolve_slot(
            SpokenSlot(raw="yesterday at 10", date="2026-09-13", time="10:00"),
            now_utc=NOW,
            home_timezone="Asia/Yerevan",
            duration_minutes=60,
        )


def test_unresolvable_phrase_raises() -> None:
    with pytest.raises(TimeResolutionError):
        resolve_slot(
            SpokenSlot(raw="sometime soon"),
            now_utc=NOW,
            home_timezone="Asia/Yerevan",
            duration_minutes=60,
        )
