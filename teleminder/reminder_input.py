"""Parser for Telegram reminder time specifications.

Recognizes (optional leading `in`/`until`):
  * compact durations: 10s 15m 2h 1d 1w 1h30m
  * number + unit:     10 minutes, 2 hours (singular and plural units)
  * today / tomorrow TIME (HH:MM or 9am / 5:30pm)
  * YYYY-MM-DD TIME
  * bare TIME (next upcoming local occurrence)
  * aware ISO 8601 timestamp
  * daily TIME / weekly WEEKDAY TIME

Returns UTC-aware datetimes. Durations are limited to [5s, 365 days].
Explicit dated/ISO input must be in the future; bare/daily times resolve to
the next future local date (possibly today). DST spring-forward gaps roll
forward and repeated fall-back times use the first fold (timeutil.localize).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .timeutil import localize, parse_time

MIN_DURATION = timedelta(seconds=5)
MAX_DURATION = timedelta(days=365)

_COMPACT_RE = re.compile(r"([0-9]+)([smhdw])")
_NUMBER_RE = re.compile(r"[0-9]+\Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")
_ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]")
_TIME_24H_RE = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])\Z")
_TIME_12H_RE = re.compile(r"([0-9]{1,2})(?::([0-5][0-9]))?(am|pm)\Z")

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}
_COMPACT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

_BAD_INPUT = 'Could not understand the time. Try e.g. "10m", "tomorrow 09:00", "daily 09:00" or an ISO timestamp with offset.'
_PAST = "That time is in the past."


@dataclass(frozen=True)
class TimeSpec:
    when: datetime
    recurrence: str | None
    consumed: int


def _zone(timezone: str) -> ZoneInfo:
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("A valid IANA timezone is required.")
    try:
        return ZoneInfo(timezone.strip())
    except Exception:
        raise ValueError("Unknown timezone.") from None


def _aware_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")
    return now.astimezone(UTC)


def _parse_clock(token: str) -> tuple[int, int]:
    text = token.lower()
    match = _TIME_12H_RE.match(text)
    if match:
        hour = int(match.group(1))
        if hour < 1 or hour > 12:
            raise ValueError(_BAD_INPUT)
        minute = int(match.group(2) or 0)
        suffix = match.group(3)
        if suffix == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute
    match = _TIME_24H_RE.match(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    raise ValueError(_BAD_INPUT)


def _localize_future(naive: datetime, zone: ZoneInfo, now_utc: datetime) -> datetime:
    candidate = localize(naive, zone).astimezone(UTC)
    if candidate <= now_utc:
        raise ValueError(_PAST)
    return candidate


def _next_occurrence(
    naive: datetime, zone: ZoneInfo, now_utc: datetime, max_days: int = 370
) -> datetime:
    candidate = localize(naive, zone).astimezone(UTC)
    if candidate > now_utc:
        return candidate
    return _localize_future(naive + timedelta(days=1), zone, now_utc)


def _parse_iso_date(token: str) -> tuple[int, int, int]:
    match = _DATE_RE.match(token)
    if not match:
        raise ValueError(_BAD_INPUT)
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        datetime(year, month, day)
    except (ValueError, OverflowError):
        raise ValueError(_BAD_INPUT) from None
    return year, month, day


def _duration_from_compact(token: str) -> timedelta | None:
    if len(token) > 64:
        return None
    pos = 0
    total = 0
    matched = False
    for match in _COMPACT_RE.finditer(token):
        if match.start() != pos:
            return None
        total += int(match.group(1)) * _COMPACT_SECONDS[match.group(2)]
        pos = match.end()
        matched = True
    if not matched or pos != len(token):
        return None
    return _checked_duration(total)


def _checked_duration(total_seconds: int) -> timedelta:
    if total_seconds < int(MIN_DURATION.total_seconds()):
        raise ValueError("Reminders must be at least 5 seconds in the future.")
    if total_seconds > int(MAX_DURATION.total_seconds()):
        raise ValueError("Reminders can be at most 365 days in the future.")
    return timedelta(seconds=total_seconds)


def _duration_from_spaced(words: list[str], index: int) -> tuple[timedelta, int] | None:
    if index + 1 >= len(words):
        return None
    number = words[index].lower()
    unit = words[index + 1].lower()
    if not _NUMBER_RE.match(number) or len(number) > 6:
        return None
    seconds = _UNIT_SECONDS.get(unit)
    if seconds is None:
        return None
    total = int(number) * seconds
    return _checked_duration(total), 2


def _parse_body(words: list[str], timezone: str, now_utc: datetime) -> TimeSpec:
    zone = _zone(timezone)
    now_local = now_utc.astimezone(zone)
    lowered = [w.lower() for w in words]

    # --- durations -----------------------------------------------------
    compact = _duration_from_compact(lowered[0])
    if compact is not None:
        return TimeSpec(now_utc + compact, None, 1)
    spaced = _duration_from_spaced(lowered, 0)
    if spaced is not None:
        delta, consumed = spaced
        return TimeSpec(now_utc + delta, None, consumed)

    head = lowered[0]

    # --- daily TIME ----------------------------------------------------
    if head == "daily":
        if len(words) < 2:
            raise ValueError(_BAD_INPUT)
        hour, minute = _parse_clock(words[1])
        naive = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        when = _next_occurrence(naive, zone, now_utc)
        return TimeSpec(when, "daily", 2)

    # --- weekly WEEKDAY TIME -------------------------------------------
    if head == "weekly":
        if len(words) < 3:
            raise ValueError(_BAD_INPUT)
        weekday = _WEEKDAYS.get(lowered[1])
        if weekday is None:
            raise ValueError(_BAD_INPUT)
        hour, minute = _parse_clock(words[2])
        days_ahead = (weekday - now_local.weekday()) % 7
        naive = (now_local + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        candidate = localize(naive, zone).astimezone(UTC)
        if candidate <= now_utc:
            naive += timedelta(days=7)
            candidate = localize(naive, zone).astimezone(UTC)
        return TimeSpec(candidate, "weekly", 3)

    # --- today / tomorrow TIME -----------------------------------------
    if head in ("today", "tomorrow"):
        if len(words) < 2:
            raise ValueError(_BAD_INPUT)
        hour, minute = _parse_clock(words[1])
        base = now_local if head == "today" else now_local + timedelta(days=1)
        naive = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        when = _localize_future(naive, zone, now_utc)
        return TimeSpec(when, None, 2)

    # --- YYYY-MM-DD TIME ------------------------------------------------
    date_match = _DATE_RE.match(head)
    if date_match:
        year, month, day = _parse_iso_date(head)
        if len(words) < 2:
            raise ValueError(_BAD_INPUT)
        hour, minute = _parse_clock(words[1])
        naive = datetime(year, month, day, hour, minute)
        when = _localize_future(naive, zone, now_utc)
        return TimeSpec(when, None, 2)

    # --- aware ISO timestamp (single token) -----------------------------
    if _ISO_RE.match(head) or "t" in head or head.endswith("z"):
        try:
            when = parse_time(words[0])
        except ValueError:
            raise ValueError(_BAD_INPUT) from None
        if when <= now_utc:
            raise ValueError(_PAST)
        return TimeSpec(when, None, 1)

    # --- bare TIME -> next upcoming local occurrence --------------------
    try:
        hour, minute = _parse_clock(words[0])
    except ValueError:
        raise ValueError(_BAD_INPUT) from None
    naive = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    when = _next_occurrence(naive, zone, now_utc)
    return TimeSpec(when, None, 1)


def parse_time_spec(words, timezone: str, now: datetime | None = None) -> TimeSpec:
    tokens = list(words) if words is not None else []
    tokens = [str(w) for w in tokens]
    if not tokens or all(not t.strip() for t in tokens):
        raise ValueError(_BAD_INPUT)
    now_utc = _aware_now(now)

    # Optional leading in/until applies to any recognized form; it only
    # affects the consumed token count, never the meaning.
    prefix = 0
    body = tokens
    if tokens[0].lower() in ("in", "until"):
        prefix = 1
        body = tokens[1:]
        if not body:
            raise ValueError(_BAD_INPUT)

    result = _parse_body(body, timezone, now_utc)
    if prefix:
        return TimeSpec(result.when, result.recurrence, result.consumed + prefix)
    return result
