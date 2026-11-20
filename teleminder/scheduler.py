"""Durable calendar scheduler with coalescing and at-most-once delivery attempts.

There is no volatile job store: every tick reads SQLite. Claiming and schedule
advancement share one transaction; a crash never replays an already claimed send.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .service import TaskService
from .timeutil import iso, localize, next_occurrence, parse_time, utcnow

logger = logging.getLogger(__name__)


def notifications_paused(now: datetime, schedule: dict) -> bool:
    if not schedule["enabled"]:
        return True
    return bool(schedule["snoozed_until"] and now < parse_time(schedule["snoozed_until"]))


def in_quiet_hours(now: datetime, schedule: dict) -> bool:
    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    start, end = schedule["quiet_start"], schedule["quiet_end"]
    quiet = (
        (start <= local.hour < end) if start < end else (local.hour >= start or local.hour < end)
    )
    return start != end and quiet


def digest_slot(now: datetime, schedule: dict) -> tuple[datetime, datetime] | None:
    zone = ZoneInfo(schedule["timezone"])
    local = now.astimezone(zone)
    if not schedule["start_hour"] <= local.hour <= schedule["end_hour"]:
        return None
    start = local.replace(
        hour=schedule["start_hour"], minute=0, second=0, microsecond=0, tzinfo=None
    )
    wall_now = local.replace(tzinfo=None)
    step = timedelta(minutes=schedule["interval_minutes"])
    end = start.replace(hour=schedule["end_hour"])
    slot_wall = start + min((wall_now - start) // step, (end - start) // step) * step
    following = slot_wall + step
    if following > end:
        following = start + timedelta(days=1)
    return localize(slot_wall, zone).astimezone(UTC), localize(following, zone).astimezone(UTC)


class Scheduler:
    def __init__(self, service: TaskService, telegram):
        self.service = service
        self.telegram = telegram

    def _claim_events(self, now: datetime) -> list[tuple[str, int | None]]:
        events = []
        stamp = iso(now)  # also rejects naive datetimes
        with self.service.db.transaction() as db:
            schedule = self.service._schedule(db)

            def claim(key, task_id=None):
                if db.execute(
                    "INSERT OR IGNORE INTO deliveries(event_key,status,created_at) VALUES (?,?,?)",
                    (key, "claimed", stamp),
                ).rowcount:
                    events.append((key, task_id))

            # Explicit user/agent requests bypass automatic notification controls.
            for request in db.execute(
                "SELECT id FROM digest_requests WHERE processed=0 ORDER BY created_at LIMIT 3"
            ).fetchall():
                claim("manual:" + request["id"])
                db.execute("UPDATE digest_requests SET processed=1 WHERE id=?", (request["id"],))
            if notifications_paused(now, schedule):
                return events
            quiet = in_quiet_hours(now, schedule)
            for reminder in db.execute(
                """SELECT r.* FROM reminders r JOIN tasks t ON t.id=r.task_id
                WHERE r.enabled=1 AND r.cancelled=0 AND r.next_run<=? AND t.completed_at IS NULL
                AND (?=0 OR r.allow_during_quiet_hours=1) ORDER BY r.next_run LIMIT 10""",
                (stamp, quiet),
            ).fetchall():
                claim(f"reminder:{reminder['id']}:{reminder['next_run']}", reminder["task_id"])
                if reminder["recurrence"]:
                    following = iso(
                        next_occurrence(
                            parse_time(reminder["recurrence_anchor"] or reminder["next_run"]),
                            reminder["recurrence"],
                            schedule["timezone"],
                            now,
                        )
                    )
                    db.execute(
                        "UPDATE reminders SET next_run=? WHERE id=?", (following, reminder["id"])
                    )
                else:
                    db.execute("UPDATE reminders SET enabled=0 WHERE id=?", (reminder["id"],))
            slots = None if quiet else digest_slot(now, schedule)
            if slots:
                slot, following = slots
                row = db.execute("SELECT value FROM state WHERE key='next_digest'").fetchone()
                if row is None or parse_time(row["value"]) <= now:
                    claim("digest:" + iso(slot))
                    db.execute(
                        "INSERT OR REPLACE INTO state VALUES (?, ?)",
                        ("next_digest", iso(following)),
                    )
        return events

    async def tick(self, now: datetime | None = None) -> None:
        now = now if now is not None else utcnow()
        for key, task_id in self._claim_events(now):
            try:
                if task_id is not None:
                    try:
                        task = self.service.get_task(task_id)
                    except ValueError:
                        task = None
                    reminder_id = int(key.split(":", 2)[1])
                    with self.service.db.connection() as db:
                        reminder = db.execute(
                            "SELECT cancelled FROM reminders WHERE id=?", (reminder_id,)
                        ).fetchone()
                    sent = (
                        await self.telegram.send_reminder(task)
                        if task
                        and not task["completed_at"]
                        and reminder
                        and not reminder["cancelled"]
                        else False
                    )
                else:
                    sent = await self.telegram.send_digest()
                status, error = ("sent" if sent else "skipped"), None
            except Exception as exc:
                status, error = "failed", type(exc).__name__
                logger.error("notification_failed", extra={"error_type": error})
            with self.service.db.transaction() as db:
                db.execute(
                    "UPDATE deliveries SET status=?,sent_at=?,error_type=? WHERE event_key=?",
                    (status, iso(now) if status == "sent" else None, error, key),
                )
        self.service.heartbeat()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # Unhandled DB/programming errors terminate the worker, rather than leave
            # an apparently healthy MCP process with dead background scheduling.
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
