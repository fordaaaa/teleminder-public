from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from test_telegram import make_callback_update, make_command_update, make_context

from teleminder.config import Settings
from teleminder.main import make_service, make_telegram_profiles
from teleminder.scheduler import Scheduler

OWNER = 101
GUEST = 202


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr("telegram.ext.ExtBot.send_message", AsyncMock())
    settings = Settings(
        database_path=str(tmp_path / "owner.sqlite"),
        telegram_bot_token="123:test",
        telegram_chat_id=OWNER,
        allowed_user_ids=frozenset({OWNER}),
        allowed_chat_ids=frozenset({OWNER}),
        telegram_profile_user_ids=frozenset({GUEST}),
    )
    service = make_service(settings)
    return make_telegram_profiles(settings, service)


async def test_profiles_isolate_commands_and_callbacks(profiles):
    owner, guest = profiles
    assert owner.application is guest.application
    owner_task = owner.service.add_task("Owner only")
    owner.service.set_task_reminder(owner_task["id"], "2090-01-01T00:00:00Z")

    text = "/remind 10m Guest only"
    update = make_command_update(text, user_id=GUEST, chat_id=GUEST)
    update.effective_chat.type = "private"
    await owner.command(update, make_context(text.split()[1:]))
    [guest_task] = guest.service.list_tasks()
    assert guest_task["id"] == owner_task["id"]
    assert guest_task["title"] == "Guest only"

    update = make_callback_update(f"cancel:{guest_task['id']}", user_id=GUEST, chat_id=GUEST)
    update.effective_chat.type = "private"
    await owner.callback(update, make_context())
    assert guest.service.list_reminders()[0]["cancelled"]
    assert owner.service.list_reminders()[0]["enabled"]

    update = make_command_update("/pause", user_id=GUEST, chat_id=GUEST)
    update.effective_chat.type = "private"
    await owner.command(update, make_context())
    assert not guest.service.get_schedule()["enabled"]
    assert owner.service.get_schedule()["enabled"]


@pytest.mark.parametrize(
    "user_id, chat_id, chat_type",
    [
        (GUEST, OWNER, "private"),
        (OWNER, GUEST, "private"),
        (GUEST, GUEST, "group"),
        (999, GUEST, "private"),
    ],
)
async def test_profile_commands_reject_mismatched_user_or_chat(
    profiles, user_id, chat_id, chat_type
):
    owner, guest = profiles
    text = "/add Something private"
    update = make_command_update(text, user_id=user_id, chat_id=chat_id)
    update.effective_chat.type = chat_type
    await owner.command(update, make_context(text.split()[1:]))
    assert not owner.service.list_tasks()
    assert not guest.service.list_tasks()


async def test_guest_scheduler_delivers_only_to_the_guest(profiles):
    owner, guest = profiles
    guest.service.update_schedule(quiet_start=0, quiet_end=0)
    now = datetime.now(UTC)
    guest.service.create_reminded_task(
        "Guest notification", (now + timedelta(minutes=1)).isoformat()
    )
    await Scheduler(guest.service, guest).tick(now + timedelta(minutes=2))

    sends = guest.application.bot.send_message.await_args_list
    assert any("Guest notification" in call.kwargs["text"] for call in sends)
    assert all(call.kwargs["chat_id"] == GUEST for call in sends)
    assert not owner.service.list_tasks()


async def test_start_shows_an_id_before_access_and_actions_after_access(profiles):
    owner, _ = profiles
    update = make_command_update("/start", user_id=999, chat_id=999)
    update.effective_chat.type = "private"
    await owner.command(update, make_context())
    reply = update.message.reply_text.await_args
    assert "999" in reply.args[0]
    assert not reply.kwargs.get("reply_markup")

    update = make_command_update("/start", user_id=GUEST, chat_id=GUEST)
    update.effective_chat.type = "private"
    await owner.command(update, make_context())
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    actions = {button.callback_data for row in markup.inline_keyboard for button in row}
    assert {"action:add", "action:remind"} <= actions
