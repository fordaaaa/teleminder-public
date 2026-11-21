import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from teleminder.config import Settings
from teleminder.mcp_server import create_http_app, create_mcp
from teleminder.service import TaskService


@pytest.fixture
def setup(tmp_path):
    return TaskService(str(tmp_path / "mcp.sqlite")), Settings(mcp_token="x" * 40)


def unpack(result):
    return json.loads(result[0].text)


async def test_sdk_all_tools_and_shared_data(setup):
    service, settings = setup
    mcp = create_mcp(service, settings)
    names = {tool.name for tool in await mcp.list_tools()}
    assert names == {
        "add_task",
        "list_tasks",
        "get_task",
        "update_task",
        "complete_task",
        "delete_task",
        "set_task_reminder",
        "list_reminders",
        "snooze_reminder",
        "cancel_reminder",
        "send_digest",
        "get_schedule",
        "update_schedule",
    }
    task = unpack(await mcp.call_tool("add_task", {"title": "From MCP", "tags": ["agent"]}))
    task_id = task["id"]
    assert service.get_task(task_id)["title"] == "From MCP"
    assert unpack(await mcp.call_tool("get_task", {"task_id": task_id}))["id"] == task_id
    await mcp.call_tool("update_task", {"task_id": task_id, "changes": {"priority": "high"}})
    assert service.get_task(task_id)["priority"] == "high"
    await mcp.call_tool("set_task_reminder", {"task_id": task_id, "when": "2090-01-01T12:00:00Z"})
    assert (await mcp.call_tool("list_reminders", {}))[0]
    assert unpack(await mcp.call_tool("send_digest", {}))["queued"]
    await mcp.call_tool("update_schedule", {"changes": {"enabled": False}})
    assert not unpack(await mcp.call_tool("get_schedule", {}))["enabled"]
    await mcp.call_tool("complete_task", {"task_id": task_id})
    assert service.list_tasks() == []
    await mcp.call_tool("delete_task", {"task_id": task_id})
    assert service.list_tasks(include_completed=True) == []


@pytest.mark.parametrize("task_id", [True, -1, 0, "x", 2**70])
async def test_sdk_invalid_ids(setup, task_id):
    service, settings = setup
    with pytest.raises(ToolError):
        await create_mcp(service, settings).call_tool("get_task", {"task_id": task_id})


def test_remote_fails_closed_without_token(setup):
    service, _ = setup
    with pytest.raises(ValueError):
        create_http_app(service, Settings())


async def test_http_auth_and_standard_initialize(setup):
    service, settings = setup
    app = create_http_app(service, settings)
    headers = {"Accept": "application/json, text/event-stream", "Host": "localhost:8765"}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client,
    ):
        assert (await client.post("/mcp", json=payload, headers=headers)).status_code == 401
        assert (
            await client.post(
                "/mcp", json=payload, headers={**headers, "Authorization": "Bearer wrong"}
            )
        ).status_code == 401
        authorized = {**headers, "Authorization": "Bearer " + settings.mcp_token}
        response = await client.post("/mcp", json=payload, headers=authorized)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["serverInfo"]["name"] == "Teleminder"
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add_task", "arguments": {"title": "HTTP"}},
        }
        response = await client.post("/mcp", json=call, headers=authorized)
        assert response.status_code == 200, response.text
        assert service.list_tasks()[0]["title"] == "HTTP"
        assert (
            await client.post("/mcp", json=payload, headers={**authorized, "Host": "evil.example"})
        ).status_code == 421


async def test_mcp_only_health_is_explicit_and_default_still_requires_worker(setup):
    service, settings = setup
    for require_worker, expected in [(True, 503), (False, 200)]:
        app = create_http_app(service, settings, require_worker=require_worker)
        async with (
            app.app.router.lifespan_context(app.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
            ) as client,
        ):
            response = await client.get("/healthz")
            assert response.status_code == expected
            if not require_worker:
                assert response.json()["mode"] == "mcp-only"
            assert (await client.post("/mcp", json={})).status_code == 401


async def test_public_health_uses_indexed_state_reads(setup, monkeypatch):
    service, settings = setup
    service.heartbeat()
    from contextlib import contextmanager

    connection = service.db.connection
    statements = []

    @contextmanager
    def traced_connection():
        with connection() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(service.db, "connection", traced_connection)
    app = create_http_app(service, settings)
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client,
    ):
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert {"status": "ok"} == response.json()
    assert all("quick_check" not in sql and "deliveries" not in sql for sql in statements)


async def test_mcp_snooze_and_cancel_share_durable_reminder_controls(setup):
    service, settings = setup
    task = service.add_task("MCP snooze")
    service.set_task_reminder(task["id"], "2090-01-01T12:00:00Z", recurrence="daily")
    mcp = create_mcp(service, settings)
    result = unpack(
        await mcp.call_tool(
            "snooze_reminder", {"task_id": task["id"], "when": "2090-01-02T12:00:00Z"}
        )
    )
    assert result["enabled"] and not result["cancelled"]
    assert service.list_reminders(task["id"])[0]["cancelled"]
    await mcp.call_tool("cancel_reminder", {"task_id": task["id"]})
    assert not service.list_reminders(task["id"])[-1]["enabled"]
    assert not service.get_task(task["id"])["completed_at"]
