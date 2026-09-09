"""Date/time resolution (T-13) and timezone handling (T-14).

The LLM is not trusted with calendar arithmetic. Everything it returns is
re-derived here from the *spoken phrase* where possible, and the two results
are compared. When they disagree, this module wins and the disagreement is
surfaced to the owner on the confirmation card.

Everything leaves this module as an aware UTC datetime.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.nlu.schema import SpokenSlot

WEEKDAYS: dict[str, int] = {
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
    # German
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonnabend": 5, "sonntag": 6,
    # Russian
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3,
    "пятница": 4, "пятницу": 4, "суббота": 5, "субботу": 5,
    "воскресенье": 6,
    # Armenian
    "երկուշաբթի": 0, "երեքշաբթի": 1, "չորեքշաբթի": 2, "հինգշաբթի": 3,
    "ուրբաթ": 4, "շաբաթ": 5, "կիրակի": 6,
}

MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6,
    "juli": 7, "oktober": 10, "dezember": 12,
}

#: "next <weekday>" markers per language.
NEXT_MARKERS = ("next", "nächste", "naechste", "nachste", "kommende", "следующ", "будущ", "հաջորդ")
THIS_MARKERS = ("this", "diese", "этот", "эту", "в эту", "այս")
TOMORROW = ("tomorrow", "morgen", "завтра", "վաղը")
DAY_AFTER_TOMORROW = ("day after tomorrow", "übermorgen", "uebermorgen", "послезавтра")
TODAY = ("today", "heute", "сегодня", "այսօր")

#: City / colloquial zone names the owner is likely to say while travelling.
ZONE_ALIASES: dict[str, str] = {
    "yerevan": "Asia/Yerevan", "armenia": "Asia/Yerevan", "ереван": "Asia/Yerevan",
    "vienna": "Europe/Vienna", "wien": "Europe/Vienna", "austria": "Europe/Vienna",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "munich": "Europe/Berlin", "münchen": "Europe/Berlin",
    "moscow": "Europe/Moscow", "москва": "Europe/Moscow", "москве": "Europe/Moscow",
    "london": "Europe/London", "uk": "Europe/London",
    "paris": "Europe/Paris", "zurich": "Europe/Zurich", "zürich": "Europe/Zurich",
    "dubai": "Asia/Dubai", "tbilisi": "Asia/Tbilisi",
    "new york": "America/New_York", "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "utc": "UTC", "gmt": "UTC",
}


class TimeResolutionError(ValueError):
    """The spoken phrase could not be turned into a concrete instant."""


@dataclass(slots=True)
class ResolvedSlot:
    start_utc: datetime
    end_utc: datetime
    timezone_name: str
    source: str  # "phrase" | "llm" | "phrase+llm"
    warnings: list[str] = field(default_factory=list)

    def local_start(self) -> datetime:
        return self.start_utc.astimezone(ZoneInfo(self.timezone_name))

    def local_end(self) -> datetime:
        return self.end_utc.astimezone(ZoneInfo(self.timezone_name))


# ---------------------------------------------------------------------------
# Timezone detection
# ---------------------------------------------------------------------------

def detect_timezone(raw: str, explicit: str | None = None) -> tuple[str | None, list[str]]:
    """Return an IANA zone named in the utterance, if any."""
    warnings: list[str] = []
    if explicit:
        try:
            ZoneInfo(explicit)
            return explicit, warnings
        except (ZoneInfoNotFoundError, ValueError):
            alias = ZONE_ALIASES.get(explicit.strip().lower())
            if alias:
                return alias, warnings
            warnings.append(f"Ignored unknown timezone {explicit!r} from extraction.")

    lowered = (raw or "").lower()
    for alias, zone in ZONE_ALIASES.items():
        # Only treat a city name as a zone when the speaker framed it as one.
        pattern = rf"\b{re.escape(alias)}\b\s*(time|zeit|время|ժամ)?"
        match = re.search(pattern, lowered)
        if match and (match.group(1) or alias in ("utc", "gmt", "cet", "cest")):
            return zone, warnings
    return None, warnings


# ---------------------------------------------------------------------------
# Time-of-day phrases
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "eins": 1, "ein": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "funf": 5,
    "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
    "zwolf": 12,
}

_MINUTE_WORDS = {
    "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25,
    "twenty-five": 25, "twentyfive": 25, "twenty minutes": 20, "five minutes": 5,
    "fünf": 5, "funf": 5, "zehn": 10, "zwanzig": 20,
    "fünfundzwanzig": 25, "funfundzwanzig": 25,
}

_GERMAN = ("halb", "viertel", "uhr", "morgen", "nachmittag", "abend")


def _word_to_minutes(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 59 else None
    if token in _MINUTE_WORDS:
        return _MINUTE_WORDS[token]
    return _NUMBER_WORDS.get(token)


def _word_to_hour(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 0 <= value <= 23 else None
    return _NUMBER_WORDS.get(token)


def parse_time_phrase(raw: str) -> tuple[time, list[str]] | None:
    """Parse a spoken time of day.

    Handles 24h/12h forms, "quarter past three", "half past three", British
    "half three" (3:30) and German "halb drei" (2:30) / "viertel nach drei".
    Returns ``None`` when no time is present.
    """
    if not raw:
        return None
    text = raw.lower().strip()
    warnings: list[str] = []
    german = any(marker in text for marker in _GERMAN)

    meridiem = None
    if re.search(r"\b(pm|p\.m\.|afternoon|evening|nachmittag|abend|вечера|дня|երեկոյան|ցերեկը)\b", text):
        meridiem = "pm"
    elif re.search(r"\b(am|a\.m\.|morning|morgens|vormittag|утра|առավոտյան)\b", text):
        meridiem = "am"

    # "halb drei" / "half three" / "half past three"
    m = re.search(r"\b(half|halb)\s+(past\s+|nach\s+)?([a-zä-ü]+|\d{1,2})\b", text)
    if m:
        hour = _word_to_hour(m.group(3))
        if hour is not None:
            explicit_past = bool(m.group(2))
            if german and not explicit_past:
                # "halb drei" = half an hour BEFORE three.
                hour -= 1
                warnings.append("Read 'halb' as German half-before-the-hour.")
            return _apply_meridiem(time(hour % 24, 30), meridiem, hour), warnings

    # "quarter past/to three", "viertel nach/vor drei"
    m = re.search(r"\b(quarter|viertel)\s+(past|after|nach|to|before|vor)\s+([a-zä-ü]+|\d{1,2})\b", text)
    if m:
        hour = _word_to_hour(m.group(3))
        if hour is not None:
            if m.group(2) in ("past", "after", "nach"):
                return _apply_meridiem(time(hour % 24, 15), meridiem, hour), warnings
            hour -= 1
            return _apply_meridiem(time(hour % 24, 45), meridiem, hour), warnings

    # "twenty past three" / "twenty-five to four" / "ten to four"
    m = re.search(
        r"\b(\d{1,2}|[a-zä-ü]+(?:[ -][a-zä-ü]+)?)\s+(past|after|to|before)\s+"
        r"([a-zä-ü]+|\d{1,2})\b",
        text,
    )
    if m:
        minutes = _word_to_minutes(m.group(1))
        hour = _word_to_hour(m.group(3))
        if minutes is not None and hour is not None and 1 <= minutes <= 59:
            if m.group(2) in ("past", "after"):
                return _apply_meridiem(time(hour % 24, minutes), meridiem, hour), warnings
            hour -= 1
            return _apply_meridiem(time(hour % 24, 60 - minutes), meridiem, hour), warnings

    # 15:00 / 15.00 / 3:30pm / 15 uhr 30
    m = re.search(r"\b(\d{1,2})[:.\s]?(?:uhr)?\s*(\d{2})\b(?!\s*(?:st|nd|rd|th))", text)
    if m and (":" in text or "uhr" in text):
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour <= 23 and minute <= 59:
            return _apply_meridiem(time(hour, minute), meridiem, hour), warnings

    # "3pm", "15 uhr", bare "3"
    m = re.search(r"\b(\d{1,2})\s*(?:uhr|o'clock)?\s*(am|pm)?\b", text)
    if m:
        hour = int(m.group(1))
        if hour <= 23:
            meridiem = m.group(2) or meridiem
            resolved = _apply_meridiem(time(hour % 24, 0), meridiem, hour)
            if meridiem is None and 1 <= hour <= 7:
                warnings.append(
                    f"Read '{hour}' as {resolved.strftime('%H:%M')} (business-hours reading)."
                )
            return resolved, warnings

    # "four o'clock" / "vier Uhr"
    number_alt = "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
    m = re.search(rf"\b({number_alt})\s*(?:o'clock|uhr)\b", text)
    if m:
        hour = _word_to_hour(m.group(1)) or 0
        return _apply_meridiem(time(hour % 24, 0), meridiem, hour), warnings

    # A spelled-out hour after a time preposition: "at four", "um vier", "в три".
    m = re.search(rf"\b(?:at|um|around|about|в|ժամը)\s+({number_alt})\b", text)
    if m:
        hour = _word_to_hour(m.group(1))
        if hour is not None:
            resolved = _apply_meridiem(time(hour % 24, 0), meridiem, hour)
            if meridiem is None and 1 <= hour <= 7:
                warnings.append(
                    f"Read '{m.group(1)}' as {resolved.strftime('%H:%M')} "
                    "(business-hours reading)."
                )
            return resolved, warnings

    return None


def _apply_meridiem(value: time, meridiem: str | None, spoken_hour: int) -> time:
    hour = value.hour
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif meridiem is None and spoken_hour <= 7 and hour == spoken_hour:
        # Nobody schedules a business meeting at 03:00.
        hour += 12
    return time(hour % 24, value.minute)


# ---------------------------------------------------------------------------
# Date phrases
# ---------------------------------------------------------------------------

def parse_date_phrase(raw: str, today: date) -> tuple[date, list[str]] | None:
    """Resolve a spoken date relative to ``today`` (already in the user's zone)."""
    if not raw:
        return None
    text = raw.lower().strip()
    warnings: list[str] = []

    if any(marker in text for marker in DAY_AFTER_TOMORROW):
        return today + timedelta(days=2), warnings
    if any(re.search(rf"\b{re.escape(m)}\b", text) for m in TOMORROW):
        return today + timedelta(days=1), warnings
    if any(re.search(rf"\b{re.escape(m)}\b", text) for m in TODAY):
        return today, warnings

    # ISO date embedded in the phrase.
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), warnings

    # "15 September" / "September 15" / "15.09" / "15/09"
    month_alt = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th|\.)?\s+({month_alt})\b", text)
    if not m:
        m2 = re.search(rf"\b({month_alt})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text)
        if m2:
            return _month_day(MONTHS[m2.group(1)], int(m2.group(2)), today), warnings
    else:
        return _month_day(MONTHS[m.group(2)], int(m.group(1)), today), warnings

    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if not m.group(3) and candidate < today:
                candidate = date(year + 1, month, day)
            return candidate, warnings

    # Weekday, with "next" meaning *next week's* occurrence.
    weekday_alt = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
    m = re.search(rf"\b({weekday_alt})\b", text)
    if m:
        target = WEEKDAYS[m.group(1)]
        prefix = text[: m.start()]
        if any(marker in prefix for marker in NEXT_MARKERS):
            monday_next_week = today - timedelta(days=today.weekday()) + timedelta(days=7)
            return monday_next_week + timedelta(days=target), warnings
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7 if not any(marker in prefix for marker in THIS_MARKERS) else 0
        return today + timedelta(days=delta), warnings

    # "the 15th" — this month if still ahead, otherwise next month.
    m = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th|\.)\b", text)
    if m:
        return _month_day(today.month, int(m.group(1)), today, allow_rollover=True), warnings

    if "next week" in text:
        return today - timedelta(days=today.weekday()) + timedelta(days=7), warnings

    return None


def _month_day(month: int, day: int, today: date, allow_rollover: bool = True) -> date:
    year = today.year
    day = min(day, calendar.monthrange(year, month)[1])
    candidate = date(year, month, day)
    if candidate < today and allow_rollover:
        if month == today.month:
            month += 1
            if month > 12:
                month, year = 1, year + 1
        else:
            year += 1
        day = min(day, calendar.monthrange(year, month)[1])
        candidate = date(year, month, day)
    return candidate


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_slot(
    spoken: SpokenSlot,
    *,
    now_utc: datetime,
    home_timezone: str,
    duration_minutes: int,
    allow_past: bool = False,
) -> ResolvedSlot:
    """Turn one spoken slot into an absolute UTC interval.

    Raises :class:`TimeResolutionError` when nothing usable can be derived, or
    when the resolved instant is in the past and ``allow_past`` is false.
    """
    warnings: list[str] = []
    zone_name, zone_warnings = detect_timezone(spoken.raw, spoken.timezone)
    warnings.extend(zone_warnings)
    zone_name = zone_name or home_timezone
    zone = ZoneInfo(zone_name)

    now_local = now_utc.astimezone(zone)
    today = now_local.date()

    phrase_date = parse_date_phrase(spoken.raw, today)
    phrase_time = parse_time_phrase(spoken.raw)

    llm_date = _parse_iso_date(spoken.date)
    llm_time = _parse_iso_time(spoken.time)

    resolved_date, date_source = _reconcile_date(phrase_date, llm_date, warnings)
    resolved_time, time_source = _reconcile_time(phrase_time, llm_time, warnings)

    if resolved_date is None:
        raise TimeResolutionError(
            f"I could not work out a date from “{spoken.raw or spoken.date or '?'}”."
        )
    if resolved_time is None:
        raise TimeResolutionError(
            f"I could not work out a time of day from “{spoken.raw or spoken.time or '?'}”."
        )

    local_start = datetime.combine(resolved_date, resolved_time, tzinfo=zone)
    start_utc = local_start.astimezone(UTC)
    end_utc = start_utc + timedelta(minutes=duration_minutes)

    if not allow_past and start_utc <= now_utc:
        raise TimeResolutionError(
            f"{local_start:%a %d %b %H:%M} ({zone_name}) is in the past."
        )

    sources = {date_source, time_source}
    source = "phrase+llm" if len(sources) > 1 else sources.pop()
    return ResolvedSlot(
        start_utc=start_utc,
        end_utc=end_utc,
        timezone_name=zone_name,
        source=source,
        warnings=warnings,
    )


def _reconcile_date(
    phrase: tuple[date, list[str]] | None, llm: date | None, warnings: list[str]
) -> tuple[date | None, str]:
    if phrase is not None:
        value, phrase_warnings = phrase
        warnings.extend(phrase_warnings)
        if llm is not None and llm != value:
            warnings.append(
                f"Extraction said {llm:%a %d %b} but the phrase reads as "
                f"{value:%a %d %b}; using {value:%a %d %b}."
            )
        return value, "phrase"
    if llm is not None:
        return llm, "llm"
    return None, "none"


def _reconcile_time(
    phrase: tuple[time, list[str]] | None, llm: time | None, warnings: list[str]
) -> tuple[time | None, str]:
    if phrase is not None:
        value, phrase_warnings = phrase
        warnings.extend(phrase_warnings)
        if llm is not None and llm != value:
            # A meridiem the phrase cannot see (context from earlier in the
            # sentence) is the common cause; trust the phrase, but say so.
            warnings.append(
                f"Extraction said {llm:%H:%M} but the phrase reads as "
                f"{value:%H:%M}; using {value:%H:%M}."
            )
        return value, "phrase"
    if llm is not None:
        return llm, "llm"
    return None, "none"


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _parse_iso_time(value: str | None) -> time | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%H:%M:%S", "%H:%M", "%H"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None
