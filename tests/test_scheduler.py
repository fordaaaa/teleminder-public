from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from teleminder.scheduler import Scheduler
from teleminder.service import TaskService
from teleminder.timeutil import advance, parse_time


@pytest.fixture
def setup(tmp_path):
    service = TaskService(str(tmp_path / "tasks.sqlite"))
    telegram = AsyncMock()
    telegram.send_digest.return_value = True
    telegram.send_reminder.return_value = True
    return service, telegram, Scheduler(service, telegram)


async def test_digest_restart_and_quiet_hours(setup):
    service, telegram, scheduler = setup
    service.add_task("x")
    await scheduler.tick(parse_time("2026-11-17T09:03:00-05:00"))
    await Scheduler(service, telegram).tick(parse_time("2026-11-17T09:05:00-05:00"))
    assert telegram.send_digest.await_count == 1
    await scheduler.tick(parse_time("2026-11-17T14:03:00-05:00"))  # missed runs coalesce
    assert telegram.send_digest.await_count == 2
    await scheduler.tick(parse_time("2026-11-17T21:00:00-05:00"))
    assert telegram.send_digest.await_count == 3
    await scheduler.tick(parse_time("2026-11-17T22:00:00-05:00"))
    await scheduler.tick(parse_time("2026-11-18T08:59:00-05:00"))
    assert telegram.send_digest.await_count == 3


async def test_due_reminder_deferred_snoozed_then_coalesced(setup):
    service, telegram, scheduler = setup
    task = service.add_task("x")
    service.set_task_reminder(task["id"], "2026-11-16T23:00:00-05:00", recurrence="daily")
    service.update_schedule(snoozed_until="2026-11-17T10:00:00-05:00")
    await scheduler.tick(parse_time("2026-11-17T09:00:00-05:00"))
    assert not telegram.send_reminder.called
    await scheduler.tick(parse_time("2026-11-17T10:01:00-05:00"))
    assert telegram.send_reminder.await_count == 1
    assert parse_time(service.list_reminders()[0]["next_run"]) > parse_time(
        "2026-11-17T10:01:00-05:00"
    )
    await scheduler.tick(parse_time("2026-11-17T10:02:00-05:00"))
    assert telegram.send_reminder.await_count == 1


async def test_disabled_and_completed_reminders(setup):
    service, telegram, scheduler = setup
    task = service.add_task("x")
    service.set_task_reminder(task["id"], "2026-11-17T10:00:00-05:00")
    service.update_schedule(enabled=False)
    await scheduler.tick(parse_time("2026-11-17T11:00:00-05:00"))
    assert not telegram.send_reminder.called
    service.complete_task(task["id"])
    service.update_schedule(enabled=True)
    await scheduler.tick(parse_time("2026-11-17T11:00:00-05:00"))
    assert not telegram.send_reminder.called


async def test_failure_not_retried_and_ledger_visible(setup):
    service, telegram, scheduler = setup
    task = service.add_task("x")
    service.set_task_reminder(task["id"], "2026-11-17T10:00:00-05:00")
    telegram.send_reminder.side_effect = TimeoutError("ambiguous")
    await scheduler.tick(parse_time("2026-11-17T10:00:00-05:00"))
    await Scheduler(service, telegram).tick(parse_time("2026-11-17T10:01:00-05:00"))
    assert telegram.send_reminder.await_count == 1
    with service.db.connection() as db:
        assert (
            db.execute(
                "SELECT status FROM deliveries WHERE event_key LIKE 'reminder:%'"
            ).fetchone()[0]
            == "failed"
        )


async def test_manual_digest_durable_queue_bypasses_quiet(setup):
    service, telegram, scheduler = setup
    service.update_schedule(enabled=False)
    result = service.request_digest()
    assert result["queued"]
    await scheduler.tick(parse_time("2026-11-17T23:00:00-05:00"))
    await scheduler.tick(parse_time("2026-11-17T23:00:01-05:00"))
    assert telegram.send_digest.await_count == 1


async def test_hourly_reminder_can_deliver_during_quiet_hours(setup):
    service, telegram, scheduler = setup
    start = "2026-11-17T23:00:00-05:00"
    task = service.add_task("Hourly checklist")
    regular = service.add_task("Regular reminder")
    service.set_task_reminder(regular["id"], start, recurrence="hourly")
    service.set_task_reminder(task["id"], start, recurrence="hourly", allow_during_quiet_hours=True)

    for when in ("23:00", "23:02", "23:59"):
        await scheduler.tick(parse_time(f"2026-11-17T{when}:00-05:00"))
    await scheduler.tick(parse_time("2026-11-18T00:00:00-05:00"))
    assert [call.args[0]["id"] for call in telegram.send_reminder.await_args_list] == [
        task["id"],
        task["id"],
    ]
    assert not telegram.send_digest.called

    service.update_schedule(enabled=False)
    await scheduler.tick(parse_time("2026-11-18T01:00:00-05:00"))
    assert telegram.send_reminder.await_count == 2


@pytest.mark.parametrize(
    "before,expected",
    [
        ("2027-03-13T02:30:00-05:00", "2027-03-14T07:30:00+00:00"),
        ("2027-11-06T01:30:00-04:00", "2027-11-07T05:30:00+00:00"),
        ("2027-03-13T09:00:00-05:00", "2027-03-14T13:00:00+00:00"),
    ],
)
def test_dst_calendar_recurrence(before, expected):
    assert advance(parse_time(before), "daily", "America/Toronto").isoformat() == expected


def test_naive_tick_rejected(setup):
    # Helpers reject naive timestamps rather than guessing host timezone.
    from teleminder.timeutil import iso

    with pytest.raises(ValueError):
        iso(datetime(2026, 1, 1))


async def test_two_scheduler_instances_claim_once(setup):
    import asyncio

    service, telegram, scheduler = setup
    task = service.add_task("Only once")
    service.set_task_reminder(task["id"], "2026-11-17T10:00:00-05:00")
    now = parse_time("2026-11-17T10:00:00-05:00")
    await asyncio.gather(scheduler.tick(now), Scheduler(service, telegram).tick(now))
    assert telegram.send_reminder.await_count == 1
    assert telegram.send_digest.await_count == 1


def test_anchor_skips_centuries_without_linear_iteration():
    from teleminder.timeutil import next_occurrence

    value = next_occurrence(
        parse_time("1900-01-31T10:00:00-05:00"),
        "monthly",
        "America/Toronto",
        parse_time("2027-02-28T10:01:00-05:00"),
    )
    assert value == parse_time("2027-03-31T10:00:00-04:00")


@pytest.mark.parametrize("action", ["cancel", "snooze"])
async def test_claimed_reminder_is_skipped_when_owner_changes_it_before_send(setup, action):
    service, telegram, scheduler = setup
    task = service.add_task("Do not send old reminder")
    service.set_task_reminder(task["id"], "2026-11-17T10:00:00-05:00")
    now = parse_time("2026-11-17T10:01:00-05:00")
    events = scheduler._claim_events(now)
    if action == "cancel":
        service.cancel_task_reminders(task["id"])
    else:
        service.snooze_task_reminder(task["id"], "2090-01-01T00:00:00Z")
    scheduler._claim_events = lambda current: events
    await scheduler.tick(now)
    assert not telegram.send_reminder.called
