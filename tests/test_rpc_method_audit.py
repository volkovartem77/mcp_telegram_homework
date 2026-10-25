"""Focused offline checks for inner RPC names without changing dispatch semantics."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from telethon import functions

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import OPERATION_ID, RpcExecutor, RpcRejected, RpcState, load_limits


class InnerMethodAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('Network forbidden'))
        guard.start();self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory()
        config=load_limits(PROJECT_ROOT/'rate_limits.yaml')
        config['cooldown']['tiers']=[]
        config['auto_waiting_max']=0
        self.state=RpcState(Path(self.temp.name)/'audit.sqlite3',config,lambda:100000.0)
        self.send=AsyncMock(return_value='ok')
        self.executor=RpcExecutor(self.state,self.send)

    async def asyncTearDown(self):
        await self.executor.close();self.state.close();self.temp.cleanup()

    def difference(self):
        return functions.updates.GetDifferenceRequest(pts=1,qts=0,date=datetime(2026,1,1,tzinfo=timezone.utc))

    async def test_nested_startup_wrappers_record_getconfig_and_send_original_once(self):
        request=functions.InvokeWithLayerRequest(layer=1,query=functions.InvokeWithoutUpdatesRequest(
            functions.InitConnectionRequest(api_id=123,device_model='test',system_version='test',app_version='test',
                                            system_lang_code='en',lang_pack='',lang_code='en',query=functions.help.GetConfigRequest())))
        identifier=self.state.begin_tool('get_message',{})
        token=OPERATION_ID.set(identifier)
        try:
            self.assertEqual(await self.executor.intercept(request),'ok')
        finally:OPERATION_ID.reset(token)
        self.state.finish_tool(identifier,'ok')
        self.send.assert_awaited_once_with(request,ordered=False)
        self.assertIs(self.send.await_args.args[0],request)
        self.assertEqual(self.state.db.execute('SELECT operation,rpc,outcome,operation_id FROM rpc_audit').fetchall(),
                         [('internal','GetConfigRequest','ok',identifier)])
        self.assertEqual(self.state.db.execute('SELECT rpc_count FROM tool_audit').fetchone()[0],1)

    async def test_wrapped_difference_counts_once_and_does_not_change_limits_or_history(self):
        old=self.state.dispatch('internal','InvokeWithoutUpdatesRequest',100000.0)
        self.state.finish(old,'ok')
        self.state.config['limits']['operations']['internal']['day']=2
        request=functions.InvokeWithoutUpdatesRequest(self.difference())
        await self.executor.intercept(request)
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(request)
        self.assertEqual(caught.exception.result['status'],'rate_limited')
        self.assertEqual(caught.exception.result['retry_after'],86400)
        self.send.assert_awaited_once_with(request,ordered=False)
        self.assertEqual(self.state.db.execute('SELECT rpc FROM rpc_audit ORDER BY id').fetchall(),
                         [('InvokeWithoutUpdatesRequest',),('GetDifferenceRequest',)])
        self.assertFalse(self.state.halted())

    async def test_connection_failure_keeps_inner_name_and_existing_halted_behavior(self):
        request=functions.InvokeWithoutUpdatesRequest(self.difference())
        self.send.side_effect=ConnectionError('Synthetic failure')
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(request)
        self.assertEqual(caught.exception.result['status'],'halted')
        self.assertEqual(self.state.db.execute('SELECT rpc,outcome,error FROM rpc_audit').fetchall(),
                         [('GetDifferenceRequest','error','ConnectionError')])
        self.assertTrue(self.state.halted())
        self.assertEqual(self.state.db.execute('SELECT reason FROM state').fetchone()[0],'uncertain_dispatch')
        with self.assertRaises(RpcRejected):
            await self.executor.intercept(functions.help.GetConfigRequest())
        self.send.assert_awaited_once()
