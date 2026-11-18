"""Tests for the native macOS package helper `teleminder.client`.

The helper is expected to be implemented (in a later call) as a module
`teleminder/client.py` exposing `run_client(action: str = 'status') -> None`.

Implementation expectations pinned by these tests (chosen for consistency
with the existing codebase, which already depends on httpx and
python-dotenv):

* HTTP is done with `httpx` (module-level `httpx.get(url, timeout=...)`);
  `status` fetches `<mcp origin>/healthz` — i.e. a URL ending in `/healthz`.
* SSH is done with `subprocess.run` (list argv, never `shell=True`, never a
  string command). The remote `.env` is piped via stdin (`input=...`), never
  via argv, never interpolated into the command line.
* All configuration metadata and secrets are validated BEFORE any subprocess
  or HTTP call happens; any failure raises ValueError.
* Deploy applies config to an already-built server image with `--no-build`
  and uploads no source.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

import pytest

import teleminder.client as client  # red until teleminder/client.py exists

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

MCP_TOKEN = "A" * 48  # random, >= 32 chars
BOT_TOKEN = "123456789:AA_bb-CC123_dEf"  # BotFather shape: digits:letters/digits/_/-
REMOTE_DIR = "/srv/teleminder"
SSH_TARGET = "deploy@example.com"
MCP_URL = "https://mcp.example.com/mcp"

ENV_OK = f"MCP_TOKEN={MCP_TOKEN}\nTELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_USER_ID=12345\n"

ENV_MCP_ONLY = f"MCP_TOKEN={MCP_TOKEN}\n"


def make_home(
    tmp_path,
    monkeypatch,
    *,
    config=None,
    env=ENV_OK,
    config_mode=0o600,
    env_mode=0o600,
    dir_mode=0o700,
):
    home = tmp_path / "home"
    tdir = home / ".teleminder"
    tdir.mkdir(parents=True)
    tdir.chmod(dir_mode)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    if config is not None:
        cpath = tdir / "deployment.json"
        cpath.write_text(json.dumps(config))
        cpath.chmod(config_mode)
    if env is not None:
        epath = tdir / ".env"
        epath.write_text(env)
        epath.chmod(env_mode)
    return home, tdir


def valid_config(**overrides):
    cfg = {"ssh_target": SSH_TARGET, "remote_directory": REMOTE_DIR, "mcp_url": MCP_URL}
    cfg.update(overrides)
    return cfg


def fake_httpx(monkeypatch, status_code=200, payload=None):
    response = SimpleNamespace(
        status_code=status_code,
        json=lambda: payload if payload is not None else {"status": "ok"},
        text=json.dumps(payload if payload is not None else {"status": "ok"}),
    )
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(client.httpx, "get", fake_get)
    return calls


def fake_subprocess(monkeypatch, returncode=0):
    calls = []

    def fake_run(argv, *args, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(client.subprocess, "run", fake_run)
    return calls


def argv_strings(calls):
    return [" ".join(str(a) for a in argv) for argv, _ in calls]


# ---------------------------------------------------------------------------
# missing / invalid configuration fails cleanly and before any SSH/HTTP call
# ---------------------------------------------------------------------------


def test_missing_deployment_json_raises_value_error(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=None)
    with pytest.raises(ValueError):
        client.run_client("status")


@pytest.mark.parametrize("missing_key", ["ssh_target", "remote_directory", "mcp_url"])
def test_missing_config_key_raises_value_error_before_ssh(tmp_path, monkeypatch, missing_key):
    cfg = valid_config()
    del cfg[missing_key]
    make_home(tmp_path, monkeypatch, config=cfg)
    run_calls = fake_subprocess(monkeypatch)
    http_calls = fake_httpx(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []
    assert http_calls == []


def test_non_dict_deployment_json_raises_value_error(tmp_path, monkeypatch):
    home, tdir = make_home(tmp_path, monkeypatch, config=None, env=None)
    (tdir / "deployment.json").write_text('["not", "a", "dict"]')
    (tdir / "deployment.json").chmod(0o600)
    with pytest.raises(ValueError):
        client.run_client("status")


def test_world_readable_env_file_fails_closed_before_ssh(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env_mode=0o644)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


def test_world_readable_config_fails_closed_before_ssh(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), config_mode=0o644)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


def test_config_owned_by_another_uid_fails_closed(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config())
    real_uid = os.stat(tmp_path / "home" / ".teleminder" / "deployment.json").st_uid
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


# ---------------------------------------------------------------------------
# invalid SSH target / remote directory / URL metadata rejected pre-SSH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target",
    [
        "-oProxyCommand=evil example.com",
        "host -oProxyCommand=evil",
        "example.com;rm -rf /",
        "exam ple.com",
        "example.com$(id)",
        "example.com`id`",
        "exam\nple.com",
        "exam\tple.com",
        "",
    ],
)
def test_deploy_rejects_unsafe_ssh_target(tmp_path, monkeypatch, bad_target):
    make_home(tmp_path, monkeypatch, config=valid_config(ssh_target=bad_target))
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


@pytest.mark.parametrize(
    "bad_directory",
    [
        "relative/path",
        "srv/teleminder",
        "/srv/tele minder",
        "/srv/tele\tm",
        "/srv/tele\nm",
        "/srv/$(id)",
        "",
    ],
)
def test_deploy_rejects_unsafe_remote_directory(tmp_path, monkeypatch, bad_directory):
    make_home(tmp_path, monkeypatch, config=valid_config(remote_directory=bad_directory))
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


@pytest.mark.parametrize("bad_url", ["not-a-url", "ftp://example.com/mcp", "", "mcp.example.com"])
def test_deploy_rejects_invalid_mcp_url(tmp_path, monkeypatch, bad_url):
    make_home(tmp_path, monkeypatch, config=valid_config(mcp_url=bad_url))
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


# ---------------------------------------------------------------------------
# secret validation: MCP token + Telegram token shape, before any SSH
# ---------------------------------------------------------------------------


def test_deploy_rejects_short_mcp_token_before_ssh(tmp_path, monkeypatch):
    env = f"MCP_TOKEN={'A' * 31}\nTELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_USER_ID=12345\n"
    make_home(tmp_path, monkeypatch, config=valid_config(), env=env)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


def test_deploy_rejects_missing_mcp_token(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env="TELEGRAM_USER_ID=1\n")
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


def test_deploy_rejects_token_with_whitespace(tmp_path, monkeypatch):
    env = f"MCP_TOKEN={'A' * 20} {'B' * 20}\n"
    make_home(tmp_path, monkeypatch, config=valid_config(), env=env)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


@pytest.mark.parametrize(
    "bad_bot_token",
    [
        "not-a-token",
        "123456789",  # missing colon part
        ":AAtoken",  # missing digits
        "123456789:",  # missing secret part
        "123456789:bad token!",  # illegal characters / space
        "abc:AA_bb-CC123",  # non-numeric bot id
    ],
)
def test_deploy_rejects_malformed_telegram_token(tmp_path, monkeypatch, bad_bot_token):
    env = f"MCP_TOKEN={MCP_TOKEN}\nTELEGRAM_BOT_TOKEN={bad_bot_token}\nTELEGRAM_USER_ID=12345\n"
    make_home(tmp_path, monkeypatch, config=valid_config(), env=env)
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


def test_deploy_rejects_unparseable_env_file(tmp_path, monkeypatch):
    make_home(
        tmp_path, monkeypatch, config=valid_config(), env="MCP_TOKEN\nthis is not dotenv\x00\x01\n"
    )
    run_calls = fake_subprocess(monkeypatch)
    with pytest.raises(ValueError):
        client.run_client("deploy")
    assert run_calls == []


# ---------------------------------------------------------------------------
# config action
# ---------------------------------------------------------------------------


def test_config_action_prints_only_config_path(tmp_path, monkeypatch, capsys):
    home, tdir = make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    client.run_client("config")
    out = capsys.readouterr().out
    assert "deployment.json" in out
    assert MCP_TOKEN not in out
    assert BOT_TOKEN not in out


def test_config_action_does_not_require_env_file(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    fake_subprocess(monkeypatch)
    client.run_client("config")  # must not raise


# ---------------------------------------------------------------------------
# status action
# ---------------------------------------------------------------------------


def test_status_fetches_healthz_and_prints_mode_and_health(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    calls = fake_httpx(monkeypatch, status_code=200, payload={"status": "ok", "mode": "worker"})
    client.run_client("status")
    out = capsys.readouterr().out
    assert len(calls) == 1
    url = calls[0][0]
    assert url.endswith("/healthz")
    assert "example.com" in url
    assert MCP_TOKEN not in out
    assert "ok" in out.lower()
    assert "worker" in out.lower()


def test_status_reports_unhealthy_without_leaking_token(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    fake_httpx(monkeypatch, status_code=503, payload={"status": "unhealthy"})
    client.run_client("status")
    out = capsys.readouterr().out
    assert "unhealthy" in out.lower()
    assert MCP_TOKEN not in out


# ---------------------------------------------------------------------------
# deploy action
# ---------------------------------------------------------------------------


def test_deploy_with_full_credentials_uses_base_compose_without_build(
    tmp_path, monkeypatch, capsys
):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    joined = argv_strings(run_calls)
    assert run_calls, "deploy must invoke ssh"
    for call in run_calls:
        argv = call[0]
        assert isinstance(argv, (list, tuple)), "argv must be a list (never a shell string)"
    all_text = " ".join(joined)
    assert "--no-build" in all_text
    assert "docker" in all_text and "compose" in all_text
    assert "docker-compose.mcp-only.yml" not in all_text
    assert "teleminder" in all_text


def test_deploy_without_telegram_credentials_uses_mcp_only_override(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_MCP_ONLY)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert "--no-build" in all_text
    assert "docker-compose.mcp-only.yml" in all_text


def test_deploy_with_zero_telegram_user_id_uses_mcp_only_override(tmp_path, monkeypatch):
    env = f"MCP_TOKEN={MCP_TOKEN}\nTELEGRAM_BOT_TOKEN={BOT_TOKEN}\nTELEGRAM_USER_ID=0\n"
    make_home(tmp_path, monkeypatch, config=valid_config(), env=env)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert "docker-compose.mcp-only.yml" in all_text


def test_deploy_with_empty_telegram_token_uses_mcp_only_override(tmp_path, monkeypatch):
    env = f"MCP_TOKEN={MCP_TOKEN}\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_USER_ID=12345\n"
    make_home(tmp_path, monkeypatch, config=valid_config(), env=env)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert "docker-compose.mcp-only.yml" in all_text


def test_deploy_never_leaks_secrets_into_argv(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    for argv, kwargs in run_calls:
        for element in argv:
            element = str(element)
            assert MCP_TOKEN not in element
            assert BOT_TOKEN not in element
            assert "MCP_TOKEN=" not in element
            assert "TELEGRAM_BOT_TOKEN=" not in element
        assert kwargs.get("shell") is not True
    out = capsys.readouterr().out
    assert MCP_TOKEN not in out
    assert BOT_TOKEN not in out


def test_deploy_sends_env_via_stdin(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    stdin_payloads = [
        kwargs.get("input") for _, kwargs in run_calls if kwargs.get("input") is not None
    ]
    assert stdin_payloads, "deploy must pipe the .env content to ssh via stdin"
    joined_payload = b"".join(
        p if isinstance(p, bytes) else str(p).encode() for p in stdin_payloads
    )
    assert MCP_TOKEN.encode() in joined_payload
    assert BOT_TOKEN.encode() in joined_payload


def test_deploy_backs_up_existing_env_before_replacement(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert re.search(r"\.env\.bak|backup.*\.env|\.env.*backup", all_text), (
        "deploy must copy the existing remote .env to a backup before replacing it"
    )


def test_deploy_quotes_remote_directory_in_remote_command(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(remote_directory="/srv/teleminder"))
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert "/srv/teleminder" in all_text


def test_deploy_does_not_upload_source_or_build(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    all_text = " ".join(argv_strings(run_calls))
    assert "scp" not in all_text
    assert "rsync" not in all_text
    assert "--build" not in all_text.replace("--no-build", "")


def test_deploy_uses_list_argv_and_no_shell_everywhere(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    for argv, kwargs in run_calls:
        assert isinstance(argv, (list, tuple))
        assert kwargs.get("shell") is not True


def test_deploy_quoted_ssh_host_in_argv(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    run_calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    # ssh_target appears verbatim as a single argv element; no extra options injected
    flat = [element for argv, _ in run_calls for element in argv]
    assert SSH_TARGET in flat
    for argv, _ in run_calls:
        # 'ssh' is the program; host is one element, options (if any) must not come from config
        assert str(argv[0]) == "ssh"


def test_deploy_failure_surfaces_value_error_or_runtime_error(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_OK)
    fake_subprocess(monkeypatch, returncode=255)
    with pytest.raises((ValueError, RuntimeError, SystemExit)):
        client.run_client("deploy")


def test_unknown_action_raises_value_error(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    with pytest.raises(ValueError):
        client.run_client("frobnicate")


def test_deploy_waits_for_container_health(tmp_path, monkeypatch):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=ENV_MCP_ONLY)
    calls = fake_subprocess(monkeypatch)
    client.run_client("deploy")
    command = " ".join(argv_strings(calls))
    assert "--wait" in command
    assert "--wait-timeout" in command


def test_proxy_error_does_not_claim_worker_mode(tmp_path, monkeypatch, capsys):
    make_home(tmp_path, monkeypatch, config=valid_config(), env=None)
    fake_httpx(monkeypatch, status_code=502, payload={})
    client.run_client("status")
    assert "mode=unknown" in capsys.readouterr().out
