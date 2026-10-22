# Telegram MCP Server Specification

## Purpose and Scope

Build a reliable, deterministic Telegram research tool for external AI agents. Develop locally first; deploy to a VPS later. Use Python 3.13, Telethon, FastMCP, and SQLite. Authenticate with a dedicated Telegram user account, not a Telegram bot.

The server exposes Telegram operations. It must not contain LLM-based reasoning, autonomous research workflows, or semantic conversation analysis. Research planning, conversation membership decisions, iterative context expansion, and research stopping decisions belong to the external AI agent and its skills.

This specification describes planned capabilities, not authorization to implement them. Development follows the user-approved increments in `AGENTS.md`.

## Planned MCP Tools

| Tool | Responsibility |
| --- | --- |
| `list_dialogs()` | List chats, groups, and channels accessible to the account. |
| `search_chat(chat_id, query, ...)` | Perform native Telegram message search within an existing chat. |
| `read_around(chat_id, message_id, before=20, after=20)` | Retrieve neighboring messages deterministically. |
| `get_message(chat_id, message_id)` | Retrieve a specific message and its metadata. |
| `get_replies(chat_id, message_id, ...)` | Retrieve available Telegram reply/thread relationships; explicitly report limitations when complete reply discovery is unavailable. |

Signatures are provisional; `...` denotes parameters still to be specified. Structured MCP responses must include message IDs, timestamps, source references, and reply metadata where applicable. Errors and incomplete discovery must be explicit, never disguised as empty successful results.

## Planned Infrastructure and Constraints

- Interactive first-time authorization using a login code and optional 2FA, with persistent Telegram session reuse.
- One Telegram session owner.
- Centralized request scheduling and conservative rate limits.
- Request accounting at actual Telegram RPC dispatch where technically feasible; any accounting gaps must be explicit.
- No uncontrolled automatic retries.
- Persistent emergency-stop state requiring manual recovery.
- Detailed structured audit logs covering operations, errors, timestamps, purpose, and outcomes, without exposing credentials, login codes, 2FA passwords, API hashes, or session files to logs or LLM context.
- Strict, configurable message age boundaries enforced in code.
- SQLite for persistent operational state where necessary.

Safety-critical restrictions must be enforced by code rather than relying solely on agent instructions. Prefer deterministic behavior and explicit errors over silent fallbacks. Live Telegram access requires explicit user authorization during development.

## Initial MVP Exclusions

- Discovering new public Telegram communities or joining groups.
- Autonomous research loops or LLM-based message interpretation.
- Vector databases or bulk history ingestion.
- Web search or report generation.

These exclusions apply to the initial MVP and do not rule out future versions.

## Open Questions

- What MCP transport and local process lifecycle should be used, and how should single session ownership be enforced?
- What are the precise tool parameters, pagination rules, ordering guarantees, response/error schemas, and source-reference formats?
- How should `read_around` define neighboring messages, handle missing anchors, and report unavailable or filtered context?
- Which reply/thread relationships can be retrieved, and how should discovery limitations be represented?
- What message age limits, timestamp semantics, and boundary behavior should apply across all tools?
- What rate limits, retry policy, emergency-stop triggers, and manual recovery procedure should be used?
- Where can Telethon RPC dispatch be accounted for reliably, including internal requests and retries, and what gaps remain?
- Which operational state belongs in SQLite, and how should secrets, sessions, audit retention, and operation-purpose metadata be managed?

## Current Status

Documentation only. No application code, dependencies, authorization flow, MCP tools, database, or tests have been implemented in this step.
