import json
import sqlite3
from pathlib import Path

import pytest

from teleminder.config import Settings
from teleminder.main import WorkerLock, restore_database
from teleminder.service import TaskService


def test_worker_authorization_fails_closed():
    with pytest.raises(ValueError):
        Settings(telegram_bot_token="token").validate_worker()
    with pytest.raises(ValueError):
        Settings(
            telegram_bot_token="token",
            telegram_chat_id=2,
            allowed_user_ids=frozenset({1}),
            allowed_chat_ids=frozenset({3}),
        ).validate_worker()


def test_single_worker_lock(tmp_path):
    path = str(tmp_path / "tasks.sqlite")
    with WorkerLock(path):
        with pytest.raises(RuntimeError):
            with WorkerLock(path):
                pass
    with WorkerLock(path):
        pass


def test_online_backup_restore_and_safety_copy(tmp_path):
    path = str(tmp_path / "tasks.sqlite")
    service = TaskService(path)
    task = service.add_task("Original")
    backup = str(tmp_path / "snapshot.sqlite")
    service.db.backup(backup)
    service.complete_task(task["id"])
    with WorkerLock(path), pytest.raises(RuntimeError):
        restore_database(path, backup)
    safety = restore_database(path, backup)
    assert TaskService(path).list_tasks()[0]["title"] == "Original"
    assert TaskService(safety).get_task(task["id"])["completed_at"]
    with pytest.raises(FileExistsError):
        service.db.backup(backup)


def test_corrupt_or_newer_schema_restore_rejected(tmp_path):
    path = str(tmp_path / "tasks.sqlite")
    TaskService(path).add_task("Keep me")
    bad = tmp_path / "bad.sqlite"
    bad.write_text("not sqlite")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_database(path, str(bad))
    assert TaskService(path).list_tasks()[0]["title"] == "Keep me"
    with sqlite3.connect(bad.with_name("newer.sqlite")) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError):
        restore_database(path, str(bad.with_name("newer.sqlite")))


def test_env_defaults_and_secrets_hidden(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "secret-value")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "another-secret")
    monkeypatch.setenv("DATABASE_PATH", "example.sqlite")
    settings = Settings.from_env()
    assert "secret-value" not in repr(settings)
    assert "another-secret" not in repr(settings)
    monkeypatch.setenv("ALLOWED_USER_IDS", "bad")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_environment_defaults_can_initialize_service(tmp_path, monkeypatch):
    from teleminder.main import make_service

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "startup.sqlite"))
    monkeypatch.delenv("SEND_EMPTY_DIGEST", raising=False)
    settings = Settings.from_env()
    assert make_service(settings).get_schedule()["send_empty"] is False


def test_v1_migration_retains_tasks_and_reminders(tmp_path):
    from teleminder.database import MIGRATION_1, SCHEMA_VERSION

    path = str(tmp_path / "old.sqlite")
    with sqlite3.connect(path) as db:
        for statement in MIGRATION_1:
            db.execute(statement)
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO tasks(title,description,created_at,due_at,priority,tags,recurrence) VALUES (?,?,?,?,?,?,?)",
            (
                "Legacy",
                "",
                "2026-11-17T10:00:00+00:00",
                "2090-01-31T15:00:00+00:00",
                "medium",
                "[]",
                "monthly",
            ),
        )
        db.execute(
            "INSERT INTO reminders(task_id,next_run,recurrence) VALUES (1,?,?)",
            ("2090-01-31T15:00:00+00:00", "monthly"),
        )
    migrated = TaskService(path)
    assert migrated.get_task(1)["title"] == "Legacy"
    assert migrated.get_task(1)["recurrence_anchor"] == "2090-01-31T15:00:00+00:00"
    assert migrated.list_reminders()[0]["recurrence_anchor"] == "2090-01-31T15:00:00+00:00"
    with migrated.db.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_restore_over_corrupt_target_preserves_raw_safety_copy(tmp_path):
    source = str(tmp_path / "snapshot.sqlite")
    TaskService(source).add_task("Recover this")
    target = tmp_path / "broken.sqlite"
    target.write_bytes(b"corrupt database bytes")
    safety = restore_database(str(target), source)
    assert TaskService(str(target)).list_tasks()[0]["title"] == "Recover this"
    assert Path(safety).read_bytes() == b"corrupt database bytes"


def test_restore_migrates_older_backup_in_isolation(tmp_path):
    from teleminder.database import MIGRATION_1
    from teleminder.service import DEFAULT_SCHEDULE

    source = str(tmp_path / "v1.sqlite")
    with sqlite3.connect(source) as db:
        for statement in MIGRATION_1:
            db.execute(statement)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO state VALUES (?,?)", ("schedule", json.dumps(DEFAULT_SCHEDULE)))
        db.execute(
            "INSERT INTO tasks(title,description,created_at,due_at,priority,tags,recurrence) VALUES (?,?,?,?,?,?,?)",
            (
                "Legacy restore",
                "",
                "2026-11-17T10:00:00+00:00",
                "2090-01-31T15:00:00+00:00",
                "medium",
                "[]",
                "monthly",
            ),
        )
    target = str(tmp_path / "live.sqlite")
    TaskService(target).add_task("Before restore")
    restore_database(target, source)
    assert TaskService(target).get_task(1)["recurrence_anchor"] == "2090-01-31T15:00:00+00:00"
    with sqlite3.connect(source) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1  # source untouched


def test_restore_excludes_active_stdio_access_lock(tmp_path):
    path = str(tmp_path / "live.sqlite")
    service = TaskService(path)
    source = str(tmp_path / "snapshot.sqlite")
    service.db.backup(source)
    with WorkerLock(path, suffix=".access.lock", shared=True):
        with pytest.raises(RuntimeError):
            restore_database(path, source)


def test_restore_after_unclean_exit_preserves_wal_in_safety_backup(tmp_path):
    import subprocess
    import sys

    path = str(tmp_path / "live.sqlite")
    service = TaskService(path)
    task = service.add_task("Before crash")
    source = str(tmp_path / "snapshot.sqlite")
    service.db.backup(source)
    code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute("UPDATE tasks SET title='After crash'")
connection.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, path], check=True)
    assert Path(path + "-wal").exists()
    safety = restore_database(path, source)
    assert TaskService(path).get_task(task["id"])["title"] == "Before crash"
    assert TaskService(safety).get_task(task["id"])["title"] == "After crash"


def test_http_cli_runs_without_telegram_credentials(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from teleminder.main import _run_command

    run = Mock()
    monkeypatch.setattr("teleminder.main.uvicorn.run", run)
    settings = Settings(database_path=str(tmp_path / "http.sqlite"), mcp_token="x" * 40)
    _run_command(SimpleNamespace(command="http"), settings, None)
    run.assert_called_once()
    assert run.call_args.kwargs["port"] == 8765


def test_health_does_not_ignore_missing_worker_without_explicit_flag(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from teleminder.main import _run_command

    response = MagicMock()
    response.__enter__.return_value.status = 200
    monkeypatch.setattr("teleminder.main.urlopen", lambda *a, **k: response)
    settings = Settings(database_path=str(tmp_path / "health.sqlite"))
    with pytest.raises(RuntimeError):
        _run_command(SimpleNamespace(command="health", mcp_only=False), settings, None)
    _run_command(SimpleNamespace(command="health", mcp_only=True), settings, None)


def test_owner_id_derives_private_chat_authorization(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_ID", "111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example-token")
    for key in ["TELEGRAM_CHAT_ID", "ALLOWED_USER_IDS", "ALLOWED_CHAT_IDS"]:
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    settings.validate_worker()
    assert settings.telegram_chat_id == 111
    assert settings.allowed_user_ids == frozenset({111})
    assert settings.allowed_chat_ids == frozenset({111})


def test_owner_id_cannot_expand_to_other_chats_or_users(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_ID", "111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-999")
    monkeypatch.setenv("ALLOWED_USER_IDS", "111,222")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "111,-999")
    settings = Settings.from_env()
    assert settings.telegram_chat_id == 111
    assert settings.allowed_user_ids == frozenset({111})
    assert settings.allowed_chat_ids == frozenset({111})


def test_invalid_owner_id_rejected(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_ID", "-1")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_additional_profile_ids_are_explicit_and_do_not_expand_owner_access(monkeypatch):
    monkeypatch.setenv("TELEGRAM_USER_ID", "111")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:example-token")
    monkeypatch.setenv("TELEGRAM_PROFILE_USER_IDS", " 222,333,222 ")
    settings = Settings.from_env()
    settings.validate_worker()
    assert settings.telegram_profile_user_ids == frozenset({222, 333})
    assert settings.allowed_user_ids == settings.allowed_chat_ids == frozenset({111})


@pytest.mark.parametrize("profile_id", [0, -1, 2**63, True])
def test_profile_ids_must_be_positive_signed_64_bit_integers(profile_id):
    settings = Settings(
        telegram_bot_token="123:example-token",
        telegram_chat_id=111,
        allowed_user_ids=frozenset({111}),
        allowed_chat_ids=frozenset({111}),
        telegram_profile_user_ids=frozenset({profile_id}),
    )
    with pytest.raises(ValueError, match="profile"):
        settings.validate_worker()


@pytest.mark.parametrize(
    ("owner", "users", "chats", "profiles"),
    [
        (111, {111}, {111}, {111}),
        (-111, {111}, {-111}, {222}),
        (111, {111, 333}, {111}, {222}),
        (111, {111}, {111, 333}, {222}),
        (111, {333}, {111}, {222}),
    ],
)
def test_profiles_require_a_distinct_single_private_owner(owner, users, chats, profiles):
    settings = Settings(
        telegram_bot_token="123:example-token",
        telegram_chat_id=owner,
        allowed_user_ids=frozenset(users),
        allowed_chat_ids=frozenset(chats),
        telegram_profile_user_ids=frozenset(profiles),
    )
    with pytest.raises(ValueError, match="profile"):
        settings.validate_worker()


def test_default_mcp_only_settings_have_no_profiles(monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROFILE_USER_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)
    settings = Settings.from_env()
    assert settings.telegram_profile_user_ids == frozenset()
