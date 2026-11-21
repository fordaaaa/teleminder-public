"""Non-secret health and notification delivery diagnostics."""

from .service import TaskService
from .timeutil import parse_time, utcnow


def _fresh_heartbeat(value: str | None) -> bool:
    if value is None:
        return False
    age = (utcnow() - parse_time(value)).total_seconds()
    return -30 <= age < 120


def is_ready(service: TaskService, *, require_worker: bool) -> bool:
    """Check indexed state keys for the unauthenticated HTTP health probe."""
    with service.db.connection() as db:
        state = dict(
            db.execute("SELECT key,value FROM state WHERE key IN ('schedule','heartbeat')")
        )
    return "schedule" in state and (not require_worker or _fresh_heartbeat(state.get("heartbeat")))


def status(service: TaskService) -> dict:
    with service.db.connection() as db:
        healthy_db = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        heartbeat = db.execute("SELECT value FROM state WHERE key='heartbeat'").fetchone()
        counts = {
            row[0]: row[1]
            for row in db.execute("SELECT status,count(*) FROM deliveries GROUP BY status")
        }
    stamp = heartbeat[0] if heartbeat else None
    return {
        "healthy": healthy_db and _fresh_heartbeat(stamp),
        "database_healthy": healthy_db,
        "heartbeat": stamp,
        "deliveries": counts,
    }
