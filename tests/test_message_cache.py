"""Focused offline cache persistence, FTS5 and MCP/RPC boundary checks."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from telethon import functions, types

from telegram_mcp.message_cache import MessageCache
from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import RpcExecutor, RpcState, load_limits
from telegram_mcp.server import create_server
from telegram_mcp.session_client import SessionClient

DATE = datetime(2026, 10, 20, tzinfo=timezone.utc)
CHAT = -1000000000042


def message(identifier=10, text='Used computer 16 GB', **kwargs):
    return types.Message(id=identifier, peer_id=types.PeerChannel(42), date=DATE,
                         message=text, from_id=types.PeerUser(7), **kwargs)


def response(*messages):
    return types.messages.Messages(messages=list(messages), topics=[], chats=[], users=[])


class CacheTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'messages.sqlite3'
        self.cache = MessageCache(self.path)

    def tearDown(self):
        self.cache.close(); self.temp.cleanup()

    def test_persistence_upsert_full_json_media_and_metadata(self):
        msg = message(reply_to=types.MessageReplyHeader(reply_to_msg_id=8, reply_to_top_id=5),
                      media=types.MessageMediaDocument(document=types.Document(
                          id=123,access_hash=1,file_reference=b'\x00\x01\x02',date=DATE,mime_type='application/pdf',
                          size=16,dc_id=2,attributes=[types.DocumentAttributeFilename('offer.pdf')])))
        self.cache.save([msg, types.MessageEmpty(id=10,peer_id=types.PeerChannel(42))])
        row = self.cache.db.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((row['chat_id'],row['message_id'],row['sender_id']), (CHAT,10,7))
        self.assertEqual((row['reply_to_msg_id'],row['top_msg_id'],row['root_msg_id']), (8,5,5))
        raw=json.loads(row['original_json'])
        self.assertEqual(raw['date'],DATE.isoformat())
        self.assertEqual(raw['reply_to']['reply_to_top_id'],5)
        self.assertEqual(raw['media']['document']['id'],123)
        self.assertEqual(raw['media']['document']['file_reference'],'AAEC')
        self.assertEqual(raw['media']['document']['attributes'][0]['file_name'],'offer.pdf')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        with patch('telegram_mcp.message_cache.time.time',return_value=2000000000):
            self.cache.save([message(text='Refreshed laptop offer', edit_date=DATE)])
        self.assertEqual(self.cache.db.execute('SELECT count(*) FROM messages').fetchone()[0],1)
        self.assertEqual(self.cache.search('computer')['status'],'empty')
        self.cache.close();self.cache=MessageCache(self.path)
        hit=self.cache.search('laptop')['messages'][0]
        self.assertEqual(hit['last_fetched_at'],'2033-05-18T03:33:20+00:00')
        self.assertEqual(hit['edit_date'],DATE.isoformat())

    def test_fts_filters_missing_context_and_chat_metadata(self):
        group=types.Channel(id=42,title='Da Nang market',photo=types.ChatPhotoEmpty(),date=DATE,
                            username='danang_market',megagroup=True)
        self.cache.save([message(reply_to=types.MessageReplyHeader(reply_to_msg_id=8,reply_to_top_id=5))],[group])
        other=types.Message(id=10,peer_id=types.PeerChannel(43),date=DATE,message='computer 16 GB')
        self.cache.save([other])
        result=self.cache.search('computer 16',chat_id=CHAT,since=DATE.isoformat())
        self.assertEqual(result['returned'],1)
        hit=result['messages'][0]
        self.assertEqual(self.cache.db.execute('SELECT username FROM chats WHERE chat_id=?',(CHAT,)).fetchone()[0],'danang_market')
        self.assertEqual(hit['chat_title'],'Da Nang market')
        self.assertEqual(hit['source_link'],'https://t.me/c/42/10')
        self.assertEqual([r['message_id'] for r in hit['missing_context']],[8,5])
        self.assertNotIn('original_json',hit)
        self.assertFalse(result['complete']);self.assertFalse(result['current'])
        self.cache.save([message(8,'Parent'),message(5,'Root')])
        self.assertEqual(self.cache.search('computer',chat_id=CHAT)['messages'][0]['missing_context'],[])
        self.assertEqual(self.cache.search('computer',chat_id=123)['status'],'empty')
        self.assertEqual(self.cache.search('computer',since='2026-10-21T00:00:00Z')['status'],'empty')
        self.assertEqual(self.cache.search('computer OR invented')['status'],'empty')
        self.assertEqual(self.cache.db.execute('SELECT count(*) FROM messages').fetchone()[0],4)


class CacheMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('Network forbidden'))
        guard.start();self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory()
        config=load_limits(PROJECT_ROOT/'rate_limits.yaml');config['cooldown']['tiers']=[]
        self.state=RpcState(Path(self.temp.name)/'operations.sqlite3',config)
        self.cache=MessageCache(Path(self.temp.name)/'messages.sqlite3')
        self.send=AsyncMock()
        self.executor=RpcExecutor(self.state,self.send)
        self.owner=SessionClient()
        self.owner.state=self.state;self.owner.cache=self.cache;self.owner.executor=self.executor;self.owner.ready=True
        async def client(request):
            return await self.executor.intercept(request)
        self.owner._client=client
        self.owner.cached_peer=lambda cid:types.InputPeerChannel(42,1) if cid==CHAT else None
        @asynccontextmanager
        async def factory():
            yield self.owner
        self.client=Client(create_server(factory));await self.client.__aenter__()

    async def asyncTearDown(self):
        await self.client.__aexit__(None,None,None)
        await self.executor.close();self.cache.close();self.state.close();self.temp.cleanup()

    async def call(self,name,args):
        return (await self.client.call_tool(name,args)).data

    async def test_cache_search_uses_audit_but_no_telegram_or_quota(self):
        self.cache.save([message()])
        result=await self.call('search_cache',{'query':'computer'})
        self.assertEqual(result['status'],'ok');self.assertTrue(result['operation_id'])
        self.send.assert_not_awaited()
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM rpc_audit').fetchone()[0],0)
        audit=self.state.db.execute('SELECT operation,rpc_count,parameters FROM tool_audit').fetchone()
        self.assertEqual(audit[:2],('search_cache',0));self.assertNotIn('computer',audit[2])
        self.assertEqual(self.state.wait('internal'),0)
        invalid=await self.call('search_cache',{'query':'computer','limit':51})
        self.assertEqual(invalid['status'],'invalid_request')
        self.state.halt('synthetic_stop')
        self.assertEqual((await self.call('search_cache',{'query':'computer'}))['status'],'halted')
        self.send.assert_not_awaited()

    async def test_all_four_tools_persist_before_filtering_without_extra_rpcs(self):
        self.send.return_value=response(message(10,'Filtered original'))
        result=await self.call('get_message',{'chat_id':CHAT,'message_id':10,'since':'2026-10-21T00:00:00Z'})
        self.assertEqual(result['status'],'filtered')
        self.send.return_value=response(message(11,'Search result'))
        await self.call('search_chat',{'chat_id':CHAT,'query':'Search'})
        self.send.return_value=response(message(12,'History result'))
        await self.call('read_around',{'chat_id':CHAT,'message_id':13,'direction':'older'})
        root=message(20,'Thread root',replies=types.MessageReplies(replies=1,replies_pts=1))
        reply=message(21,'Thread reply',reply_to=types.MessageReplyHeader(reply_to_msg_id=20))
        self.send.side_effect=[response(root),response(reply)]
        await self.call('get_replies',{'chat_id':CHAT,'message_id':20})
        self.assertEqual(self.send.await_count,5)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM rpc_audit').fetchone()[0],5)
        self.assertEqual([r[0] for r in self.cache.db.execute('SELECT message_id FROM messages ORDER BY message_id')],[10,11,12,20,21])
        self.assertEqual(self.cache.search('original')['returned'],1)
        self.assertEqual(self.cache.search('reply')['messages'][0]['root_msg_id'],20)

    async def test_dialog_metadata_updates_chats_without_caching_dialog_messages(self):
        self.send.return_value=types.messages.Dialogs(dialogs=[],messages=[message()],chats=[
            types.ChannelForbidden(id=42,access_hash=1,title='Original title',megagroup=True)],users=[])
        await self.call('list_dialogs',{'page_size':5})
        self.assertEqual(self.cache.db.execute('SELECT title FROM chats WHERE chat_id=?',(CHAT,)).fetchone()[0],'Original title')
        self.assertEqual(self.cache.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        self.send.return_value=types.messages.Dialogs(dialogs=[],messages=[],chats=[
            types.ChannelForbidden(id=42,access_hash=1,title='Updated title',megagroup=True)],users=[])
        await self.call('list_dialogs',{'page_size':5})
        self.assertEqual(self.cache.db.execute('SELECT title FROM chats WHERE chat_id=?',(CHAT,)).fetchone()[0],'Updated title')
        self.assertEqual(self.send.await_count,2)
