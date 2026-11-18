"""SQLite connections, schema migrations and online backups."""

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
MIGRATION_1 = [
    """CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
       description TEXT NOT NULL, created_at TEXT NOT NULL, due_at TEXT, priority TEXT NOT NULL,
       tags TEXT NOT NULL, completed_at TEXT, recurrence TEXT, parent_id INTEGER UNIQUE)""",
    """CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT,
       task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
       next_run TEXT NOT NULL, recurrence TEXT, enabled INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX reminders_due ON reminders(enabled, next_run)",
    "CREATE INDEX tasks_active ON tasks(completed_at, due_at)",
    "CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE deliveries (event_key TEXT PRIMARY KEY, status TEXT NOT NULL,
       created_at TEXT NOT NULL, sent_at TEXT, error_type TEXT)""",
    """CREATE TABLE digest_requests (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
       processed INTEGER NOT NULL DEFAULT 0)""",
]


class Database:
    def __init__(self, path: str):
        if path == ":memory:":
            raise ValueError("Use a filesystem database for persistence")
        self.path = path
        parent = Path(path).resolve().parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN EXCLUSIVE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("Database schema is newer than this application; refuse downgrade")
            if version < 1:
                for statement in MIGRATION_1:
                    db.execute(statement)
                db.execute("PRAGMA user_version=1")
            if version < 2:
                db.execute("ALTER TABLE tasks ADD COLUMN recurrence_anchor TEXT")
                db.execute("UPDATE tasks SET recurrence_anchor=due_at WHERE recurrence IS NOT NULL")
                db.execute("ALTER TABLE reminders ADD COLUMN recurrence_anchor TEXT")
                db.execute(
                    "UPDATE reminders SET recurrence_anchor=next_run WHERE recurrence IS NOT NULL"
                )
                db.execute("PRAGMA user_version=2")
            if version < 3:
                db.execute("ALTER TABLE reminders ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
                db.execute("PRAGMA user_version=3")
        os.chmod(path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(path + suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def backup(self, destination: str) -> None:
        if Path(destination).resolve() == Path(self.path).resolve():
            raise ValueError("Backup destination must differ from database")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive creation prevents accidentally overwriting a previous backup.
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self.connection() as source, closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
