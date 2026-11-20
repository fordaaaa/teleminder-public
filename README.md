# Teleminder

A personal Telegram task manager with persistent reminders and thirteen MCP tools. Runs continuously on your home server, including when your laptop is off. Telegram uses long polling: no webhook or public port is needed. AI clients connect through your tailnet using authenticated Streamable HTTP, or locally over stdio.

Python 3.12+, official `mcp.server.fastmcp` SDK, python-telegram-bot, SQLite, and a small persistent scheduler. No external database or AI subscription is required by the service. AI clients supply their own models.

See [verification](plans/verification.md) for how to check a deployment with your own bot.

## macOS companion

The package provides `teleminder client status` and `teleminder client deploy`. Private deployment metadata and credentials live in `~/.teleminder` (directory mode 700; files mode 600). The installed companion manages the existing home-server container; it does not run a second Telegram worker on the Mac.

For a configured installation, Telegram needs only these two values in `~/.teleminder/.env`:

```dotenv
TELEGRAM_USER_ID=your_numeric_user_id
TELEGRAM_BOT_TOKEN=your_BotFather_token
```

Then run:

```sh
teleminder client deploy
teleminder client status
```

Send `/start` to your bot first. Your user ID becomes both the notification destination and the private-chat authorization boundary. A numeric bot ID alone cannot authenticate the bot. Keep the existing MCP credential and server settings unchanged. Applying configuration backs up the remote `.env`, sends new values through SSH stdin, and switches from MCP-only mode to the full worker once both Telegram values exist. It uses the already-built image and does not upload source.

To install the CLI on a new Mac from this repository:

```sh
uv tool install --python 3.12 --constraints requirements.lock .
```

Create `~/.teleminder/deployment.json` using your SSH target, the absolute path to your server checkout, and the URL of your MCP endpoint. For example:

```json
{
  "ssh_target": "operator@server.example",
  "remote_directory": "/srv/teleminder",
  "mcp_url": "https://homelab.example.ts.net/mcp"
}
```

Set the directory to mode 700 and the files inside it to mode 600. `teleminder client config` shows the file path. Keep deployment metadata, credentials, and Telegram IDs out of source control.

## Setup

1. In Telegram, open the official [@BotFather](https://t.me/BotFather), send `/newbot`, and save the returned token privately. Send `/start` to your new bot from your own account. For groups, keep BotFather privacy mode enabled and use only a trusted group.
2. Copy configuration and fill in the token:

   ```sh
   cp .env.example .env
   chmod 600 .env
   # Edit TELEGRAM_BOT_TOKEN in .env. Do not commit this file.
   ```

3. Find your user/chat IDs with your own bot, without a third-party lookup service. With [uv](https://docs.astral.sh/uv/) installed:

   ```sh
   uv sync --frozen --extra dev --python 3.12
   uv run python scripts/telegram_ids.py
   ```

   Stop any existing polling worker first. This reads pending updates and prints only IDs. If no updates appear, send `/start` again. In a private chat, user and chat IDs normally match; group chat IDs are negative. Set only `TELEGRAM_USER_ID` in `.env`; the private chat and both allowlists are derived from it. With this setting present, other user/chat allowlists cannot expand access. Advanced group/multi-user mode is optional: omit `TELEGRAM_USER_ID` and explicitly configure `TELEGRAM_CHAT_ID`, `ALLOWED_USER_IDS`, and `ALLOWED_CHAT_IDS` instead. All authorized users then share one task list, and everyone in a target group can read its messages.

4. Generate a separate MCP secret with `openssl rand -hex 32` and put it in `MCP_TOKEN`. Keep the generated value out of source control. Set `MCP_ALLOWED_HOSTS` to your server's full tailnet DNS name, for example `homelab.example.ts.net,homelab.example.ts.net:*`.
5. Build and run on the home server:

   ```sh
   docker compose up -d --build
   docker compose logs -f teleminder
   docker compose exec teleminder teleminder health
   ```

   Install Docker Engine and its Compose plugin first. On systems with the standalone Compose executable, replace `docker compose` with `docker-compose`. This repository was validated with standalone Compose.

6. Send `/add Buy milk`, then `/list`. Press **✅ Complete** and confirm the task disappears. Use an AI client to create another task and `send_digest` to check the whole connection. Delivery must be checked with your own Telegram credentials.

Optional BotFather `/setcommands`:

```text
start - Get started
help - Show commands
list - Active tasks, optionally a page number
add - Add a task title
done - Complete a task by ID
today - Tasks due today
settings - Show or change notification settings
```


## Add a Telegram user

1. Have the person open a private chat with the bot and send `/start`. The bot shows their Telegram user ID if they haven't been added yet.
2. In the private server `.env`, keep your existing `TELEGRAM_USER_ID` and set `TELEGRAM_PROFILE_USER_IDS` to their numeric ID. Separate multiple IDs with commas. If you deploy with the companion CLI, include the same setting in its private `~/.teleminder/.env`.
3. Apply the configuration with `docker compose up -d --force-recreate`. Then have them send `/start` again. They can set reminders with `/remind 10m Take a break` or use the action buttons.

Each added user starts with an empty profile. Their tasks, reminders, and notification settings are private to their Telegram chat. The original database and MCP endpoint belong to the owner. Keep the owner ID fixed when adding profiles because the original database follows that setting.

Profile databases are stored beside the owner database in a directory named after the database file with a `.profiles` suffix. Include that directory in your backup plan. Removing an ID from the list disables that profile's Telegram access and scheduled deliveries. Adding the same ID again reopens its saved profile.

## Explicit MCP-only mode

To manage tasks before Telegram credentials are configured:

```sh
docker compose -f docker-compose.yml -f docker-compose.mcp-only.yml up -d --build
docker compose exec teleminder teleminder health --mcp-only
```

This runs `teleminder http`, without a Telegram poller or scheduler. `/healthz` reports `mode: mcp-only`; no worker heartbeat is fabricated. All thirteen tools remain authenticated, but reminders and queued `send_digest` requests wait for the full worker. Switch to `docker compose -f docker-compose.yml up -d` after configuring Telegram; the same volume retains tasks and schedules. Do not run full and HTTP-only instances on the same port simultaneously.

## Deploy through Tailscale

Copy this directory to the home server, excluding `.venv`, local databases, and backups. Create `.env` on that server. Keep the SQLite volume on a **local disk**, not NFS/SMB. Install Tailscale on the server and on the computer running your AI client; connect both to your tailnet.

The Compose port binds only to `127.0.0.1:8765`. On the server, enable tailnet HTTPS/MagicDNS when prompted and run:

```sh
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

If port 443 is already serving another application, use an unused HTTPS port instead, e.g. `tailscale serve --bg --https=9449 http://127.0.0.1:8765`, and include `:9449` in the MCP URL. This preserves existing services.

Set the hostname shown by Serve in `.env`:

```dotenv
MCP_ALLOWED_HOSTS=homelab.example.ts.net,homelab.example.ts.net:*
```

Then apply the environment change with `docker compose up -d --force-recreate`. Your MCP URL is `https://homelab.example.ts.net/mcp`. Serve provides private tailnet HTTPS; keep the bearer token and restrict reachability with tailnet access rules. Do not enable Funnel or open router ports. [Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve).

From another tailnet device:

```sh
curl --fail https://homelab.example.ts.net/healthz
# Expected while healthy: {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://homelab.example.ts.net/mcp
# Expected: 401 (missing bearer token)
```

`/healthz` intentionally returns only an unauthenticated health boolean. Task tools always require authentication. A `421` from MCP means the requested hostname is missing from `MCP_ALLOWED_HOSTS`. Browser-origin requests are blocked; the supported coding clients do not need browser CORS.

The host needs outbound internet access to Telegram and reliable system time. Tailscale must be connected on the AI client's machine when it accesses remote MCP. Telegram reminders continue without an MCP connection or your personal computer.

## Connect AI agents

Set `TELEMINDER_MCP_TOKEN` in the environment of the AI client to the same secret as the server's `MCP_TOKEN`. GUI clients must receive it through their own environment/configuration; a shell export does not automatically reach an already running application. These examples use static bearer headers, not an OAuth login.

### Claude Code

Project `.mcp.json` (the credential is referenced, not embedded):

```json
{
  "mcpServers": {
    "teleminder": {
      "type": "http",
      "url": "https://homelab.example.ts.net/mcp",
      "headers": {"Authorization": "Bearer ${TELEMINDER_MCP_TOKEN}"}
    }
  }
}
```

Alternatively:

```sh
claude mcp add --transport http --scope user teleminder \
  https://homelab.example.ts.net/mcp \
  --header 'Authorization: Bearer ${TELEMINDER_MCP_TOKEN}'
```

Check `/mcp` inside Claude Code. See the official [Claude Code MCP instructions](https://code.claude.com/docs/en/mcp) for configuration scopes and environment expansion.

### Codex

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.teleminder]
url = "https://homelab.example.ts.net/mcp"
bearer_token_env_var = "TELEMINDER_MCP_TOKEN"
```

Or run:

```sh
codex mcp add teleminder --url https://homelab.example.ts.net/mcp \
  --bearer-token-env-var TELEMINDER_MCP_TOKEN
codex mcp list
```

Syntax was checked against the installed Codex CLI and the official [Codex MCP documentation](https://developers.openai.com/codex/mcp). Restart/reconnect your client after changing configuration. Other MCP clients can use the same URL and `Authorization: Bearer …` header.

### OpenCode

Use the current `mcp.servers` layout in global `~/.config/opencode/opencode.json` (existing supported V1 configurations may retain their current layout):

```json
{
  "mcp": {
    "servers": {
      "teleminder": {
        "type": "remote",
        "url": "https://homelab.example.ts.net:9449/mcp",
        "oauth": false,
        "headers": {"Authorization": "Bearer {file:~/.teleminder/mcp-token}"}
      }
    }
  }
}
```

Store your MCP credential in the referenced private file with mode 600. Run `opencode reload`, then `opencode mcp list` to check the connection. Official [OpenCode MCP configuration](https://opencode.ai/docs/mcp-servers/) and [file substitution](https://opencode.ai/docs/config/#files) describe the supported syntax.

Reopen Codex and Claude Code after updating their `TELEMINDER_MCP_TOKEN` environment variable. Merge the examples into your existing client configuration and keep copies under your private configuration directory.

### Local stdio

Stdio runs an MCP-only process: it does not start a second Telegram worker. On the same host as the Compose project, Claude Code can use this configuration (replace the absolute project path):

```json
{
  "mcpServers": {
    "teleminder-local": {
      "command": "docker",
      "args": ["compose", "-f", "/absolute/path/teleminder/docker-compose.yml", "--project-directory", "/absolute/path/teleminder", "run", "--rm", "--no-deps", "-T", "teleminder", "teleminder", "stdio"]
    }
  }
}
```

Codex equivalent:

```toml
[mcp_servers.teleminder_local]
command = "docker"
args = ["compose", "-f", "/absolute/path/teleminder/docker-compose.yml", "--project-directory", "/absolute/path/teleminder", "run", "--rm", "--no-deps", "-T", "teleminder", "teleminder", "stdio"]
```

`-T` disables terminal allocation and preserves the MCP byte stream. Compose's default interactive stdin remains enabled. The shared named volume connects to the running worker's data; no service ports are published by `compose run`.

For a native Python installation, use an absolute path to `teleminder` (or `uv --directory /absolute/path/teleminder run teleminder stdio`) and set `DATABASE_PATH` to the same absolute SQLite file as the worker. Stdio trusts the local OS user; keep its file access private. Prefer remote HTTP when your client is on a different machine.

### Example prompts

- “Use Teleminder to add Finish biology assignment, high priority, due tomorrow at 5 PM Toronto time. Set a reminder for 4 PM. Show me the saved task and reminder.”
- “List my active tasks tagged school and complete task 12.”
- “Create a weekly task to take out recycling, due Friday at 7 PM Toronto time, and set a reminder at the due time.”
- “Snooze automatic notifications until 3 PM Toronto time, then show the schedule.”
- “Send my current task digest to Telegram.”

Agents translate natural language into tool parameters with explicit date offsets. This server manages tasks and notifications; its tools do not execute shell commands on your homelab.

## Tools and Telegram commands

| MCP tool | Behavior |
| --- | --- |
| `add_task` | Title, optional description/deadline, high/medium/low priority, tags, optional daily/weekly/monthly recurrence |
| `list_tasks` | Active by default; include completed, filter tag, paginate with limit/offset |
| `get_task` | One task by ID, including history |
| `update_task` | `task_id`, `changes` object; explicit null clears due_at/recurrence |
| `complete_task` | Idempotent completion; retains history and creates a recurring successor |
| `delete_task` | Permanently removes the task and its reminders |
| `set_task_reminder` | Replaces active reminder; aware `when`, optional hourly, daily, weekly, or monthly recurrence |
| `list_reminders` | All reminders, including disabled/canceled history; optional task ID |
| `snooze_reminder` | Snooze one active task until an aware timestamp; preserve cadence |
| `cancel_reminder` | Cancel task reminders without deleting or completing the task |
| `send_digest` | Durable immediate-send request; returns queue ID, not delivery confirmation |
| `get_schedule` | Current persisted schedule |
| `update_schedule` | `changes` object, validated and persisted |

Tool input examples:

```json
{"title":"Biology", "due_at":"2027-01-15T17:00:00-05:00", "priority":"high", "tags":["school"]}
{"task_id":1, "when":"2027-01-15T16:00:00-05:00"}
{"task_id":1, "changes":{"description":"Read chapters 3–4", "priority":"low"}}
{"changes":{"interval_minutes":120, "enabled":true, "snoozed_until":null}}
```

Telegram commands work directly in your private chat — an AI client is optional:

| Command | Example / action |
| --- | --- |
| `/remind TIME TITLE` | `/remind 10m Take a break` or `/remind tomorrow 09:00 Review notes` |
| `/remind #ID TIME` | `/remind #2 1h` adds/replaces the reminder on an existing task |
| `/remind daily TIME TITLE` | `/remind daily 9am Take vitamins` |
| `/remind weekly DAY TIME TITLE` | `/remind weekly mon 09:00 Plan the week` |
| `/snooze ID TIME` | `/snooze 2 2h` or `/snooze 2 until tomorrow 09:00` |
| `/snooze ID until DATE TIME` | `/snooze 2 until 2027-01-15 14:30` |
| `/reminders [page]` | Upcoming enabled reminders, with task IDs |
| `/cancel ID` | Cancel reminders, keep the task open |
| `/pause [TIME]` | Pause all automatic notifications; `/pause 2h`, `/pause until tomorrow 9am`, or indefinitely with `/pause` |
| `/resume` | Clear global pause/snooze and enable automatic notifications; quiet hours still apply |
| `/list [page]`, `/add TITLE`, `/done ID`, `/today`, `/settings`, `/help` | Existing task/settings commands |

Durations accept `10s`, `15m`, `2h`, `1d`, `1w`, `1h30m`, or `10 minutes` (5 seconds to 365 days). Local time accepts `09:00`, `9am`, or `5:30pm`. A bare time means its next occurrence; explicit dates/today must be in the future. Date inputs use the persisted timezone, and confirmations show the exact local time. Quiet hours or an existing global pause can defer automatic delivery. Daily/weekly manual reminders repeat until the task is completed; recurring tasks that create future successors remain separately configurable through MCP.

Reminder cards have a bold title, description, local deadline/timezone, priority, task ID, and native **Done**, **+15 min**, **+1 hour**, **Custom time**, and **My tasks** buttons. Custom time offers tomorrow at 9 AM, cancellation, and copyable commands for arbitrary dates. Per-task snooze does not pause other tasks and preserves the original recurrence cadence. Already-claimed canceled/replaced reminders are skipped before sending. Cancellation persists and is not resurrected by completing a recurring task. Task lists/digests use the same safe HTML formatting; arbitrary task text cannot inject markup.

The bot registers its command menu for your destination chat. Legacy `/settings snooze MINUTES` and the digest's **Pause all 1h** button remain available. Task lists show up to 20 items per page. Message bodies and callback data are bounded to Telegram limits.

### Calling and voice

The official Telegram calling method is [limited to user accounts](https://core.telegram.org/method/phone.requestCall); this Bot API service cannot initiate a Telegram voice call. Telegram supports [voice-note messages](https://core.telegram.org/bots/api#sendvoice), but they are messages, not ringing calls. A real phone-call escalation would need a separate calling provider and its credentials. No paid provider, user-account automation, or call integration is enabled by this update.

A due date does **not** automatically set a specific reminder: call `set_task_reminder`. The task still appears in active digests. Editing a task's due date does not shift its independently configured reminder. Re-setting a reminder disables the previous one.

## Scheduling and delivery behavior

Defaults: hourly digests at 09:00 through 21:00 inclusive, `America/Toronto`; quiet hours 22:00–09:00, end exclusive. Digests are skipped when no active tasks exist unless `send_empty` is true. Interval slots start at `start_hour:00`, end no later than `end_hour:00`. Equal quiet start/end disables quiet hours. The worker checks persistent schedules every 10 seconds and paces scheduled sends to the target chat. Large backlogs take longer. Delivery is approximate, not an alarm-clock guarantee.

Schedule fields: `timezone`, `start_hour`, `end_hour`, `interval_minutes`, `quiet_start`, `quiet_end`, `enabled`, `send_empty`, `snoozed_until`. `/settings off` disables **automatic digests and reminders**. Snooze postpones automatic notifications without completing tasks. Clear snooze using MCP `update_schedule` with `{"changes":{"snoozed_until":null}}`. Explicit `send_digest` requests bypass quiet hours, disable, and snooze, but still skip an empty list by default.

Schedules and queued digest requests survive restarts. Missed reminders coalesce to one notification when allowed again; the next recurrence is in the future. Missed digests coalesce to the latest slot within the current digest window. No overnight digest backlog is replayed. A fresh start within the digest window may immediately send the current slot. An MCP-only process can queue a digest, but the worker must be running to send it.

Dates require ISO 8601 **with an explicit offset** (`-05:00`, `-04:00`, or `Z`). Naive timestamps are rejected. Storage is UTC; display and recurrence follow the persisted IANA timezone. Calendar recurrence preserves the original local time and original day of month: January 31 → February 28 → March 31. Spring-forward gaps roll forward by the gap; fall-back uses the first occurrence. Completing a recurring task creates exactly one future successor, skipping missed occurrences; historical completions stay stored. If the completed task had a reminder, the successor gets one at its due time with the latest reminder cadence. Original lead time is not copied.

**Duplicate prevention and delivery limits:** claiming a notification and advancing its schedule happen in one SQLite transaction before sending. The same scheduled event is never automatically attempted twice. Telegram offers no idempotency key, so a crash or ambiguous network failure after the claim can lose that notification. Failed attempts are logged and retained in the ledger; abandoned claims become failed after one day. Ordinary later digests still include outstanding tasks. This favors avoiding duplicates over automatic retries. Separate manual digest requests intentionally create separate messages. Inspect `teleminder status`; re-request a digest or reset a reminder if needed. Ledger records are retained for 30 days.

## Configuration

See [.env.example](.env.example). All credentials come from the environment; Compose loads `.env`, native CLI loads `~/.teleminder/.env` first, then a working-directory `.env`, without overriding exported variables.

| Variable | Initial default / purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Required BotFather token for worker |
| `TELEGRAM_USER_ID` | Personal owner ID; derives private chat and both allowlists |
| `TELEGRAM_PROFILE_USER_IDS` | Comma-separated IDs for additional private Telegram profiles |
| `TELEGRAM_CHAT_ID`, `ALLOWED_USER_IDS`, `ALLOWED_CHAT_IDS` | Optional advanced mode when owner ID is omitted |
| `MCP_TOKEN` | Required for HTTP; at least 32 characters |
| `MCP_ALLOWED_HOSTS` | Exact remote DNS hosts; localhost always allowed |
| `DATABASE_PATH` | `/data/teleminder.db` in Docker; `data/teleminder.sqlite` native |
| `TIMEZONE` | `America/Toronto` |
| `MCP_HOST`, `MCP_PORT` | Container `0.0.0.0:8765`; native `127.0.0.1:8765` |
| `BIND_ADDRESS` | Host port binding `127.0.0.1`; keep loopback with Serve |
| `DIGEST_START_HOUR`, `DIGEST_END_HOUR` | `9`, `21` |
| `DIGEST_INTERVAL_MINUTES` | `60` |
| `QUIET_START_HOUR`, `QUIET_END_HOUR` | `22`, `9` |
| `SEND_EMPTY_DIGEST` | `false` |

`TIMEZONE` and digest/quiet variables seed SQLite **only on first initialization**. Afterwards use the schedule tools; rebuilding or changing those environment values does not overwrite saved preferences. Integer hour values are local hours. Use real random tokens, not the example placeholders.

## Operations, backup and restore

```sh
docker compose exec teleminder teleminder status
docker compose exec teleminder teleminder health
docker compose logs --tail 100 teleminder
docker compose restart teleminder
docker compose down                    # keeps the named data volume
```

Health checks SQLite, the background heartbeat (under 120 seconds old), and HTTP reachability. A healthy process does not prove Telegram delivery: monitor failed/claimed delivery counts and perform a live test. Docker restarts exited processes; an unhealthy check alone does not trigger Docker's restart policy. Unexpected scheduler errors terminate the process so the restart policy can act. A token conflict with another polling bot instance also exits. Keep exactly one worker per bot/database; an OS lock prevents two workers sharing a database locally.

The named volume `teleminder-data` holds the database, WAL files and worker lock. Do not delete the volume or the active lock file. SQLite uses versioned transactional migrations, WAL, foreign keys and busy timeouts. Back up before upgrades; older application versions refuse a newer schema. Never run an older version against an upgraded database.

Create a consistent online backup using SQLite's backup API, then copy it off the server disk:

```sh
docker compose exec teleminder teleminder backup /data/backups/snapshot.sqlite
mkdir -p backups
docker compose cp teleminder:/data/backups/snapshot.sqlite ./backups/snapshot.sqlite
chmod 600 backups/snapshot.sqlite
```

The destination must not already exist; use a new name each time. Do not copy only a live `.db` file: recent changes may still be in WAL. Backups contain private task data and should be encrypted/protected as appropriate.

Restore with the worker **and all stdio clients stopped**:

```sh
docker compose stop teleminder
# If restoring an off-server backup, first copy it into the volume:
# docker compose cp ./backups/snapshot.sqlite teleminder:/data/backups/snapshot.sqlite
# A copied file may be root-owned; keep it readable by the container's runtime user.
docker compose run --rm --no-deps -T teleminder teleminder restore /data/backups/snapshot.sqlite
docker compose up -d
```

Restore validates integrity/schema, migrates supported older backups in an isolated working copy, and leaves the original backup unchanged. Newer or unsupported schemas are refused. It creates a uniquely named `.pre-restore-…sqlite` safety backup before atomically replacing the target database. If the target is corrupt, it preserves raw bytes and any WAL/SHM sidecars as forensic safety copies instead. Worker and stdio processes launched through the CLI hold a maintenance lock; restore refuses while any are active. Restores include task/reminder/schedule/delivery state; a deliberately older snapshot may re-enable notifications already sent after that snapshot. Credentials remain in `.env` and need a separate secure backup. Rehearse restores periodically on an isolated copy.

Compose uses a non-root runtime user, read-only root filesystem, dropped capabilities, `no-new-privileges`, bounded log rotation and a persistent local volume. Native CLI sets a private umask. Tokens, message bodies, task titles, and request URLs are excluded from application logs. Anyone with the MCP token can read/modify this personal task database. Rotate leaked credentials and recreate the service with updated environment values.

## Development and validation

Keep each commit focused. Use `feat:` for a feature and `fix:` for a correction. Use sample values in documentation and tests.

```sh
uv sync --frozen --extra dev --python 3.12
uv run pytest -q
uv run ruff check teleminder tests scripts
uv run ruff format --check teleminder tests scripts
uv build
# Validate without supplying a real .env:
docker compose --env-file .env.example config --no-env-resolution -q
docker build -t teleminder:local .
uv run python scripts/smoke_image.py teleminder:local
uv run python scripts/smoke_http_image.py teleminder:local
```

The smoke test uses an ephemeral, network-disabled container with no credentials and exercises the official MCP client against the image's stdio server. Tests cover CRUD, concurrency/idempotence, authorization, callbacks, mocked Telegram HTTP responses, scheduling, DST, migrations, backups, HTTP authentication and both official SDK client transports. [Implementation contract](plans/implementation.md) records architecture and interfaces.

Runtime dependency versions and hashes are exported from `uv.lock` into `requirements.lock`. To deliberately refresh dependencies, run `uv lock --upgrade`, test, then `uv export --format requirements-txt --no-dev --no-emit-project --output-file requirements.lock`. Rebuild images with `--pull` to receive base-image security fixes.
