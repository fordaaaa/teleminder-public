"""Tests for teleminder.telegram.TelegramInterface.

Telegram API objects (Update, CallbackContext) are mocked; the TaskService
is replaced by an in-memory fake that honours the fixed contract in
plans/implementation.md (idempotent complete_task, JSON-compatible dicts,
schedule dict). No network access is performed: python-telegram-bot is only
used to construct a real Application with a dummy token, which never polls.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from teleminder.telegram import TelegramInterface  # noqa: F401  (red until implemented)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mock_bot_transport(monkeypatch):
    # PTB instances are immutable; patch the class method, preserving real Application.
    monkeypatch.setattr("telegram.ext.ExtBot.send_message", AsyncMock())


ALLOWED_USER = 111
ALLOWED_CHAT = 222
BOT_TOKEN = "123456:dummy-test-token"


def make_settings(**overrides):
    fields = dict(
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id=ALLOWED_CHAT,
        allowed_user_ids=frozenset({ALLOWED_USER}),
        allowed_chat_ids=frozenset({ALLOWED_CHAT}),
        timezone="America/Toronto",
        database_path=":memory:",
        mcp_token="m" * 40,
        mcp_host="127.0.0.1",
        mcp_port=8765,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class FakeTaskService:
    """In-memory stand-in implementing the fixed TaskService contract."""

    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.next_id = 1
        self.schedule = dict(
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

    # -- tasks ---------------------------------------------------------
    def add_task(
        self, title, description="", due_at=None, priority="medium", tags=None, recurrence=None
    ):
        task = dict(
            id=self.next_id,
            title=title,
            description=description,
            created_at="2026-11-17T00:00:00+00:00",
            due_at=due_at,
            priority=priority,
            tags=list(tags or []),
            completed_at=None,
            recurrence=recurrence,
            parent_id=None,
        )
        self.tasks[task["id"]] = task
        self.next_id += 1
        return dict(task)

    def list_tasks(self, include_completed=False, tag=None, limit=100, offset=0):
        items = [
            dict(t)
            for t in sorted(self.tasks.values(), key=lambda t: t["id"])
            if include_completed or t["completed_at"] is None
        ]
        if tag is not None:
            items = [t for t in items if tag in t["tags"]]
        return items[offset : offset + limit]

    def get_task(self, task_id):
        if task_id not in self.tasks:
            raise ValueError(f"task {task_id} not found")
        return dict(self.tasks[task_id])

    def complete_task(self, task_id):
        if task_id not in self.tasks:
            raise ValueError(f"task {task_id} not found")
        task = self.tasks[task_id]
        if task["completed_at"] is None:
            task = dict(task)
            task["completed_at"] = "2026-11-17T12:00:00+00:00"
            self.tasks[task_id] = task
        return dict(task)

    def delete_task(self, task_id):
        if task_id not in self.tasks:
            raise ValueError(f"task {task_id} not found")
        del self.tasks[task_id]
        return {"deleted": task_id}

    # -- schedule --------------------------------------------------------
    def get_schedule(self):
        return dict(self.schedule)

    def update_schedule(self, **changes):
        self.schedule.update(changes)
        return dict(self.schedule)


def make_context(args=None):
    context = MagicMock()
    context.args = list(args or [])
    return context


def make_command_update(text, user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.callback_query = None
    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock()
    update.message = message
    return update


def make_callback_update(data, user_id=ALLOWED_USER, chat_id=ALLOWED_CHAT):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message = None
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    update.callback_query = query
    return update


def seed_tasks(service, count, title_prefix="task"):
    return [service.add_task(f"{title_prefix} {i + 1}") for i in range(count)]


def reply_texts(update):
    return [
        call.args[0] if call.args else call.kwargs.get("text", "")
        for call in update.message.reply_text.await_args_list
    ]


def query_texts(update):
    texts = []
    if update.callback_query.edit_message_text.await_count:
        texts += [
            c.args[0] if c.args else c.kwargs.get("text", "")
            for c in update.callback_query.edit_message_text.await_args_list
        ]
    if update.callback_query.message.reply_text.await_count:
        texts += [
            c.args[0] if c.args else c.kwargs.get("text", "")
            for c in update.callback_query.message.reply_text.await_args_list
        ]
    return texts


def bot_sent_texts(interface):
    sends = interface.application.bot.send_message.await_args_list
    return [c.kwargs.get("text", c.args[1] if len(c.args) > 1 else "") for c in sends]


def make_interface(service=None, settings=None):
    service = service or FakeTaskService()
    settings = settings or make_settings()
    interface = TelegramInterface(settings, service)
    return interface, service, settings


# ---------------------------------------------------------------------------
# construction / registration
# ---------------------------------------------------------------------------


async def test_constructor_builds_application_and_registers_handlers():
    interface, _, _ = make_interface()
    assert interface.application is not None
    handlers = interface.application.handlers
    total = sum(len(h) for h in handlers.values())
    assert total >= 2  # at least command + callback (plus error handler)
    assert not interface.application.running  # never starts polling


# ---------------------------------------------------------------------------
# authorization: both user AND chat must be allowlisted, fail closed
# ---------------------------------------------------------------------------


async def test_command_rejected_for_unauthorized_user_but_known_chat():
    interface, service, _ = make_interface()
    update = make_command_update("/list", user_id=999)
    await interface.command(update, make_context())
    assert service.list_tasks(limit=1) == []  # no tasks seeded, but importantly:
    # service was never used to leak data; reply either absent or a denial
    texts = reply_texts(update)
    if texts:
        assert "authoriz" in texts[0].lower()


async def test_command_rejected_for_unauthorized_chat_but_known_user():
    interface, service, _ = make_interface()
    service.add_task("secret task")
    update = make_command_update("/list", chat_id=999)
    await interface.command(update, make_context())
    texts = reply_texts(update)
    assert all("secret task" not in t for t in texts)
    if texts:
        assert "authoriz" in texts[0].lower()


async def test_command_rejected_when_both_unauthorized():
    interface, service, _ = make_interface()
    service.add_task("secret task")
    update = make_command_update("/list", user_id=999, chat_id=888)
    await interface.command(update, make_context())
    texts = reply_texts(update)
    assert all("secret task" not in t for t in texts)


async def test_callback_rejected_for_unauthorized_user():
    interface, service, _ = make_interface()
    task = service.add_task("do thing")
    update = make_callback_update(f"done:{task['id']}", user_id=999)
    await interface.callback(update, make_context())
    assert service.get_task(task["id"])["completed_at"] is None


async def test_callback_rejected_for_unauthorized_chat():
    interface, service, _ = make_interface()
    task = service.add_task("do thing")
    update = make_callback_update(f"done:{task['id']}", chat_id=999)
    await interface.callback(update, make_context())
    assert service.get_task(task["id"])["completed_at"] is None


async def test_unauthorized_command_does_not_touch_service():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.list_tasks)
    service.list_tasks = spy
    update = make_command_update("/list", user_id=999)
    await interface.command(update, make_context())
    spy.assert_not_called()


async def test_unauthorized_callback_does_not_touch_service():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.complete_task)
    service.complete_task = spy
    update = make_callback_update("done:1", chat_id=999)
    await interface.callback(update, make_context())
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# /add
# ---------------------------------------------------------------------------


async def test_add_command_creates_task_with_given_title():
    interface, service, _ = make_interface()
    update = make_command_update("/add Buy milk")
    await interface.command(update, make_context(["Buy", "milk"]))
    titles = [t["title"] for t in service.list_tasks()]
    assert titles == ["Buy milk"]
    assert reply_texts(update)  # some confirmation was sent


async def test_add_command_without_title_does_not_create():
    interface, service, _ = make_interface()
    update = make_command_update("/add")
    await interface.command(update, make_context([]))
    assert service.list_tasks() == []
    assert reply_texts(update)  # usage message


async def test_add_title_preserves_multiword_and_punctuation():
    interface, service, _ = make_interface()
    update = make_command_update("/add Call plumber; fix sink (urgent)")
    await interface.command(update, make_context(["Call", "plumber;", "fix", "sink", "(urgent)"]))
    assert service.list_tasks()[0]["title"] == "Call plumber; fix sink (urgent)"


# ---------------------------------------------------------------------------
# /done and completion persistence / repeat idempotence
# ---------------------------------------------------------------------------


async def test_done_command_completes_task():
    interface, service, _ = make_interface()
    task = service.add_task("write report")
    update = make_command_update(f"/done {task['id']}")
    await interface.command(update, make_context([str(task["id"])]))
    assert service.get_task(task["id"])["completed_at"] is not None
    assert reply_texts(update)


async def test_done_command_with_non_numeric_id_does_not_complete():
    interface, service, _ = make_interface()
    service.add_task("stay pending")
    update = make_command_update("/done banana")
    await interface.command(update, make_context(["banana"]))
    assert service.list_tasks()[0]["completed_at"] is None
    assert reply_texts(update)  # error/usage reply


async def test_done_command_with_missing_id_replies_usage():
    interface, service, _ = make_interface()
    update = make_command_update("/done")
    await interface.command(update, make_context([]))
    assert reply_texts(update)


async def test_done_command_with_unknown_id_replies_error():
    interface, service, _ = make_interface()
    update = make_command_update("/done 4242")
    await interface.command(update, make_context(["4242"]))
    assert reply_texts(update)  # error surfaced to user, no crash


async def test_completed_task_disappears_from_default_list():
    interface, service, _ = make_interface()
    task = service.add_task("finish me")
    service.add_task("still open")
    update = make_command_update(f"/done {task['id']}")
    await interface.command(update, make_context([str(task["id"])]))
    list_update = make_command_update("/list")
    await interface.command(list_update, make_context([]))
    texts = "\n".join(reply_texts(list_update))
    assert "still open" in texts
    assert "finish me" not in texts


async def test_repeated_done_is_idempotent():
    interface, service, _ = make_interface()
    task = service.add_task("once only")
    first = make_command_update(f"/done {task['id']}")
    await interface.command(first, make_context([str(task["id"])]))
    completed_at = service.get_task(task["id"])["completed_at"]
    second = make_command_update(f"/done {task['id']}")
    await interface.command(second, make_context([str(task["id"])]))
    after = service.get_task(task["id"])
    assert after["completed_at"] == completed_at  # no further effects
    assert reply_texts(second)  # still acknowledges, does not crash


async def test_callback_done_completes_task_and_answers():
    interface, service, _ = make_interface()
    task = service.add_task("tap to complete")
    update = make_callback_update(f"done:{task['id']}")
    await interface.callback(update, make_context())
    assert service.get_task(task["id"])["completed_at"] is not None
    update.callback_query.answer.assert_awaited()


async def test_callback_done_with_bad_id_does_not_complete():
    interface, service, _ = make_interface()
    service.add_task("untouched")
    update = make_callback_update("done:notanint")
    await interface.callback(update, make_context())
    assert service.list_tasks()[0]["completed_at"] is None
    update.callback_query.answer.assert_awaited()


# ---------------------------------------------------------------------------
# /list pagination (20-item pages) and bounded output
# ---------------------------------------------------------------------------


async def test_list_empty_state():
    interface, service, _ = make_interface()
    update = make_command_update("/list")
    await interface.command(update, make_context([]))
    texts = reply_texts(update)
    assert texts and isinstance(texts[0], str) and texts[0].strip()


async def test_list_first_page_shows_up_to_20_tasks():
    interface, service, _ = make_interface()
    seed_tasks(service, 45)
    update = make_command_update("/list")
    await interface.command(update, make_context([]))
    texts = "\n".join(reply_texts(update))
    assert "task 1" in texts and "task 20" in texts
    assert "task 21" not in texts
    assert len(texts) <= 4096


async def test_list_second_and_third_pages():
    interface, service, _ = make_interface()
    seed_tasks(service, 45)
    page2 = make_command_update("/list 2")
    await interface.command(page2, make_context(["2"]))
    texts2 = "\n".join(reply_texts(page2))
    assert "task 21" in texts2 and "task 40" in texts2
    assert "task 1" not in texts2

    page3 = make_command_update("/list 3")
    await interface.command(page3, make_context(["3"]))
    texts3 = "\n".join(reply_texts(page3))
    assert "task 41" in texts3 and "task 45" in texts3
    assert "task 40" not in texts3


async def test_list_output_stays_within_telegram_limit_with_long_titles():
    interface, service, _ = make_interface()
    for i in range(25):
        service.add_task(f"longline {i} " + "x" * 300)
    update = make_command_update("/list")
    await interface.command(update, make_context([]))
    for text in reply_texts(update):
        assert len(text) <= 4096


async def test_list_beyond_last_page_still_replies():
    interface, service, _ = make_interface()
    seed_tasks(service, 3)
    update = make_command_update("/list 99")
    await interface.command(update, make_context(["99"]))
    assert reply_texts(update)  # graceful reply (empty page or clamp), no crash


# ---------------------------------------------------------------------------
# /today (authorization + replies; content semantics intentionally loose)
# ---------------------------------------------------------------------------


async def test_today_command_replies_for_authorized_user():
    interface, service, _ = make_interface()
    service.add_task("plain task")
    update = make_command_update("/today")
    await interface.command(update, make_context([]))
    texts = reply_texts(update)
    assert texts and isinstance(texts[0], str)


async def test_today_command_denied_for_unauthorized_user():
    interface, service, _ = make_interface()
    update = make_command_update("/today", user_id=999)
    await interface.command(update, make_context([]))
    texts = reply_texts(update)
    if texts:
        assert "authoriz" in texts[0].lower()


# ---------------------------------------------------------------------------
# /settings (show / on / off / snooze MINUTES)
# ---------------------------------------------------------------------------


async def test_settings_show_replies_with_schedule():
    interface, service, _ = make_interface()
    update = make_command_update("/settings")
    await interface.command(update, make_context([]))
    texts = reply_texts(update)
    assert texts and isinstance(texts[0], str) and texts[0].strip()


async def test_settings_show_subcommand_replies_with_schedule():
    interface, service, _ = make_interface()
    update = make_command_update("/settings show")
    await interface.command(update, make_context(["show"]))
    assert reply_texts(update)


async def test_settings_off_disables_schedule():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_command_update("/settings off")
    await interface.command(update, make_context(["off"]))
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("enabled") is False
    assert service.get_schedule()["enabled"] is False


async def test_settings_on_enables_schedule():
    interface, service, _ = make_interface()
    service.update_schedule(enabled=False)
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_command_update("/settings on")
    await interface.command(update, make_context(["on"]))
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("enabled") is True
    assert service.get_schedule()["enabled"] is True


async def test_settings_snooze_sets_snoozed_until():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_command_update("/settings snooze 45")
    await interface.command(update, make_context(["snooze", "45"]))
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("snoozed_until")


async def test_settings_snooze_rejects_non_numeric_minutes():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_command_update("/settings snooze soon")
    await interface.command(update, make_context(["snooze", "soon"]))
    spy.assert_not_called()
    assert reply_texts(update)  # usage/error reply


async def test_settings_unknown_subcommand_does_not_change_schedule():
    interface, service, _ = make_interface()
    before = service.get_schedule()
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_command_update("/settings frobnicate")
    await interface.command(update, make_context(["frobnicate"]))
    spy.assert_not_called()
    assert service.get_schedule() == before
    assert reply_texts(update)


# ---------------------------------------------------------------------------
# callbacks: refresh and snooze:60
# ---------------------------------------------------------------------------


async def test_callback_refresh_answers_without_mutating():
    interface, service, _ = make_interface()
    service.add_task("refreshable")
    update = make_callback_update("refresh")
    await interface.callback(update, make_context())
    update.callback_query.answer.assert_awaited()
    assert service.list_tasks()[0]["completed_at"] is None


async def test_callback_snooze_60_sets_snoozed_until():
    interface, service, _ = make_interface()
    spy = MagicMock(wraps=service.update_schedule)
    service.update_schedule = spy
    update = make_callback_update("snooze:60")
    await interface.callback(update, make_context())
    if spy.called:
        _, kwargs = spy.call_args
        assert kwargs.get("snoozed_until")
    else:  # snooze may be handled inside service.get/update differently, must still answer
        update.callback_query.answer.assert_awaited()


async def test_callback_unknown_data_answers_and_does_not_mutate():
    interface, service, _ = make_interface()
    service.add_task("keep")
    update = make_callback_update("wat:1")
    await interface.callback(update, make_context())
    update.callback_query.answer.assert_awaited()
    assert service.list_tasks()[0]["completed_at"] is None
    assert service.get_schedule()["snoozed_until"] is None


# ---------------------------------------------------------------------------
# send_digest
# ---------------------------------------------------------------------------


async def test_send_digest_returns_false_when_empty_and_send_empty_false():
    interface, service, _ = make_interface()
    assert await interface.send_digest() is False
    assert interface.application.bot.send_message.await_count == 0


async def test_send_digest_sends_when_empty_but_send_empty_true():
    interface, service, _ = make_interface()
    service.update_schedule(send_empty=True)
    assert await interface.send_digest() is True
    assert interface.application.bot.send_message.await_count >= 1


async def test_send_digest_lists_pending_tasks_and_returns_true():
    interface, service, _ = make_interface()
    service.add_task("alpha")
    service.add_task("beta")
    assert await interface.send_digest() is True
    texts = bot_sent_texts(interface)
    assert texts
    assert "alpha" in texts[0] and "beta" in texts[0]


async def test_send_digest_excludes_completed_tasks():
    interface, service, _ = make_interface()
    done = service.add_task("already done")
    service.add_task("still pending")
    service.complete_task(done["id"])
    assert await interface.send_digest() is True
    texts = "\n".join(bot_sent_texts(interface))
    assert "still pending" in texts
    assert "already done" not in texts


async def test_send_digest_caps_message_at_20_tasks_but_shows_total():
    interface, service, _ = make_interface()
    seed_tasks(service, 25)
    assert await interface.send_digest() is True
    texts = bot_sent_texts(interface)
    assert texts
    combined = "\n".join(texts)
    assert "25" in combined  # total surfaced
    shown = sum(1 for i in range(25) if f"task {i + 1}" in combined)
    assert shown <= 20


async def test_send_digest_keeps_text_within_telegram_limit():
    interface, service, _ = make_interface()
    for i in range(20):
        service.add_task(f"digest {i} " + "y" * 400)
    assert await interface.send_digest() is True
    for text in bot_sent_texts(interface):
        assert len(text) <= 4096


async def test_send_digest_callback_data_within_64_bytes():
    interface, service, _ = make_interface()
    seed_tasks(service, 20)
    assert await interface.send_digest() is True
    sends = interface.application.bot.send_message.await_args_list
    for call in sends:
        markup = call.kwargs.get("reply_markup")
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data is not None:
                    assert len(button.callback_data.encode("utf-8")) <= 64


# ---------------------------------------------------------------------------
# on_error: logs only the exception type, never the message
# ---------------------------------------------------------------------------


async def test_on_error_logs_only_exception_type(caplog):
    interface, _, _ = make_interface()
    update = make_command_update("/list")
    context = make_context()
    context.error = ValueError("sensitive-token-12345 leaked detail")
    with caplog.at_level(logging.DEBUG):
        await interface.on_error(update, context)
    assert "sensitive-token-12345" not in caplog.text
    assert "ValueError" in caplog.text
