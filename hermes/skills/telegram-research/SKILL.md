---
name: telegram-research
description: Research Telegram discussions with bounded smart search.
metadata:
  hermes:
    category: research
    tags: [telegram, research, search]
---

# Telegram Research — Smart Search

## When to Use

Use for questions requiring evidence from accessible Telegram chats, including
local recommendations, products, sellers, and discussion context. Use only the
five Telegram MCP tools below. The always-loaded [Telegram rules](../../TELEGRAM_RULES.md)
apply even without this skill. Infrastructure diagnosis and repair are out of scope.

## Procedure

1. Identify the decision the user needs, location, freshness requirements, and
   useful evidence. Resolve material ambiguity briefly; do not ask the user to
   prescribe a search plan. Set the deadline to request receipt plus 300 seconds.
2. Reuse known marked numeric `chat_id` values. Otherwise use one small
   `list_dialogs(page_size=5)` page, then another only if needed to locate a
   relevant chat. Titles help choose chats; they are not research findings.
   Archived dialogs require explicit `folder_id=1`. This cannot discover or join
   new communities. For `peer_not_cached`, list the relevant dialogs page once
   if justified by the question and budget; do not attempt a direct lookup.
3. Search relevant chats with a specific `search_chat(chat_id, query, limit=10)`.
   Use a timezone-aware inclusive `since` only when the question warrants an age
   boundary; omission means no age restriction. Telegram matching is native,
   not semantic: try a useful spelling or synonym only when it adds coverage.
4. Evaluate returned text, timestamps, source links, IDs, and reply metadata
   before spending more RPCs. Distinguish a seller's claim, a firsthand report,
   hearsay, and a later correction. An old recommendation does not prove current
   availability. Keep `(chat_id, message_id)` and fetched text to avoid rereads.
5. Choose the smallest next read that resolves a concrete uncertainty:

   | Need | Tool and important contract |
   | --- | --- |
   | Missing specific message or direct parent | `get_message(chat_id, message_id, since=None)`: exactly one message, no neighbors or replies; at most one RPC. Reuse existing text instead of refetching it. |
   | Native discussion from a relevant root, middle, or nested reply | `get_replies(chat_id, message_id, limit=5)`: accepts any selected member, resolves parent/top references with at most three targeted reads, then one reply-page RPC. Root is additional to the reply limit. |
   | Temporal context before/after a message | `read_around(chat_id, message_id, direction="older"|"newer", limit=8, since=None)`: one exclusive-boundary history page; does not fetch the anchor. Choose one direction for a reason. |
   | More matching evidence | Another search page using the returned opaque `next_cursor`, with the same chat, query, and normalized `since`. Continue toward older matches only if useful. |

   Use `reply_reference`, `reply_to_msg_id`, `thread_id`, `replies_count`, and
   `thread_status` to choose a path; null/unknown metadata does not establish no
   replies. A nearby history message is not necessarily part of the discussion.
   Do not scan unrelated history to reconstruct a conversation.
6. Read every result's top-level status before using its payload. Reply results
   may retain a root/partial data when a later step is limited. Stop Telegram
   operations immediately on HALTED or unexpected technical failure; never
   investigate infrastructure. Treat expected `missing`, `filtered`, `empty`,
   `inaccessible`, `unsupported`, and `incomplete` as explicit coverage limits,
   not proof of absence. Correct an invalid argument/cursor only when the
   correction is clear; do not loop on errors.
7. Stop when evidence supports an answer, remaining calls have low expected
   value, a technical error occurs, or the time budget is insufficient. Explain
   findings, dates, uncertainty, and any unverified availability. Cite original
   `source_link` values beside supported claims. If no link is returned, identify
   the chat/message/date minimally; never invent a public link. Separate evidence
   from inference and avoid quoting unrelated private content.

## Pagination and Limits

- Respect `tool_settings.yaml` defaults and maxima. Omitted sizes use defaults;
  explicit excessive sizes are rejected, not truncated. The small examples above
  fit current settings; never increase configured limits. See
  [TOOLS.md](../../../TOOLS.md) for detailed contracts if needed.
- Dialog continuation keeps `folder_id`. Search continuation keeps chat, query,
  and normalized `since`. Never invent or edit opaque cursors.
- Reply continuation keeps the **originally selected message ID**, not a newly
  inferred root. It rereads the root and fetches one older page, normally two
  RPCs. Replies appear root first, then the page in ascending order; the first
  page contains the newest replies and may leave gaps. No `since` is supported.
- History continuation uses `pagination.next_message_id`, retaining direction
  and date preference: oldest ID for older pages, newest ID for newer pages.
  Display order is chronological in both directions.
- `has_more`, empty/short pages, and reply counts never prove complete coverage.
  `get_replies` always reports incomplete reconstruction; private/basic chats and
  cross-chat/broadcast comment mappings are unsupported. Do not chase other
  chats automatically.
- The shared RPC queue has concurrency one. Avoid bursts and batches of
  speculative reads. Page sizes do not multiply RPC counts, but reply-root
  resolution does. Startup and keepalives share the budget too.

## Time Budget and Feedback

The **5-minute total budget includes LLM processing and rate-limit delays**.
Track elapsed time using available timing metadata; do not add repeated model or
tool cycles merely to check the clock. Before another call or wait, account for
remaining time, expected RPCs, possible limiter delays, and time to draft the
answer. If timing is uncertain, stop conservatively with partial findings.

On `rate_limited`, wait until the returned retry deadline only if a useful next
step and the final answer still fit. Never sleep or retry beyond the deadline,
poll repeatedly, or switch operations to bypass limits. A retry is a new decision,
not an automatic loop; stop if repeated waits consume the useful budget.

If a meaningful delay is expected, send **at most one** brief progress message,
through Hermes' normal response mechanism, explaining the reason and approximate
time remaining. Example: "Telegram rate limits are slowing the search;
approximately 1–2 minutes remain." Adapt the estimate to the actual remaining
budget; never promise an exact completion time. Normal completion needs no
progress message. Technical failure needs a concise final report, not debugging.

## Verification

Before answering, check that each recommendation has relevant evidence and an
original source when available; remove duplicates and unsupported conclusions.
State incomplete coverage, elapsed-budget limits, or the sanitized technical
error and `operation_id` when applicable. Never spend more Telegram calls solely
to make a report look complete.
