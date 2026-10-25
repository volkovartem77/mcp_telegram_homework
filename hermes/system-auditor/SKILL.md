---
name: system-auditor
description: Explicitly requested read-only diagnosis of Telegram MCP HALTED/errors, completed-request metrics, and RPC quota consumption.
metadata:
  hermes:
    category: devops
    tags: [auditor, diagnostics, halted, request-metrics]
---

# System Auditor

Use only when the user explicitly asks for a system audit, incident diagnosis,
completed-request metrics, or RPC quota consumption, including "Where did the
internal RPC budget go?", "Auditor, investigate the latest Telegram
MCP HALTED/error" or "Auditor, audit the last completed request". This is a
separate read-only auditing role, not Telegram research or system administration.
Never enter this role automatically because research failed. Research must stop
and report the failure under the existing Telegram Research Rules.

## Fixed diagnostic interface

Use the existing `terminal` tool solely to run this fixed command. Do not use
arbitrary terminal commands, scripts, SQL, Python snippets, or file readers to
expand an audit. The helper reads fixed local sources independently of MCP:

```sh
/home/ubuntu/mcp_telegram_homework/venv/bin/python /home/ubuntu/.hermes/skills/system-auditor/scripts/audit.py latest-incident
```

For the latest completed foreground Telegram request, including success:

```sh
/home/ubuntu/mcp_telegram_homework/venv/bin/python /home/ubuntu/.hermes/skills/system-auditor/scripts/audit.py last-request
```

For RPC quota consumption, use this fixed read-only command:

```sh
/home/ubuntu/mcp_telegram_homework/venv/bin/python /home/ubuntu/.hermes/skills/system-auditor/scripts/audit.py rpc-usage --operation internal --hours 24
```

`--hours` is a positive integer, default 24: a rolling window ending at the
observation time, never a midnight-based calendar day. Omit `--operation` to
group all operations, or pass a stored label matching `[a-z_]{1,64}`. Report the
exact UTC window, every returned operation/RPC type and count, and totals from
the precomputed SQL aggregation. Failures consume dispatch quota too. Historical
`PingRequest` rows charged to `internal` are included and counted explicitly;
never silently remove them or move them to `keepalive`. Historical wrapper names
cannot be expanded into unrecorded methods. Do not equate all-operation totals
with the global quota. This command reads only the audit database, accepts no
SQL or file paths, writes no reports, and performs no Telegram or service actions.
Use it directly for quota questions; no session/log investigation is needed.

Optional selectors (one only), for `latest-incident` or `last-request`:

- `--operation-id 561a952d-7061-4f84-9315-409c015fbed2`: exact audited tool invocation.
- `--session 20261023_092827_a830e765`: last completed request in that session.
- `--at 2026-10-23T16:47:16Z`: request containing that explicit timezone timestamp.

Pass only validated selector values in these forms; never interpolate user prose
into a shell command. If the current session ID is available, use it with
`last-request` to stay in that conversation. Otherwise the helper selects the
latest completed Telegram request globally and identifies its session and user
message ID. Do not imply that global selection is necessarily this chat.

The helper uses SQLite `mode=ro`, `query_only`, fixed SELECTs, local process/socket
metadata, and sanitized classifications of fixed logs. It never imports Telethon,
calls MCP, acquires the Telegram session lock, or connects to Telegram. Session
credentials and message bodies are never returned. Configuration on disk is
not proof of the running process's loaded settings.

## Interpretation and response

1. Run one selected audit. Use the precomputed Python/SQL numbers; never ask a
   model to count tokens, aggregate raw logs, or infer missing usage. `null`
   metrics mean unavailable/incomplete, not zero. Do not substitute session totals.
2. Identify exact UTC request boundaries, session ID and incident operation ID.
   A completed request ends at its saved final response before the next user.
   This is not proof of delivery to Telegram. The current unfinished audit is
   excluded. Background pings/updates are excluded from research RPC totals by
   tool operation IDs; detached review has no persisted foreground response.
   Ambiguous log/trace correlations explicitly prevent complete LLM/token totals.
3. Report elapsed time, LLM/API call count and latency, logged input/output
   tokens, MCP calls, correlated research RPCs, rate-limit results, internal
   limiter waiting and RPC execution. Summed times may overlap or have incomplete
   coverage; do not invent an exact residual breakdown. Limiter waits do not
   include explicit agent sleeps. Do not treat a soft research budget overrun
   as a failure or recommend tightening it.
4. Give a short timeline and relevant tool/RPC IDs, names, outcomes, error
   classes, dispatch timestamps, waits and duration. A failed dispatched attempt
   is not proof of wire transmission. A later HALTED rejection may be an old
   latch, not a new failure. Compare recent failed RPC timestamps, current state,
   ping timestamps, quotas, PID starts and deployed commit. Preserve that distinction.
5. Separate confirmed evidence, likely explanation and unknowns. Missing sockets
   or a ping gap alone do not prove disconnection. Nearby log events are context,
   not automatically part of the selected request. Unconfirmed incident/request
   correlation must be disclosed. Do not claim an old bug recurred without evidence.
6. Recommend the smallest human-operated next step; do not execute it. If evidence
   is unavailable or sanitized away, say so and stop. Do not troubleshoot the
   diagnostic helper or install more tooling during an audit.

Answer concisely in the user's language, with a small metrics/timeline table if
useful. No raw log dumps, private chat content, tokens, credentials or session data.
For an explicitly requested long report, append `--report` to the same single
command: its only write is a sanitized `.txt` report under
`/home/ubuntu/.hermes/cache/system-auditor/`. Attach only the returned `report_path`
using Hermes' existing final-response `MEDIA:/absolute/path` syntax, with a short
summary. This uses the existing bot response transport, never Telegram MCP.
Do not upload source logs, databases, configs, or any other files.

## Hard boundary

No repairs, authentication, HALTED reset, file/config/skill/code/database changes,
service actions, Git writes, recovery, tests, network probes, or live Telegram
requests. Only the optional sanitized report artifact may be written. Do not
modify this skill through self-improvement. Do not bypass the research limiter
or restrictions. This skill constrains behavior; it does not sandbox Hermes'
existing terminal capability. The helper is the narrowly scoped interface.
