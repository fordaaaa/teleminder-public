"""Credential-free container HTTP/auth/health smoke; Telegram is not started."""

import asyncio
import json
import subprocess
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BOOTSTRAP = """
import os
import uvicorn
from teleminder.config import Settings
from teleminder.main import make_service
from teleminder.mcp_server import create_http_app
os.umask(0o077)
settings = Settings(mcp_token="smoke-test-only-" * 4, database_path="/data/smoke.sqlite", mcp_host="0.0.0.0")
service = make_service(settings)
service.add_task("Container HTTP smoke")
service.db.backup("/data/snapshot.sqlite")
service.heartbeat()
uvicorn.run(create_http_app(service, settings), host="0.0.0.0", port=8765, access_log=False)
"""


async def check(base):
    async with httpx.AsyncClient() as client:
        assert (await client.post(base + "/mcp", json={})).status_code == 401
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + "smoke-test-only-" * 4}
    ) as client:
        async with (
            streamable_http_client(base + "/mcp", http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert len((await session.list_tools()).tools) == 13
            result = await session.call_tool("add_task", {"title": "Through Docker HTTP"})
            assert not result.isError
            task = json.loads(result.content[0].text)
            assert not (await session.call_tool("complete_task", {"task_id": task["id"]})).isError


def main(image):
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "-p",
            "127.0.0.1::8765",
            "-e",
            "DATABASE_PATH=/data/smoke.sqlite",
            image,
            "python",
            "-c",
            BOOTSTRAP,
        ],
        text=True,
    ).strip()
    try:
        port = (
            subprocess.check_output(["docker", "port", container, "8765/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        base = "http://127.0.0.1:" + port
        for _ in range(50):
            try:
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Container did not become healthy")
        asyncio.run(check(base))
        subprocess.run(["docker", "exec", container, "teleminder", "health"], check=True)
        uid = subprocess.check_output(["docker", "exec", container, "id", "-u"], text=True).strip()
        assert uid != "0"
        print(
            "Docker HTTP smoke passed: non-root, health, backup, auth rejection, 13 SDK tools and mutations. Telegram not started."
        )
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "5", container], check=True, stdout=subprocess.DEVNULL
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "teleminder:local")
