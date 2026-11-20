"""Test-first contract for service-level reminder controls and schema v3.

Real tmp SQLite throughout. These tests pin the v3 service contract:

- create_reminded_task: atomic non-recurring task + reminder, fully validated
  before any insert (no orphan task/reminder on bad input).
- snooze_task_reminder: future-only; active task required; keeps cadence and the
  ORIGINAL recurrence_anchor of the latest reminder; cancels/disables all prior
  rows; works on already-fired one-shots and on previously canceled reminders;
  old (including already claimed) rows stay marked canceled.
- cancel_task_reminders: disabled+canceled all rows, task retained, returns
  count of previously enabled rows; idempotent.
- complete_task inherits only the latest NON-canceled reminder.
- Schema v3: reminders.cancelled INTEGER NOT NULL DEFAULT 0; migration v2->v3
  preserves existing records and defaults cancelled to false.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from teleminder.database import MIGRATION_1, SCHEMA_VERSION
from teleminder.service import TaskService


@pytest.fixture
def service(tmp_path):
    return TaskService(str(tmp_path / "tasks.sqlite"))


def future_iso(**kwargs) -> str:
    return (datetime.now(UTC) + timedelta(**kwargs)).isoformat()


def past_iso(**kwargs) -> str:
    return (datetime.now(UTC) - timedelta(**kwargs)).isoformat()


def task_count(db_service) -> int:
    with db_service.db.connection() as db:
        return db.execute("SELECT count(*) FROM tasks").fetchone()[0]


def reminder_count(db_service) -> int:
    with db_service.db.connection() as db:
        return db.execute("SELECT count(*) FROM reminders").fetchone()[0]


def all_reminders(db_service, task_id):
    return db_service.list_reminders(task_id)


def mark_fired(db_service, reminder_id):
    """Simulate a scheduler claim for a one-shot reminder (disable the row)."""
    with db_service.db.transaction() as db:
        db.execute("UPDATE reminders SET enabled=0 WHERE id=?", (reminder_id,))


# --- create_reminded_task -------------------------------------------------


def test_create_reminded_task_success(service):
    when = future_iso(hours=3)
    result = service.create_reminded_task("Buy milk", when)
    assert set(result) == {"task", "reminder"}
    task, reminder = result["task"], result["reminder"]
    assert task["title"] == "Buy milk"
    assert task["recurrence"] is None
    assert task["due_at"] == reminder["next_run"]
    assert reminder["task_id"] == task["id"]
    assert reminder["recurrence"] is None
    assert reminder["enabled"] is True
    assert reminder["cancelled"] is False


@pytest.mark.parametrize("cadence", ["hourly", "daily", "weekly", "monthly"])
def test_create_reminded_task_with_repeating_reminder(service, cadence):
    result = service.create_reminded_task("Stretch", future_iso(days=1), cadence)
    assert result["reminder"]["recurrence"] == cadence
    assert result["reminder"]["enabled"] is True
    assert result["task"]["recurrence"] is None  # the task itself never repeats


@pytest.mark.parametrize("title", ["", "   ", "a" * 301, "bad\x00title", "bad\x07title"])
def test_create_reminded_task_invalid_title_creates_nothing(service, title):
    with pytest.raises(ValueError):
        service.create_reminded_task(title, future_iso(hours=1))
    assert task_count(service) == 0
    assert reminder_count(service) == 0


def test_create_reminded_task_naive_when_creates_nothing(service):
    with pytest.raises(ValueError):
        service.create_reminded_task("Naive", "2090-01-01 09:00:00")
    assert task_count(service) == 0
    assert reminder_count(service) == 0


def test_create_reminded_task_garbage_when_creates_nothing(service):
    with pytest.raises(ValueError):
        service.create_reminded_task("Garbage", "not-a-time")
    assert task_count(service) == 0
    assert reminder_count(service) == 0


def test_create_reminded_task_past_when_creates_nothing(service):
    with pytest.raises(ValueError):
        service.create_reminded_task("Past", past_iso(minutes=1))
    assert task_count(service) == 0
    assert reminder_count(service) == 0


@pytest.mark.parametrize("cadence", ["yearly", "DAILY", "week", "dailyy", 123, True])
def test_create_reminded_task_invalid_cadence_creates_nothing(service, cadence):
    with pytest.raises(ValueError):
        service.create_reminded_task("Bad cadence", future_iso(hours=1), cadence)
    assert task_count(service) == 0
    assert reminder_count(service) == 0


# --- snooze_task_reminder -------------------------------------------------


def test_snooze_task_with_no_prior_reminder(service):
    task = service.add_task("Solo", due_at=future_iso(days=2))
    when = future_iso(hours=2)
    reminder = service.snooze_task_reminder(task["id"], when)
    assert reminder["task_id"] == task["id"]
    assert reminder["next_run"] == when
    assert reminder["enabled"] is True
    assert reminder["cancelled"] is False
    assert reminder["recurrence"] is None


def test_snooze_preserves_original_daily_anchor(service):
    task = service.add_task("Daily", due_at=future_iso(days=2))
    original_when = future_iso(days=3)
    service.set_task_reminder(task["id"], original_when, "daily")
    snoozed_when = future_iso(days=1)
    reminder = service.snooze_task_reminder(task["id"], snoozed_when)
    assert reminder["recurrence"] == "daily"
    assert reminder["recurrence_anchor"] == original_when
    assert reminder["next_run"] == snoozed_when
    rows = all_reminders(service, task["id"])
    prior, current = rows[0], rows[-1]
    assert current["id"] == reminder["id"]
    assert prior["enabled"] is False
    assert prior["cancelled"] is True


def test_snooze_preserves_original_monthly_anchor(service):
    task = service.add_task("Monthly", due_at=future_iso(days=40))
    original_when = future_iso(days=30)
    service.set_task_reminder(task["id"], original_when, "monthly")
    snoozed_when = future_iso(hours=5)
    reminder = service.snooze_task_reminder(task["id"], snoozed_when)
    assert reminder["recurrence"] == "monthly"
    assert reminder["recurrence_anchor"] == original_when
    assert reminder["next_run"] == snoozed_when


def test_snooze_works_on_already_fired_one_shot(service):
    task = service.add_task("Ping", due_at=future_iso(days=2))
    reminder = service.set_task_reminder(task["id"], future_iso(hours=1))
    mark_fired(service, reminder["id"])
    snoozed = service.snooze_task_reminder(task["id"], future_iso(hours=3))
    assert snoozed["enabled"] is True
    assert snoozed["cancelled"] is False
    assert all_reminders(service, task["id"])[0]["cancelled"] is True


def test_snooze_reactivates_previously_canceled_reminder(service):
    task = service.add_task("Back", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    service.cancel_task_reminders(task["id"])
    snoozed = service.snooze_task_reminder(task["id"], future_iso(hours=4))
    assert snoozed["enabled"] is True
    assert snoozed["cancelled"] is False


def test_snooze_cancels_every_prior_row(service):
    task = service.add_task("Many", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    service.set_task_reminder(task["id"], future_iso(hours=2))
    service.snooze_task_reminder(task["id"], future_iso(hours=5))
    rows = all_reminders(service, task["id"])
    *prior, current = rows
    assert all(not r["enabled"] and r["cancelled"] for r in prior)
    assert current["enabled"] and not current["cancelled"]


def test_snooze_rejects_completed_and_missing_and_invalid_ids(service):
    task = service.add_task("Done", due_at=future_iso(days=1))
    service.complete_task(task["id"])
    with pytest.raises(ValueError):
        service.snooze_task_reminder(task["id"], future_iso(hours=1))
    with pytest.raises(ValueError):
        service.snooze_task_reminder(999_999, future_iso(hours=1))
    for bad in (0, -1, True, "1", 2**63):
        with pytest.raises(ValueError):
            service.snooze_task_reminder(bad, future_iso(hours=1))


def test_snooze_rejects_past_and_naive_when_leaving_prior_rows_untouched(service):
    task = service.add_task("Stable", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    before = all_reminders(service, task["id"])
    with pytest.raises(ValueError):
        service.snooze_task_reminder(task["id"], past_iso(minutes=1))
    with pytest.raises(ValueError):
        service.snooze_task_reminder(task["id"], "2090-01-01 09:00:00")
    assert all_reminders(service, task["id"]) == before


# --- cancel_task_reminders ------------------------------------------------


def test_cancel_task_reminders_disables_and_marks_rows(service):
    task = service.add_task("Quiet", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    service.set_task_reminder(task["id"], future_iso(hours=2))
    result = service.cancel_task_reminders(task["id"])
    assert result == {"task_id": task["id"], "cancelled": 1}  # only the enabled row counts
    rows = all_reminders(service, task["id"])
    assert all(not r["enabled"] and r["cancelled"] for r in rows)
    assert service.get_task(task["id"])["completed_at"] is None  # task retained


def test_cancel_task_reminders_returns_zero_without_enabled_rows(service):
    task = service.add_task("Already quiet", due_at=future_iso(days=2))
    result = service.cancel_task_reminders(task["id"])
    assert result == {"task_id": task["id"], "cancelled": 0}


@pytest.mark.parametrize("bad", [0, -1, True, "1", 2**63])
def test_cancel_task_reminders_invalid_ids(service, bad):
    with pytest.raises(ValueError):
        service.cancel_task_reminders(bad)


def test_cancel_task_reminders_missing_task(service):
    with pytest.raises(ValueError):
        service.cancel_task_reminders(999_999)


def test_cancel_task_reminders_idempotent(service):
    task = service.add_task("Again", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    first = service.cancel_task_reminders(task["id"])["cancelled"]
    second = service.cancel_task_reminders(task["id"])["cancelled"]
    assert (first, second) == (1, 0)
    assert service.get_task(task["id"])["completed_at"] is None


def test_completion_does_not_resurrect_canceled_reminder(service):
    task = service.add_task("Repeat", due_at=future_iso(days=30), recurrence="monthly")
    service.set_task_reminder(task["id"], future_iso(days=2), "daily")
    service.cancel_task_reminders(task["id"])
    service.complete_task(task["id"])
    successor = next(t for t in service.list_tasks() if t["parent_id"] == task["id"])
    assert all_reminders(service, successor["id"]) == []
    original = all_reminders(service, task["id"])
    assert all(r["cancelled"] for r in original)


def test_completion_inherits_latest_non_canceled_fired_one_shot(service):
    task = service.add_task("Legacy", due_at=future_iso(days=30), recurrence="monthly")
    reminder = service.set_task_reminder(task["id"], future_iso(hours=1))
    mark_fired(service, reminder["id"])  # fired one-shot, not canceled
    service.complete_task(task["id"])
    successor = next(t for t in service.list_tasks() if t["parent_id"] == task["id"])
    inherited = all_reminders(service, successor["id"])
    assert len(inherited) == 1
    assert inherited[0]["cancelled"] is False


def test_set_task_reminder_marks_old_rows_canceled(service):
    task = service.add_task("History", due_at=future_iso(days=2))
    service.set_task_reminder(task["id"], future_iso(hours=1))
    service.set_task_reminder(task["id"], future_iso(hours=2))
    rows = all_reminders(service, task["id"])
    assert rows[0]["cancelled"] is True
    assert rows[0]["enabled"] is False
    assert rows[-1]["cancelled"] is False
    assert rows[-1]["enabled"] is True


# --- migration v2 -> v3 ---------------------------------------------------


def test_migration_v2_to_v3_preserves_records(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    db = sqlite3.connect(path)
    for statement in MIGRATION_1:
        db.execute(statement)
    db.execute("ALTER TABLE tasks ADD COLUMN recurrence_anchor TEXT")
    db.execute("UPDATE tasks SET recurrence_anchor=due_at WHERE recurrence IS NOT NULL")
    db.execute("ALTER TABLE reminders ADD COLUMN recurrence_anchor TEXT")
    db.execute("UPDATE reminders SET recurrence_anchor=next_run WHERE recurrence IS NOT NULL")
    db.execute("PRAGMA user_version=2")
    due = "2090-01-31T15:00:00+00:00"
    db.execute(
        "INSERT INTO tasks(title,description,created_at,due_at,priority,tags,recurrence,recurrence_anchor)"
        " VALUES('Legacy task','', '2020-01-01T00:00:00+00:00', ?, 'high', '[]', 'monthly', ?)",
        (due, due),
    )
    task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
    db.execute(
        "INSERT INTO reminders(task_id,next_run,recurrence,enabled,recurrence_anchor)"
        " VALUES(?,?,'monthly',1,?)",
        (task_id, due, due),
    )
    db.commit()
    db.close()

    service = TaskService(path)
    tasks = service.list_tasks(include_completed=True)
    assert [t["id"] for t in tasks] == [task_id]
    assert tasks[0]["title"] == "Legacy task"
    reminders = service.list_reminders(task_id)
    assert len(reminders) == 1
    assert reminders[0]["next_run"] == due
    assert reminders[0]["recurrence"] == "monthly"
    assert reminders[0]["recurrence_anchor"] == due
    assert reminders[0]["cancelled"] is False
    assert reminders[0]["enabled"] is True
    with service.db.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 3
