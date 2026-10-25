"""MCP initialization test with an entirely fake session owner."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastmcp import Client

from telegram_mcp.server import create_server


class ServerSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_initializes_with_six_tools(self) -> None:
        @asynccontextmanager
        async def fake_session():
            yield SimpleNamespace()

        with patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in tests")):
            async with Client(create_server(fake_session)) as client:
                self.assertTrue(await client.ping())
                self.assertEqual({tool.name for tool in await client.list_tools()}, {"list_dialogs", "get_message", "search_chat", "get_replies", "read_around", "search_cache"})
