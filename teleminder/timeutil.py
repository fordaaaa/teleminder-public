"""Explicit UTC storage and local-calendar recurrence, including DST transitions."""

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

RECURRENCES = {None, "daily", "weekly", "monthly"}
REMINDER_RECURRENCES = RECURRENCES | {"hourly"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Use an ISO 8601 timestamp with timezone offset")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError(
            "Use an ISO 8601 timestamp with timezone offset, e.g. 2027-01-15T14:00:00-05:00"
        ) from None


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def localize(value: datetime, zone: ZoneInfo) -> datetime:
    """First fold on fall-back; roll forward by the gap on spring-forward."""
    return value.replace(tzinfo=zone, fold=0).astimezone(UTC).astimezone(zone)


def advance(value: datetime, recurrence: str, timezone: str) -> datetime:
    local = value.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    if recurrence == "daily":
        local += timedelta(days=1)
    elif recurrence == "weekly":
        local += timedelta(weeks=1)
    elif recurrence == "monthly":
        year = local.year + local.month // 12
        month = local.month % 12 + 1
        local = local.replace(
            year=year, month=month, day=min(local.day, calendar.monthrange(year, month)[1])
        )
    else:
        raise ValueError("Recurrence must be daily, weekly or monthly")
    return localize(local, ZoneInfo(timezone)).astimezone(UTC)


def next_occurrence(value: datetime, recurrence: str, timezone: str, after: datetime) -> datetime:
    """Next anchored occurrence, coalescing missed dates in constant time.

    Keep the original anchor to preserve month-end days and wall-clock time
    after a short month or a spring-forward gap. First occurrence is anchor+1.
    """
    if recurrence == "hourly":
        anchor = value.astimezone(UTC)
        hour = timedelta(hours=1)
        count = max(1, (after - anchor) // hour + 1)
        return anchor + count * hour
    zone = ZoneInfo(timezone)
    anchor = value.astimezone(zone).replace(tzinfo=None)
    local_after = after.astimezone(zone).replace(tzinfo=None)
    if recurrence in ("daily", "weekly"):
        days = 1 if recurrence == "daily" else 7
        count = max(1, (local_after.date() - anchor.date()).days // days)
        candidate = anchor + timedelta(days=count * days)
    elif recurrence == "monthly":
        count = max(1, (local_after.year - anchor.year) * 12 + local_after.month - anchor.month)
        month_index = anchor.year * 12 + anchor.month - 1 + count
        year, month = divmod(month_index, 12)
        candidate = anchor.replace(
            year=year, month=month + 1, day=min(anchor.day, calendar.monthrange(year, month + 1)[1])
        )
    else:
        raise ValueError("Recurrence must be daily, weekly or monthly")
    result = localize(candidate, zone).astimezone(UTC)
    if result <= after:
        # At most one additional step; calculate from the original anchor.
        if recurrence in ("daily", "weekly"):
            candidate = anchor + timedelta(days=(count + 1) * days)
        else:
            month_index += 1
            year, month = divmod(month_index, 12)
            candidate = anchor.replace(
                year=year,
                month=month + 1,
                day=min(anchor.day, calendar.monthrange(year, month + 1)[1]),
            )
        result = localize(candidate, zone).astimezone(UTC)
    return result
