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

## Source Policy

- For Telegram research, use Telegram MCP exclusively by default.
- Do not use web search or external sources unless the user explicitly requests them.
- If Telegram evidence is insufficient, say so. Never search the web just to fill gaps.
- When external verification is explicitly requested, web research is allowed;
  clearly distinguish its findings from Telegram evidence.

## Response Style

For open-ended questions, default to a short synthesis rather than a research report: give the conclusion, 2–3 strongest supporting findings with source links, and only essential caveats.

Do not recount every retrieved message, repeat the conclusion, or include separate sections for minor evidence and coverage limitations. Additional evidence should inform your judgment, not automatically appear in the answer.

Expand when the user explicitly requests details, or when the requested format genuinely requires them. Preserve requested lists and item counts without arbitrary shortening. For high-stakes questions, retain any qualifications essential to a safe, accurate answer.

## Procedure

1. Identify the decision the user needs, location, freshness requirements, and
   useful evidence. Resolve material ambiguity briefly; do not ask the user to
   prescribe a search plan. Use the time-budget guidance below.
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
   If a truncated word returns empty, try the complete product name before
   concluding there are no matches; native search need not match arbitrary
   prefixes. For Russian terms, consider е/ё spellings and a distinct product
   synonym when the first query misses useful evidence. Compare returned message
   IDs after a spelling variant: if е/ё queries return the same page, treat that
   variant as redundant and spend the next call on a distinct synonym or useful
   pagination instead. Search a distinct product synonym before descending far
   into old matches, because newer relevant discussions may use a different noun.
   Replace a query dominated by unrelated results with a more specific phrase;
   do not paginate through noise.
4. Evaluate returned text, timestamps, source links, IDs, and reply metadata
   before spending more RPCs. Distinguish a seller's claim, a firsthand report,
   hearsay, and a later correction. An old recommendation does not prove current
   availability. Keep `(chat_id, message_id)` and fetched text to avoid rereads.
   Track the fields needed for the user's decision and choose the next read to
   fill a concrete gap rather than merely collect more mentions. Skip banter
   and irrelevant matches.
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
   Prefer the direct parent when it can resolve an ambiguous claim; expand the
   thread only when needed. For a reference such as “this place” without reply
   metadata, try one small older history page. If it does not resolve the
   reference, preserve the uncertainty rather than scan unrelated history.
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
  resolution does. Startup and background-update RPCs remain rate-limited; PingRequest uses a separate keepalive budget.

## Time Budget and Feedback

Aim for a useful synthesis within roughly five minutes rather than exhaustive
coverage. This is a planning guideline, not permission to exceed any hard limit
in the mandatory Telegram Rules; those rules always take precedence.
Account for processing, RPCs, cooldowns, and time to answer without repeated
clock-check calls. Stop earlier when evidence is sufficient or the next read has
low expected value.

On `rate_limited`, honor both `retry_at` and `retry_after`, using the later
deadline. Retry only if a useful follow-up and final response fit within the
applicable limits; do not poll or switch operations to evade a cooldown.

If a meaningful delay is expected, send at most one brief, approximate update
through the normal response channel. Otherwise, return findings without process
narration.

## Verification

Before answering, verify that each material claim has relevant evidence, the
correct context, and an original source/date when available. Remove duplicates
and unsupported conclusions; distinguish firsthand reports, recommendations,
and inference. Do not present historical evidence as current confirmation.

Preserve requested lists, counts, and decision-relevant qualifiers. If fewer
items meet the criteria, state the shortfall rather than pad the list or invent
missing fields. Keep weaker leads separate from supported findings.

State material coverage limits and unresolved uncertainty. On a technical stop,
include the sanitized error and `operation_id` when available. Do not spend more
RPCs solely to make the report look complete.
