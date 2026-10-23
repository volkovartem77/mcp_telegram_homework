"""Offline keepalive regressions using real Telethon ping/pong methods."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import errors, functions, types
from telethon.network.mtprotosender import MTProtoSender

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import RpcExecutor, RpcState, load_limits
from telegram_mcp.session_client import SessionClient


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.now = 100000.0
        config = load_limits(PROJECT_ROOT / "rate_limits.yaml")
        config["auto_waiting_max"] = 5
        config["cooldown"]["tiers"] = []
        self.state = RpcState(Path(self.temp.name) / "operations.sqlite3", config, lambda: self.now)
        self.send = AsyncMock(return_value=None)
        self.executor = RpcExecutor(self.state, self.send)
        self.sender = MagicMock()
        self.sender._ping = None
        self.owner = SessionClient(session_path=Path(self.temp.name) / "unused.session")
        self.owner._client = SimpleNamespace(_sender=self.sender)
        self.owner.executor = self.executor

    async def asyncTearDown(self):
        await self.executor.close()
        self.state.close()
        self.temp.cleanup()

    def block_locally(self):
        with self.state.db:
            self.state.db.execute("UPDATE state SET cooldown=?", (self.now + 10,))

    def ping(self, identifier=41):
        self.owner._keepalive_ping(identifier)

    async def settle(self):
        await self.executor.queue.join()
        # Completion callbacks also run for admission rejections, which never enter the queue.
        await asyncio.sleep(0)

    def audit(self):
        return self.state.db.execute("SELECT operation,rpc,outcome,error FROM rpc_audit ORDER BY id").fetchall()

    async def test_original_hook_reproduces_false_missing_pong(self):
        self.sender.send = self.executor.intercept
        self.block_locally()
        self.ping()
        await self.settle()
        self.assertEqual(self.sender._ping, 41)
        self.assertEqual(self.audit(), [])
        self.ping(42)
        self.sender._start_reconnect.assert_called_once_with(None)
        self.send.assert_not_awaited()

    async def test_local_cooldown_rejection_clears_only_unsent_ping(self):
        self.sender.send = self.owner._intercept
        self.block_locally()
        self.ping()
        await self.settle()
        self.assertIsNone(self.sender._ping)
        self.assertEqual(self.audit(), [])
        self.assertEqual(self.state.wait("internal"), 10)
        self.assertFalse(self.state.halted())
        self.now += 11
        self.ping(42)
        await self.settle()
        self.sender._start_reconnect.assert_not_called()
        self.assertEqual(self.audit(), [("internal", "PingRequest", "ok", None)])
        self.send.assert_awaited_once()

    async def test_global_limit_and_persisted_flood_are_not_bypassed(self):
        self.sender.send = self.owner._intercept
        self.state.config["limits"]["global"]["minute"] = 1
        await self.executor.intercept(functions.updates.GetStateRequest())
        self.state.preserve_flood(90)
        self.ping()
        await self.settle()
        self.assertIsNone(self.sender._ping)
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(self.state.wait("internal"), 90)
        self.send.assert_awaited_once()

    async def test_rejection_at_queued_dispatch_also_clears_ping(self):
        self.sender.send = self.owner._intercept
        self.state.config["rpc_timeout_seconds"] = 1

        async def first_request(*args, **kwargs):
            self.state.preserve_flood(10)

        self.send.side_effect = first_request
        first = self.executor.intercept(functions.updates.GetStateRequest())
        self.ping()
        await first
        await self.settle()
        self.assertIsNone(self.sender._ping)
        self.assertEqual(len(self.audit()), 1)
        self.send.assert_awaited_once()

    async def test_stale_callback_does_not_clear_newer_ping(self):
        self.sender.send = self.owner._intercept
        self.block_locally()
        self.ping()
        self.sender._ping = 42
        await self.settle()
        self.assertEqual(self.sender._ping, 42)

    async def test_dispatched_floodwait_remains_accounted_and_preserved(self):
        self.sender.send = self.owner._intercept
        self.send.side_effect = errors.FloodWaitError(None, capture=60)
        self.ping()
        await self.settle()
        self.assertEqual(self.sender._ping, 41)
        self.assertEqual(self.state.wait("internal"), 60)
        self.assertFalse(self.state.halted())
        self.assertEqual(self.audit(), [("internal", "PingRequest", "flood_wait", "FloodWaitError")])
        self.send.assert_awaited_once()

    async def test_connection_failure_still_halts_without_retry(self):
        self.sender.send = self.owner._intercept
        self.send.side_effect = ConnectionError("Synthetic disconnected transport")
        self.ping()
        await self.settle()
        self.assertTrue(self.state.halted())
        self.assertEqual(self.state.db.execute("SELECT reason FROM state").fetchone()[0], "uncertain_dispatch")
        self.assertEqual(self.sender._ping, 41)
        self.assertEqual(self.audit(), [("internal", "PingRequest", "error", "ConnectionError")])
        self.send.assert_awaited_once()

    async def test_dispatched_ping_without_pong_still_triggers_reconnect(self):
        self.sender.send = self.owner._intercept
        self.ping()
        await self.settle()
        self.assertEqual(self.sender._ping, 41)
        self.ping(42)
        self.sender._start_reconnect.assert_called_once_with(None)
        self.send.assert_awaited_once()

    async def test_real_pong_clears_marker_and_each_ping_is_accounted(self):
        self.sender.send = self.owner._intercept

        async def pong(request, ordered=False):
            result = types.Pong(msg_id=123, ping_id=request.ping_id)
            await MTProtoSender._handle_pong(self.sender, SimpleNamespace(obj=result))
            return result

        self.send.side_effect = pong
        for identifier in (41, 42):
            self.ping(identifier)
            await self.settle()
            self.assertIsNone(self.sender._ping)
        self.sender._start_reconnect.assert_not_called()
        self.assertEqual(self.audit(), [("internal", "PingRequest", "ok", None)] * 2)
        self.assertEqual(self.send.await_count, 2)

    async def test_automatic_wait_crossing_tick_does_not_reconnect_or_duplicate_ping(self):
        self.sender.send = self.owner._intercept
        self.state.config["auto_waiting_max"] = 60
        self.block_locally()
        waiting, release = asyncio.Event(), asyncio.Event()

        async def wait(seconds):
            waiting.set()
            await release.wait()
            self.now += seconds

        self.executor.sleep = wait
        self.ping()
        await waiting.wait()
        # A heartbeat tick while the first ping is queued must not reconnect.
        self.ping(42)
        self.sender._start_reconnect.assert_not_called()
        self.assertEqual(self.executor.pending, 1)
        self.send.assert_not_awaited()
        release.set()
        await self.settle()
        self.assertEqual(self.audit(), [("internal", "PingRequest", "ok", None)])
        self.assertEqual(self.state.db.execute("SELECT wait_seconds FROM rpc_audit").fetchone()[0], 10)
        # Once dispatch finishes, genuine missing-pong behavior is unchanged.
        self.ping(43)
        self.sender._start_reconnect.assert_called_once_with(None)

    async def test_pending_ping_without_pong_still_hits_rpc_timeout_and_halts(self):
        self.sender.send = self.owner._intercept
        self.state.config["rpc_timeout_seconds"] = 0.01
        dispatched = asyncio.Event()

        async def no_pong(*args, **kwargs):
            dispatched.set()
            await asyncio.Event().wait()

        self.send.side_effect = no_pong
        self.ping()
        await dispatched.wait()
        self.ping(42)
        self.sender._start_reconnect.assert_not_called()
        await self.settle()
        self.assertTrue(self.state.halted())
        self.assertEqual(self.audit(), [("internal", "PingRequest", "error", "TimeoutError")])
        self.send.assert_awaited_once()
