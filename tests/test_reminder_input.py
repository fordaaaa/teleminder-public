"""Tests for teleminder.reminder_input.parse_time_spec.

Parser contract (plans/telegram-reminders.md):

    @dataclass(frozen=True) class TimeSpec:
        when: datetime          # UTC aware
        recurrence: str | None  # 'daily' | 'weekly' | None
        consumed: int           # tokens consumed from `words`

    parse_time_spec(words: Sequence[str], timezone: str,
                    now: datetime | None = None) -> TimeSpec

Recognized forms (optional leading `in`/`until`):
  * compact durations: 10s 15m 2h 1d 1w 1h30m
  * number + unit:     10 minutes, 2 hours (singular and plural units)
  * today/tomorrow HH:MM or 9am / 5:30pm
  * YYYY-MM-DD TIME
  * bare TIME (next upcoming local occurrence)
  * aware ISO timestamp (naive rejected)
  * daily TIME / weekly WEEKDAY TIME

Rules pinned here:
  * duration bounds: min 5 seconds, max 365 days, must be positive
  * explicit dated/today/ISO input must land in the future; bare/daily times
    choose the next future local date (possibly today)
  * DST spring-forward gaps roll forward; fall-back repeats use first fold
    (same behaviour as timeutil.localize)
  * invalid zone / naive `now` / bad input raise ValueError (never e.g.
    OverflowError)
  * tokens after `consumed` are left untouched for the caller to use as title
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from teleminder.reminder_input import TimeSpec, parse_time_spec

TZ = "America/Toronto"  # EST (UTC-5) in November 2026
# Tuesday 2026-11-17 12:30 local (17:30 UTC)
NOW = datetime(2026, 11, 17, 17, 30, tzinfo=UTC)


def spec(words, now=NOW, timezone=TZ):
    return parse_time_spec(list(words), timezone, now=now)


def utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# ---------------------------------------------------------------------------
# TimeSpec shape
# ---------------------------------------------------------------------------


def test_timespec_is_frozen_dataclass_with_expected_fields():
    result = spec(["10m"])
    assert isinstance(result, TimeSpec)
    assert (result.when, result.recurrence, result.consumed) == (
        NOW + timedelta(minutes=10),
        None,
        1,
    )
    with pytest.raises(FrozenInstanceError):
        result.recurrence = "daily"  # type: ignore[misc]


def test_when_is_utc_aware():
    result = spec(["10m"])
    assert result.when.tzinfo is not None
    assert result.when.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# relative durations — compact, compound, optional in/until
# ---------------------------------------------------------------------------


def test_compact_duration_units():
    assert spec(["30s"]).when == NOW + timedelta(seconds=30)
    assert spec(["15m"]).when == NOW + timedelta(minutes=15)
    assert spec(["2h"]).when == NOW + timedelta(hours=2)
    assert spec(["1d"]).when == NOW + timedelta(days=1)
    assert spec(["1w"]).when == NOW + timedelta(weeks=1)
    assert spec(["1h30m"]).when == NOW + timedelta(hours=1, minutes=30)


def test_compact_compound_duration_consumes_one_token():
    result = spec(["1h30m", "stretch", "break"])
    assert result.when == utc(2026, 11, 17, 19, 0)
    assert result.consumed == 1


def test_optional_leading_in_and_until():
    assert spec(["in", "10m"]).when == NOW + timedelta(minutes=10)
    assert spec(["in", "10m"]).consumed == 2
    assert spec(["until", "10m"]).when == NOW + timedelta(minutes=10)
    assert spec(["until", "10m"]).consumed == 2
    assert spec(["in", "15m", "stand", "up"]).consumed == 2


def test_leading_in_without_duration_is_invalid():
    with pytest.raises(ValueError):
        spec(["in"])


def test_leading_until_without_duration_is_invalid():
    with pytest.raises(ValueError):
        spec(["until"])


def test_spaced_number_and_unit():
    assert spec(["10", "minutes"]).when == NOW + timedelta(minutes=10)
    assert spec(["10", "minutes"]).consumed == 2
    assert spec(["2", "hours"]).when == NOW + timedelta(hours=2)
    assert spec(["3", "days"]).when == NOW + timedelta(days=3)
    assert spec(["1", "week"]).when == NOW + timedelta(weeks=1)
    assert spec(["45", "seconds"]).when == NOW + timedelta(seconds=45)
    assert spec(["1", "minute"]).when == NOW + timedelta(minutes=1)


def test_spaced_number_and_unit_with_in_prefix():
    result = spec(["in", "10", "minutes", "tea"])
    assert result.when == NOW + timedelta(minutes=10)
    assert result.consumed == 3


def test_minimum_duration_is_five_seconds():
    assert spec(["5s"]).when == NOW + timedelta(seconds=5)
    assert spec(["5", "seconds"]).when == NOW + timedelta(seconds=5)
    with pytest.raises(ValueError):
        spec(["4s"])
    with pytest.raises(ValueError):
        spec(["1s"])


def test_durations_must_be_positive():
    with pytest.raises(ValueError):
        spec(["0s"])
    with pytest.raises(ValueError):
        spec(["0m"])
    with pytest.raises(ValueError):
        spec(["0", "minutes"])


def test_maximum_duration_is_365_days():
    assert spec(["365d"]).when == NOW + timedelta(days=365)
    assert spec(["52w"]).when == NOW + timedelta(weeks=52)  # 364 days
    with pytest.raises(ValueError):
        spec(["366d"])
    with pytest.raises(ValueError):
        spec(["53w"])  # 371 days > 365


def test_absurd_durations_raise_value_error_not_overflow():
    with pytest.raises(ValueError):
        spec(["999999999999d"])
    with pytest.raises(ValueError):
        spec(["99999999w"])


def test_garbage_unit_is_invalid():
    with pytest.raises(ValueError):
        spec(["10x"])
    with pytest.raises(ValueError):
        spec(["10", "parsecs"])
    with pytest.raises(ValueError):
        spec(["-5m"])


# ---------------------------------------------------------------------------
# today / tomorrow (local wall clock)
# ---------------------------------------------------------------------------


def test_today_future_time():
    result = spec(["today", "17:00", "standup"])
    assert result.when == utc(2026, 11, 17, 22, 0)  # 17:00 EST = 22:00 UTC
    assert result.recurrence is None
    assert result.consumed == 2


def test_today_past_time_is_rejected():
    with pytest.raises(ValueError):
        spec(["today", "09:00"])  # already past at 12:30 local


def test_tomorrow_time_24h():
    result = spec(["tomorrow", "09:00", "dentist"])
    assert result.when == utc(2026, 11, 18, 14, 0)
    assert result.recurrence is None
    assert result.consumed == 2


def test_tomorrow_time_ampm():
    assert spec(["tomorrow", "9am"]).when == utc(2026, 11, 18, 14, 0)
    assert spec(["tomorrow", "5:30pm"]).when == utc(2026, 11, 18, 22, 30)
    assert spec(["tomorrow", "9pm"]).when == utc(2026, 11, 19, 2, 0)


def test_today_ampm():
    assert spec(["today", "5:30pm"]).when == utc(2026, 11, 17, 22, 30)


def test_ampm_noon_and_midnight():
    assert spec(["today", "11:00pm"]).when == utc(2026, 11, 18, 4, 0)
    result = spec(["tomorrow", "12am"])  # midnight
    assert result.when == utc(2026, 11, 18, 5, 0)
    result = spec(["tomorrow", "12pm"])  # noon
    assert result.when == utc(2026, 11, 18, 17, 0)


def test_time_must_be_valid():
    for bad in (
        ["today", "25:00"],
        ["tomorrow", "9:99"],
        ["today", "banana"],
        ["tomorrow", "24:30"],
    ):
        with pytest.raises(ValueError):
            spec(bad)


def test_today_with_am_pm_only_no_minutes():
    assert spec(["tomorrow", "9am"]).when == utc(2026, 11, 18, 14, 0)
    assert spec(["today", "6pm"]).when == utc(2026, 11, 17, 23, 0)


# ---------------------------------------------------------------------------
# bare TIME -> next upcoming local occurrence
# ---------------------------------------------------------------------------


def test_bare_time_later_today():
    assert spec(["18:00", "review"]).when == utc(2026, 11, 17, 23, 0)


def test_bare_time_already_passed_rolls_to_tomorrow():
    assert spec(["09:00"]).when == utc(2026, 11, 18, 14, 0)


def test_bare_time_ampm():
    assert spec(["5pm"]).when == utc(2026, 11, 17, 22, 0)
    assert spec(["9am"]).when == utc(2026, 11, 18, 14, 0)  # already past today
    assert spec(["1pm"]).when == utc(2026, 11, 17, 18, 0)


def test_bare_time_invalid():
    with pytest.raises(ValueError):
        spec(["25:00"])
    with pytest.raises(ValueError):
        spec(["10:70"])


# ---------------------------------------------------------------------------
# explicit date YYYY-MM-DD TIME
# ---------------------------------------------------------------------------


def test_iso_date_and_time():
    result = spec(["2026-11-20", "09:00", "flight"])
    assert result.when == utc(2026, 11, 20, 14, 0)
    assert result.recurrence is None
    assert result.consumed == 2


def test_iso_date_with_ampm():
    assert spec(["2026-11-20", "5:30pm"]).when == utc(2026, 11, 20, 22, 30)


def test_iso_date_in_past_is_rejected():
    with pytest.raises(ValueError):
        spec(["2026-11-16", "09:00"])
    with pytest.raises(ValueError):
        spec(["2026-11-01", "09:00"])


def test_iso_date_invalid_calendar_day():
    with pytest.raises(ValueError):
        spec(["2026-13-01", "09:00"])
    with pytest.raises(ValueError):
        spec(["2026-02-30", "09:00"])


def test_iso_date_today_but_past_time_is_rejected():
    with pytest.raises(ValueError):
        spec(["2026-11-17", "09:00"])


def test_iso_date_requires_time():
    with pytest.raises(ValueError):
        spec(["2026-11-20"])


# ---------------------------------------------------------------------------
# aware ISO timestamps
# ---------------------------------------------------------------------------


def test_aware_iso_timestamp_with_offset():
    result = spec(["2026-11-20T09:00:00-05:00", "call", "mom"])
    assert result.when == utc(2026, 11, 20, 14, 0)
    assert result.consumed == 1


def test_aware_iso_timestamp_zulu():
    result = spec(["2026-11-20T09:00:00Z"])
    assert result.when == utc(2026, 11, 20, 9, 0)


def test_naive_iso_timestamp_is_rejected():
    with pytest.raises(ValueError):
        spec(["2026-11-20T09:00:00"])


def test_past_iso_timestamp_is_rejected():
    with pytest.raises(ValueError):
        spec(["2026-11-17T10:00:00-05:00"])  # 10:00 local EST = 15:00 UTC, already past


def test_invalid_iso_timestamp_is_rejected():
    with pytest.raises(ValueError):
        spec(["2026-11-20T99:00:00-05:00"])


# ---------------------------------------------------------------------------
# recurring: daily TIME / weekly WEEKDAY TIME
# ---------------------------------------------------------------------------


def test_daily_time():
    result = spec(["daily", "09:00", "standup"])
    assert result.when == utc(2026, 11, 18, 14, 0)  # today's already past
    assert result.recurrence == "daily"
    assert result.consumed == 2


def test_daily_time_later_today():
    result = spec(["daily", "18:00"])
    assert result.when == utc(2026, 11, 17, 23, 0)
    assert result.recurrence == "daily"


def test_daily_ampm():
    result = spec(["daily", "9am"])
    assert result.when == utc(2026, 11, 18, 14, 0)
    assert result.recurrence == "daily"


def test_daily_requires_valid_time():
    with pytest.raises(ValueError):
        spec(["daily"])
    with pytest.raises(ValueError):
        spec(["daily", "banana"])
    with pytest.raises(ValueError):
        spec(["daily", "25:00"])


def test_weekly_weekday_time_rolls_forward():
    result = spec(["weekly", "mon", "09:00", "sync"])
    assert result.when == utc(2026, 11, 23, 14, 0)  # next Monday
    assert result.recurrence == "weekly"
    assert result.consumed == 3


def test_weekly_same_weekday_past_time_next_week():
    result = spec(["weekly", "tue", "09:00"])  # now is Tuesday 12:30
    assert result.when == utc(2026, 11, 24, 14, 0)
    assert result.recurrence == "weekly"


def test_weekly_same_weekday_future_time_today():
    result = spec(["weekly", "tue", "18:00"])
    assert result.when == utc(2026, 11, 17, 23, 0)
    assert result.recurrence == "weekly"


def test_weekly_full_weekday_names():
    assert spec(["weekly", "monday", "09:00"]).recurrence == "weekly"
    assert spec(["weekly", "monday", "09:00"]).when == utc(2026, 11, 23, 14, 0)
    assert spec(["weekly", "tuesday", "09:00"]).when == utc(2026, 11, 24, 14, 0)
    assert spec(["weekly", "sunday", "09:00"]).when == utc(2026, 11, 22, 14, 0)


def test_weekly_requires_weekday_and_time():
    with pytest.raises(ValueError):
        spec(["weekly"])
    with pytest.raises(ValueError):
        spec(["weekly", "mon"])
    with pytest.raises(ValueError):
        spec(["weekly", "funday", "09:00"])
    with pytest.raises(ValueError):
        spec(["weekly", "mon", "banana"])


def test_recurrence_is_none_for_one_shot_inputs():
    assert spec(["10m"]).recurrence is None
    assert spec(["tomorrow", "09:00"]).recurrence is None
    assert spec(["2026-11-20", "09:00"]).recurrence is None
    assert spec(["09:00"]).recurrence is None
    assert spec(["2026-11-20T09:00:00-05:00"]).recurrence is None


# ---------------------------------------------------------------------------
# consumed counts / title preservation
# ---------------------------------------------------------------------------


def test_consumed_leaves_title_tokens_untouched():
    words = ["tomorrow", "09:00", "dentist", "appointment"]
    result = spec(words)
    assert result.consumed == 2
    assert words[result.consumed :] == ["dentist", "appointment"]


def test_title_preserved_for_duration_and_recurrence():
    for words, expected_consumed in (
        (["in", "15m", "stand", "up"], 2),
        (["1h30m", "call", "the", "plumber"], 1),
        (["daily", "09:00", "standup", "with", "team"], 2),
        (["weekly", "mon", "09:00", "team", "sync"], 3),
        (["2026-11-20", "09:00", "flight", "to", "yyz"], 2),
        (["2026-11-20T09:00:00-05:00", "ping"], 1),
    ):
        assert spec(words).consumed == expected_consumed


def test_consumed_never_exceeds_input_length():
    result = spec(["10m"])
    assert 0 < result.consumed <= len(["10m"])


# ---------------------------------------------------------------------------
# case-insensitivity
# ---------------------------------------------------------------------------


def test_keywords_are_case_insensitive():
    assert spec(["Tomorrow", "09:00"]).when == utc(2026, 11, 18, 14, 0)
    assert spec(["TOMORROW", "09:00"]).when == utc(2026, 11, 18, 14, 0)
    assert spec(["DAILY", "09:00"]).recurrence == "daily"
    assert spec(["Weekly", "Mon", "09:00"]).recurrence == "weekly"
    assert spec(["TODAY", "17:00"]).when == utc(2026, 11, 17, 22, 0)


# ---------------------------------------------------------------------------
# DST: spring-forward gap rolls forward, fall-back first fold
# ---------------------------------------------------------------------------


def test_dst_gap_rolls_forward():
    # 2026-03-08 02:30 does not exist in America/Toronto (02:00 -> 03:00)
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    result = spec(["2026-03-08", "02:30"], now=now)
    assert result.when == utc(2026, 3, 8, 7, 30)  # 03:30 EDT
    assert result.when.astimezone(ZoneInfo(TZ)).hour == 3
    assert result.when.astimezone(ZoneInfo(TZ)).minute == 30


def test_dst_fold_uses_first_occurrence():
    # 2026-11-01 01:30 happens twice (EDT then EST); first fold is EDT (UTC-4)
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    result = spec(["2026-11-01", "01:30"], now=now)
    assert result.when == utc(2026, 11, 1, 5, 30)  # 01:30 EDT


def test_dst_gap_for_daily_recurrence_rolls_forward():
    now = datetime(2026, 3, 6, 12, 0, tzinfo=UTC)
    # ask for daily at a nonexistent wall-clock time after the gap date
    result = spec(["2026-03-08", "02:30"], now=now)
    assert result.when > now


# ---------------------------------------------------------------------------
# invalid input / now / zone
# ---------------------------------------------------------------------------


def test_empty_words_raise_value_error():
    with pytest.raises(ValueError):
        spec([])


def test_unknown_leading_word_raises_value_error():
    with pytest.raises(ValueError):
        spec(["at", "10m"])


def test_naive_now_raises_value_error():
    with pytest.raises(ValueError):
        parse_time_spec(["10m"], TZ, now=datetime(2026, 11, 17, 12, 30))


def test_invalid_timezone_raises_value_error():
    with pytest.raises(ValueError):
        spec(["10m"], timezone="Mars/Olympus_Mons")


def test_now_defaults_to_current_utc_when_omitted():
    result = parse_time_spec(["10m"], TZ)
    assert result.when > datetime.now(UTC) + timedelta(minutes=9)


def test_explicit_future_requirement_uses_supplied_now():
    # a time that is future relative to one `now` is past relative to another
    words = ["today", "17:00"]
    result = spec(words)
    assert result.when == utc(2026, 11, 17, 22, 0)
    later = utc(2026, 11, 17, 23, 0)  # 18:00 EST — after 17:00 local
    with pytest.raises(ValueError):
        spec(words, now=later)
