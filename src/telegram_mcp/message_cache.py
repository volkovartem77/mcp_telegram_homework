"""Passive message persistence and bounded, local-only FTS5 search."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from telethon import types, utils

from telegram_mcp.research_tools import parse_since, reply_metadata


def cache_options(query, chat_id=None, since=None, limit=10):
    if not isinstance(query, str) or not query.strip() or len(query) > 4096 or '\x00' in query:
        raise ValueError("Invalid cache query")
    if len(query.split()) > 64:
        raise ValueError("Too many cache search terms")
    if chat_id is not None and (type(chat_id) is not int or not -(2**63) < chat_id < 2**63 or chat_id == 0):
        raise ValueError("Invalid chat ID")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Cache limit must be between 1 and 50")
    # Literal AND terms: user text cannot inject FTS operators or SQL.
    match = ' AND '.join('"' + term.replace('"', '""') + '"' for term in query.split())
    return match, parse_since(since)


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class MessageCache:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    text TEXT NOT NULL, message_date REAL NOT NULL, edit_date REAL,
                    sender_id INTEGER, source_link TEXT, last_fetched_at REAL NOT NULL,
                    reply_to_msg_id INTEGER, reply_to_peer_id INTEGER, top_msg_id INTEGER,
                    thread_id INTEGER, root_msg_id INTEGER, replies_count INTEGER,
                    thread_status TEXT NOT NULL, reply_metadata_json TEXT NOT NULL,
                    original_json TEXT NOT NULL, PRIMARY KEY(chat_id,message_id));
                CREATE INDEX IF NOT EXISTS message_chat_date ON messages(chat_id,message_date);
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    text, content='messages', content_rowid='rowid', tokenize='unicode61');
                CREATE TRIGGER IF NOT EXISTS messages_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid,text) VALUES(new.rowid,new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_delete AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.rowid,old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_update AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.rowid,old.text);
                    INSERT INTO messages_fts(rowid,text) VALUES(new.rowid,new.text);
                END;
            ''')
        except BaseException:
            self.db.close()
            raise

    def save(self, messages, entities=(), thread=None):
        """Store every actual received message, including locally filtered messages."""
        with self.db:
            for entity in entities:
                if not isinstance(entity, (types.Chat, types.ChatForbidden, types.Channel, types.ChannelForbidden, types.User)):
                    continue
                title = getattr(entity, 'title', None)
                if isinstance(entity, types.User):
                    title = ' '.join(filter(None, [entity.first_name, entity.last_name])) or None
                self.db.execute('''INSERT INTO chats VALUES(?,?,?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET title=COALESCE(excluded.title,chats.title),
                    username=excluded.username,updated_at=excluded.updated_at''',
                    (utils.get_peer_id(entity), title, getattr(entity, 'username', None), time.time()))
            for message in messages:
                if isinstance(message, types.MessageEmpty):
                    continue  # A placeholder must not overwrite previously fetched content.
                if not isinstance(message, (types.Message, types.MessageService)):
                    raise ValueError("Unexpected message type")
                if not isinstance(message.date, datetime) or message.date.tzinfo is None:
                    raise ValueError("Missing message timestamp")
                chat_id = utils.get_peer_id(message.peer_id)
                metadata = reply_metadata(message)
                header = getattr(message, 'reply_to', None)
                peer = getattr(header, 'reply_to_peer_id', None)
                parent_chat = utils.get_peer_id(peer) if peer else None
                original = message.to_json()
                raw = json.loads(original)
                known_thread = thread[1] if thread and thread[0] == chat_id else None
                root = metadata['thread_id'] or known_thread or (message.id if metadata['thread_status'] == 'root' else None)
                sender = getattr(message, 'from_id', None)
                edited = getattr(message, 'edit_date', None)
                source = f'https://t.me/c/{message.peer_id.channel_id}/{message.id}' if isinstance(message.peer_id, types.PeerChannel) else None
                self.db.execute('''
                    INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(chat_id,message_id) DO UPDATE SET
                      text=excluded.text,message_date=excluded.message_date,edit_date=excluded.edit_date,
                      sender_id=excluded.sender_id,source_link=excluded.source_link,
                      last_fetched_at=excluded.last_fetched_at,reply_to_msg_id=excluded.reply_to_msg_id,
                      reply_to_peer_id=excluded.reply_to_peer_id,top_msg_id=excluded.top_msg_id,
                      thread_id=excluded.thread_id,root_msg_id=excluded.root_msg_id,
                      replies_count=excluded.replies_count,thread_status=excluded.thread_status,
                      reply_metadata_json=excluded.reply_metadata_json,original_json=excluded.original_json
                ''', (chat_id, message.id, getattr(message, 'message', '') or '', message.date.timestamp(),
                      edited.timestamp() if edited else None, utils.get_peer_id(sender) if sender else getattr(message, 'sender_id', None),
                      source, time.time(), metadata['reply_to_msg_id'], parent_chat, metadata['thread_id'],
                      metadata['thread_id'] or known_thread, root, metadata['replies_count'], metadata['thread_status'],
                      json.dumps({'reply_to': raw.get('reply_to'), 'replies': raw.get('replies')}, ensure_ascii=False), original))

    def search(self, query, chat_id=None, since=None, limit=10):
        match, boundary = cache_options(query, chat_id, since, limit)
        sql = '''SELECT m.*,c.title AS chat_title FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
                 LEFT JOIN chats c ON c.chat_id=m.chat_id
                 WHERE messages_fts MATCH ?'''
        args = [match]
        if chat_id is not None:
            sql += ' AND m.chat_id=?'; args.append(chat_id)
        if boundary is not None:
            sql += ' AND m.message_date>=?'; args.append(boundary.timestamp())
        sql += ' ORDER BY bm25(messages_fts),m.message_date DESC,m.chat_id,m.message_id DESC LIMIT ?'
        args.append(limit)
        messages = []
        for row in self.db.execute(sql, args).fetchall():
            item = {k: row[k] for k in ('chat_id','chat_title','message_id','text','source_link','sender_id',
                    'reply_to_msg_id','reply_to_peer_id','top_msg_id','thread_id','root_msg_id','replies_count','thread_status')}
            item.update(timestamp=iso(row['message_date']), edit_date=iso(row['edit_date']),
                        last_fetched_at=iso(row['last_fetched_at']), cached=True,
                        reply_metadata=json.loads(row['reply_metadata_json']), missing_context=[])
            parent_chat = row['reply_to_peer_id'] or row['chat_id']
            item['reply_reference'] = {'chat_id': parent_chat, 'message_id': row['reply_to_msg_id']} if row['reply_to_msg_id'] else None
            seen = set()
            for relation, message_id in (('parent', row['reply_to_msg_id']), ('root', row['root_msg_id'])):
                reference = (row['chat_id'] if relation == 'root' and message_id == row['message_id'] else parent_chat, message_id)
                if message_id and reference not in seen:
                    seen.add(reference)
                    if not self.db.execute('SELECT 1 FROM messages WHERE chat_id=? AND message_id=?', reference).fetchone():
                        item['missing_context'].append({'chat_id': reference[0], 'message_id': message_id, 'relation': relation})
            messages.append(item)
        return {'status': 'ok' if messages else 'empty', 'source': 'cache', 'messages': messages,
                'returned': len(messages), 'limit': limit,
                'complete': False, 'current': False,
                'limitation': 'Only previously fetched messages are cached; absence, current availability and complete Telegram coverage cannot be inferred.'}

    def close(self):
        self.db.close()
