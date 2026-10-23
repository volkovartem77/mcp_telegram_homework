"""Real Telethon update-loop regressions; no transport or session access."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import TelegramClient, errors, functions, types

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import OPERATION, RpcExecutor, RpcRejected, RpcState, load_limits
from telegram_mcp.session_client import _Client


class BackgroundUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        config = load_limits(PROJECT_ROOT / "rate_limits.yaml")
        config["cooldown"]["tiers"] = []
        self.state = RpcState(Path(self.temp.name) / "state.sqlite3", config, lambda: 100000.0)
        self.send = AsyncMock()
        self.executor = RpcExecutor(self.state, self.send)

    async def asyncTearDown(self):
        await self.executor.close()
        self.state.close()
        self.temp.cleanup()

    def request(self, channel=False):
        if channel:
            return functions.updates.GetChannelDifferenceRequest(
                channel=types.InputChannel(1, 1), filter=types.ChannelMessagesFilterEmpty(), pts=1, limit=1)
        return functions.updates.GetDifferenceRequest(
            pts=1, date=datetime(2026, 1, 1, tzinfo=timezone.utc), qts=0)

    def exhaust(self, window="hour"):
        self.state.config["limits"]["operations"]["internal"][window] = 1
        identifier = self.state.dispatch("internal", "SyntheticRequest", 100000.0)
        self.state.finish(identifier, "ok")

    def client(self, cls=_Client):
        # Memory session with synthetic credentials; never connect this client.
        client = cls(None, 123, "a" * 32, receive_updates=False, request_retries=0,
                     auto_reconnect=False, flood_sleep_threshold=0)
        client._log = MagicMock()
        client._sender.send = MagicMock(side_effect=self.executor.intercept)
        client.is_connected = MagicMock(return_value=True)
        client.disconnect = AsyncMock()
        return client

    async def update_loop(self, client, channel=False):
        box = client._message_box = MagicMock()
        box.is_empty.return_value = False
        box.get_difference.side_effect = [None, None] if channel else [self.request(), None]
        box.get_channel_difference.side_effect = [self.request(True), None] if channel else [None]
        # End verification at the next ordinary idle wait, with no timer/retry simulation.
        box.check_deadlines.side_effect = asyncio.CancelledError()
        await client._update_loop()
        return box

    async def test_original_client_disconnects_on_local_quota_rejection(self):
        self.exhaust()
        client = self.client(TelegramClient)
        await self.update_loop(client)
        client.disconnect.assert_awaited_once()
        self.assertIsInstance(client._updates_error, RpcRejected)
        self.assertEqual(client._updates_error.result["status"], "rate_limited")
        self.send.assert_not_awaited()

    async def test_hourly_and_daily_rejections_defer_account_and_channel_updates(self):
        for window in ("hour", "day"):
            for channel in (False, True):
                with self.subTest(window=window, channel=channel):
                    self.state.config["limits"]["operations"]["internal"].update(hour=50, day=200)
                    self.state.db.execute("DELETE FROM rpc_audit")
                    self.exhaust(window)
                    client = self.client()
                    box = await self.update_loop(client, channel)
                    client.disconnect.assert_not_awaited()
                    self.assertIsNone(client._updates_error)
                    if channel:
                        box.end_channel_difference.assert_called_once()
                    else:
                        box.end_difference.assert_called_once()
                    client._sender.send.assert_called_once()
                    self.send.assert_not_awaited()
                    self.assertEqual(self.state.db.execute("SELECT count(*) FROM rpc_audit").fetchone()[0], 1)
                    self.assertEqual(self.state.wait("internal"), 3600 if window == "hour" else 86400)
                    self.assertFalse(self.state.halted())

    async def test_tool_scoped_rejection_keeps_its_structured_error(self):
        self.exhaust()
        self.state.config["limits"]["global"]["hour"] = 1
        client = self.client()
        token = OPERATION.set("search_chat")
        try:
            with self.assertRaises(RpcRejected) as caught:
                await client(self.request())
        finally:
            OPERATION.reset(token)
        self.assertEqual(caught.exception.result["retry_after"], 3600)
        self.send.assert_not_awaited()

    async def test_other_internal_requests_are_not_reclassified(self):
        self.exhaust()
        with self.assertRaises(RpcRejected):
            await self.client()(functions.updates.GetStateRequest())
        self.send.assert_not_awaited()

    async def test_halted_rejection_still_follows_existing_fatal_path(self):
        self.state.halt("synthetic_halt")
        client = self.client()
        await self.update_loop(client)
        client.disconnect.assert_awaited_once()
        self.assertEqual(client._updates_error.result["status"], "halted")
        self.assertTrue(self.state.halted())
        self.send.assert_not_awaited()

    async def test_connection_failure_still_halts_and_disconnects(self):
        self.send.side_effect = ConnectionError("Synthetic transport failure")
        client = self.client()
        await self.update_loop(client)
        client.disconnect.assert_awaited_once()
        self.assertTrue(self.state.halted())
        self.assertEqual(client._updates_error.result["error"], "ConnectionError")
        self.assertEqual(self.state.db.execute("SELECT outcome,error FROM rpc_audit").fetchone(),
                         ("error", "ConnectionError"))
        self.send.assert_awaited_once()

    async def test_dispatched_floodwait_is_not_reclassified_or_retried(self):
        self.send.side_effect = errors.FloodWaitError(None, capture=3600)
        with self.assertRaises(RpcRejected) as caught:
            await self.client()(self.request())
        self.assertEqual(caught.exception.result["error"], "FloodWaitError")
        self.assertEqual(self.state.wait("internal"), 3600)
        self.assertEqual(self.state.db.execute("SELECT outcome FROM rpc_audit").fetchone()[0], "flood_wait")
        self.send.assert_awaited_once()
