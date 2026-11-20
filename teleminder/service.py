"""Single task/schedule API shared by Telegram, scheduler, and both MCP transports."""

import json
import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database
from .timeutil import RECURRENCES, REMINDER_RECURRENCES, iso, next_occurrence, parse_time, utcnow

DEFAULT_SCHEDULE = dict(
    timezone="America/Toronto",
    start_hour=9,
    end_hour=21,
    interval_minutes=60,
    quiet_start=22,
    quiet_end=9,
    enabled=True,
    send_empty=False,
    snoozed_until=None,
)


def valid_id(value: int) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("ID must be a positive integer")
    return value


def _text(value: Any, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(
            f"{name} must be {'nonempty ' if required else ''}text up to {maximum} characters"
        )
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{name} contains control characters")
    return value.strip()


def _task(row) -> dict:
    result = dict(row)
    result["tags"] = json.loads(result["tags"])
    return result


def _reminder(row) -> dict:
    return {
        **dict(row),
        "enabled": bool(row["enabled"]),
        "cancelled": bool(row["cancelled"]),
        "allow_during_quiet_hours": bool(row["allow_during_quiet_hours"]),
    }


class TaskService:
    def __init__(
        self,
        database_path: str,
        timezone: str = "America/Toronto",
        schedule_defaults: dict | None = None,
    ):
        ZoneInfo(timezone)
        self.database_path = database_path
        self.db = Database(database_path)
        defaults = {**DEFAULT_SCHEDULE, "timezone": timezone, **(schedule_defaults or {})}
        self._validate_schedule(defaults)
        with self.db.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO state VALUES (?, ?)", ("schedule", json.dumps(defaults))
            )

    @staticmethod
    def _validate_task(data: dict) -> dict:
        result = dict(data)
        result["title"] = _text(result["title"], "title", 300, True)
        result["description"] = _text(result["description"], "description", 4000)
        if result["priority"] not in ("high", "medium", "low"):
            raise ValueError("priority must be high, medium or low")
        tags = result["tags"]
        if not isinstance(tags, list) or len(tags) > 20:
            raise ValueError("tags must be a list of at most 20 strings")
        result["tags"] = list(dict.fromkeys(_text(t, "tag", 50, True) for t in tags))
        recurrence = result["recurrence"]
        if not isinstance(recurrence, (str, type(None))) or recurrence not in RECURRENCES:
            raise ValueError("recurrence must be daily, weekly, monthly or null")
        if result["due_at"] is not None:
            result["due_at"] = iso(parse_time(result["due_at"]))
        if recurrence and not result["due_at"]:
            raise ValueError("Recurring tasks require a due_at timestamp")
        return result

    @staticmethod
    def _insert_task(db, data: dict, parent_id: int | None = None) -> int:
        return db.execute(
            """INSERT INTO tasks
            (title, description, created_at, due_at, priority, tags, recurrence, parent_id, recurrence_anchor)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["title"],
                data["description"],
                iso(utcnow()),
                data["due_at"],
                data["priority"],
                json.dumps(data["tags"]),
                data["recurrence"],
                parent_id,
                data.get("recurrence_anchor") or data["due_at"],
            ),
        ).lastrowid

    def add_task(
        self,
        title: str,
        description: str = "",
        due_at: str | None = None,
        priority: str = "medium",
        tags: list[str] | None = None,
        recurrence: str | None = None,
    ) -> dict:
        data = self._validate_task(
            dict(
                title=title,
                description=description,
                due_at=due_at,
                priority=priority,
                tags=[] if tags is None else tags,
                recurrence=recurrence,
            )
        )
        with self.db.transaction() as db:
            task_id = self._insert_task(db, data)
            return _task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def create_reminded_task(self, title: str, when: str, recurrence: str | None = None) -> dict:
        # Everything is validated before any insert; a failure inserts nothing.
        title = _text(title, "title", 300, True)
        when_dt = parse_time(when)
        if when_dt <= utcnow():
            raise ValueError("when must be in the future")
        if not isinstance(recurrence, (str, type(None))) or recurrence not in REMINDER_RECURRENCES:
            raise ValueError("recurrence must be hourly, daily, weekly, monthly or null")
        data = dict(
            title=title,
            description="",
            due_at=iso(when_dt),
            priority="medium",
            tags=[],
            recurrence=None,
        )
        with self.db.transaction() as db:
            task_id = self._insert_task(db, data)
            reminder_id = db.execute(
                "INSERT INTO reminders(task_id,next_run,recurrence,recurrence_anchor,enabled,cancelled)"
                " VALUES (?,?,?,?,1,0)",
                (task_id, iso(when_dt), recurrence, iso(when_dt)),
            ).lastrowid
            return {
                "task": _task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
                "reminder": _reminder(
                    db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
                ),
            }

    def get_task(self, task_id: int) -> dict:
        with self.db.connection() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (valid_id(task_id),)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            return _task(row)

    def list_tasks(
        self,
        include_completed: bool = False,
        tag: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        if type(include_completed) is not bool or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("include_completed must be boolean; limit must be 1..500")
        if type(offset) is not int or not 0 <= offset <= 2**31:
            raise ValueError("offset must be a nonnegative integer")
        clauses, params = [], []
        if not include_completed:
            clauses.append("completed_at IS NULL")
        if tag is not None:
            tag = _text(tag, "tag", 50, True)
            clauses.append("EXISTS (SELECT 1 FROM json_each(tasks.tags) WHERE value=?)")
            params.append(tag)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.connection() as db:
            return [
                _task(row)
                for row in db.execute(
                    "SELECT * FROM tasks"
                    + where
                    + """ ORDER BY due_at IS NULL, due_at,
                CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id LIMIT ? OFFSET ?""",
                    (*params, limit, offset),
                )
            ]

    def count_tasks(self, include_completed: bool = False) -> int:
        with self.db.connection() as db:
            return db.execute(
                "SELECT count(*) FROM tasks"
                + ("" if include_completed else " WHERE completed_at IS NULL")
            ).fetchone()[0]

    def update_task(self, task_id: int, **changes) -> dict:
        allowed = {"title", "description", "due_at", "priority", "tags", "recurrence"}
        if not changes or changes.keys() - allowed:
            raise ValueError("Provide supported task fields: " + ", ".join(sorted(allowed)))
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (valid_id(task_id),)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            data = self._validate_task({**_task(row), **changes})
            db.execute(
                "UPDATE tasks SET title=?, description=?, due_at=?, priority=?, tags=?, recurrence=?, recurrence_anchor=? WHERE id=?",
                (
                    data["title"],
                    data["description"],
                    data["due_at"],
                    data["priority"],
                    json.dumps(data["tags"]),
                    data["recurrence"],
                    data["due_at"]
                    if "due_at" in changes or "recurrence" in changes
                    else row["recurrence_anchor"],
                    task_id,
                ),
            )
            return _task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def complete_task(self, task_id: int) -> dict:
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (valid_id(task_id),)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            task = _task(row)
            if task["completed_at"]:
                return task
            now = utcnow()
            if task["recurrence"]:
                task["due_at"] = iso(
                    next_occurrence(
                        parse_time(task["recurrence_anchor"] or task["due_at"]),
                        task["recurrence"],
                        self._schedule(db)["timezone"],
                        max(now, parse_time(task["due_at"])),
                    )
                )
                successor = self._insert_task(db, task, parent_id=task_id)
                # Carry the latest NON-canceled reminder cadence to the successor.
                # A one-shot already sent is disabled but still expresses reminder intent.
                previous = db.execute(
                    "SELECT recurrence, allow_during_quiet_hours FROM reminders"
                    " WHERE task_id=? AND cancelled=0"
                    " ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if previous:
                    db.execute(
                        "INSERT INTO reminders(task_id,next_run,recurrence,recurrence_anchor,"
                        "allow_during_quiet_hours) VALUES (?,?,?,?,?)",
                        (
                            successor,
                            task["due_at"],
                            previous["recurrence"],
                            task["due_at"],
                            previous["allow_during_quiet_hours"],
                        ),
                    )
            db.execute("UPDATE tasks SET completed_at=? WHERE id=?", (iso(now), task_id))
            db.execute("UPDATE reminders SET enabled=0 WHERE task_id=?", (task_id,))
            return _task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def delete_task(self, task_id: int) -> dict:
        with self.db.transaction() as db:
            if not db.execute("DELETE FROM tasks WHERE id=?", (valid_id(task_id),)).rowcount:
                raise ValueError("Task not found")
        return {"deleted": task_id}

    def set_task_reminder(
        self,
        task_id: int,
        when: str,
        recurrence: str | None = None,
        *,
        allow_during_quiet_hours: bool = False,
    ) -> dict:
        when = iso(parse_time(when))
        if not isinstance(recurrence, (str, type(None))) or recurrence not in REMINDER_RECURRENCES:
            raise ValueError("recurrence must be hourly, daily, weekly, monthly or null")
        if type(allow_during_quiet_hours) is not bool:
            raise ValueError("allow_during_quiet_hours must be boolean")
        with self.db.transaction() as db:
            task = db.execute(
                "SELECT completed_at FROM tasks WHERE id=?", (valid_id(task_id),)
            ).fetchone()
            if task is None or task["completed_at"]:
                raise ValueError("Reminder requires an active task")
            # Re-setting replaces active reminders, avoiding accidental duplicate schedules.
            db.execute("UPDATE reminders SET enabled=0, cancelled=1 WHERE task_id=?", (task_id,))
            reminder_id = db.execute(
                "INSERT INTO reminders(task_id,next_run,recurrence,recurrence_anchor,"
                "allow_during_quiet_hours) VALUES (?,?,?,?,?)",
                (task_id, when, recurrence, when, allow_during_quiet_hours),
            ).lastrowid
            return _reminder(
                db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
            )

    def snooze_task_reminder(self, task_id: int, when: str) -> dict:
        when = iso(parse_time(when))
        if parse_time(when) <= utcnow():
            raise ValueError("when must be in the future")
        with self.db.transaction() as db:
            task = db.execute(
                "SELECT completed_at FROM tasks WHERE id=?", (valid_id(task_id),)
            ).fetchone()
            if task is None or task["completed_at"]:
                raise ValueError("Snooze requires an active task")
            latest = db.execute(
                "SELECT * FROM reminders WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            cadence = latest["recurrence"] if latest else None
            anchor = latest["recurrence_anchor"] or latest["next_run"] if latest else when
            # Mark every prior row canceled, including already-claimed ones, then insert.
            db.execute("UPDATE reminders SET enabled=0, cancelled=1 WHERE task_id=?", (task_id,))
            reminder_id = db.execute(
                "INSERT INTO reminders(task_id,next_run,recurrence,recurrence_anchor,"
                "allow_during_quiet_hours) VALUES (?,?,?,?,?)",
                (
                    task_id,
                    when,
                    cadence,
                    anchor,
                    latest["allow_during_quiet_hours"] if latest else 0,
                ),
            ).lastrowid
            return _reminder(
                db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone()
            )

    def cancel_task_reminders(self, task_id: int) -> dict:
        with self.db.transaction() as db:
            valid_id(task_id)
            if db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise ValueError("Task not found")
            previously_enabled = db.execute(
                "SELECT count(*) FROM reminders WHERE task_id=? AND enabled=1", (task_id,)
            ).fetchone()[0]
            db.execute("UPDATE reminders SET enabled=0, cancelled=1 WHERE task_id=?", (task_id,))
            return {"task_id": task_id, "cancelled": previously_enabled}

    def list_reminders(self, task_id: int | None = None) -> list[dict]:
        with self.db.connection() as db:
            rows = db.execute(
                "SELECT * FROM reminders"
                + (" WHERE task_id=?" if task_id is not None else "")
                + " ORDER BY id",
                (valid_id(task_id),) if task_id is not None else (),
            )
            return [_reminder(row) for row in rows]

    @staticmethod
    def _schedule(db) -> dict:
        return json.loads(db.execute("SELECT value FROM state WHERE key='schedule'").fetchone()[0])

    def get_schedule(self) -> dict:
        with self.db.connection() as db:
            return self._schedule(db)

    @staticmethod
    def _validate_schedule(schedule: dict) -> None:
        if schedule.keys() != DEFAULT_SCHEDULE.keys():
            raise ValueError("Unknown schedule field")
        for field in ("start_hour", "end_hour", "quiet_start", "quiet_end"):
            if type(schedule[field]) is not int or not 0 <= schedule[field] <= 23:
                raise ValueError(f"{field} must be an integer from 0 to 23")
        if schedule["end_hour"] < schedule["start_hour"]:
            raise ValueError("end_hour must be >= start_hour")
        if (
            type(schedule["interval_minutes"]) is not int
            or not 1 <= schedule["interval_minutes"] <= 1440
        ):
            raise ValueError("interval_minutes must be 1..1440")
        if type(schedule["enabled"]) is not bool or type(schedule["send_empty"]) is not bool:
            raise ValueError("enabled and send_empty must be booleans")
        try:
            ZoneInfo(schedule["timezone"])
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("timezone must be a valid IANA name") from None
        if schedule["snoozed_until"] is not None:
            schedule["snoozed_until"] = iso(parse_time(schedule["snoozed_until"]))

    def update_schedule(self, **changes) -> dict:
        with self.db.transaction() as db:
            schedule = {**self._schedule(db), **changes}
            self._validate_schedule(schedule)
            db.execute("UPDATE state SET value=? WHERE key='schedule'", (json.dumps(schedule),))
            if changes.keys() - {"snoozed_until", "enabled", "send_empty"}:
                db.execute("DELETE FROM state WHERE key='next_digest'")
            return schedule

    def request_digest(self) -> dict:
        request_id = str(uuid.uuid4())
        with self.db.transaction() as db:
            # Local stdio is trusted, but bound the queue against accidental agent loops.
            if (
                db.execute("SELECT count(*) FROM digest_requests WHERE processed=0").fetchone()[0]
                >= 100
            ):
                raise ValueError("Digest queue is full")
            db.execute(
                "INSERT INTO digest_requests(id,created_at) VALUES (?,?)",
                (request_id, iso(utcnow())),
            )
        return {"queued": True, "request_id": request_id}

    def heartbeat(self) -> None:
        with self.db.transaction() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", ("heartbeat", iso(utcnow())))
            cutoff = iso(utcnow() - timedelta(days=30))
            db.execute(
                "UPDATE deliveries SET status='failed',error_type='AbandonedClaim' WHERE status='claimed' AND created_at<?",
                (iso(utcnow() - timedelta(days=1)),),
            )
            db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
            db.execute("DELETE FROM digest_requests WHERE processed=1 AND created_at<?", (cutoff,))
