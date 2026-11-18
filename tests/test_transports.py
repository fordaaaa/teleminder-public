"""Official SDK client/server round trips over both supported transports."""

import sys

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from teleminder.config import Settings
from teleminder.mcp_server import create_http_app
from teleminder.service import TaskService


async def test_official_stdio_client(tmp_path):
    database = str(tmp_path / "stdio.sqlite")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "teleminder.main", "stdio"],
        env={"DATABASE_PATH": database, "TIMEZONE": "America/Toronto"},
    )
    with anyio.fail_after(15):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Teleminder"
            tools = await session.list_tools()
            assert len(tools.tools) == 13
            created = await session.call_tool("add_task", {"title": "Stdio round trip"})
            assert not created.isError
            active = await session.call_tool("list_tasks", {})
            assert not active.isError
    assert TaskService(database).list_tasks()[0]["title"] == "Stdio round trip"


async def test_official_streamable_http_client(tmp_path):
    service = TaskService(str(tmp_path / "http.sqlite"))
    settings = Settings(mcp_token="x" * 40)
    app = create_http_app(service, settings)
    # Exercise actual SDK client encoding, HTTP transport, protocol negotiation,
    # tool discovery and responses. Only the TCP hop is replaced by ASGITransport.
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + settings.mcp_token},
        ) as client:
            with anyio.fail_after(15):
                async with (
                    streamable_http_client("http://localhost:8765/mcp", http_client=client) as (
                        read,
                        write,
                        _,
                    ),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 13
                    created = await session.call_tool("add_task", {"title": "HTTP SDK round trip"})
                    assert not created.isError
                    invalid = await session.call_tool("get_task", {"task_id": False})
                    assert invalid.isError
    assert service.list_tasks()[0]["title"] == "HTTP SDK round trip"
