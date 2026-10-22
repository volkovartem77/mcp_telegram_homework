# Telegram MCP Server

A local, read-only Telegram research server for AI agents, using Python 3.13,
Telethon, FastMCP, and SQLite. It uses a dedicated **user account**, not a bot.
Five tools list dialogs, search messages, read individual messages, retrieve
native reply pages, and read nearby history. Research planning and interpretation
belong to the external agent. See [TOOLS.md](TOOLS.md) for tool usage.

## Install and configure

Run commands from the project root. Python 3.13 and macOS/Linux are required
(session ownership uses a POSIX file lock). Reuse the existing `venv`.

```bash
# Only if venv does not already exist:
python3.13 -m venv venv

venv/bin/python -m pip install -e .
# First-time configuration; preserve an existing .env:
cp -n .env.example .env
chmod 600 .env
```

Obtain your own Telegram API credentials at [my.telegram.org](https://my.telegram.org)
and edit the local `.env` with `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
Never share `.env`, login codes, 2FA passwords, or session files with an agent.

## Authorize the session manually

Stop any running MCP server, then run:

```bash
venv/bin/python scripts/login.py
```

This connects to Telegram. Enter your phone number and login code in the console;
enter your 2FA password if requested. Hidden password input is preferred; a warning
precedes visible fallback input. Success saves `data/telegram.session`. An already
authorized session is reused without requesting another code. Failed logins are
not automatically retried. The MCP server never performs interactive login.

## Start the MCP server

```bash
venv/bin/python -m telegram_mcp.server
```

The server uses **stdio**, connects to Telegram at startup, and waits for an MCP
client; it is not an interactive command prompt. For Hermes or another MCP host,
use the absolute path to `venv/bin/python` as the command and
`["-m", "telegram_mcp.server"]` as its arguments. Run only one session owner.

## Settings and operational state

| File | Purpose |
| --- | --- |
| `tool_settings.yaml` | Default and maximum page sizes for each paginated tool. |
| `rate_limits.yaml` | Global/per-operation RPC windows, burst cooldowns, queue size, maximum wait, and RPC timeout. Values are placeholders, not Telegram-safe guarantees. |
| `data/operations.sqlite3` | Persistent counters, cooldowns, HALTED state, and linked MCP/RPC audit records. Message content and credentials are excluded from these audits. |

Restart the server after changing settings. Keep operational state across restarts;
deleting it would discard safety history.

## Troubleshooting and recovery

- **Missing/unauthorized session:** stop the server and use the manual login script.
  Resolve any separate HALTED condition before restarting.
- **Session already owned:** stop the other server/login/recovery process. Do not
  delete the lock file to bypass ownership.
- **`inaccessible` / `peer_not_cached`:** list the relevant dialogs page first.
- **`rate_limited`:** wait until `retry_at`; do not retry in a tight loop. Startup
  and internal requests also consume the shared quota.
- **HALTED:** stop the server and inspect the cause offline:

```bash
venv/bin/python scripts/recover.py
# Only after reviewing and resolving the cause, explicitly reset:
venv/bin/python scripts/recover.py --confirm
```

Inspection can latch HALTED when unfinished work from a crash is detected; it is
not a strictly read-only database viewer. Recovery preserves counters and all
cooldowns, including FloodWait, and never replays requests or contacts Telegram.
Agents must not reset HALTED. RPC accounting has transport-level gaps; see
[SPEC.md](SPEC.md) and [RATE_LIMIT_TEST_REPORT.md](RATE_LIMIT_TEST_REPORT.md).
