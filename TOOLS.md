# Telegram tools for Hermes agents

This guide describes the implemented MCP interfaces. Tools read an existing
authorized user session; they do not log in, join chats, send messages, or plan
research. Human setup and recovery are in [README.md](README.md).

## Shared conventions

- Use marked numeric Telegram `chat_id` values returned by `list_dialogs`, not
  usernames or links. Message IDs are positive integers below `2**31`.
- `limit=None` or `page_size=None` uses the tool's `default_limit` from
  `tool_settings.yaml`. Explicit sizes must be integers from 1 through its
  `max_limit`; excessive values return `invalid_request`, not silent truncation.
- Optional `since` is a timezone-aware ISO timestamp, for example
  `2026-09-01T00:00:00Z`. Its lower boundary is inclusive. Omitted means no age
  restriction. `get_replies` has no `since` parameter.
- Responses contain `status` and an audit `operation_id` (possibly null if audit
  admission failed). Error responses may include `error`, `reason`, `hint`, or
  `limitation`; successful payload fields are not guaranteed on errors.
- All pagination is explicit. Treat cursors as opaque; never manufacture them.
  `has_more` estimates whether continuation may help, not completeness or a
  snapshot guarantee. An empty filtered page can still offer continuation.
- Except for dialog listing, peer resolution uses only the local session cache.
  `inaccessible` with `peer_not_cached` means no lookup RPC was attempted; listing
  the relevant dialogs page can populate the cache.

### Shared message object and reply metadata

Message objects contain `chat_id`, `message_id`, UTC ISO `timestamp`, `text`,
`source_link`, `reply_reference`, `reply_to_msg_id`, `thread_id`, `replies_count`,
and `thread_status`. Text may be empty; media is not downloaded. Channel/supergroup
source links use `https://t.me/c/...` and require access; other peer types return
null. A reply reference is `{chat_id, message_id}` or null.

| Metadata | Meaning |
| --- | --- |
| `reply_to_msg_id` | Direct parent ID from the returned reply header, or null. |
| `thread_id` | Telegram's `reply_to_top_id`, or null; not always a resolved root. |
| `replies_count` | Telegram's reported reply count, or null if omitted. |
| `thread_status` | `root`: top ID equals self, or positive reply count without parent/top references; `member`: other parent/top reference; `none`: explicit zero count without parent/top references; `unknown`: absent information. A root resolved by `get_replies` may be marked `root`. |

These fields use existing response data and add no RPCs. Missing metadata is not
proof of no replies; even `none` describes only the returned snapshot.

## list_dialogs(page_size=None, cursor=None, folder_id=0)

Read one native dialogs page, at most one tool RPC. `page_size` is an optional
integer; `cursor` is an optional string. `folder_id=0` selects the main list;
`1` explicitly selects archived dialogs.

Output: `status: ok`, `dialogs: [{chat_id, title, type}]`, and `pagination` with
`page_size`, `returned`, `folder_id`, `has_more`, `next_cursor`, and `limitation`.
Types are `private`, `group`, `supergroup`, `channel`, or `unknown`. Private display
names can appear as titles; phone numbers, usernames, and previews are omitted.
An empty dialogs page still has `status: ok`.

Pass `next_cursor` with the same folder for another page. First pages include
pinned dialogs; continuation excludes pinned dialogs. A full page without a
usable chronological offset can report `has_more: true` but no cursor; read its
`limitation`. An uncached cursor peer returns `invalid_cursor`. Dialog ordering
can change between calls; this tool does not discover unjoined communities.

## search_chat(chat_id, query, limit=None, since=None, cursor=None)

`chat_id` is an integer; `query` is a nonblank string. `limit` is an optional
integer, `since` an optional timestamp string, and `cursor` an optional string.
One native Telegram search RPC reads one page, newest IDs first.

Output: `status: ok` or `empty`, shared message objects in `messages`, and
`pagination: {limit, returned, filtered, has_more, next_cursor}`. `filtered` counts
returned messages excluded by `since`. Native date filtering is used where
representable, followed by an exact inclusive timestamp check locally.

Pass `next_cursor` with the same chat, query, and normalized `since`. Continuation
moves toward older matches. No extra page is fetched to fill filtered results or
prove completion. Search follows Telegram's native matching, not semantic analysis.

## get_message(chat_id, message_id, since=None)

Both IDs are integers; `since` is an optional timestamp string. At most one RPC
reads exactly the requested message, without neighbors or replies.

Output: `status: ok` and `message` containing the shared message object. A message
older than `since` returns `filtered`, `reason: before_since`, `chat_id`,
`message_id`, and `timestamp`, without text. The age check uses the returned
timestamp and adds no RPC. There is no pagination. `missing` cannot distinguish
deleted, nonexistent, or hidden messages; explicit access failures are
`inaccessible`.

## get_replies(chat_id, message_id, limit=None, cursor=None)

Both IDs are integers; the selected message can be a root or member. `limit` is
an optional integer counting **replies**, with the root additional; `cursor` is
an optional string. No date-filter parameter is supported.

Successful output contains `root_id`, `root`, `messages` (root once, then replies
in ascending ID order), `completeness`, and `pagination` with `limit`,
`returned_replies`, `has_more`, `next_cursor`, `direction: older`, and
`order: root_then_page_ascending`. `completeness` contains `complete: false`,
`status: incomplete`, `reason`, and, when reached, `root_resolution` and
`reported_replies_count`. No replies can still yield `status: ok` with the root.

Resolution follows a top ID or direct parent chain using at most three targeted
message reads, then one native reply-page RPC: up to four tool RPCs. An unresolved
root returns `incomplete`; an unannotated message may remain a candidate root.
Native supergroup/discussion threads are supported; private/basic chats and
cross-chat/broadcast comment mappings are `unsupported`.

Pass `next_cursor` with the same chat and **originally selected message ID**.
Continuation rereads the root and requests one older reply page (normally two
RPCs). The initial page contains the newest available replies, so root-first
display may have gaps. Empty/short pages and reported counts never prove complete
thread reconstruction. Errors can retain a fetched root or partial messages;
inspect status before using them. An intermediate rejection is not auto-retried.

## read_around(chat_id, message_id, direction="older", limit=None, since=None)

Both IDs are integers. `direction` is `older` or `newer`; `limit` is an optional
integer and `since` an optional timestamp string. At most one native history RPC
uses the message ID as an **exclusive boundary**, without fetching the anchor.
The boundary need not identify an existing message. Only one direction is read.

Output: `status: ok` or `empty`, shared message objects in `messages`, and
`pagination: {direction, order, limit, returned, filtered, has_more,
next_message_id, limitation}`. `order` is `chronological`. `since` is checked
locally on returned timestamps; excluded items are not replaced with extra reads.

Continue by replacing `message_id` with `pagination.next_message_id`, retaining
the direction and date preference. For **older** pages this is the oldest ID,
not the last displayed ID; for **newer** pages it is the newest ID. If filtering
removes every newer result, continuation can still reference an undisplayed ID.
An older page reaching the `since` boundary ends continuation. Near the history
end, the native response may include boundary/wrong-direction entries; these are
excluded locally. Short pages and `has_more` remain estimates, not completeness
guarantees. History neighbors do not establish conversational membership.

## Research with bounded context

1. Locate the target chat with one small `list_dialogs` page; reuse known cached
   chat IDs rather than listing on every question.
2. Use a specific `search_chat` query and `since` when relevant. Keep returned
   message objects so you do not refetch their text merely to inspect metadata.
3. Use `get_message` for a specific missing message or direct parent. Use
   `get_replies` when native thread relationships are needed, remembering its
   larger RPC cost and incomplete coverage.
4. Use a small `read_around` page in the needed direction for temporal context.
   Choose additional pages only when they answer a concrete remaining question.
5. Track `(chat_id, message_id)` to deduplicate; retain source links and timestamps.
   Stop when evidence is sufficient. Do not scan every page or assume nearby
   messages belong to one conversation.

## Limits, errors, and retries

All agents/tools share one RPC queue with concurrency 1. Global and per-operation
sliding minute/hour/day counters, progressive burst cooldowns, and FloodWait
deadlines persist in SQLite. Operations without a named rate-limit entry use the
configured default. Startup, session checks, and internal keepalives consume
quota separately from tool-linked RPCs. Result page sizes do not multiply native
RPC counts; multi-step root resolution does.

`auto_waiting_max` (currently 60 seconds) is the sole automatic queue/limiter-wait
budget per RPC. Waits within it execute internally; longer estimated waits return
`rate_limited` with `estimated_wait_seconds`, `retry_after` (seconds), and UTC
`retry_at`. Wait at least that long before deciding whether to retry; estimates
can change with other agents' activity. FloodWait preserves Telegram's full
cooldown. No automatic application retries, reconnection, or pagination occur.
Avoid parallel bursts and repeated requests while blocked.

`invalid_request` requires corrected inputs; `inaccessible` is not an empty
success. `unavailable` means the owner is not ready. `halted` requires a human:
stop requesting operations and report the operation ID/error. Never run recovery,
change limits, delete state, or launch login to bypass a block. Unexpected errors,
uncertain timeouts, and audit failures fail closed; recovery is not an MCP tool.

MCP/RPC records share `operation_id`; raw queries, message text, credentials, and
session secrets are excluded from those audits. Accounting covers admitted
application dispatches, not all network traffic or transport-level retransmissions.
See [SPEC.md](SPEC.md) for detailed safety boundaries and implementation caveats.
