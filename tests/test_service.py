from concurrent.futures import ThreadPoolExecutor

import pytest

from teleminder.service import TaskService


@pytest.fixture
def service(tmp_path):
    return TaskService(str(tmp_path / "tasks.sqlite"))


def test_crud_persistence_and_filters(service):
    task = service.add_task(
        "Biology", due_at="2026-11-18T10:00:00-05:00", tags=["school"], priority="high"
    )
    assert task["due_at"] == "2026-11-18T15:00:00+00:00"
    assert TaskService(service.database_path).get_task(task["id"]) == task
    assert service.list_tasks(tag="school") == [task]
    assert service.list_tasks(tag="other") == []
    updated = service.update_task(task["id"], title="Revised", due_at=None)
    assert updated["title"] == "Revised" and updated["due_at"] is None
    service.complete_task(task["id"])
    assert service.list_tasks() == []
    assert len(service.list_tasks(include_completed=True)) == 1
    service.delete_task(task["id"])
    with pytest.raises(ValueError):
        service.get_task(task["id"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"title": "a" * 301},
        {"title": "x", "priority": "urgent"},
        {"title": "x", "due_at": "2026-11-18T10:00:00"},
        {"title": "x", "recurrence": "daily"},
        {"title": "x", "tags": ["a" * 51]},
        {"title": "x", "due_at": "garbage"},
    ],
)
def test_invalid_tasks(service, kwargs):
    with pytest.raises(ValueError):
        service.add_task(**kwargs)


def test_recurring_completion_concurrent_and_reminder(service):
    task = service.add_task("Repeat", due_at="2090-01-31T10:00:00-05:00", recurrence="monthly")
    service.set_task_reminder(task["id"], task["due_at"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.complete_task(task["id"]), range(8)))
    active = service.list_tasks()
    assert len(active) == 1
    assert active[0]["parent_id"] == task["id"]
    assert active[0]["due_at"] == "2090-02-28T15:00:00+00:00"
    assert service.get_task(task["id"])["completed_at"]
    assert len(service.list_reminders(active[0]["id"])) == 1
    assert not service.list_reminders(task["id"])[0]["enabled"]


def test_invalid_ids_and_changes(service):
    for task_id in [0, -1, True, "1; DROP TABLE tasks", 2**70]:
        with pytest.raises(ValueError):
            service.get_task(task_id)
    task = service.add_task("x")
    with pytest.raises(ValueError):
        service.update_task(task["id"], completed_at="x")
    with pytest.raises(ValueError):
        service.set_task_reminder(task["id"], "naive")
    service.set_task_reminder(task["id"], "2090-01-01T00:00:00Z", recurrence="weekly")
    service.delete_task(task["id"])
    assert service.list_reminders() == []


def test_schedule_validation_and_persistence(service):
    assert service.get_schedule()["timezone"] == "America/Toronto"
    schedule = service.update_schedule(
        enabled=False, interval_minutes=30, snoozed_until="2090-01-01T00:00:00Z"
    )
    assert TaskService(service.database_path).get_schedule() == schedule
    for kwargs in [
        {"start_hour": 24},
        {"interval_minutes": 0},
        {"timezone": "bad"},
        {"enabled": "false"},
        {"snoozed_until": "naive"},
        {"start_hour": 22, "end_hour": 9},
    ]:
        with pytest.raises(ValueError):
            service.update_schedule(**kwargs)


def test_monthly_anchor_and_local_wall_time_survive_short_month(service):
    from unittest.mock import patch

    from teleminder.timeutil import parse_time

    with patch("teleminder.service.utcnow", return_value=parse_time("2089-01-01T00:00:00Z")):
        task = service.add_task(
            "Month end", due_at="2090-01-31T10:00:00-05:00", recurrence="monthly"
        )
        service.complete_task(task["id"])
        feb = service.list_tasks()[0]
        service.complete_task(feb["id"])
        march = service.list_tasks()[0]
        assert march["due_at"] == "2090-03-31T14:00:00+00:00"
