"""Smoke test for MCP initialization without Telegram access."""

import unittest

from fastmcp import Client

from telegram_mcp.server import mcp


class ServerSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_initializes_without_tools(self) -> None:
        async with Client(mcp) as client:
            self.assertTrue(await client.ping())
            self.assertEqual(await client.list_tools(), [])
