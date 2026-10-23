"""Focused offline automatic-wait tests with a synthetic clock and sender."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import yaml
from telethon import errors, functions

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import OPERATION, OPERATION_ID, RpcExecutor, RpcRejected, RpcState, load_limits


class AutomaticWaitingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.now = 100000.0
        config = load_limits(PROJECT_ROOT / "rate_limits.yaml")
        config["cooldown"]["tiers"] = []
        self.state = RpcState(Path(self.temp.name) / "state.sqlite3", config, lambda: self.now)
        self.send = AsyncMock(return_value="ok")
        self.sleeps = []
        self.executor = RpcExecutor(self.state, self.send, self.sleep)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    async def asyncTearDown(self):
        await self.executor.close()
        self.state.close()
        self.temp.cleanup()

    def cooldown(self, seconds):
        with self.state.db:
            self.state.db.execute("UPDATE state SET cooldown=?", (self.now + seconds,))

    async def test_short_wait_executes_once_and_records_tool_and_rpc_wait(self):
        self.cooldown(12)
        identifier = self.state.begin_tool("search_chat", {})
        token = OPERATION_ID.set(identifier)
        operation = OPERATION.set("search_chat")
        try:
            self.assertEqual(await self.executor.intercept(functions.updates.GetStateRequest()), "ok")
        finally:
            OPERATION_ID.reset(token)
            OPERATION.reset(operation)
        self.state.finish_tool(identifier, "ok")
        self.assertEqual(self.sleeps, [12])
        self.send.assert_awaited_once()
        self.assertEqual(self.state.db.execute("SELECT operation,wait_seconds,outcome,operation_id FROM rpc_audit").fetchone(),
                         ("search_chat", 12, "ok", identifier))
        self.assertEqual(self.state.db.execute("SELECT status,wait_seconds,rpc_count FROM tool_audit").fetchone(),
                         ("ok", 12, 1))

    async def test_wait_at_threshold_executes_but_larger_wait_returns_deadline(self):
        self.cooldown(60)
        self.assertEqual(await self.executor.intercept(functions.updates.GetStateRequest()), "ok")
        self.assertEqual(self.sleeps, [60])
        self.cooldown(61)
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(functions.updates.GetStateRequest())
        data = caught.exception.result
        self.assertEqual(data["status"], "rate_limited")
        self.assertEqual(data["retry_after"], 61)
        self.assertEqual(datetime.fromisoformat(data["retry_at"]).timestamp(), self.now + 61)
        self.assertEqual(self.sleeps, [60])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM rpc_audit").fetchone()[0], 1)
        self.send.assert_awaited_once()
        self.assertFalse(self.state.halted())

    async def test_quota_waits_still_serialize_and_count_every_dispatch(self):
        self.state.config["limits"]["global"]["minute"] = 1
        first = self.executor.intercept(functions.updates.GetStateRequest())
        second = self.executor.intercept(functions.updates.GetStateRequest())
        self.assertEqual(await first, "ok")
        self.assertEqual(await second, "ok")
        rows = self.state.db.execute("SELECT dispatched_at,wait_seconds,outcome FROM rpc_audit ORDER BY id").fetchall()
        self.assertEqual(rows, [(100000, 0, "ok"), (100060, 60, "ok")])
        self.assertEqual(self.send.await_count, 2)

    async def test_changed_deadline_is_rechecked_without_exceeding_wait_budget(self):
        self.cooldown(10)

        async def longer_cooldown(seconds):
            await self.sleep(seconds)
            self.state.preserve_flood(100)

        self.executor.sleep = longer_cooldown
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(functions.updates.GetStateRequest())
        data = caught.exception.result
        self.assertEqual(data["retry_after"], 100)
        self.assertEqual(datetime.fromisoformat(data["retry_at"]).timestamp(), self.now + 100)
        self.send.assert_not_awaited()
        self.assertEqual(self.sleeps, [10])
        self.assertEqual(self.state.db.execute("SELECT count(*) FROM rpc_audit").fetchone()[0], 0)

    async def test_halt_during_wait_prevents_dispatch(self):
        self.cooldown(10)

        async def halt(seconds):
            await self.sleep(seconds)
            self.state.halt("synthetic_halt")

        self.executor.sleep = halt
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(functions.updates.GetStateRequest())
        self.assertEqual(caught.exception.result["status"], "halted")
        self.assertTrue(self.state.halted())
        self.send.assert_not_awaited()

    async def test_connection_failure_remains_audited_and_blocks_later_dispatch(self):
        self.send.side_effect = ConnectionError("Synthetic failure")
        for _ in range(2):
            with self.assertRaises(RpcRejected) as caught:
                await self.executor.intercept(functions.updates.GetStateRequest())
            self.assertEqual(caught.exception.result["status"], "halted")
        self.send.assert_awaited_once()
        self.assertEqual(self.state.db.execute("SELECT outcome,error FROM rpc_audit").fetchall(),
                         [("error", "ConnectionError")])

    async def test_floodwait_is_not_replayed_and_next_request_honors_deadline(self):
        self.send.side_effect = errors.FloodWaitError(None, capture=20)
        with self.assertRaises(RpcRejected) as caught:
            await self.executor.intercept(functions.updates.GetStateRequest())
        self.assertEqual(caught.exception.result["error"], "FloodWaitError")
        self.send.assert_awaited_once()
        self.assertEqual(self.sleeps, [])
        self.send.side_effect = None
        self.assertEqual(await self.executor.intercept(functions.updates.GetStateRequest()), "ok")
        self.assertEqual(self.sleeps, [20])
        self.assertEqual(self.state.db.execute("SELECT outcome,wait_seconds FROM rpc_audit ORDER BY id").fetchall(),
                         [("flood_wait", 0), ("ok", 20)])

    def test_only_new_setting_is_accepted_and_value_must_be_finite_nonnegative(self):
        config = load_limits(PROJECT_ROOT / "rate_limits.yaml")
        self.assertEqual(config["auto_waiting_max"], 60)
        path = Path(self.temp.name) / "limits.yaml"
        for value in (-1, True, float("inf"), float("nan")):
            with self.subTest(value=value):
                invalid = dict(config, auto_waiting_max=value)
                path.write_text(yaml.safe_dump(invalid))
                with self.assertRaises(ValueError):
                    load_limits(path)
        for legacy in (True, False):
            invalid = dict(config, max_wait_seconds=5)
            if legacy:
                invalid.pop("auto_waiting_max")
            path.write_text(yaml.safe_dump(invalid))
            with self.assertRaisesRegex(ValueError, "no longer supported"):
                load_limits(path)
