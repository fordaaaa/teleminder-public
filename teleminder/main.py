"""Process lifecycle and operational CLI. stdout is reserved for MCP in stdio mode."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from urllib.request import urlopen

import uvicorn
from dotenv import load_dotenv
from telegram.error import Conflict, InvalidToken

from .config import Settings
from .database import SCHEMA_VERSION, Database
from .diagnostics import status
from .mcp_server import create_http_app, create_mcp
from .scheduler import Scheduler
from .service import TaskService
from .telegram import TelegramInterface
from .timeutil import iso, utcnow


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Exception messages/tracebacks and request inputs are deliberately omitted.
        return json.dumps(
            {
                "time": iso(utcnow()),
                "level": record.levelname,
                "logger": record.name,
                "event": record.getMessage(),
                **({"error_type": record.error_type} if hasattr(record, "error_type") else {}),
            }
        )


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Third-party HTTP logs can contain Telegram's token in the request URL.
    # Keep application-owned diagnostics only; Uvicorn access logging is disabled.
    for name in ("httpx", "httpcore", "telegram", "mcp", "uvicorn"):
        external = logging.getLogger(name)
        external.handlers = [logging.NullHandler()]
        external.propagate = False


class WorkerLock:
    """OS lock released on crash; lock file must never be deleted during use."""

    def __init__(self, database_path: str, suffix: str = ".worker.lock", shared: bool = False):
        self.path = str(Path(database_path).resolve()) + suffix
        self.shared = shared
        self.file = None

    def __enter__(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = open(self.path, "a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(
                self.file, (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Worker/restore already active for this database") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


def make_service(settings: Settings) -> TaskService:
    return TaskService(
        settings.database_path,
        settings.timezone,
        dict(
            start_hour=settings.digest_start_hour,
            end_hour=settings.digest_end_hour,
            interval_minutes=settings.digest_interval_minutes,
            quiet_start=settings.quiet_start_hour,
            quiet_end=settings.quiet_end_hour,
            send_empty=settings.send_empty_digest,
        ),
    )


def make_telegram_profiles(settings: Settings, service: TaskService) -> list[TelegramInterface]:
    settings.validate_worker()
    owner = TelegramInterface(settings, service)
    profiles = [owner]
    database = Path(service.database_path)
    profile_directory = database.with_name(database.name + ".profiles")
    for user_id in sorted(settings.telegram_profile_user_ids):
        profile_settings = replace(
            settings,
            telegram_chat_id=user_id,
            allowed_user_ids=frozenset({user_id}),
            allowed_chat_ids=frozenset({user_id}),
            telegram_profile_user_ids=frozenset(),
            database_path=str(profile_directory / f"{user_id}.sqlite"),
        )
        profiles.append(owner.add_profile(profile_settings, make_service(profile_settings)))
    return profiles


async def serve(settings: Settings, service: TaskService) -> None:
    settings.validate_remote()
    profiles = make_telegram_profiles(settings, service)
    telegram = profiles[0]
    stop = asyncio.Event()
    fatal_polling = asyncio.Event()

    def polling_error(error):
        logging.getLogger(__name__).error(
            "telegram_polling_failed", extra={"error_type": type(error).__name__}
        )
        if isinstance(error, (Conflict, InvalidToken)):
            fatal_polling.set()

    config = uvicorn.Config(
        create_http_app(service, settings),
        host=settings.mcp_host,
        port=settings.mcp_port,
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)
    async with telegram.application:
        for profile in profiles:
            await profile.configure_menu()
        await telegram.application.start()
        try:
            await telegram.application.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=False,
                bootstrap_retries=0,
                error_callback=polling_error,
            )
            service.heartbeat()
            schedulers = [
                asyncio.create_task(Scheduler(profile.service, profile).run(stop))
                for profile in profiles
            ]
            http_server = asyncio.create_task(server.serve())
            polling_failed = asyncio.create_task(fatal_polling.wait())
            try:
                done, _ = await asyncio.wait(
                    [*schedulers, http_server, polling_failed], return_when=asyncio.FIRST_COMPLETED
                )
                if polling_failed in done:
                    raise RuntimeError("Telegram polling stopped: token conflict or invalid token")
                for task in done:
                    task.result()  # propagate worker/server failures
            finally:
                stop.set()
                server.should_exit = True
                polling_failed.cancel()
                await asyncio.gather(
                    *schedulers, http_server, polling_failed, return_exceptions=True
                )
        finally:
            if telegram.application.updater.running:
                await telegram.application.updater.stop()
            await telegram.application.stop()


def _validate_backup(snapshot, version: int) -> None:
    if not 1 <= version <= SCHEMA_VERSION:
        raise ValueError("Backup schema is unsupported or newer than this application")
    if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Backup failed integrity check")
    columns = {
        "tasks": "id,title,description,created_at,due_at,priority,tags,completed_at,recurrence,parent_id",
        "reminders": "id,task_id,next_run,recurrence,enabled",
        "deliveries": "event_key,status,created_at,sent_at,error_type",
        "digest_requests": "id,created_at,processed",
    }
    tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if (
        not set(columns) | {"state"} <= tables
        or snapshot.execute("PRAGMA foreign_key_check").fetchone()
    ):
        raise ValueError("Backup has invalid tables or foreign keys")
    for table, fields in columns.items():
        snapshot.execute(f"SELECT {fields} FROM {table} LIMIT 0")
    if version >= 2:
        snapshot.execute("SELECT recurrence_anchor FROM tasks LIMIT 0")
        snapshot.execute("SELECT recurrence_anchor FROM reminders LIMIT 0")
    if version >= 3:
        snapshot.execute("SELECT cancelled FROM reminders LIMIT 0")
    if version >= 4:
        snapshot.execute("SELECT allow_during_quiet_hours FROM reminders LIMIT 0")
    row = snapshot.execute("SELECT value FROM state WHERE key='schedule'").fetchone()
    if not row:
        raise ValueError("Backup missing schedule")
    TaskService._validate_schedule(json.loads(row[0]))


def restore_database(database_path: str, source: str) -> str | None:
    """Validate and migrate in isolation, preserve the current DB, then swap files.

    Exclusive maintenance lock also excludes stdio clients launched through CLI.
    Use only local filesystems; all processes accessing this database must stop.
    """
    target, source_path = Path(database_path).resolve(), Path(source).resolve()
    if target == source_path or not source_path.is_file():
        raise ValueError("Restore source must be a different, existing SQLite backup")
    with WorkerLock(database_path), WorkerLock(database_path, suffix=".access.lock"):
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(
            prefix=".teleminder-restore-", dir=target.parent
        ) as staging:
            candidate = Path(staging) / "candidate.sqlite"
            # Never migrate or mutate the original backup.
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as snapshot:
                _validate_backup(snapshot, snapshot.execute("PRAGMA user_version").fetchone()[0])
                with closing(sqlite3.connect(candidate)) as destination:
                    snapshot.backup(destination)
            migrated = Database(str(candidate))
            with migrated.transaction() as db:
                _validate_backup(db, SCHEMA_VERSION)
                db.execute("DELETE FROM state WHERE key='heartbeat'")
            # Close/checkpoint candidate completely before moving its main file.
            with closing(sqlite3.connect(candidate)) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                db.execute("PRAGMA journal_mode=DELETE").fetchone()
            safety = None
            if target.exists():
                safety = str(target) + ".pre-restore-" + uuid.uuid4().hex + ".sqlite"
                try:
                    # Backup API recovers any surviving WAL and avoids running
                    # migrations on the database we are about to replace.
                    with (
                        closing(sqlite3.connect(target)) as old,
                        closing(sqlite3.connect(safety)) as saved,
                    ):
                        old.backup(saved)
                        old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                except sqlite3.DatabaseError:
                    # Damaged SQLite cannot be backed up logically. Preserve raw
                    # bytes and sidecars for recovery instead of blocking restore.
                    shutil.copyfile(target, safety)
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(target) + suffix)
                        if sidecar.exists():
                            shutil.copyfile(sidecar, safety + suffix)
                            os.chmod(safety + suffix, 0o600)
                os.chmod(safety, 0o600)
            # Maintenance lock excludes worker/stdio; safety copy exists first.
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.chmod(candidate, 0o600)
            os.replace(candidate, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return safety


def _run_command(args, settings, parser) -> None:
    service = make_service(settings)
    if args.command == "serve":
        with WorkerLock(settings.database_path):
            asyncio.run(serve(settings, service))
    elif args.command == "http":
        logging.getLogger(__name__).info("mcp_only_started_telegram_worker_disabled")
        uvicorn.run(
            create_http_app(service, settings, require_worker=False),
            host=settings.mcp_host,
            port=settings.mcp_port,
            access_log=False,
            log_config=None,
        )
    elif args.command == "stdio":
        create_mcp(service, settings).run(transport="stdio")
    elif args.command == "backup":
        if not args.path:
            parser.error("backup requires a destination path")
        service.db.backup(args.path)
        print(json.dumps({"backup_created": True}))
    elif args.command == "status":
        print(json.dumps(status(service)))
    elif args.command == "health":
        healthy = status(service)["database_healthy" if args.mcp_only else "healthy"]
        with urlopen(f"http://127.0.0.1:{settings.mcp_port}/healthz", timeout=3) as response:
            healthy = healthy and response.status == 200
        if not healthy:
            raise RuntimeError("Service unhealthy")


def main() -> None:
    os.umask(0o077)
    load_dotenv(dotenv_path=Path.home() / ".teleminder" / ".env")
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    configure_logging()
    parser = argparse.ArgumentParser(description="Private Telegram task manager and MCP server")
    parser.add_argument(
        "command",
        choices=["serve", "http", "stdio", "health", "status", "backup", "restore", "client"],
        nargs="?",
        default="serve",
    )
    parser.add_argument("path", nargs="?")
    parser.add_argument(
        "--mcp-only",
        action="store_true",
        help="Health: explicitly check MCP HTTP without requiring a Telegram worker",
    )
    args = parser.parse_args()
    try:
        if args.command == "client":
            from .client import run_client

            run_client(args.path or "status")
            return
        settings = Settings.from_env()
        if args.command == "restore":
            if not args.path:
                parser.error("restore requires a source path")
            safety = restore_database(settings.database_path, args.path)
            print(json.dumps({"restored": True, "safety_backup": safety}))
            return
        with WorkerLock(settings.database_path, suffix=".access.lock", shared=True):
            _run_command(args, settings, parser)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger(__name__).error(
            "command_failed", extra={"error_type": type(exc).__name__}
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
