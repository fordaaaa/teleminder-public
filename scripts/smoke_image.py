"""Credential-free, network-disabled MCP smoke test of the built Docker image."""

import asyncio
import json
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(image: str):
    params = StdioServerParameters(
        command="docker",
        args=[
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            image,
            "teleminder",
            "stdio",
        ],
    )
    with anyio.fail_after(30):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 13
            result = await session.call_tool("add_task", {"title": "Container smoke test"})
            assert not result.isError
            task = json.loads(result.content[0].text)
            assert not (await session.call_tool("complete_task", {"task_id": task["id"]})).isError
            remaining = await session.call_tool("list_tasks", {})
            assert not remaining.isError
            assert remaining.content == [] or "Container smoke test" not in str(remaining.content)
    print("Docker MCP smoke passed: 13 tools, create/complete/list, no network, no credentials.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "teleminder:local"))
