"""Telegram transport for teleminder.

Owns the python-telegram-bot Application. Never starts polling; the
scheduler/worker drives delivery. Authorization fails closed for both the
effective user AND the effective chat on every incoming update.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .reminder_input import parse_time_spec
from .telegram_views import (
    local_time,
    render_reminder,
    render_scheduled,
    render_tasks,
    safe_text,
    task_line,
)

logger = logging.getLogger(__name__)

COMMAND_MENU = [
    ("remind", "Set a reminder: /remind 10m Take a break"),
    ("reminders", "Show upcoming reminders"),
    ("snooze", "Snooze a task: /snooze 2 until tomorrow 09:00"),
    ("cancel", "Turn off reminders for a task"),
    ("list", "Show your tasks"),
    ("add", "Add a task"),
    ("done", "Complete a task"),
    ("today", "Tasks due today"),
    ("pause", "Pause all notifications, optionally until a time"),
    ("resume", "Resume automatic notifications"),
    ("settings", "Notification settings"),
    ("help", "Examples and commands"),
    ("start", "Get started"),
]

PAGE_SIZE = 20
MAX_MESSAGE = 4096
MAX_ID = 2**63 - 1
MAX_SNOOZE_MINUTES = 7 * 24 * 60  # one week
TITLE_LINE = 60
TITLE_FULL = 200

_DONE_RE = re.compile(r"done:([0-9]{1,19})\Z")
_SNOOZE_RE = re.compile(r"snooze:([0-9]{1,5})\Z")


def _clip(text: str, limit: int = MAX_MESSAGE) -> str:
    text = str(text)
    encoded = text.encode("utf-16-le")
    return (
        text
        if len(encoded) // 2 <= limit
        else encoded[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    )


def _short_title(title, limit: int = TITLE_LINE) -> str:
    title = " ".join(str(title or "").split())
    return title if len(title) <= limit else title[: limit - 1] + "…"


def _parse_id(raw) -> int | None:
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw.isdigit() or len(raw) > 19:
        return None
    value = int(raw)
    if value <= 0 or value > MAX_ID:
        return None
    return value


def _parse_positive_int(raw, maximum: int) -> int | None:
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw.isdigit() or (raw and len(raw) > len(str(maximum))):
        return None
    value = int(raw)
    if value <= 0 or value > maximum:
        return None
    return value


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


class TelegramInterface:
    def __init__(self, settings, service, *, application=None):
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self.settings = settings
        self.service = service
        self._profiles: dict[int, TelegramInterface] = {}
        if application is None:
            self.application = Application.builder().token(settings.telegram_bot_token).build()
            self.register_handlers()
        else:
            self.application = application

    def add_profile(self, settings, service) -> TelegramInterface:
        profile = TelegramInterface(settings, service, application=self.application)
        self._profiles[settings.telegram_chat_id] = profile
        return profile

    @staticmethod
    def _private_user_id(update) -> int | None:
        chat = getattr(update, "effective_chat", None)
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        if (
            type(user_id) is int
            and user_id > 0
            and getattr(chat, "type", None) == "private"
            and getattr(chat, "id", None) == user_id
        ):
            return user_id
        return None

    # ------------------------------------------------------------------
    # registration / errors
    # ------------------------------------------------------------------
    def register_handlers(self) -> None:
        self.application.add_handler(
            CommandHandler([name for name, _ in COMMAND_MENU], self.command)
        )
        self.application.add_handler(CallbackQueryHandler(self.callback))
        self.application.add_error_handler(self.on_error)

    async def on_error(self, update, context) -> None:
        error = getattr(context, "error", None)
        logger.error(
            "Telegram handler error: %s", type(error).__name__ if error is not None else "Unknown"
        )

    # ------------------------------------------------------------------
    # authorization (fail closed, both user AND chat)
    # ------------------------------------------------------------------
    def _authorized(self, update) -> bool:
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        user_id = getattr(user, "id", None)
        chat_id = getattr(chat, "id", None)
        if user_id is None or chat_id is None:
            return False
        allowed_users = getattr(self.settings, "allowed_user_ids", frozenset())
        allowed_chats = getattr(self.settings, "allowed_chat_ids", frozenset())
        return user_id in allowed_users and chat_id in allowed_chats

    async def _deny_command(self, update) -> None:
        message = getattr(update, "message", None)
        if message is not None:
            try:
                words = (message.text or "").split()
                command = words[0].split("@", 1)[0].lower() if words else ""
                user_id = self._private_user_id(update)
                if command == "/start" and user_id is not None:
                    await message.reply_text(
                        f"Ask the bot owner to add your Telegram user ID: {user_id}."
                    )
                else:
                    await message.reply_text("Not authorized.")
            except Exception as exc:  # never leak internals
                logger.error("Telegram reply failed: %s", type(exc).__name__)

    # ------------------------------------------------------------------
    # shared formatting / keyboards
    # ------------------------------------------------------------------
    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.service.get_schedule()["timezone"])
        except Exception:
            return ZoneInfo("UTC")

    def _total_pending(self) -> int:
        count = getattr(self.service, "count_tasks", None)
        if callable(count):
            return count()
        # Lightweight injected service implementations may only provide list_tasks.
        total = 0
        while True:
            page = self.service.list_tasks(limit=500, offset=total)
            total += len(page)
            if len(page) < 500:
                return total

    def _task_line(self, task) -> str:
        return task_line(task, self._tz().key)

    def _render_list(self, tasks, total, page: int = 1, pages: int | None = None) -> str:
        return render_tasks(
            tasks,
            total,
            page,
            pages or max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            self._tz().key,
        )

    async def configure_menu(self) -> None:
        try:
            await self.application.bot.set_my_commands(
                COMMAND_MENU, scope=BotCommandScopeChat(self.settings.telegram_chat_id)
            )
        except Exception as exc:
            logger.warning("command_menu_update_failed: %s", type(exc).__name__)

    @staticmethod
    def _keyboard(tasks) -> InlineKeyboardMarkup:
        rows = []
        row = []
        for task in list(tasks)[:PAGE_SIZE]:
            task_id = task.get("id")
            if not isinstance(task_id, int) or task_id <= 0:
                continue
            row.append(
                InlineKeyboardButton(f"✅ Complete {task_id}", callback_data=f"done:{task_id}")
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="refresh"),
                InlineKeyboardButton("💤 Pause all 1h", callback_data="snooze:60"),
            ]
        )
        rows.extend(
            [
                [
                    InlineKeyboardButton("➕ Add task", callback_data="action:add"),
                    InlineKeyboardButton("⏰ Set reminder", callback_data="action:remind"),
                ],
                [
                    InlineKeyboardButton("📅 Today", callback_data="action:today"),
                    InlineKeyboardButton("⚙️ Settings", callback_data="action:settings"),
                ],
            ]
        )
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _single_keyboard(task_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}")],
                [
                    InlineKeyboardButton("⏱ +15 min", callback_data=f"later:{task_id}:15"),
                    InlineKeyboardButton("💤 +1 hour", callback_data=f"later:{task_id}:60"),
                ],
                [
                    InlineKeyboardButton("🗓 Custom time", callback_data=f"custom:{task_id}"),
                    InlineKeyboardButton("📋 My tasks", callback_data="refresh"),
                ],
            ]
        )

    async def _reply(self, update, text: str, reply_markup=None, *, html: bool = False) -> None:
        message = getattr(update, "message", None)
        if message is None:
            message = getattr(getattr(update, "callback_query", None), "message", None)
        if message is not None:
            await message.reply_text(
                text if html else _clip(text),
                reply_markup=reply_markup,
                parse_mode="HTML" if html else None,
            )

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def command(self, update, context) -> None:
        profile = self._profiles.get(self._private_user_id(update))
        if profile is not None:
            await profile.command(update, context)
            return
        if not self._authorized(update):
            await self._deny_command(update)
            return
        message = getattr(update, "message", None)
        text = (getattr(message, "text", None) or "").strip()
        parts = text.split()
        command = (
            parts[0][1:].split("@", 1)[0].lower() if parts and parts[0].startswith("/") else ""
        )
        args = getattr(context, "args", None)
        args = list(args) if args is not None else parts[1:]

        handlers = {
            "start": self._cmd_help,
            "help": self._cmd_help,
            "add": self._cmd_add,
            "done": self._cmd_done,
            "list": self._cmd_list,
            "today": self._cmd_today,
            "settings": self._cmd_settings,
            "remind": self._cmd_remind,
            "reminders": self._cmd_reminders,
            "snooze": self._cmd_snooze,
            "cancel": self._cmd_cancel,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
        }
        handler = handlers.get(command)
        if handler is None:
            await self._reply(update, "Unknown command. Try /help.")
            return
        try:
            await handler(update, args)
        except ValueError as exc:
            logger.error("Command %s rejected: %s", command or "unknown", type(exc).__name__)
            await self._reply(
                update, f"⚠️ {safe_text(str(exc), 500)}\n\nTry /help for examples.", html=True
            )
        except Exception as exc:
            logger.error("Command %s failed: %s", command or "unknown", type(exc).__name__)
            await self._reply(update, "Something went wrong. Please try again.")

    async def _cmd_help(self, update, args) -> None:
        await self._reply(
            update,
            "<b>⏰ Your reminder desk</b>\n\n"
            "<b>Set a reminder</b>\n"
            "<code>/remind 10m Take a break</code>\n"
            "<code>/remind tomorrow 09:00 Review notes</code>\n"
            "<code>/remind daily 9am Take vitamins</code>\n"
            "<code>/remind weekly mon 09:00 Plan the week</code>\n"
            "<code>/remind #2 1h</code> — an existing task\n\n"
            "<b>Change a reminder</b>\n"
            "<code>/snooze 2 2h</code>\n"
            "<code>/snooze 2 until tomorrow 09:00</code>\n"
            "<code>/snooze 2 until 2027-01-15 14:30</code>\n"
            "<code>/cancel 2</code> — keep the task, turn reminders off\n"
            "<code>/reminders</code> — upcoming reminders\n\n"
            "<b>Take a break</b>\n"
            "<code>/pause 2h</code> or <code>/pause until tomorrow 9am</code>\n"
            "<code>/pause</code> — pause until you <code>/resume</code>\n\n"
            "<b>Tasks</b>\n/add TITLE · /done ID · /list [page] · /today · /settings\n\n"
            f"<i>Local times use {safe_text(self._tz().key, 100)}. Daily/weekly reminders repeat until completed. Delivery can take about 10 seconds.</i>",
            reply_markup=self._keyboard([]),
            html=True,
        )

    def _one_time(self, words):
        spec = parse_time_spec(words, self._tz().key)
        if spec.consumed != len(words) or spec.recurrence:
            raise ValueError("Use one duration or date/time, e.g. 2h or until tomorrow 09:00.")
        return spec.when.isoformat()

    async def _cmd_remind(self, update, args) -> None:
        if not args:
            raise ValueError("Usage: /remind 10m Take a break, or /remind #2 tomorrow 09:00")
        existing = args[0].startswith("#")
        task_id = _parse_id(args[0][1:]) if existing else None
        if existing and task_id is None:
            raise ValueError("Use a positive task ID after #.")
        words = args[1:] if existing else args
        spec = parse_time_spec(words, self._tz().key)
        if existing:
            if spec.consumed != len(words):
                raise ValueError("For an existing task, provide only #ID and a time.")
            task = self.service.get_task(task_id)
            reminder = self.service.set_task_reminder(
                task_id, spec.when.isoformat(), spec.recurrence
            )
        else:
            title = " ".join(words[spec.consumed :]).strip()
            if not title:
                raise ValueError("Add a title after the time, e.g. /remind 10m Take a break.")
            result = self.service.create_reminded_task(
                title, spec.when.isoformat(), spec.recurrence
            )
            task, reminder = result["task"], result["reminder"]
        await self._reply(
            update,
            render_scheduled(task, reminder, self._tz().key),
            self._single_keyboard(task["id"]),
            html=True,
        )

    async def _cmd_snooze(self, update, args) -> None:
        if args and args[0].lower() == "all":
            await self._cmd_pause(update, args[1:])
            return
        if len(args) < 2 or (task_id := _parse_id(args[0].lstrip("#"))) is None:
            raise ValueError("Usage: /snooze ID 2h or /snooze ID until tomorrow 09:00")
        reminder = self.service.snooze_task_reminder(task_id, self._one_time(args[1:]))
        task = self.service.get_task(task_id)
        await self._reply(
            update,
            render_scheduled(task, reminder, self._tz().key, snoozed=True),
            self._single_keyboard(task_id),
            html=True,
        )

    async def _cmd_cancel(self, update, args) -> None:
        if len(args) != 1 or (task_id := _parse_id(args[0].lstrip("#"))) is None:
            raise ValueError("Usage: /cancel ID — turn off reminders without deleting the task.")
        self.service.cancel_task_reminders(task_id)
        await self._reply(
            update, f"🔕 Reminders turned off for task #{task_id}. The task is still in your list."
        )

    async def _cmd_pause(self, update, args) -> None:
        if args:
            when = self._one_time(args)
            self.service.update_schedule(enabled=True, snoozed_until=when)
            text = f"💤 <b>All automatic notifications paused</b>\n\nUntil {safe_text(local_time(when, self._tz().key), 120)}."
        else:
            self.service.update_schedule(enabled=False, snoozed_until=None)
            text = "💤 <b>All automatic notifications paused</b>\n\nResume whenever you are ready."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Resume", callback_data="resume")]])
        await self._reply(update, text, markup, html=True)

    async def _cmd_resume(self, update, args) -> None:
        if args:
            raise ValueError("Usage: /resume")
        self.service.update_schedule(enabled=True, snoozed_until=None)
        await self._reply(
            update, "▶️ Automatic notifications resumed. Your quiet hours still apply."
        )

    async def _cmd_reminders(self, update, args) -> None:
        page = _parse_positive_int(args[0], 1_000_000) if args else 1
        if page is None or len(args) > 1:
            raise ValueError("Usage: /reminders [page]")
        rows = []
        for reminder in self.service.list_reminders():
            if not reminder["enabled"] or reminder.get("cancelled"):
                continue
            try:
                task = self.service.get_task(reminder["task_id"])
            except ValueError:
                continue
            if not task["completed_at"]:
                rows.append({**task, "due_at": reminder["next_run"]})
        rows.sort(key=lambda task: task["due_at"])
        if not rows:
            await self._reply(update, "⏰ No active reminders. Try /remind 10m Take a break.")
            return
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages)
        shown = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        await self._reply(
            update,
            render_tasks(
                shown,
                len(rows),
                page,
                pages,
                self._tz().key,
                heading="Upcoming reminders",
                page_command="reminders",
            ),
            self._keyboard(shown),
            html=True,
        )

    async def _cmd_add(self, update, args) -> None:
        title = " ".join(str(a) for a in args).strip()
        if not title:
            await self._reply(update, "Usage: /add TITLE…")
            return
        task = self.service.add_task(_clip(title, 500))
        await self._reply(
            update, f"Added task {task.get('id')}: {_short_title(task.get('title'), TITLE_FULL)}"
        )

    async def _cmd_done(self, update, args) -> None:
        if not args:
            await self._reply(update, "Usage: /done ID")
            return
        task_id = _parse_id(args[0])
        if task_id is None:
            await self._reply(update, "Usage: /done ID (positive numeric task ID)")
            return
        task = self.service.complete_task(task_id)
        await self._reply(
            update, f"Done: {task.get('id')}. {_short_title(task.get('title'), TITLE_FULL)}"
        )

    async def _cmd_list(self, update, args) -> None:
        page = 1
        if args:
            parsed = _parse_positive_int(args[0], 1_000_000)
            if parsed is None:
                await self._reply(update, "Usage: /list [page] (positive page number)")
                return
            page = parsed
        total = self._total_pending()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
        offset = (page - 1) * PAGE_SIZE
        tasks = self.service.list_tasks(limit=PAGE_SIZE, offset=offset)
        if not tasks and page > 1:
            page = pages
            offset = (page - 1) * PAGE_SIZE
            tasks = self.service.list_tasks(limit=PAGE_SIZE, offset=offset)
        text = self._render_list(tasks, total, page, pages)
        await self._reply(update, text, reply_markup=self._keyboard(tasks), html=True)

    async def _cmd_today(self, update, args) -> None:
        tz = self._tz()
        today = datetime.now(tz).date()
        due_today = []
        offset = 0
        while len(due_today) < PAGE_SIZE:
            batch = self.service.list_tasks(limit=500, offset=offset)
            for task in batch:
                due = _parse_iso(task.get("due_at"))
                if due is not None and due.astimezone(tz).date() == today:
                    due_today.append(task)
                if len(due_today) >= PAGE_SIZE:
                    break
            if len(batch) < 500:
                break
            offset += 500
        if not due_today:
            await self._reply(update, "Nothing due today.")
            return
        text = render_tasks(due_today, len(due_today), 1, 1, self._tz().key, heading="Due today")
        await self._reply(update, text, reply_markup=self._keyboard(due_today), html=True)

    async def _cmd_settings(self, update, args) -> None:
        sub = str(args[0]).lower() if args else "show"
        if sub == "show":
            schedule = self.service.get_schedule()
            await self._reply(update, self._render_schedule(schedule))
            return
        if sub in ("on", "off"):
            schedule = self.service.update_schedule(enabled=(sub == "on"))
            await self._reply(
                update, f"Schedule {'enabled' if schedule.get('enabled') else 'disabled'}."
            )
            return
        if sub == "snooze":
            if len(args) < 2:
                await self._reply(update, "Usage: /settings snooze MINUTES")
                return
            minutes = _parse_positive_int(args[1], MAX_SNOOZE_MINUTES)
            if minutes is None:
                await self._reply(update, "Usage: /settings snooze MINUTES (1-10080)")
                return
            snoozed_until = (
                (datetime.now(UTC) + timedelta(minutes=minutes)).astimezone(self._tz()).isoformat()
            )
            self.service.update_schedule(enabled=True, snoozed_until=snoozed_until)
            await self._reply(update, f"Snoozed for {minutes} minute(s).")
            return
        await self._reply(update, "Usage: /settings [show|on|off|snooze MINUTES]")

    @staticmethod
    def _render_schedule(schedule: dict) -> str:
        lines = [
            "Digest settings:",
            f"enabled: {bool(schedule.get('enabled', True))}",
            f"send_empty: {bool(schedule.get('send_empty', False))}",
            f"interval_minutes: {schedule.get('interval_minutes', 60)}",
            f"window: {schedule.get('start_hour', 9)}:00-{schedule.get('end_hour', 21)}:00",
            f"quiet hours: {schedule.get('quiet_start', 22)}:00-{schedule.get('quiet_end', 9)}:00",
            f"timezone: {schedule.get('timezone', 'America/Toronto')}",
            f"snoozed_until: {schedule.get('snoozed_until')}",
        ]
        return _clip("\n".join(lines))

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------
    async def callback(self, update, context) -> None:
        profile = self._profiles.get(self._private_user_id(update))
        if profile is not None:
            await profile.callback(update, context)
            return
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        if not self._authorized(update):
            await query.answer()
            return
        data = query.data if isinstance(query.data, str) else ""
        action, value = self._parse_callback(data)
        if action is None:
            await query.answer("Invalid action.")
            return
        await query.answer()
        try:
            if action == "action":
                prompts = {
                    "add": "➕ Send /add followed by a task. Example: /add Call the dentist",
                    "remind": "⏰ Send /remind followed by a time and reminder. Example: /remind 30m Take a break",
                }
                if value == "today":
                    await self._cmd_today(query, [])
                elif value == "settings":
                    await self._cmd_settings(query, [])
                else:
                    await query.message.reply_text(prompts[value])
            elif action == "done":
                self.service.complete_task(value)
                await self._refresh_callback_message(query, "✅ Completed")
            elif action == "refresh":
                await self._refresh_callback_message(query)
            elif action == "snooze":
                self.service.update_schedule(
                    enabled=True,
                    snoozed_until=(datetime.now(UTC) + timedelta(minutes=value)).isoformat(),
                )
                await self._refresh_callback_message(
                    query, f"💤 All notifications paused for {value} minutes"
                )
            elif action == "resume":
                self.service.update_schedule(enabled=True, snoozed_until=None)
                await self._refresh_callback_message(query, "▶️ Notifications resumed")
            elif action in ("later", "tomorrow"):
                task_id = value[0] if action == "later" else value
                when = (
                    (datetime.now(UTC) + timedelta(minutes=value[1])).isoformat()
                    if action == "later"
                    else self._one_time(["tomorrow", "09:00"])
                )
                reminder = self.service.snooze_task_reminder(task_id, when)
                task = self.service.get_task(task_id)
                await self._edit_query(
                    query,
                    render_scheduled(task, reminder, self._tz().key, snoozed=True),
                    self._single_keyboard(task_id),
                )
            elif action == "cancel":
                self.service.cancel_task_reminders(value)
                task = self.service.get_task(value)
                text = f"🔕 <b>Reminder turned off</b>\n\n<b>{safe_text(task['title'], 600)}</b>\n\n<i>The task is still open. /remind #{value} 1h sets a new reminder.</i>"
                await self._edit_query(
                    query,
                    text,
                    InlineKeyboardMarkup(
                        [[InlineKeyboardButton("📋 My tasks", callback_data="refresh")]]
                    ),
                )
            elif action == "custom":
                task = self.service.get_task(value)
                if task["completed_at"]:
                    raise ValueError("This task is already completed.")
                text = (
                    f"🗓 <b>Choose a snooze time</b>\n\n<b>{safe_text(task['title'], 600)}</b>\n\n"
                    f"<code>/snooze {value} 2h</code>\n"
                    f"<code>/snooze {value} until tomorrow 09:00</code>\n"
                    f"<code>/snooze {value} until 2027-01-15 14:30</code>\n\n"
                    f"<i>Times use {safe_text(self._tz().key, 100)}. Only this task changes.</i>"
                )
                markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("⏱ +30 min", callback_data=f"later:{value}:30"),
                            InlineKeyboardButton("💤 +2 hours", callback_data=f"later:{value}:120"),
                        ],
                        [
                            InlineKeyboardButton(
                                "🌅 Tomorrow 9 AM", callback_data=f"tomorrow:{value}"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔕 Cancel reminder", callback_data=f"cancel:{value}"
                            ),
                            InlineKeyboardButton("📋 My tasks", callback_data="refresh"),
                        ],
                    ]
                )
                await self._edit_query(query, text, markup)
        except ValueError as exc:
            await self._edit_query(
                query, f"⚠️ {safe_text(str(exc), 500)}\n\nUse /list to refresh your tasks."
            )
        except Exception as exc:
            logger.error("callback_failed: %s", type(exc).__name__)

    @staticmethod
    def _parse_callback(data: str):
        if len(data.encode("utf-8", errors="replace")) > 64:
            return None, None
        if data in ("refresh", "resume"):
            return data, None
        if data in ("action:add", "action:remind", "action:today", "action:settings"):
            return "action", data.split(":", 1)[1]
        match = _DONE_RE.fullmatch(data)
        if match and (identifier := _parse_id(match.group(1))) is not None:
            return "done", identifier
        match = _SNOOZE_RE.fullmatch(data)
        if (
            match
            and (minutes := _parse_positive_int(match.group(1), MAX_SNOOZE_MINUTES)) is not None
        ):
            return "snooze", minutes
        match = re.fullmatch(r"later:([0-9]{1,19}):([0-9]{1,4})", data)
        if (
            match
            and (identifier := _parse_id(match[1])) is not None
            and int(match[2]) in {15, 30, 60, 120, 180, 240}
        ):
            return "later", (identifier, int(match[2]))
        match = re.fullmatch(r"(custom|cancel|tomorrow):([0-9]{1,19})", data)
        if match and (identifier := _parse_id(match[2])) is not None:
            return match[1], identifier
        return None, None

    async def _edit_query(self, query, text: str, markup=None) -> None:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def _refresh_callback_message(self, query, notice: str | None = None) -> None:
        total = self._total_pending()
        tasks = self.service.list_tasks(limit=PAGE_SIZE, offset=0)
        text = self._render_list(tasks, total)
        if notice:
            text = f"<b>{safe_text(notice, 150)}</b>\n\n" + text
        await self._edit_query(query, text, self._keyboard(tasks))

    # ------------------------------------------------------------------
    # digest / reminder delivery
    # ------------------------------------------------------------------
    async def _send_scheduled(self, **kwargs) -> None:
        # Stay below Telegram's per-chat sustained message limit when missed
        # reminders coalesce after downtime. One worker owns this transport.
        async with self._send_lock:
            delay = 1.1 - (time.monotonic() - self._last_send)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_send = time.monotonic()
            await self.application.bot.send_message(**kwargs)

    async def send_digest(self) -> bool:
        schedule = self.service.get_schedule()
        total = self._total_pending()
        tasks = self.service.list_tasks(limit=PAGE_SIZE, offset=0) if total else []
        if not tasks and not schedule.get("send_empty", False):
            return False
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        text = render_tasks(tasks, total, 1, pages, self._tz().key, heading="Your to-do list")
        await self._send_scheduled(
            chat_id=self.settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._keyboard(tasks) if tasks else None,
        )
        return True

    async def send_reminder(self, task: dict) -> bool:
        if (
            not isinstance(task, dict)
            or type(task.get("id")) is not int
            or not 0 < task["id"] <= MAX_ID
        ):
            return False
        text = render_reminder(task, self._tz().key)
        await self._send_scheduled(
            chat_id=self.settings.telegram_chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=self._single_keyboard(task["id"]),
        )
        return True
