# Verify a Teleminder deployment

Run the checks in [Development and validation](../README.md#development-and-validation) before deploying. The test suite uses temporary SQLite databases and a simulated Telegram API. No bot token or live reminder data is required to run it.

| Check | Expected result |
| --- | --- |
| Core service | Tasks and settings persist across restarts. Completing a task twice creates at most one recurring successor. |
| Reminder scheduling | Quiet hours, pause, snooze, cancellation, hourly cadence, and calendar recurrence behave as configured. |
| Telegram profiles | Each added profile has a separate database. Commands and callbacks from mismatched users or chats are denied. |
| MCP transports | Authorized SDK clients can use the tools over stdio or HTTP. HTTP rejects requests without the bearer token. |
| Database maintenance | Supported older snapshots migrate during restore. Invalid or newer snapshots are rejected. |
| Container smoke checks | Both stdio and HTTP tools work with a temporary database and synthetic configuration. |

## Check live delivery with your own bot

1. Follow the [setup instructions](../README.md#setup) and run `docker compose exec teleminder teleminder health`. If you deploy only MCP, pass `--mcp-only` and skip to step 5.
2. In Telegram, send `/remind 1m Test reminder`. Check that it appears under `/reminders` and that the bot posts its reminder card at the expected time.
3. Tap **Done**. Check that the task is no longer pending under `/list`. The worker's health check confirms the service is running. Only a live check confirms Telegram delivery.
4. If you enabled a second profile, repeat the check from that person's private chat. Confirm their reminder does not appear in the owner's task list or MCP tools.
5. If you enabled remote MCP access, use your endpoint's `/healthz` and test an authenticated client connection as described in the README. Keep tokens, IDs, hostnames, and test receipts in private deployment notes.
