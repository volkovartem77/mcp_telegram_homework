"""Audited, bounded Telegram tools over local MCP stdio."""

from contextlib import asynccontextmanager
import asyncio
import yaml

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult

from telegram_mcp import research_tools
from telegram_mcp.rpc import OPERATION_ID
from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.session_client import SessionClient


def load_tool_settings(path):
    with open(path) as file:
        settings = yaml.safe_load(file)
    for operation in ("list_dialogs", "search_chat", "get_replies", "read_around"):
        tool = settings[operation]
        default, maximum = tool["default_limit"], tool["max_limit"]
        if type(default) is not int or type(maximum) is not int or not 1 <= default <= maximum:
            raise ValueError(f"Invalid {operation} limits: require 1 <= default_limit <= max_limit")
    return settings


def validate_call(name, arguments, tool_settings=None):
    """Allowlist inputs before framework validation; never retain arbitrary strings."""
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        return False, {}
    sanitized = {}
    if name == "read_around":
        sanitized = {"since_provided": arguments.get("since") is not None}
        if set(arguments) - {"chat_id", "message_id", "direction", "limit", "since"}:
            return False, sanitized
        direction = arguments.get("direction", "older")
        try:
            limit, boundary = research_tools.history_options(
                arguments.get("chat_id"), arguments.get("message_id"), direction,
                arguments.get("limit"), arguments.get("since"), tool_settings["read_around"],
            )
        except ValueError:
            return False, sanitized
        sanitized.update(chat_id=arguments["chat_id"], message_id=arguments["message_id"],
                         direction=direction, limit=limit)
        if boundary is not None:
            sanitized["since"] = boundary.isoformat()
        return True, sanitized
    if name == "get_replies":
        sanitized = {"cursor_provided": arguments.get("cursor") is not None}
        if set(arguments) - {"chat_id", "message_id", "limit", "cursor"}:
            return False, sanitized
        try:
            limit, _ = research_tools.replies_options(
                arguments.get("chat_id"), arguments.get("message_id"),
                arguments.get("limit"), arguments.get("cursor"), tool_settings["get_replies"],
            )
        except ValueError:
            return False, sanitized
        sanitized.update(chat_id=arguments["chat_id"], message_id=arguments["message_id"], limit=limit)
        return True, sanitized
    if name == "search_chat":
        sanitized = {"cursor_provided": arguments.get("cursor") is not None,
                     "since_provided": arguments.get("since") is not None,
                     "query_length": len(arguments["query"]) if isinstance(arguments.get("query"), str) else None}
        if set(arguments) - {"chat_id", "query", "limit", "since", "cursor"}:
            return False, sanitized
        try:
            limit, boundary, _, _ = research_tools.search_options(
                arguments.get("chat_id"), arguments.get("query"), arguments.get("limit"),
                arguments.get("since"), arguments.get("cursor"), tool_settings["search_chat"],
            )
        except ValueError:
            return False, sanitized
        sanitized.update(chat_id=arguments["chat_id"], limit=limit)
        if boundary is not None:
            sanitized["since"] = boundary.isoformat()
        return True, sanitized
    if name == "list_dialogs":
        settings = tool_settings["list_dialogs"]
        size = arguments.get("page_size")
        size = settings["default_limit"] if size is None else size
        folder = arguments.get("folder_id", 0)
        cursor = arguments.get("cursor")
        sanitized = {"cursor_provided": cursor is not None}
        if type(size) is int:
            sanitized["page_size"] = size if 1 <= size <= settings["max_limit"] else "invalid"
        if type(folder) is int:
            sanitized["folder_id"] = folder if folder in (0, 1) else "invalid"
        valid = (not set(arguments) - {"page_size", "cursor", "folder_id"}
                 and type(size) is int and 1 <= size <= settings["max_limit"]
                 and type(folder) is int and folder in (0, 1))
        if cursor is not None:
            try:
                research_tools.decode_cursor(cursor, folder)
            except ValueError:
                valid = False
        return valid, sanitized
    if name == "get_message":
        valid = not set(arguments) - {"chat_id", "message_id", "since"}
        for key, minimum, maximum in (("chat_id", -(2**63) + 1, 2**63 - 1), ("message_id", 1, 2**31 - 1)):
            value = arguments.get(key)
            if type(value) is not int or not minimum <= value <= maximum or value == 0:
                valid = False
                sanitized[key] = "invalid"
            else:
                sanitized[key] = value
        sanitized["since_provided"] = arguments.get("since") is not None
        try:
            since = research_tools.parse_since(arguments.get("since"))
            if since is not None:
                sanitized["since"] = since.isoformat()
        except ValueError:
            valid = False
        return valid, sanitized
    return False, {}


class AuditMiddleware(Middleware):
    def __init__(self, runtime, tool_settings):
        self.runtime = runtime
        self.tool_settings = tool_settings

    async def on_call_tool(self, context, call_next):
        owner = self.runtime["owner"]
        state = owner.state
        name = context.message.name
        known = name in {"list_dialogs", "get_message", "search_chat", "get_replies", "read_around"}
        operation = name if known else "unknown_tool"
        identifier = None
        token = None
        try:
            valid, parameters = validate_call(name, context.message.arguments, self.tool_settings)
            identifier = state.begin_tool(operation, parameters)
            token = OPERATION_ID.set(identifier)
            if state.halted():
                data = {"status": "halted", "error": "emergency_stop"}
            elif not known:
                data = {"status": "invalid_request", "error": "unknown_tool"}
            elif not valid:
                data = {"status": "invalid_request", "error": "invalid_parameters"}
            else:
                result = await call_next(context)
                data = dict(result.structured_content)
            data["operation_id"] = identifier
            state.finish_tool(identifier, data["status"], data.get("error") or data.get("reason"))
            return ToolResult(structured_content=data)
        except BaseException as exc:
            try:
                state.halt("tool_cancelled" if isinstance(exc, asyncio.CancelledError) else "tool_or_audit_failure")
            except Exception:
                pass  # The in-memory HALTED latch is set before persistent writes.
            if owner.executor:
                owner.executor.failed = True
            if identifier:
                try:
                    state.finish_tool(identifier, "halted", type(exc).__name__)
                except Exception:
                    pass  # Never return a successful result after audit failure.
            if isinstance(exc, asyncio.CancelledError):
                raise
            return ToolResult(structured_content={"status": "halted", "error": "critical_failure", "operation_id": identifier})
        finally:
            if token is not None:
                OPERATION_ID.reset(token)


def create_server(session_factory=SessionClient, settings_path=None):
    tool_settings = load_tool_settings(settings_path or PROJECT_ROOT / "tool_settings.yaml")
    runtime = {}

    @asynccontextmanager
    async def lifespan(server):
        async with session_factory() as owner:
            runtime["owner"] = owner
            try:
                yield
            finally:
                runtime.clear()

    server = FastMCP("Telegram MCP", lifespan=lifespan, middleware=[AuditMiddleware(runtime, tool_settings)], mask_error_details=True)

    @server.tool
    async def list_dialogs(page_size: int | None = None, cursor: str | None = None, folder_id: int = 0) -> dict:
        """Read one dialogs page with default and maximum sizes from tool_settings.yaml. Use next_cursor explicitly; folder 0 is main, 1 archived. No automatic pagination."""
        return await research_tools.list_dialogs(runtime["owner"], page_size, cursor, folder_id, settings=tool_settings["list_dialogs"])

    @server.tool
    async def get_message(chat_id: int, message_id: int, since: str | None = None) -> dict:
        """Read one message from a cached chat ID. Optional timezone-aware ISO since is inclusive; omitted means no date restriction. Never fetch context."""
        return await research_tools.get_message(runtime["owner"], chat_id, message_id, since)

    @server.tool
    async def search_chat(chat_id: int, query: str, limit: int | None = None,
                          since: str | None = None, cursor: str | None = None) -> dict:
        """Search one chat natively, one page only. Default and maximum limits come from tool_settings.yaml; timezone-aware since is inclusive. Pass next_cursor explicitly."""
        return await research_tools.search_chat(runtime["owner"], chat_id, query, limit, since, cursor, tool_settings["search_chat"])

    @server.tool
    async def read_around(chat_id: int, message_id: int, direction: str = "older",
                          limit: int | None = None, since: str | None = None) -> dict:
        """Read one exclusive history page, older or newer, in chronological order. Limits come from tool_settings.yaml; timezone-aware since is inclusive. Continue explicitly using pagination.next_message_id; older pages use the oldest ID. No anchor fetch or automatic pagination."""
        return await research_tools.read_around(runtime["owner"], chat_id, message_id, direction, limit, since, tool_settings["read_around"])

    @server.tool
    async def get_replies(chat_id: int, message_id: int, limit: int | None = None,
                          cursor: str | None = None) -> dict:
        """Read one native reply page from a root or thread member. Default and maximum reply limits come from tool_settings.yaml; root is additional. Root lookup is bounded; completeness is never guaranteed. Pass next_cursor explicitly."""
        return await research_tools.get_replies(runtime["owner"], chat_id, message_id, limit, cursor, tool_settings["get_replies"])

    return server


mcp = create_server()


def main() -> None:
    """Start stdio MCP and reuse the existing authorized Telegram session."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
