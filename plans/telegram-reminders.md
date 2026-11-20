# Telegram reminder controls and message refinement

This document records the original design for manual reminder creation, snooze and cancellation in Telegram.

## Acceptance
1. Authorized Telegram users can create reminders with relative durations, local dates/times and daily/weekly schedules; malformed or past input does not create orphan tasks.
2. `/remind 10m TITLE`, `/remind tomorrow 09:00 TITLE`, `/remind daily 09:00 TITLE`, `/remind weekly mon 09:00 TITLE`; existing tasks use `/remind #ID TIME`.
3. `/snooze ID TIME` accepts durations or `until DATE TIME`; inline +15m/+1h controls snooze only that task. `/pause TIME` pauses all automatic notifications; `/resume` clears pause/snooze; legacy global snooze still works. `/reminders [page]` lists active schedules; `/cancel ID` cancels reminders while retaining task.
4. Snooze/cancel are durable and atomic. Snooze preserves original recurrence anchor, replaces/cancels old reminder rows, works on an already-fired one-shot, and refuses completed/missing tasks. Cancel prevents recurring-task completion from resurrecting a canceled reminder. Already-claimed old canceled reminders are skipped before a new send.
5. Reminder and digest messages use safe HTML with visible title hierarchy, local deadline/timezone, priority and useful buttons. Escape all user text and preserve Telegram length limits. Completion, refresh, snooze and cancel callbacks acknowledge and update the displayed message. Bot's command menu/help teaches the actual syntax.
6. Expose snooze_reminder/cancel_reminder MCP tools through the same service; keep existing tools. Tests cover parser/DST, CRUD/reminder control, authorization, malformed callback/input, HTML escaping/Unicode bounds and real mocked PTB message/callback plumbing.

## Interfaces

Parser contract:
`@dataclass(frozen=True) class TimeSpec: when:datetime; recurrence:str|None; consumed:int`
`parse_time_spec(words:Sequence[str], timezone:str, now:datetime|None=None)->TimeSpec` returns UTC aware datetime, consumed token count, optional cadence. No title/ID parsing. Recognize optional leading `in`/`until`; compact positive durations (10s, 15m, 2h, 1d, 1w, 1h30m), number+unit (10 minutes, 2 hours), today/tomorrow HH:MM or 9am/5:30pm, YYYY-MM-DD TIME, bare TIME (next upcoming), aware ISO timestamp, daily TIME, weekly WEEKDAY TIME. Durations min 5 seconds/max365 days. Explicit dated/today/ISO input must be in future; bare/daily times choose next future date. DST gaps roll forward; repeated local times use first fold, as timeutil.localize. Preserve original title tokens after consumed count. Raise ValueError for bad input/naive now/invalid zone. Datetime helpers stdlib only; use existing timeutil.

Service additions:
`create_reminded_task(title:str, when:str, recurrence:str|None=None)->{'task':taskdict,'reminder':reminderdict}` atomically inserts new non-recurring task due at when plus reminder (optional repeating reminder until task completion). Validate future when/cadence/title BEFORE insert; no orphan on failure.
`snooze_task_reminder(task_id:int, when:str)->reminderdict`: future aware when; active task required; copy cadence and ORIGINAL recurrence_anchor from latest reminder (or None/when if absent), cancel/disable all prior rows and insert new reminder. Old claimed reminder ID must remain marked canceled. Explicit snooze can reactivate a previously canceled reminder.
`cancel_task_reminders(task_id:int)->{'task_id':int,'cancelled':int}` validates task exists, disables and marks all prior rows canceled, does not complete/delete task. Return number of previously enabled rows disabled.
Schema v3: add reminders.cancelled INTEGER NOT NULL DEFAULT0. Reminders returned with boolean cancelled. set_task_reminder marks old rows canceled; complete_task inherits only latest non-canceled reminder, preserving existing fired-one-shot inheritance for non-canceled history. New successor defaults canceled false. No destructive migration. Existing tests remain intact.

MCP: add `snooze_reminder(task_id,when)` and `cancel_reminder(task_id)`, strict IDs, service validation. Update exact tool-count tests/smokes from11 to13 as intentional capability expansion.
