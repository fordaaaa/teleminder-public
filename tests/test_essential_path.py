"""Real PTB request/Update plumbing and real SQLite/MCP; only Telegram network is fake."""

import json

import pytest
from telegram import Update
from telegram.ext import Application, ExtBot
from telegram.request import BaseRequest

from teleminder.config import Settings
from teleminder.mcp_server import create_mcp
from teleminder.scheduler import Scheduler
from teleminder.service import TaskService
from teleminder.telegram import TelegramInterface
from teleminder.timeutil import parse_time


class TelegramAPI(BaseRequest):
    def __init__(self):
        self.calls = []
        self.fail_sends = False

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {
                "id": 123,
                "is_bot": True,
                "first_name": "Test",
                "username": "teleminder_test_bot",
            }
        elif endpoint in ("sendMessage", "editMessageText"):
            if self.fail_sends:
                return 500, b'{"ok":false,"description":"Internal error"}'
            result = {
                "message_id": 100,
                "date": 1794916800,
                "chat": {"id": 111, "type": "private"},
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.fixture
async def setup(tmp_path):
    service = TaskService(str(tmp_path / "integration.sqlite"))
    settings = Settings(
        telegram_bot_token="123:test",
        telegram_chat_id=111,
        allowed_user_ids=frozenset({111}),
        allowed_chat_ids=frozenset({111}),
    )
    api = TelegramAPI()
    bot = ExtBot(settings.telegram_bot_token, request=api, get_updates_request=TelegramAPI())
    interface = TelegramInterface(settings, service)
    interface.application = Application.builder().bot(bot).build()
    interface.register_handlers()
    await interface.application.initialize()
    yield service, settings, api, interface
    await interface.application.shutdown()


def callback_update(bot, task_id, user=111):
    return Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": "query1",
                "from": {"id": user, "is_bot": False, "first_name": "Owner"},
                "chat_instance": "abc",
                "data": f"done:{task_id}",
                "message": {
                    "message_id": 100,
                    "date": 1794916800,
                    "chat": {"id": 111, "type": "private"},
                    "text": "old digest",
                    "from": {"id": 123, "is_bot": True, "first_name": "Test"},
                },
            },
        },
        bot,
    )


async def test_mcp_to_digest_to_inline_completion_persists(setup):
    service, settings, api, telegram = setup
    mcp = create_mcp(service, settings)
    result = await mcp.call_tool("add_task", {"title": "Biology", "priority": "high"})
    task = json.loads(result[0].text)
    assert await telegram.send_digest()
    sent = next(params for method, params in api.calls if method == "sendMessage")
    assert "Biology" in sent["text"]
    assert any(
        button["callback_data"] == f"done:{task['id']}"
        for row in sent["reply_markup"]["inline_keyboard"]
        for button in row
    )
    await telegram.application.process_update(callback_update(telegram.application.bot, task["id"]))
    assert any(method == "answerCallbackQuery" for method, _ in api.calls)
    assert any(
        method == "editMessageText" and "Biology" not in params["text"]
        for method, params in api.calls
    )
    assert TaskService(service.database_path).get_task(task["id"])["completed_at"]
    assert service.list_tasks() == []
    assert json.loads((await mcp.call_tool("get_task", {"task_id": task["id"]}))[0].text)[
        "completed_at"
    ]


async def test_unauthorized_real_callback_has_no_side_effects(setup):
    service, _, api, telegram = setup
    task = service.add_task("Private")
    await telegram.application.process_update(
        callback_update(telegram.application.bot, task["id"], user=999)
    )
    assert not service.get_task(task["id"])["completed_at"]
    assert [method for method, _ in api.calls] == ["getMe", "answerCallbackQuery"]


async def test_transport_failure_recorded_in_ledger(setup):
    service, _, api, telegram = setup
    task = service.add_task("Due")
    service.set_task_reminder(task["id"], "2026-11-17T10:00:00-05:00")
    api.fail_sends = True
    await Scheduler(service, telegram).tick(parse_time("2026-11-17T10:00:00-05:00"))
    with service.db.connection() as db:
        row = db.execute(
            "SELECT status,error_type FROM deliveries WHERE event_key LIKE 'reminder:%'"
        ).fetchone()
        assert row["status"] == "failed"
        assert row["error_type"]


async def test_timezone_and_emoji_message_bounds(setup):
    service, _, api, telegram = setup
    service.update_schedule(timezone="Asia/Tokyo")
    for _ in range(20):
        service.add_task("🧪" * 280, due_at="2090-01-01T23:30:00Z")
    await telegram.send_digest()
    sent = next(params for method, params in api.calls if method == "sendMessage")
    assert len(sent["text"].encode("utf-16-le")) // 2 <= 4096
    assert "2090-01-02" in sent["text"]  # local deadline, not UTC date


async def test_manual_command_and_snooze_go_through_real_ptb_dispatch(setup):
    service, _, api, interface = setup
    update = Update.de_json(
        {
            "update_id": 10,
            "message": {
                "message_id": 10,
                "date": 1794916800,
                "from": {"id": 111, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": 111, "type": "private"},
                "text": "/remind 10m <test>",
                "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
            },
        },
        interface.application.bot,
    )
    await interface.application.process_update(update)
    task = service.list_tasks()[0]
    assert task["title"] == "<test>" and service.list_reminders(task["id"])[0]["enabled"]
    sent = [params for method, params in api.calls if method == "sendMessage"][-1]
    assert sent["parse_mode"] == "HTML" and "&lt;test&gt;" in sent["text"]
    data = callback_update(interface.application.bot, task["id"]).to_dict()
    data["callback_query"]["data"] = f"later:{task['id']}:15"
    await interface.application.process_update(Update.de_json(data, interface.application.bot))
    assert len(service.list_reminders(task["id"])) == 2
    assert service.list_reminders(task["id"])[0]["cancelled"]
    assert any(method == "editMessageText" for method, _ in api.calls)


async def test_command_menu_is_scoped_to_owner_chat(setup):
    _, settings, api, interface = setup
    await interface.configure_menu()
    request = [params for method, params in api.calls if method == "setMyCommands"][-1]
    assert request["scope"]["chat_id"] == settings.telegram_chat_id
    assert {"remind", "snooze", "cancel", "pause", "resume"} <= {
        item["command"] for item in request["commands"]
    }
