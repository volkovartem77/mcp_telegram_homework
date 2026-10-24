"""Bounded background-ping recovery with real Telethon sender code and fake transport."""
import asyncio
import logging
from collections import deque
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from telethon import errors, functions

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import OPERATION, RpcExecutor, RpcRejected, RpcState, load_limits
from telegram_mcp.session_client import SessionClient, _Client
from telegram_mcp.session_lock import SessionLock


class FakeConnection:
    def __init__(self, owner, initial=False):
        self.owner, self.initial = owner, initial
        self._connected = False

    async def connect(self, timeout=None):
        if not self.initial:
            self.owner.attempts += 1
            self.owner.connecting.set()
            if self.owner.connect_gate is not None:
                await self.owner.connect_gate.wait()
            if self.owner.failures:
                raise self.owner.failures.popleft()
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def send(self, data):
        self.owner.wire_sends += 1
        if self.initial:
            raise ConnectionResetError('Synthetic transport reset')
        raise AssertionError('Recovery must not send or replay application requests')

    async def recv(self):
        await asyncio.Event().wait()


class KeepaliveRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        quiet = patch.object(logging.getLogger('telegram_mcp.keepalive'), 'warning')
        quiet.start(); self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite3'
        config = load_limits(PROJECT_ROOT/'rate_limits.yaml')
        config['cooldown']['tiers'] = []
        self.state = RpcState(self.path, config, lambda:100000.0)
        self.owner = SessionClient(session_path=Path(self.temp.name)/'unused.session')
        self.owner.lock.__enter__()
        self.owner.state = self.state
        self.client = _Client(None,123,'a'*32,connection_retries=0,request_retries=0,
                              auto_reconnect=False,receive_updates=False,base_logger='offline_keepalive_test')
        log=logging.getLogger('offline_keepalive_test')
        log.disabled=True
        log.addHandler(logging.NullHandler());log.propagate=False
        self.owner._client = self.client
        self.client._sender.auth_key.key = bytes(range(256))
        self.client._update_loop = AsyncMock()
        self.client._keepalive_loop = AsyncMock()
        self.attempts = self.wire_sends = 0
        self.failures = deque()
        self.connecting = asyncio.Event()
        self.connect_gate = None
        self.client._connection = lambda *a,**kw:FakeConnection(self)
        self.sender = self.client._sender
        self.executor = RpcExecutor(self.state,self.sender.send,recover_keepalive=self.owner._recover_keepalive)
        self.owner.executor = self.executor
        self.owner.ready = True
        self.sender.send = self.owner._intercept
        real_sleep = asyncio.sleep
        async def fast_sleep(seconds):
            await real_sleep(0)
        sleeper = patch('telegram_mcp.session_client.asyncio.sleep',side_effect=fast_sleep)
        sleeper.start();self.addCleanup(sleeper.stop)
        await self.sender.connect(FakeConnection(self,initial=True))
        # The real keepalive loop normally observes this future; this fixture mocks that loop.
        self.sender._disconnected.add_done_callback(lambda f:None if f.cancelled() else f.exception())

    async def asyncTearDown(self):
        await self.executor.close()
        await self.client._disconnect()
        self.owner.lock.__exit__(None,None,None)
        self.state.close()
        self.temp.cleanup()

    async def settle(self):
        await asyncio.wait_for(self.executor.queue.join(),1)
        await asyncio.sleep(0)

    def audit(self):
        return self.state.db.execute('SELECT operation,rpc,outcome,error FROM rpc_audit ORDER BY id').fetchall()

    async def test_real_sender_reset_recovers_without_replaying_ping(self):
        self.failures.extend([ConnectionRefusedError(),ConnectionResetError()])
        self.owner._keepalive_ping(41)
        await self.settle()
        self.assertEqual(self.attempts,3)
        self.assertEqual(self.wire_sends,1)
        self.assertFalse(self.state.halted())
        self.assertTrue(self.sender._transport_connected())
        self.assertEqual(self.audit(),[('keepalive','PingRequest','error','ConnectionResetError')])
        self.assertEqual(self.state.wait('internal'),0)
        self.assertEqual(self.state.db.execute('SELECT count(*) FROM rpc_audit').fetchone()[0],1)
        self.assertIsNone(self.sender._ping)
        self.client._update_loop.assert_awaited_once()
        self.client._keepalive_loop.assert_awaited_once()
        self.executor.send = AsyncMock(return_value='ok')
        self.assertEqual((await self.owner.execute('get_message',functions.updates.GetStateRequest()))['status'],'ok')

    async def test_five_failed_attempts_persist_halted(self):
        self.failures.extend(ConnectionRefusedError() for _ in range(5))
        self.owner._keepalive_ping(41)
        await self.settle()
        self.assertEqual(self.attempts,5)
        self.assertEqual(self.wire_sends,1)
        self.assertTrue(self.state.halted())
        self.assertTrue(self.state.halt_path.exists())
        self.assertEqual(self.audit(),[('keepalive','PingRequest','error','ConnectionResetError')])
        self.assertEqual(self.state.db.execute('SELECT reason FROM state').fetchone()[0],'keepalive_reconnect_failed')
        self.state.close()
        self.state = RpcState(self.path,self.state.config,lambda:100000.0)
        self.assertTrue(self.state.halted())
        self.executor.state = self.owner.state = self.state

    async def test_120_second_deadline_halts_without_waiting_for_five_attempts(self):
        self.connect_gate = asyncio.Event()
        real_timeout = asyncio.timeout
        def short_deadline(seconds):
            self.assertEqual(seconds,120)
            return real_timeout(0.02)
        with patch('telegram_mcp.rpc.asyncio.timeout',side_effect=short_deadline):
            self.owner._keepalive_ping(41)
            await self.settle()
        self.assertEqual(self.attempts,1)
        self.assertTrue(self.state.halted())
        self.assertEqual(self.audit()[0][-1],'ConnectionResetError')
        self.assertFalse(self.executor.recovering)

    async def test_recovery_blocks_new_and_queued_user_rpcs(self):
        entered,fail = asyncio.Event(),asyncio.Event()
        self.connect_gate = asyncio.Event()
        sent_users=[]
        async def send(request,ordered=False):
            if type(request) is functions.PingRequest:
                entered.set();await fail.wait();raise ConnectionError()
            sent_users.append(self.executor.recovering)
            return 'ok'
        self.executor.send=send
        self.owner._keepalive_ping(41)
        await entered.wait()
        queued=asyncio.create_task(self.owner.execute('get_message',functions.updates.GetStateRequest()))
        await asyncio.sleep(0)
        fail.set();await self.connecting.wait()
        self.assertTrue(self.executor.recovering)
        with self.assertRaises(RuntimeError):
            SessionLock(self.owner.path).__enter__()
        self.assertEqual(sent_users,[])
        self.assertEqual(await self.owner.execute('search_chat',functions.updates.GetStateRequest()),{'status':'unavailable'})
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(functions.updates.GetStateRequest())
        self.assertEqual(caught.exception.result['status'],'unavailable')
        self.connect_gate.set()
        self.assertEqual((await queued)['status'],'ok')
        await self.settle()
        self.assertEqual(sent_users,[False])
        self.assertFalse(self.state.halted())

    async def test_user_ping_failure_does_not_recover(self):
        token=OPERATION.set('get_message')
        try:
            with self.assertRaises(RpcRejected):
                await self.owner._intercept(functions.PingRequest(1))
        finally:
            OPERATION.reset(token)
        self.assertTrue(self.state.halted())
        self.assertEqual(self.attempts,0)

    async def test_other_failures_do_not_enter_recovery(self):
        self.executor.send=AsyncMock(side_effect=TimeoutError())
        self.owner._keepalive_ping(41)
        await self.settle()
        self.assertTrue(self.state.halted())
        self.assertEqual(self.attempts,0)
        self.assertEqual(self.audit()[0][-1],'TimeoutError')

    async def test_cancelling_recovery_halts_and_settles_callers(self):
        self.connect_gate=asyncio.Event()
        self.owner._keepalive_ping(41)
        await self.connecting.wait()
        await self.executor.close()
        self.assertTrue(self.state.halted())
        self.assertFalse(self.executor.recovering)
        self.assertTrue(self.owner._keepalive_future.done())
        self.assertEqual(self.audit()[0][-1],'ConnectionResetError')
