from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_telegram import make_callback_update, make_command_update, make_context

from teleminder.config import Settings
from teleminder.service import TaskService
from teleminder.telegram import TelegramInterface
from teleminder.timeutil import parse_time


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram.ext.ExtBot.send_message", AsyncMock())
    service = TaskService(str(tmp_path / "controls.sqlite"))
    settings = Settings(
        telegram_bot_token="123:test",
        telegram_chat_id=222,
        allowed_user_ids=frozenset({111}),
        allowed_chat_ids=frozenset({222}),
    )
    return TelegramInterface(settings, service), service


async def command(interface, text):
    update = make_command_update(text)
    await interface.command(update, make_context(text.split()[1:]))
    return update


async def test_manual_reminder_creates_task_and_schedule(setup):
    interface, service = setup
    update = await command(interface, "/remind 10m Wash dishes")
    tasks = service.list_tasks()
    assert len(tasks) == 1 and tasks[0]["title"] == "Wash dishes"
    assert len(service.list_reminders()) == 1
    assert service.list_reminders()[0]["enabled"]
    assert update.message.reply_text.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.parametrize(
    "text",
    ["/remind someday No task", "/remind 10m", "/remind today 00:00 Past", "/snooze 1 nonsense"],
)
async def test_invalid_manual_input_does_not_create_tasks(setup, text):
    interface, service = setup
    await command(interface, text)
    assert service.count_tasks() == 0 and not service.list_reminders()


async def test_existing_task_can_get_a_manual_reminder_without_duplication(setup):
    interface, service = setup
    task = service.add_task("Existing")
    await command(interface, f"/remind #{task['id']} 20m")
    assert service.count_tasks() == 1
    assert service.list_reminders(task["id"])[0]["enabled"]


async def test_snooze_until_local_date_and_pause_resume(setup):
    interface, service = setup
    task = service.add_task("Snooze me")
    service.set_task_reminder(task["id"], "2090-01-01T15:00:00Z", recurrence="daily")
    await command(interface, f"/snooze {task['id']} until tomorrow 09:00")
    rows = service.list_reminders(task["id"])
    assert rows[-1]["enabled"] and rows[-1]["recurrence"] == "daily"
    assert parse_time(rows[-1]["next_run"]) > datetime.now(UTC)
    assert rows[0]["cancelled"]
    await command(interface, "/pause 2h")
    assert service.get_schedule()["snoozed_until"]
    await command(interface, "/resume")
    assert service.get_schedule()["enabled"] and service.get_schedule()["snoozed_until"] is None
    await command(interface, "/pause")
    assert not service.get_schedule()["enabled"]


async def test_inline_snooze_only_changes_that_task(setup):
    interface, service = setup
    first = service.add_task("First")
    second = service.add_task("Second")
    service.set_task_reminder(first["id"], "2090-01-01T00:00:00Z")
    service.set_task_reminder(second["id"], "2090-01-01T00:00:00Z")
    original = service.list_reminders(second["id"])
    update = make_callback_update(f"later:{first['id']}:15")
    await interface.callback(update, make_context())
    assert update.callback_query.answer.await_count == 1
    assert len(service.list_reminders(first["id"])) == 2
    assert service.list_reminders(second["id"]) == original
    assert update.callback_query.edit_message_text.await_count == 1


async def test_cancel_does_not_complete_or_delete_task(setup):
    interface, service = setup
    task = service.add_task("Keep task")
    service.set_task_reminder(task["id"], "2090-01-01T00:00:00Z")
    update = make_callback_update(f"cancel:{task['id']}")
    await interface.callback(update, make_context())
    assert not service.get_task(task["id"])["completed_at"]
    assert service.list_reminders(task["id"])[0]["cancelled"]
    assert update.callback_query.answer.await_count == 1


@pytest.mark.parametrize(
    "data",
    [
        "later:1:-1",
        "later:1:999999",
        "later:1:15evil",
        "cancel:bad",
        "custom:-1",
        "later:9999999999999999999999999:15",
    ],
)
async def test_bad_callback_cannot_mutate_reminders(setup, data):
    interface, service = setup
    task = service.add_task("Keep")
    service.set_task_reminder(task["id"], "2090-01-01T00:00:00Z")
    before = service.list_reminders()
    await interface.callback(make_callback_update(data), make_context())
    assert service.list_reminders() == before


@pytest.mark.parametrize(
    "text",
    ["/remind 10m Secret", "/snooze 1 2h", "/pause 2h", "/resume", "/cancel 1", "/reminders"],
)
async def test_new_commands_reject_unapproved_user_before_service_access(setup, text):
    interface, _ = setup
    interface.service = MagicMock()
    update = make_command_update(text, user_id=999)
    await interface.command(update, make_context(text.split()[1:]))
    assert not interface.service.mock_calls


async def test_help_and_real_reminder_expose_the_new_actions(setup):
    interface, service = setup
    update = await command(interface, "/help")
    text = update.message.reply_text.await_args.args[0]
    assert all(name in text for name in ["/remind", "/snooze", "/pause", "/resume", "/cancel"])
    task = service.add_task("<test>", due_at="2090-01-01T00:00:00Z")
    await interface.send_reminder(task)
    call = interface.application.bot.send_message.await_args.kwargs
    assert call["parse_mode"] == "HTML" and "&lt;test&gt;" in call["text"]
    callbacks = {
        button.callback_data for row in call["reply_markup"].inline_keyboard for button in row
    }
    assert {f"done:{task['id']}", f"later:{task['id']}:15", f"later:{task['id']}:60"} <= callbacks
    assert all(len(data.encode()) <= 64 for data in callbacks)


async def test_timed_global_pause_replaces_an_indefinite_pause(setup):
    interface, service = setup
    service.update_schedule(enabled=False)
    await interface.callback(make_callback_update("snooze:60"), make_context())
    assert service.get_schedule()["enabled"]
    assert service.get_schedule()["snoozed_until"]
