"""Native macOS helper for the deployed teleminder service.

Actions:
  config  — print the path to ~/.teleminder/deployment.json (never secrets)
  status  — fetch <mcp origin>/healthz and print mode/health
  deploy  — validate ~/.teleminder/.env, back up the remote .env, then
            atomically replace it and restart with `docker compose up -d
            --no-build` (base compose for full serve, explicit mcp-only
            override otherwise). Applies config to an already-built image;
            uploads no source.

Secrets travel only via SSH stdin; they are never interpolated into command
arguments, never printed, and no shell is used. Configuration metadata is
owner-checked and fails closed before any subprocess/HTTP invocation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

_SSH_TARGET_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REMOTE_DIR_RE = re.compile(r"/[A-Za-z0-9._/-]*\Z")
_BOT_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
_USER_ID_RE = re.compile(r"[0-9]+\Z")
_MAX_REMOTE_DIR = 256


def _uid() -> int:
    getter = getattr(os, "geteuid", None) or os.getuid
    return getter()


def _check_private_dir(path: Path) -> None:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ValueError(f"missing required directory: {path}") from None
    if not path.is_dir():
        raise ValueError(f"expected a directory: {path}")
    if st.st_uid != _uid():
        raise ValueError(f"{path} must be owned by the current user")
    if st.st_mode & 0o077:
        raise ValueError(f"{path} must not be group/world accessible")


def _check_private_file(path: Path, label: str) -> None:
    try:
        st = path.stat()
    except FileNotFoundError:
        raise ValueError(f"missing required file: {label}") from None
    if not path.is_file():
        raise ValueError(f"expected a file: {label}")
    if st.st_uid != _uid():
        raise ValueError(f"{label} must be owned by the current user")
    if st.st_mode & 0o077:
        raise ValueError(f"{label} must not be group/world accessible (chmod 600)")


def _validate_ssh_target(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("ssh_target is invalid")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("ssh_target must not contain whitespace or control characters")
    if not _SSH_TARGET_RE.match(value):
        raise ValueError(
            "ssh_target must be a plain host or user@host (no options, spaces, or metacharacters)"
        )
    return value


def _validate_remote_directory(value) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_REMOTE_DIR:
        raise ValueError("remote_directory is invalid")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("remote_directory must not contain whitespace or control characters")
    if not _REMOTE_DIR_RE.match(value):
        raise ValueError("remote_directory must be an absolute path of safe characters")
    if any(part == ".." for part in value.split("/")):
        raise ValueError('remote_directory must not contain ".."')
    return value


def _validate_mcp_url(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("mcp_url is invalid")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("mcp_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("mcp_url is not a valid URL") from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError("mcp_url must use http or https")
    if not parsed.hostname:
        raise ValueError("mcp_url must include a host")
    if "@" in parsed.netloc:
        raise ValueError("mcp_url must not embed credentials")
    return value


def _load_config() -> tuple[Path, dict]:
    config_dir = Path.home() / ".teleminder"
    config_path = config_dir / "deployment.json"
    _check_private_dir(config_dir)
    _check_private_file(config_path, "deployment.json")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("deployment.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("deployment.json must contain a JSON object")
    for key in ("ssh_target", "remote_directory", "mcp_url"):
        if key not in data:
            raise ValueError(f"deployment.json is missing required key: {key}")
    return config_dir, {
        "ssh_target": _validate_ssh_target(data["ssh_target"]),
        "remote_directory": _validate_remote_directory(data["remote_directory"]),
        "mcp_url": _validate_mcp_url(data["mcp_url"]),
    }


def _parse_env(config_dir: Path) -> tuple[bytes, dict]:
    env_path = config_dir / ".env"
    _check_private_file(env_path, ".env")
    raw = env_path.read_bytes()
    try:
        values = dotenv_values(env_path)
    except Exception:
        raise ValueError(".env could not be parsed") from None

    token = values.get("MCP_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError(".env is missing MCP_TOKEN")
    if token != token.strip():
        raise ValueError("MCP_TOKEN must not contain surrounding whitespace")
    if len(token) < 32:
        raise ValueError("MCP_TOKEN must be at least 32 characters")
    if any(c.isspace() for c in token):
        raise ValueError("MCP_TOKEN must not contain whitespace")

    bot_token = values.get("TELEGRAM_BOT_TOKEN") or ""
    if not isinstance(bot_token, str):
        raise ValueError("TELEGRAM_BOT_TOKEN must be a string")
    bot_token = bot_token.strip()
    if bot_token and not _BOT_TOKEN_RE.match(bot_token):
        raise ValueError("TELEGRAM_BOT_TOKEN is not in BotFather form (digits:letters_digits_-)")

    user_id_raw = values.get("TELEGRAM_USER_ID")
    if user_id_raw is None:
        user_id_raw = ""
    if not isinstance(user_id_raw, str):
        user_id_raw = str(user_id_raw)
    user_id_raw = user_id_raw.strip()
    if user_id_raw and not _USER_ID_RE.match(user_id_raw):
        raise ValueError("TELEGRAM_USER_ID must be a non-negative integer")
    user_id = int(user_id_raw) if user_id_raw else 0
    if user_id > 2**63 - 1:
        raise ValueError("TELEGRAM_USER_ID is out of range")

    return raw, {"mcp_token": token, "bot_token": bot_token, "user_id": user_id}


def _healthz_url(mcp_url: str) -> str:
    parsed = urlsplit(mcp_url)
    return f"{parsed.scheme}://{parsed.netloc}/healthz"


def _cmd_config() -> None:
    config_path = Path.home() / ".teleminder" / "deployment.json"
    _check_private_dir(config_path.parent)
    _check_private_file(config_path, "deployment.json")
    print(config_path)


def _cmd_status() -> None:
    _, cfg = _load_config()
    try:
        response = httpx.get(_healthz_url(cfg["mcp_url"]), timeout=5)
    except Exception as exc:
        raise RuntimeError(f"healthz request failed: {type(exc).__name__}") from None
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        status = "ok" if response.status_code == 200 else "unhealthy"
    mode = payload.get("mode")
    if not isinstance(mode, str) or not mode:
        mode = "worker" if response.status_code == 200 else "unknown"
    print(f"mode={mode} health={status}")


def _compose_command(full_serve: bool) -> str:
    if full_serve:
        return "docker compose -f docker-compose.yml"
    return "docker compose -f docker-compose.yml -f docker-compose.mcp-only.yml"


def _cmd_deploy() -> None:
    config_dir, cfg = _load_config()
    raw, env = _parse_env(config_dir)
    full_serve = bool(env["bot_token"]) and env["user_id"] > 0
    compose = _compose_command(full_serve)
    backup_suffix = uuid.uuid4().hex
    remote_script = (
        f"set -eu; cd {shlex.quote(cfg['remote_directory'])}; "
        f'if [ -f .env ]; then cp -p .env ".env.bak.{backup_suffix}"; fi; '
        f"umask 077; cat > .env.tmp; chmod 600 .env.tmp; mv .env.tmp .env; "
        f"{compose} up -d --no-build --wait --wait-timeout 90"
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", cfg["ssh_target"], remote_script],
        input=raw,
        capture_output=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"remote deploy failed (ssh exit {proc.returncode})")
    mode = "full serve" if full_serve else "mcp-only"
    print(f"Deploy complete ({mode}).")


def run_client(action: str = "status") -> None:
    action = (action or "").strip().lower()
    if action == "config":
        _cmd_config()
    elif action == "status":
        _cmd_status()
    elif action == "deploy":
        _cmd_deploy()
    else:
        raise ValueError(f"unknown client action: {action!r}")
