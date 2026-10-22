"""Two bounded Telegram reads; no implicit pagination or context expansion."""

import base64
from datetime import datetime, timezone
import json

from telethon import functions, types, utils


def parse_since(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid timestamp")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return date.astimezone(timezone.utc)


def decode_cursor(cursor, folder_id):
    try:
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if set(value) != {"chat_id", "message_id", "date", "folder_id"}:
            raise ValueError
        if value["folder_id"] != folder_id or not all(type(value[key]) is int for key in ("chat_id", "message_id", "folder_id")):
            raise ValueError
        if not -(2**63) < value["chat_id"] < 2**63 or not 0 < value["message_id"] < 2**31:
            raise ValueError
        value["date"] = parse_since(value["date"])
        if value["date"] is None:
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("Invalid pagination cursor") from None


async def list_dialogs(owner, page_size=None, cursor=None, folder_id=0, *, settings):
    page_size = settings["default_limit"] if page_size is None else page_size
    if type(page_size) is not int or not 1 <= page_size <= settings["max_limit"] or type(folder_id) is not int or folder_id not in (0, 1):
        return {"status": "invalid_request", "error": "invalid_parameters"}
    offset = decode_cursor(cursor, folder_id) if cursor is not None else None
    peer = types.InputPeerEmpty()
    if offset:
        peer = owner.cached_peer(offset["chat_id"])
        if peer is None:
            return {"status": "invalid_cursor", "reason": "cursor_peer_not_cached"}
    result = await owner.execute("list_dialogs", functions.messages.GetDialogsRequest(
        offset_date=offset["date"] if offset else None,
        offset_id=offset["message_id"] if offset else 0,
        offset_peer=peer, limit=page_size, hash=0,
        exclude_pinned=bool(offset), folder_id=folder_id,
    ))
    if result["status"] != "ok":
        return result
    data = result["data"]
    if not isinstance(data, (types.messages.Dialogs, types.messages.DialogsSlice)) or len(data.dialogs) > page_size:
        raise RuntimeError("Unexpected dialogs response")
    entities = {utils.get_peer_id(entity): entity for entity in [*data.users, *data.chats]}
    messages = {(utils.get_peer_id(message.peer_id), message.id): message
                for message in data.messages if getattr(message, "peer_id", None)}
    dialogs = []
    for dialog in data.dialogs:
        chat_id = utils.get_peer_id(dialog.peer)
        entity = entities.get(chat_id)
        if isinstance(entity, (types.Channel, types.ChannelForbidden)):
            kind = "supergroup" if entity.megagroup else "channel"
        elif isinstance(entity, (types.Chat, types.ChatForbidden)):
            kind = "group"
        elif isinstance(entity, (types.User, types.UserEmpty)):
            kind = "private"
        else:
            kind = "unknown"
        # Private display names are titles; phone numbers/usernames are never returned.
        title = getattr(entity, "title", None)
        if isinstance(entity, types.User):
            title = " ".join(filter(None, [entity.first_name, entity.last_name])) or None
        dialogs.append({"chat_id": chat_id, "title": title, "type": kind})
    more = isinstance(data, types.messages.DialogsSlice) and len(data.dialogs) == page_size
    next_cursor = None
    limitation = None
    if more:
        last = data.dialogs[-1]
        last_id = utils.get_peer_id(last.peer)
        message = messages.get((last_id, last.top_message))
        if getattr(last, "pinned", False) or not message or not getattr(message, "date", None):
            limitation = f"No reliable chronological offset in this page; increase page_size up to {settings['max_limit']} or restart pagination."
        else:
            value = {"chat_id": last_id, "message_id": last.top_message,
                     "date": message.date.isoformat(), "folder_id": folder_id}
            next_cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    return {"status": "ok", "dialogs": dialogs,
            "pagination": {"page_size": page_size, "returned": len(dialogs), "folder_id": folder_id,
                           "has_more": more, "next_cursor": next_cursor, "limitation": limitation}}


async def fetch_message(owner, chat_id, message_id, operation):
    peer = owner.cached_peer(chat_id)
    if peer is None:
        return {"status": "inaccessible", "reason": "peer_not_cached", "hint": "List the relevant dialogs page first."}
    if isinstance(peer, types.InputPeerChannel):
        request = functions.channels.GetMessagesRequest(
            channel=types.InputChannel(peer.channel_id, peer.access_hash),
            id=[types.InputMessageID(message_id)],
        )
    else:
        request = functions.messages.GetMessagesRequest(id=[types.InputMessageID(message_id)])
    result = await owner.execute(operation, request)
    if result["status"] != "ok":
        return result
    messages = result["data"].messages
    if len(messages) > 1:
        raise RuntimeError("Unexpected message response")
    message = next((item for item in messages if not isinstance(item, types.MessageEmpty)
                    and item.id == message_id and utils.get_peer_id(item.peer_id) == chat_id), None)
    if message is None:
        return {"status": "missing", "reason": "not_returned_by_telegram",
                "limitation": "Telegram may not distinguish deleted, nonexistent, and hidden messages."}
    return {"status": "ok", "data": message}


def reply_metadata(message):
    """Describe only relationships present in this Telegram response."""
    header = getattr(message, "reply_to", None)
    parent = getattr(header, "reply_to_msg_id", None)
    top = getattr(header, "reply_to_top_id", None)
    replies = getattr(message, "replies", None)
    count = getattr(replies, "replies", None)
    if top == message.id:
        status = "root"
    elif top or parent:
        status = "member"
    elif count is not None and count > 0:
        status = "root"
    elif count == 0:
        status = "none"
    else:
        status = "unknown"
    return {"reply_to_msg_id": parent, "thread_id": top,
            "replies_count": count, "thread_status": status}


def message_view(message, chat_id, peer):
    """Serialize a fetched message without fetching additional metadata."""
    if not isinstance(message.date, datetime) or message.date.tzinfo is None:
        raise RuntimeError("Message timestamp is missing or ambiguous")
    metadata = reply_metadata(message)
    header = getattr(message, "reply_to", None)
    reply_peer = getattr(header, "reply_to_peer_id", None)
    reference = None
    if metadata["reply_to_msg_id"]:
        reference = {"chat_id": utils.get_peer_id(reply_peer) if reply_peer else chat_id,
                     "message_id": metadata["reply_to_msg_id"]}
    source = f"https://t.me/c/{peer.channel_id}/{message.id}" if isinstance(peer, types.InputPeerChannel) else None
    return {"chat_id": chat_id, "message_id": message.id,
            "timestamp": message.date.astimezone(timezone.utc).isoformat(),
            "text": getattr(message, "message", "") or "", "reply_reference": reference,
            "source_link": source, **metadata}


async def get_message(owner, chat_id, message_id, since=None):
    boundary = parse_since(since)
    result = await fetch_message(owner, chat_id, message_id, "get_message")
    if result["status"] != "ok":
        return result
    message = result["data"]
    view = message_view(message, chat_id, owner.cached_peer(chat_id))
    if boundary is not None and message.date < boundary:
        return {"status": "filtered", "reason": "before_since", "chat_id": chat_id,
                "message_id": message_id, "timestamp": view["timestamp"]}
    return {"status": "ok", "message": view}


def search_options(chat_id, query, limit, since, cursor, settings):
    """Validate one search page and bind its cursor to the original search."""
    import hashlib

    if type(chat_id) is not int or not -(2**63) < chat_id < 2**63 or chat_id == 0:
        raise ValueError("Invalid chat ID")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query must not be empty")
    limit = settings["default_limit"] if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= settings["max_limit"]:
        raise ValueError("Invalid search limit")
    boundary = parse_since(since)
    scope = hashlib.sha256(json.dumps(
        [chat_id, query, boundary.isoformat() if boundary else None], ensure_ascii=False,
    ).encode()).hexdigest()
    offset = 0
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, dict) or set(value) != {"offset_id", "scope"}:
                raise ValueError
            offset = value["offset_id"]
            if type(offset) is not int or not 0 < offset < 2**31 or value["scope"] != scope:
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid search cursor") from None
    return limit, boundary, offset, scope


async def search_chat(owner, chat_id, query, limit, since, cursor, settings):
    """Issue one native search RPC, then filter its returned timestamps locally."""
    import math

    limit, boundary, offset, scope = search_options(chat_id, query, limit, since, cursor, settings)
    peer = owner.cached_peer(chat_id)
    if peer is None:
        return {"status": "inaccessible", "reason": "peer_not_cached",
                "hint": "List the relevant dialogs page first."}
    # Telegram min_date is exclusive and timestamps have second precision.
    native_min = None
    if boundary is not None and 1 <= boundary.timestamp() <= 2**31 - 1:
        native_min = datetime.fromtimestamp(math.floor(boundary.timestamp()) - 1, timezone.utc)
    result = await owner.execute("search_chat", functions.messages.SearchRequest(
        peer=peer, q=query, filter=types.InputMessagesFilterEmpty(),
        min_date=native_min, max_date=None, offset_id=offset, add_offset=0,
        limit=limit, max_id=0, min_id=0, hash=0,
    ))
    if result["status"] != "ok":
        return result
    data = result["data"]
    if not isinstance(data, (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)):
        raise RuntimeError("Unexpected search response")
    raw = data.messages
    if len(raw) > limit:
        raise RuntimeError("Search response exceeded requested page size")
    messages = []
    previous_id = offset or 2**31
    filtered = 0
    for message in raw:
        if isinstance(message, types.MessageEmpty):
            raise RuntimeError("Search returned an empty message placeholder")
        if (not 0 < message.id < previous_id or utils.get_peer_id(message.peer_id) != chat_id
                or not isinstance(message.date, datetime) or message.date.tzinfo is None):
            raise RuntimeError("Search response has inconsistent message metadata")
        previous_id = message.id
        timestamp = message.date.astimezone(timezone.utc)
        if boundary is not None and timestamp < boundary:
            filtered += 1
            continue
        messages.append(message_view(message, chat_id, peer))
    # A full page may have more results. Never probe by requesting an extra item/page.
    more = len(raw) == limit and bool(raw) and raw[-1].id > 1
    next_cursor = None
    if more:
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"offset_id": raw[-1].id, "scope": scope},
        ).encode()).decode()
    return {"status": "ok" if messages else "empty", "messages": messages,
            "pagination": {"limit": limit, "returned": len(messages), "filtered": filtered,
                           "has_more": more, "next_cursor": next_cursor}}


def history_options(chat_id, message_id, direction, limit, since, settings):
    """Validate one exclusive, directional history page."""
    if (type(chat_id) is not int or not -(2**63) < chat_id < 2**63 or chat_id == 0
            or type(message_id) is not int or not 0 < message_id < 2**31):
        raise ValueError("Invalid message reference")
    if direction not in ("older", "newer"):
        raise ValueError("Invalid direction")
    limit = settings["default_limit"] if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= settings["max_limit"]:
        raise ValueError("Invalid history limit")
    return limit, parse_since(since)


async def read_around(owner, chat_id, message_id, direction, limit, since, settings):
    """Read one native history page without resolving or fetching its anchor."""
    limit, boundary = history_options(chat_id, message_id, direction, limit, since, settings)
    peer = owner.cached_peer(chat_id)
    if peer is None:
        return {"status": "inaccessible", "reason": "peer_not_cached",
                "hint": "List the relevant dialogs page first."}
    # Match Telethon's reverse history offsets, without its pagination iterator.
    newer = direction == "newer"
    raw = []
    if not (newer and message_id == 2**31 - 1):
        result = await owner.execute("read_around", functions.messages.GetHistoryRequest(
            peer=peer, offset_id=message_id + int(newer), offset_date=None,
            add_offset=-limit if newer else 0, limit=limit, max_id=0, min_id=0, hash=0,
        ))
        if result["status"] != "ok":
            return result
        data = result["data"]
        if not isinstance(data, (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)):
            raise RuntimeError("Unexpected history response")
        raw = data.messages
    if len(raw) > limit:
        raise RuntimeError("History response exceeded requested page size")
    messages, eligible = [], []
    previous_id, filtered = 2**31, 0
    for message in raw:
        if (isinstance(message, types.MessageEmpty) or not 0 < message.id < previous_id
                or utils.get_peer_id(message.peer_id) != chat_id):
            raise RuntimeError("History response has inconsistent message metadata")
        previous_id = message.id
        view = message_view(message, chat_id, peer)
        # Near the history end Telegram can return the anchor or older entries.
        if not (message.id > message_id if newer else message.id < message_id):
            continue
        eligible.append(message.id)
        if boundary is not None and message.date < boundary:
            filtered += 1
            continue
        messages.append(view)
    messages.sort(key=lambda item: (item["timestamp"], item["message_id"]))
    next_id = (max(eligible) if newer else min(eligible)) if eligible else None
    more = len(raw) == limit and next_id is not None and (newer or next_id > 1)
    if not newer and boundary is not None and filtered:
        more = False  # Older continuation cannot satisfy the same since boundary.
    return {"status": "ok" if messages else "empty", "messages": messages,
            "pagination": {"direction": direction, "order": "chronological", "limit": limit,
                           "returned": len(messages), "filtered": filtered, "has_more": more,
                           "next_message_id": next_id if more else None,
                           "limitation": "One native page only; has_more is an estimate, not a complete-history guarantee. For older pages continue from the oldest ID, not the last displayed ID."}}


def replies_options(chat_id, message_id, limit, cursor, settings):
    """Validate a bounded reply page and its chat/anchor-specific cursor."""
    if (type(chat_id) is not int or not -(2**63) < chat_id < 2**63 or chat_id == 0
            or type(message_id) is not int or not 0 < message_id < 2**31):
        raise ValueError("Invalid message reference")
    limit = settings["default_limit"] if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= settings["max_limit"]:
        raise ValueError("Invalid reply limit")
    value = None
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, dict) or set(value) != {"chat_id", "anchor_id", "root_id", "offset_id"}:
                raise ValueError
            if value["chat_id"] != chat_id or value["anchor_id"] != message_id:
                raise ValueError
            if not all(type(value[key]) is int for key in value):
                raise ValueError
            if not all(0 < value[key] < 2**31 for key in ("anchor_id", "root_id", "offset_id")):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("Invalid replies cursor") from None
    return limit, value


async def get_replies(owner, chat_id, message_id, limit, cursor, settings):
    """Resolve a root with at most three targeted reads, then fetch one native page."""
    limit, position = replies_options(chat_id, message_id, limit, cursor, settings)
    peer = owner.cached_peer(chat_id)
    completeness = {"complete": False, "status": "incomplete",
                    "reason": "Only Telegram's accessible native thread page is represented; hidden/deleted replies and full reconstruction are not guaranteed."}
    if peer is None:
        return {"status": "inaccessible", "reason": "peer_not_cached", "root": None, "messages": [], "completeness": completeness}
    if not isinstance(peer, types.InputPeerChannel):
        return {"status": "unsupported", "reason": "native_threads_require_supergroup_or_discussion_peer",
                "root": None, "messages": [], "completeness": completeness}
    current_id = position["root_id"] if position else message_id
    root = None
    explicit_root = bool(position)
    seen = set()
    resolution = "cursor" if position else "candidate"
    for _ in range(3):
        if current_id in seen:
            break
        seen.add(current_id)
        fetched = await fetch_message(owner, chat_id, current_id, "get_replies")
        if fetched["status"] != "ok":
            return {**fetched, "root": None, "messages": [], "completeness": completeness}
        current = fetched["data"]
        header = getattr(current, "reply_to", None)
        other_peer = getattr(header, "reply_to_peer_id", None)
        if ((other_peer and utils.get_peer_id(other_peer) != chat_id)
                or getattr(getattr(current, "replies", None), "comments", False)):
            return {"status": "unsupported", "reason": "cross_chat_discussion_resolution_not_supported",
                    "root": None, "messages": [message_view(current, chat_id, peer)], "completeness": completeness}
        if explicit_root:
            root = current
            break
        metadata = reply_metadata(current)
        top = metadata["thread_id"]
        if top == current.id:
            root = current
            resolution = "reply_to_top_id"
            break
        if top:
            current_id, explicit_root, resolution = top, True, "reply_to_top_id"
        elif metadata["reply_to_msg_id"]:
            current_id, resolution = metadata["reply_to_msg_id"], "parent_chain"
        else:
            root = current
            if metadata["replies_count"] is not None:
                resolution = "replies_metadata"
            break
    if root is None:
        completeness["reason"] = "Thread root unresolved within three targeted message lookups; no history scan performed."
        return {"status": "incomplete", "root": None, "messages": [], "completeness": completeness}
    root_view = message_view(root, chat_id, peer)
    if explicit_root:
        root_view["thread_status"] = "root"
    offset = position["offset_id"] if position else 0
    response = await owner.execute("get_replies", functions.messages.GetRepliesRequest(
        peer=peer, msg_id=root.id, offset_id=offset, offset_date=None,
        add_offset=0, limit=limit, max_id=0, min_id=0, hash=0,
    ))
    completeness["root_resolution"] = resolution
    if response["status"] != "ok":
        status = "unsupported" if response["status"] == "missing" else response["status"]
        return {**response, "status": status, "root": root_view,
                "messages": [root_view], "completeness": completeness}
    data = response["data"]
    if not isinstance(data, (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)) or len(data.messages) > limit:
        raise RuntimeError("Unexpected replies response")
    replies = []
    identifiers = set()
    for message in data.messages:
        if isinstance(message, types.MessageEmpty):
            continue  # Keep the completeness warning; never infer absence from placeholders.
        if (utils.get_peer_id(message.peer_id) != chat_id or message.id in identifiers
                or (offset and message.id >= offset)):
            raise RuntimeError("Inconsistent native thread page")
        identifiers.add(message.id)
        if message.id != root.id:
            replies.append(message_view(message, chat_id, peer))
    replies.sort(key=lambda item: item["message_id"])
    more = len(data.messages) == limit and bool(replies)
    next_cursor = None
    if more:
        next_cursor = base64.urlsafe_b64encode(json.dumps({"chat_id": chat_id,
            "anchor_id": message_id, "root_id": root.id, "offset_id": replies[0]["message_id"]}).encode()).decode()
    completeness["reported_replies_count"] = getattr(data, "count", None)
    return {"status": "ok", "root_id": root.id, "root": root_view,
            "messages": [root_view, *replies], "completeness": completeness,
            "pagination": {"limit": limit, "returned_replies": len(replies), "has_more": more,
                           "next_cursor": next_cursor, "direction": "older", "order": "root_then_page_ascending"}}
