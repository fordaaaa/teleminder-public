# Teleminder implementation contract

This document records the original implementation contract.

## Acceptance criteria
1. SQLite task CRUD persists across instances; completion is idempotent, completed records retained; daily/weekly/monthly recurring tasks create one successor on completion.
2. Authorized Telegram commands and inline completion/refresh/snooze work against the same TaskService as MCP; unauthorized user OR chat fails closed before accessing tasks. Messages are plain text and bounded to Telegram limits.
3. Durable reminders and local-time digests run without MCP clients; quiet hours, disable, snooze, restart coalescing, DST and duplicate suppression are tested. A durable delivery ledger claims each notification before delivery. Ambiguous delivery failures are not retried automatically (Telegram has no idempotency key).
4. Official MCP Python SDK exposes all eleven requested tools through stdio and authenticated Streamable HTTP. Authentication and actual SDK tool calls are tested.
5. Non-root Docker deployment persists SQLite, has health checks, restricted port binding, restart policy, environment-only secrets, documented Tailscale HTTPS Serve and client examples.
6. Meaningful unit/integration tests, lint, packaging, and available container checks pass. Real Telegram and homelab delivery are explicitly unverified without credentials/access.

## Data model
SQLite schema version migrations under an exclusive initialization transaction, WAL, foreign keys, busy timeout. UTC ISO timestamps in storage; display and calendar recurrence in configured IANA timezone. Tasks: id/title/description/created_at/due_at/priority/tags/completed_at/recurrence/parent_id/recurrence_anchor. Reminders: id/task_id/next_run/recurrence/enabled/recurrence_anchor. singleton schedule persisted as JSON. delivery ledger: unique event key/status/created_at/sent_at; bounded cleanup. Heartbeat for worker health. Single worker process lock prevents duplicate long polling. Shared access locks exclude restore while worker/stdio clients run. Schema v2 adds original recurrence anchors, with a tested v1 migration.

## Public interfaces (fixed contract for parallel work)
`Settings.from_env()` dataclass fields: telegram_bot_token:str, telegram_chat_id:int, allowed_user_ids:frozenset[int], allowed_chat_ids:frozenset[int], timezone:str, database_path:str, mcp_token:str, mcp_host:str, mcp_port:int. Additional defaults allowed. `Settings.validate_worker()` requires token and both allowlists, target in allowed chats. `Settings.validate_remote()` requires 32+ character token.
`TaskService(database_path: str, timezone: str = 'America/Toronto')` initializes DB. Synchronous methods return JSON-compatible dicts/lists; invalid arguments raise ValueError, missing task raises ValueError. Thread-safe connection-per-operation.
- add_task(title, description='', due_at=None, priority='medium', tags=None, recurrence=None) -> task dict
- list_tasks(include_completed=False, tag=None, limit=100, offset=0) -> list[task]
- get_task(task_id:int) -> task
- update_task(task_id, **changes) -> task; supports title/description/due_at/priority/tags/recurrence; None clears nullable fields
- complete_task(task_id) -> task; repeated completion has no further effects
- delete_task(task_id) -> {'deleted': id}
- set_task_reminder(task_id, when:str, recurrence=None) -> reminder
- list_reminders(task_id=None) -> list[reminder]
- get_schedule() -> dict with timezone, start_hour=9, end_hour=21 (inclusive), interval_minutes=60, quiet_start=22, quiet_end=9 (exclusive), enabled=True, send_empty=False, snoozed_until=None
- update_schedule(**changes) -> schedule; snoozed_until accepts aware ISO string or None
Task dict keys: id, title, description, created_at, due_at, priority ('high'/'medium'/'low'), tags:list[str], completed_at, recurrence (None/'daily'/'weekly'/'monthly'), parent_id.
Datetime input must have explicit timezone offset; stored UTC; reject invalid/ambiguous naive timestamps. Recurrence uses local calendar with preserved wall-clock time and skips missed intervals; nonexistent DST time rolls forward, repeated time uses first occurrence.
`TelegramInterface(settings, service)` owns `application` (PTB Application), `register_handlers()`, async `send_digest()` -> bool, async `command(update, context)`, async `callback(update, context)`. Constructor registers handlers; never starts polling. `send_digest` returns False if empty and send_empty false, else True on send. For scheduler's durable delivery, send_digest is just a transport; no ledger logic here. /settings supports show, on, off, snooze MINUTES. /add syntax title, no natural-language parsing. /done ID. callbacks `done:ID`, `refresh`, `snooze:60`. Authorize both effective_user.id and effective_chat.id for every incoming update. async `on_error` logs only exception type. Digest max 20 tasks per message, show total, provide /list pagination if needed; keep text <=4096 and callbacks <=64 bytes. /list [page] uses 20-item pages.
`Scheduler(service, telegram)` async tick(now:datetime|None=None) processes durable deliveries; independent worker loop every <=15 seconds; main owns implementation.
`create_mcp(service, settings, notifier=None)` returns FastMCP; main owns implementation.
