"""Bounded Telegram HTML: native typography, clear dates and safe user content."""

from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from .timeutil import parse_time


def safe_text(value: object, budget: int = 600) -> str:
    """Limit escaped source as well as displayed UTF-16 units; never cut a tag/entity."""
    text = str(value or "").encode("utf-8", errors="replace").decode("utf-8")
    fragments, used = [], 0
    for character in text:
        fragment = escape(character, quote=False)
        size = len(fragment.encode("utf-16-le")) // 2
        if used + size > budget - 1:
            return "".join(fragments) + "…"
        fragments.append(fragment)
        used += size
    return "".join(fragments)


def local_time(value: str | None, timezone: str, now: datetime | None = None) -> str:
    if not value:
        return "No deadline"
    zone = ZoneInfo(timezone)
    local = parse_time(value).astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    days = (local.date() - today).days
    date = "Today" if days == 0 else "Tomorrow" if days == 1 else f"{local:%a} · {local:%Y-%m-%d}"
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{date}, {clock} {local.tzname()}"


def priority(task: dict) -> str:
    return {
        "high": "🔴 High priority",
        "medium": "🟡 Medium priority",
        "low": "🟢 Low priority",
    }.get(task.get("priority"), "🟡 Medium priority")


def task_line(task: dict, timezone: str, now: datetime | None = None) -> str:
    completed = bool(task.get("completed_at"))
    due_at = task.get("due_at")
    stamp = now or datetime.now(UTC)
    overdue = bool(due_at and not completed and parse_time(due_at) < stamp)
    if completed:
        icon = "✅"
    elif overdue:
        icon = "🔴"
    else:
        icon = priority(task).split(" ", 1)[0]
    title = safe_text(" ".join(str(task.get("title", "")).split()), 70)
    identifier = safe_text(task.get("id"), 24)
    if completed:
        title = f"<s>{title}</s>"
    due = ""
    if due_at:
        timestamp = safe_text(local_time(due_at, timezone, stamp), 70)
        label = "Overdue · " if overdue else ""
        due = f"\n   <i>{label}{timestamp}</i>"
    return f"{icon} <b>{title}</b> <code>#{identifier}</code>{due}"


def render_tasks(
    tasks: list[dict],
    total: int,
    page: int,
    pages: int,
    timezone: str,
    now: datetime | None = None,
    *,
    heading: str = "Your tasks",
    page_command: str = "list",
) -> str:
    if not tasks:
        return "<b>No pending tasks</b>\n\nUse /add for a task or /remind for a timed reminder."
    stamp = now or datetime.now(UTC)
    header = f"📋 <b>{safe_text(heading, 60)}</b>\n<i>{total} remaining · page {page}/{pages}</i>"
    lines = [task_line(task, timezone, stamp) for task in tasks[:20]]
    footer = (
        f"\n\n<i>Use /{page_command} 2 for the next page.</i>" if pages > 1 and page == 1 else ""
    )
    return header + "\n\n" + "\n\n".join(lines) + footer


def render_reminder(task: dict, timezone: str, now: datetime | None = None) -> str:
    title = safe_text(task.get("title"), 600)
    parts = ["🔔 <b>Reminder</b>", f"<b>{title}</b>"]
    if task.get("description"):
        parts.append(safe_text(task["description"], 700))
    timestamp = safe_text(local_time(task.get("due_at"), timezone, now), 100)
    identifier = safe_text(task.get("id"), 24)
    details = f"🕒 <b>{timestamp}</b>\n{priority(task)} · <code>#{identifier}</code>"
    parts.append(details)
    tags = task.get("tags") or []
    if tags:
        parts.append("<i>" + " · ".join("#" + safe_text(tag, 65) for tag in tags[:3]) + "</i>")
    return "\n\n".join(parts)


def render_scheduled(task: dict, reminder: dict, timezone: str, *, snoozed: bool = False) -> str:
    heading = "💤 Reminder snoozed" if snoozed else "⏰ Reminder set"
    text = f"<b>{heading}</b>\n\n<b>{safe_text(task['title'], 600)}</b>\n\n🕒 {safe_text(local_time(reminder['next_run'], timezone), 100)}\n<code>#{task['id']}</code>"
    if reminder.get("recurrence"):
        text += f"\n<i>Repeats {safe_text(reminder['recurrence'], 20)} until completed.</i>"
    return text
