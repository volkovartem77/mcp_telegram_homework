"""Offline checks for the PingRequest-only quota boundary and persistent accounting."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from telethon import functions

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import OPERATION, RpcExecutor, RpcRejected, RpcState, load_limits


class KeepaliveBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.now = 100000.0
        self.config = load_limits(PROJECT_ROOT / "rate_limits.yaml")
        self.config["auto_waiting_max"] = 0
        self.config["cooldown"]["tiers"] = []
        self.state = RpcState(self.path, self.config, lambda: self.now)
        self.send = AsyncMock(return_value="ok")
        self.executor = RpcExecutor(self.state, self.send)

    async def asyncTearDown(self):
        await self.executor.close()
        self.state.close()
        self.temp.cleanup()

    async def submit(self, request, operation="internal"):
        token = OPERATION.set(operation)
        try:
            return await self.executor.intercept(request)
        finally:
            OPERATION.reset(token)

    async def test_ping_survives_exhausted_global_and_internal_windows(self):
        for group in (self.config["limits"]["global"], self.config["limits"]["operations"]["internal"]):
            group.update(minute=1, hour=1, day=1)
        await self.submit(functions.updates.GetStateRequest())
        self.assertEqual(self.state.wait("internal"), 86400)
        await self.submit(functions.PingRequest(1))
        self.assertEqual(self.state.wait("internal"), 86400)
        self.assertEqual(self.state.db.execute("SELECT operation,rpc FROM rpc_audit ORDER BY id").fetchall(),
                         [("internal", "GetStateRequest"), ("keepalive", "PingRequest")])
        self.assertEqual(self.send.await_count, 2)

    async def test_pings_do_not_consume_global_or_internal_allowance(self):
        self.config["limits"]["global"]["minute"] = 1
        self.config["limits"]["operations"]["internal"]["minute"] = 1
        await self.submit(functions.PingRequest(1), "search_chat")
        self.assertEqual(self.state.wait("internal"), 0)
        await self.submit(functions.updates.GetStateRequest())
        with self.assertRaises(RpcRejected):
            await self.submit(functions.updates.GetStateRequest())
        await self.submit(functions.PingRequest(2))
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual(self.state.db.execute("SELECT operation FROM rpc_audit ORDER BY id").fetchall(),
                         [("keepalive",), ("internal",), ("keepalive",)])

    async def test_other_internal_startup_and_wrapped_requests_remain_limited(self):
        self.config["limits"]["global"]["minute"] = 1
        await self.submit(functions.updates.GetStateRequest(), "search_chat")
        requests = [
            functions.PingDelayDisconnectRequest(ping_id=1, disconnect_delay=75),
            functions.InvokeWithoutUpdatesRequest(functions.PingRequest(1)),
            functions.InvokeWithLayerRequest(layer=1, query=functions.updates.GetStateRequest()),
            functions.updates.GetDifferenceRequest(pts=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc), qts=0),
        ]
        for request in requests:
            with self.subTest(request=type(request).__name__), self.assertRaises(RpcRejected) as caught:
                await self.submit(request)
            self.assertEqual(caught.exception.result["status"], "rate_limited")
        self.send.assert_awaited_once()

    async def test_non_ping_cannot_claim_reserved_keepalive_operation(self):
        with self.assertRaises(RpcRejected) as caught:
            await self.submit(functions.updates.GetStateRequest(), "keepalive")
        self.assertEqual(caught.exception.result["status"], "halted")
        self.assertTrue(self.state.halted())
        self.send.assert_not_awaited()
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM rpc_audit").fetchone()[0], 0)

    async def test_keepalive_windows_are_enforced_and_survive_reopen(self):
        for window, seconds in (("minute", 60), ("hour", 3600), ("day", 86400)):
            with self.subTest(window=window):
                self.now += 86401
                self.config["limits"]["keepalive"] = {"minute": 3, "hour": 90, "day": 1800}
                self.config["limits"]["keepalive"][window] = 1
                await self.submit(functions.PingRequest(1))
                await self.executor.close()
                self.state.close()
                self.state = RpcState(self.path, self.config, lambda: self.now)
                self.executor = RpcExecutor(self.state, self.send)
                with self.assertRaises(RpcRejected) as caught:
                    await self.submit(functions.PingRequest(2))
                self.assertEqual(caught.exception.result["retry_after"], seconds)
                self.assertEqual(self.state.wait("internal"), 0)
                self.now += seconds
                await self.submit(functions.PingRequest(3))

    async def test_ping_uses_the_same_serial_queue(self):
        self.config["auto_waiting_max"] = 60
        started, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        async def send(request, ordered=False):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            if not isinstance(request, functions.PingRequest):
                started.set()
                await release.wait()
            active -= 1
            return "ok"

        self.send.side_effect = send
        first = asyncio.create_task(self.submit(functions.updates.GetStateRequest()))
        await started.wait()
        second = asyncio.create_task(self.submit(functions.PingRequest(1)))
        await asyncio.sleep(0)
        self.assertEqual(self.send.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(maximum, 1)
        self.assertEqual(self.state.db.execute("SELECT operation FROM rpc_audit ORDER BY id").fetchall(),
                         [("internal",), ("keepalive",)])
