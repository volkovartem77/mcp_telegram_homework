"""Offline startup quota regressions; no real Telegram client or session data."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from telethon import functions
import yaml

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import RpcRejected, RpcState, load_limits
from telegram_mcp.server import create_server
from telegram_mcp.session_client import SessionClient
from telegram_mcp.session_lock import SessionLock


class FakeClient:
    def __init__(self):
        self.raw_send = AsyncMock(return_value=None)
        self._sender = SimpleNamespace(send=self.raw_send)
        self.session = SimpleNamespace(auth_key=True)
        self.disconnect = AsyncMock()

    async def connect(self):
        await self._sender.send(functions.InvokeWithLayerRequest(layer=1, query=functions.help.GetConfigRequest()))

    async def __call__(self, request):
        return await self._sender.send(request)


class StartupQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('Network forbidden'))
        guard.start();self.addCleanup(guard.stop)
        quiet=patch('telegram_mcp.session_client.logging.getLogger')
        quiet.start();self.addCleanup(quiet.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'telegram.session';self.path.touch()
        self.dbpath=self.root/'operations.sqlite3';self.configpath=self.root/'limits.yaml'
        self.now=100000.0
        self.config=load_limits(PROJECT_ROOT/'rate_limits.yaml')
        self.config['cooldown']['tiers']=[]
        self.fake=FakeClient()
        self.client_patch=patch('telegram_mcp.session_client._Client',return_value=self.fake)
        self.client_patch.start();self.addCleanup(self.client_patch.stop)
        credentials=patch('telegram_mcp.session_client.dotenv_values',return_value={'TELEGRAM_API_ID':'123','TELEGRAM_API_HASH':'a'*32})
        credentials.start();self.addCleanup(credentials.stop)
        state=patch('telegram_mcp.session_client.RpcState',side_effect=lambda path,cfg:RpcState(path,cfg,lambda:self.now))
        state.start();self.addCleanup(state.stop)

    def owner(self):
        return SessionClient(session_path=self.path,state_path=self.dbpath,config_path=self.configpath)

    def exhaust(self,operation):
        self.config['limits']['operations'][operation]['day']=1
        self.configpath.write_text(yaml.safe_dump(self.config))
        state=RpcState(self.dbpath,self.config,lambda:self.now)
        identifier=state.dispatch(operation,'PreviouslyDispatchedRequest',self.now)
        state.finish(identifier,'ok');state.close()

    async def test_internal_startup_limit_keeps_mcp_cache_and_ownership_available(self):
        self.exhaust('internal');owner=self.owner()
        async with Client(create_server(lambda:owner)) as client:
            self.assertEqual(len(await client.list_tools()),6)
            self.assertFalse(owner.ready)
            self.assertFalse(owner.state.halted())
            self.assertEqual((await client.call_tool('search_cache',{'query':'computer'})).data['status'],'empty')
            result=(await client.call_tool('list_dialogs',{'page_size':5})).data
            self.assertEqual(result['status'],'rate_limited')
            self.assertEqual(result['retry_after'],86400)
            self.assertTrue(result['manual_reload_required'])
            self.fake.raw_send.assert_not_awaited()
            self.assertEqual(owner.state.db.execute('SELECT count(*) FROM rpc_audit').fetchone()[0],1)
            self.assertEqual(owner.state.db.execute("SELECT rpc_count FROM tool_audit WHERE operation='search_cache'").fetchone()[0],0)
            with self.assertRaises(RuntimeError):SessionLock(self.path).__enter__()
            self.now+=86401
            expired=(await client.call_tool('list_dialogs',{'page_size':5})).data
            self.assertEqual(expired['status'],'unavailable')
            self.assertEqual(expired['retry_after'],0)
            self.fake.raw_send.assert_not_awaited()
        # A fresh manual reload after expiry follows normal, accounted startup.
        self.fake=FakeClient()
        self.client_patch.stop()
        with patch('telegram_mcp.session_client._Client',return_value=self.fake):
            async with self.owner() as fresh:
                self.assertTrue(fresh.ready)
                self.assertEqual(self.fake.raw_send.await_count,2)
                self.assertFalse(fresh.state.halted())

    async def test_session_check_limit_is_also_local_deferred_startup(self):
        self.exhaust('session_check');owner=self.owner()
        async with owner:
            self.assertFalse(owner.ready)
            self.assertTrue(owner._startup_limited)
            self.assertEqual(owner._startup_operation,'session_check')
            result=await owner.execute('list_dialogs',functions.updates.GetStateRequest())
            self.assertEqual(result['retry_after'],86400)
            self.assertEqual(self.fake.raw_send.await_count,1)
            owner.state.halt('synthetic_stop')
            self.assertEqual((await owner.execute('list_dialogs',functions.updates.GetStateRequest()))['status'],'halted')

    async def test_dispatched_floodwait_and_connection_errors_still_fail_startup(self):
        self.configpath.write_text(yaml.safe_dump(self.config))
        with patch.object(self.fake,'connect',side_effect=RpcRejected({'status':'rate_limited','error':'FloodWaitError'})):
            owner=self.owner()
            with self.assertRaises(RuntimeError):await owner.__aenter__()
            self.assertFalse(owner._startup_limited)
            self.assertIsNone(owner.state)
        with patch.object(self.fake,'connect',side_effect=ConnectionError('Synthetic failure')):
            owner=self.owner()
            with self.assertRaises(RuntimeError):await owner.__aenter__()
            self.assertFalse(owner._startup_limited)
            state=RpcState(self.dbpath,self.config,lambda:self.now)
            self.assertTrue(state.halted());state.close()
