"""Non-secret health and notification delivery diagnostics."""

from .service import TaskService
from .timeutil import parse_time, utcnow


def status(service: TaskService) -> dict:
    with service.db.connection() as db:
        healthy_db = db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        heartbeat = db.execute("SELECT value FROM state WHERE key='heartbeat'").fetchone()
        counts = {
            row[0]: row[1]
            for row in db.execute("SELECT status,count(*) FROM deliveries GROUP BY status")
        }
    age = (utcnow() - parse_time(heartbeat[0])).total_seconds() if heartbeat else None
    return {
        "healthy": healthy_db and age is not None and -30 <= age < 120,
        "database_healthy": healthy_db,
        "heartbeat": heartbeat[0] if heartbeat else None,
        "deliveries": counts,
    }
