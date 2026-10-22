# Telegram MCP Server Specification

## Purpose and Scope

Build a reliable, deterministic Telegram research tool for external AI agents. Develop locally first; deploy to a VPS later. Use Python 3.13, Telethon, FastMCP, and SQLite. Authenticate with a dedicated Telegram user account, not a Telegram bot.

The server exposes Telegram operations. It must not contain LLM-based reasoning, autonomous research workflows, or semantic conversation analysis. Research planning, conversation membership decisions, iterative context expansion, and research stopping decisions belong to the external AI agent and its skills.

This specification describes planned capabilities, not authorization to implement them. Development follows the user-approved increments in `AGENTS.md`.

## Planned MCP Tools

| Tool | Responsibility |
| --- | --- |
| `list_dialogs(page_size=20, cursor=None, folder_id=0)` | Read one explicit dialogs page, capped at 50 items. |
| `search_chat(chat_id, query, limit=None, since=None, cursor=None)` | Perform native Telegram message search within an existing chat. |
| `read_around(chat_id, message_id, before=20, after=20)` | Retrieve neighboring messages deterministically. |
| `get_message(chat_id, message_id, since=None)` | Read one message, optionally filtering by its returned timestamp. |
| `get_replies(chat_id, message_id, limit=None, cursor=None)` | Retrieve available Telegram reply/thread relationships; explicitly report limitations when complete reply discovery is unavailable. |

Signatures are provisional; `...` denotes parameters still to be specified. Structured MCP responses must include message IDs, timestamps, source references, and reply metadata where applicable. Errors and incomplete discovery must be explicit, never disguised as empty successful results.

## Planned Infrastructure and Constraints

- Interactive first-time authorization using a login code and optional 2FA, with persistent Telegram session reuse.
- One Telegram session owner.
- Centralized request scheduling and conservative rate limits.
- Request accounting at actual Telegram RPC dispatch where technically feasible; any accounting gaps must be explicit.
- No uncontrolled automatic retries.
- Persistent emergency-stop state requiring manual recovery.
- Detailed structured audit logs covering operations, errors, timestamps, purpose, and outcomes, without exposing credentials, login codes, 2FA passwords, API hashes, or session files to logs or LLM context.
- Optional per-request `since` preferences enforced in code. No global message-age restriction; omitted means unrestricted by date.
- SQLite for persistent operational state where necessary.

Safety-critical restrictions must be enforced by code rather than relying solely on agent instructions. Prefer deterministic behavior and explicit errors over silent fallbacks. Live Telegram access requires explicit user authorization during development.

## Initial MVP Exclusions

- Discovering new public Telegram communities or joining groups.
- Autonomous research loops or LLM-based message interpretation.
- Vector databases or bulk history ingestion.
- Web search or report generation.

These exclusions apply to the initial MVP and do not rule out future versions.

## Open Questions

- What deployment transport and service lifecycle should be used beyond local stdio?
- What are the precise tool parameters, pagination rules, ordering guarantees, response/error schemas, and source-reference formats?
- How should `read_around` define neighboring messages, handle missing anchors, and report unavailable or filtered context?
- Which reply/thread relationships can be retrieved, and how should discovery limitations be represented?
- What operational rate-limit values should replace the explicitly labeled placeholders?
- Which operational state belongs in SQLite, and how should secrets, sessions, audit retention, and operation-purpose metadata be managed?

## Current Status

The Python skeleton, standalone manual login, shared session client, persistent executor, and mocked tests are implemented. The MCP entry point exposes `list_dialogs`, `get_message`, `search_chat`, and `get_replies`. Its lifespan connects using the existing authorized session and disconnects on shutdown; it never initiates login.

All MCP tools use the same session client and RPC executor. `SessionClient` is an async context manager that reuses the existing session, rejects missing/unauthorized sessions, and never prompts for credentials. It reads API credentials from the local `.env`. `execute(operation, request)` accepts one Telethon request, not callbacks, iterators, batches, or pagination loops. Future Telegram integrations must use this owner and executor rather than construct separate clients. Authentication RPCs and additional exported senders are blocked in this client.

### Ownership and Compatibility

Telethon is pinned to the tested version `1.45.0` because interception uses its private sender interface. A POSIX `flock` on `data/telegram.session.lock` is shared by the session client, standalone login, and recovery command. The file must not be deleted while a process owns it. The kernel releases ownership on process exit, including termination; clean context-manager shutdown also disconnects. This is a cooperative lock for macOS/Linux on a local filesystem, not protection against external software that ignores the lock. Windows is unsupported in this step. Login otherwise retains its existing behavior.

### Queue, Limits, and Audit

`rate_limits.yaml` contains placeholder limits, explicitly not Telegram-safe recommendations. Global and per-operation minute/hour/day sliding windows count dispatch attempts, including failures. Unknown operation labels use the configured default limits. Dispatches at exactly the window boundary expire from that window.

A bounded FIFO queue permits one application RPC in flight. Configurable cooldown tiers count recent global activity; each dispatch can extend the persisted cooldown deadline. FloodWait variants persist the full reported duration globally and never retry the failed request. No automatic application retry, reconnection, or pagination is enabled. FloodWait deadlines are persisted before audit completion so an audit-completion failure cannot erase an already persisted cooldown.

Admission estimates combine the current limiter delay with one configured RPC timeout per preceding request. Estimates are conservative for RPC execution but cannot predict a future FloodWait or newly triggered cooldown. They are recalculated before dispatch. If estimated total queue/limiter wait exceeds `max_wait_seconds`, the result is immediately `rate_limited`, with `estimated_wait_seconds`, `retry_after` (seconds), and UTC `retry_at`. Zero `max_wait_seconds` permits no queue/limiter wait but allows ordinary scheduling overhead for an otherwise unrestricted request. The final dispatch reservation rechecks limits/HALTED at one finite timestamp under a SQLite write transaction. RPC execution has a separate configurable timeout. Future timing estimators can use the stored wait and completion history.

`data/operations.sqlite3` stores dispatch timestamps, operation labels, request class names, queue-entry times, actual wait durations, completion times, outcomes, and exception class names. RPC records store no request arguments, message bodies, personal names, or exception text. The separate MCP tool audit stores only allowlisted numeric target IDs, message IDs, page size, folder ID, normalized `since`, and whether a cursor was supplied. Search auditing additionally records the effective limit and query length, never the raw query. Raw cursors, arbitrary inputs, titles, and message content are never audited. Operation labels must be static identifiers, not queries or user content. Records are retained without automatic pruning in this step. Failed dispatches still consume limits.

Unexpected errors and uncertain timeouts halt subsequent application RPCs. A committed `in_flight` RPC or tool record left after a crash causes HALTED on restart. A separate `.halted` marker protects against database-write failures where the filesystem remains writable. If persistence storage itself is unavailable, no durable write can be guaranteed; initialization fails closed rather than recreating state. Clock rollback and nonfinite clock values also halt operations. Cancelling an in-flight RPC halts operations at the executor boundary; queued cancellations consume no dispatch budget. An unexpectedly terminated worker settles remaining queued callers with HALTED.

Inspection and recovery are manual-only offline commands. Stop the MCP server first so the CLI can acquire the same session lock. Without `--confirm`, the command only reports state (and detects unfinished operations on restart):

```bash
venv/bin/python scripts/recover.py
# Only after reviewing the state and audit:
venv/bin/python scripts/recover.py --confirm
```

It acquires session ownership, clears HALTED, marks unfinished outcomes uncertain, and preserves counters, cooldowns, and FloodWait deadlines. It never replays requests or contacts Telegram.

### RPC Accounting Boundaries

The hook is installed on the primary sender's `send` method before connection. It covers normal client requests, entity-resolution requests using that sender, and internal startup requests such as `InvokeWithLayer(InitConnection(GetConfig))`. Wrapper requests count as one dispatch; the audit records the outer request class. Tests exercise real Telethon `_call` and `connect` code with a mocked transport to verify both direct and internal interception.

Accounting measures application requests admitted to Telethon's sender, not confirmed wire transmissions. Primary-sender keepalive pings use `sender.send` in pinned Telethon 1.45.0, so they are intercepted and consume the internal/global budgets. Connection setup, DNS, socket/TLS/proxy activity, MTProto key negotiation, acknowledgements, incoming updates, container packing, and protocol corrections/retransmissions below `sender.send` are not intercepted or separately counted. In particular, Telethon may resend messages after bad-server-salt or bad-message notifications internally. These protocol actions are outside the application concurrency/limiter and HALTED guarantees; application retries remain disabled. HALTED blocks new application RPCs, but transport housekeeping may continue until the client is closed. Additional data-center/exported senders are prohibited rather than silently excluded. The manually run login script is outside operational RPC accounting and is protected by the shared ownership lock.

Session reuse, `list_dialogs`, and `get_message` have passed a minimal live check. `search_chat` passed one authorized live search for `селедка` on 2026-10-22, returning one page of 20 messages with linked MCP/RPC audits; no continuation was fetched. Reply metadata and `get_replies` passed one authorized live scenario on 2026-10-22: first metadata-bearing result 378872, one root plus four native replies, all operations audited; completeness remains explicitly unguaranteed. Tests use a mocked lifespan with network guards. `read_around` and deployment integration remain unimplemented.

### MCP Invocation Audit and Error Classification

FastMCP middleware starts a durable `tool_audit` row before dispatching a tool, including invalid-parameter and unknown-tool attempts. Each call receives a UUID `operation_id`, returned to the caller and copied into every underlying `rpc_audit` row. The queue captures that ID per item to prevent cross-call leakage. Startup/internal RPCs outside a tool invocation have no tool operation ID.

Tool records contain start/completion timestamps (UTC epoch seconds), operation name, sanitized parameters, actual queue/limiter wait, execution time excluding that wait, RPC count, status, and sanitized error codes. Responses and message content are not stored. If audit admission fails, no RPC is sent. If completion auditing fails, the successful payload is withheld and HALTED is persisted/latching attempted. If both SQLite and the marker filesystem fail, an in-memory latch still prevents further work, but durable state cannot be guaranteed on broken storage.

Expected input failures return `invalid_request` without RPCs. Known access errors (`ChannelPrivate`, `ChannelInvalid`, `ChatAdminRequired`, `ChatIdInvalid`, `PeerIdInvalid`, and `UserBannedInChannel`) return `inaccessible`; `MessageIdInvalid` returns `missing`. FloodWait variants return `rate_limited` and preserve the full deadline. These expected outcomes do not halt the server. Unknown RPC errors, session revocation, network/timeouts with uncertain outcomes, malformed responses, and audit failures are critical and halt subsequent operations. Recovery is never an MCP tool and never replays operations.

### Implemented Tool Contracts

`list_dialogs(page_size=20, cursor=None, folder_id=0)` issues one `messages.GetDialogs` request with a page size between 1 and 50. Folder 0 is the main dialog list; folder 1 is archived dialogs and must be requested explicitly. It returns marked Telegram chat IDs, titles, and `private`, `group`, `supergroup`, `channel`, or `unknown` types. Private display names serve as titles; phone numbers, usernames, access hashes, unread counts, and message previews are omitted.

Pagination metadata includes `page_size`, `returned`, `folder_id`, `has_more`, `next_cursor`, and `limitation`. Cursors contain only offset IDs, a timestamp, and folder ID; peer access hashes stay in the local Telethon session cache. First pages include pinned dialogs; continuation excludes pinned dialogs following Telethon's pagination convention. If a full page has no reliable chronological continuation (for example, only pinned entries), `has_more` remains true and a limitation is returned with no cursor. The tool does not silently fetch another page. A full slice may require a caller-requested empty page to establish completion. Dialog ordering can change while paging; this is not a snapshot guarantee.

`get_message(chat_id, message_id, since=None)` resolves the marked peer ID only from the existing session cache. An unknown peer returns `inaccessible` with `peer_not_cached`; listing the relevant dialogs page can populate that cache. It requests exactly one message ID using `channels.GetMessages` for channels/supergroups or `messages.GetMessages` otherwise. It verifies the returned message belongs to the requested chat before exposing it.

Successful output contains message/chat IDs, Telegram's message timestamp, text, an optional reply reference, and a `https://t.me/c/...` link for channels/supergroups. Such links require access to the chat; private dialogs and basic groups return no link. No surrounding messages, replies, or media downloads are fetched. Missing/empty results return `missing`, explicit access failures return `inaccessible`, an excluded old message returns `filtered` without text, and critical failures return `halted`. Telegram can conceal whether an empty result means nonexistent, deleted, or hidden; `missing` explicitly reports that limitation.

`search_chat(chat_id, query, limit=None, since=None, cursor=None)` issues exactly one native `messages.Search` RPC per page. `tool_settings.yaml` is loaded when the MCP server is initialized; its search default and maximum are both 20. Explicit limits must be between 1 and the configured maximum, which cannot exceed 20. Peer resolution uses the existing local cache, just like `get_message`.

Search returns IDs, chat IDs, UTC timestamps, full text, and available channel/supergroup source links. It returns `empty` when the returned page has no messages after filtering, `inaccessible` for unknown cached peers or known access errors, and explicit limiter/critical-error states otherwise. No additional search or page is fetched automatically. A full raw page produces a search-bound offset cursor and `has_more: true` (meaning more results may exist); a short page ends pagination. Cursors are bound to chat ID, query, and normalized `since` and cannot be reused for a different search. They contain an offset and a scope digest, not peer access hashes or raw queries.

Telegram's native `min_date` is exclusive. Search widens it to the preceding second, then checks the original inclusive boundary locally on the returned message timestamps. Dates outside the native timestamp range use local filtering only. No extra age-check requests are issued. A full page may contain locally excluded messages; pagination reports the filtered count and can still offer a cursor without silently expanding context.

Message outputs from `get_message`, `search_chat`, and `get_replies` include `reply_to_msg_id`, `thread_id` (the returned `reply_to_top_id`, otherwise null), `replies_count` (null if Telegram omitted it), and `thread_status`. A parent/top reference indicates `member`; explicit root metadata or a positive reply counter indicates `root`; an explicit zero counter with no parent indicates `none`; absent information indicates `unknown`. These are snapshot metadata classifications, not proof of complete reply discovery. No RPC is added just for these fields.

`get_replies(chat_id, message_id, limit=None, cursor=None)` accepts a root or a member. `tool_settings.yaml` sets default 20 and maximum 50 replies per page, with the root returned separately and included once in `messages`. The tool follows `reply_to_top_id` when available, otherwise direct parent references, using at most three targeted single-message lookups. It never scans history. A root lacking explicit metadata remains a candidate; deeper unresolved chains are explicitly `incomplete`. Continuation cursors bind chat, selected message, root, and offset; continuation fetches the root and one page without rewalking the member chain.

The native `messages.GetReplies` API is used for supergroup/discussion peers. Private/basic chats, cross-chat reply references, and broadcast-channel comment mappings are explicitly `unsupported` in this MVP; no other chat is followed automatically. Missing or inaccessible roots and native API failures retain explicit statuses. `MSG_ID_INVALID` and `TOPIC_ID_INVALID` are expected unavailable-thread outcomes rather than evidence of no replies.

Each reply page is displayed as root first, then replies in ascending message-ID conversation order. Native pages start with the newest available replies; explicit continuation goes toward older replies and may leave gaps between the root and the current page. A page is at most the configured number of replies plus one root. Completeness is always reported as `complete: false`: native counts or a short/empty page do not establish the absence of deleted, hidden, inaccessible, or otherwise undiscoverable replies. Multi-RPC root resolution and the native page share one operation ID and one limiter; an intermediate `rate_limited` result is returned without automatic retry.

### Freshness Policy

`since` is an optional timezone-aware ISO 8601 timestamp, normalized to UTC. The boundary is inclusive: only messages strictly older than `since` are excluded. Omission applies no date restriction, regardless of message age. Filtering uses the original message timestamp returned by Telegram, not edit time. It never issues an age-check RPC. Future `search_chat` and `read_around` must use the same preference, using native Telegram date filtering where supported and otherwise filtering timestamps already returned. There is no hardcoded global maximum age.

### Manual Verification

Offline tests:

```bash
venv/bin/python -m unittest discover -s tests -v
```

Before live verification, inspect HALTED with `venv/bin/python scripts/recover.py`; manually reset with `--confirm` only when ready. Existing counters and FloodWait cooldowns remain in force. The session and API credentials must already exist; do not run login merely to start MCP.

Configure a stdio MCP client with the project's absolute `venv/bin/python` path as its command and `-m telegram_mcp.server` as its arguments. Launching this server starts a real Telegram connection. Call:

```json
{"name": "list_dialogs", "arguments": {"page_size": 5}}
```

Verify no more than five items are returned. If `next_cursor` is supplied, pass it explicitly with the same folder ID to request the next page. Use `folder_id: 1` separately for archived chats. Then use a returned chat ID and a message ID already known from the Telegram UI:

```json
{"name": "get_message", "arguments": {"chat_id": -1000000000042, "message_id": 7}}
```

The numbers above are placeholders. Repeat with `since` exactly equal to the returned timestamp (expect `ok`) and later than that timestamp (expect `filtered`). Omit `since` again to verify old messages remain accessible. Placeholder rate limits may return `rate_limited`; honor `retry_at` rather than repeatedly retrying.

Inspect audit correlation locally without selecting response content:

```bash
sqlite3 data/operations.sqlite3 'SELECT operation_id,operation,status,rpc_count,wait_seconds,execution_seconds FROM tool_audit ORDER BY started_at DESC LIMIT 10;'
sqlite3 data/operations.sqlite3 'SELECT operation_id,operation,rpc,outcome,error FROM rpc_audit ORDER BY id DESC LIMIT 10;'
```

The matching operation IDs should correlate each tool invocation to its RPCs. No message text should be present in these tables.


### Rate-Limiter Verification Status

See `RATE_LIMIT_TEST_REPORT.md` for the mock-only regression and stress results. The admitted application-RPC boundary is covered by sliding-window, cooldown, concurrency, cancellation, persistence, and audit-failure tests. This is not a wire-transmission guarantee: private protocol resends bypass interception. Forward UTC clock jumps can also shorten deadlines measured against wall time. These remain unresolved design questions; the project is **NOT READY for limited live testing under a strict no-dispatch-violation invariant**. A successful mock suite does not establish Telegram-safe quotas or real-time behavior. Existing HALTED state must be reviewed manually and is not cleared by tests.
