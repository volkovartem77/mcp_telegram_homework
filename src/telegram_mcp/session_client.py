"""Non-interactive session owner for the shared MCP runtime."""

import logging
import asyncio

from dotenv import dotenv_values
from telethon import TelegramClient, functions, utils
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.tlobject import TLRequest

from telegram_mcp.paths import PROJECT_ROOT, SESSION_PATH
from telegram_mcp.rpc import OPERATION, RpcExecutor, RpcRejected, RpcState, load_limits
from telegram_mcp.session_lock import SessionLock


class SessionUnavailable(RuntimeError):
    """A sanitized, expected session precondition failure."""


class _Client(TelegramClient):
    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        try:
            return await super()._call(sender, request, ordered, flood_sleep_threshold)
        except RpcRejected as exc:
            if (OPERATION.get() == "internal"
                    and isinstance(request, (functions.updates.GetDifferenceRequest,
                                             functions.updates.GetChannelDifferenceRequest))
                    and exc.result.get("status") == "rate_limited"
                    and "error" not in exc.result):
                # Telethon 1.45 defers difference processing on ValueError; an
                # unrecognized RpcRejected instead reaches its fatal disconnect.
                raise ValueError("Background updates deferred by local rate limit") from None
            raise

    async def start(self, *args, **kwargs):
        raise RuntimeError("Interactive login is prohibited")

    async def sign_in(self, *args, **kwargs):
        raise RuntimeError("Interactive login is prohibited")

    async def send_code_request(self, *args, **kwargs):
        raise RuntimeError("Interactive login is prohibited")

    async def _borrow_exported_sender(self, *args, **kwargs):
        raise RuntimeError("Additional Telegram senders are not supported")


class SessionClient:
    """Use as an async context manager; submit one raw RPC per execute call."""

    def __init__(self, session_path=SESSION_PATH, state_path=None, config_path=None):
        self.path = session_path
        self.state_path = state_path or PROJECT_ROOT / "data" / "operations.sqlite3"
        self.config_path = config_path or PROJECT_ROOT / "rate_limits.yaml"
        self.lock = SessionLock(session_path)
        self.state = self.executor = self._client = None
        self._keepalive_future = None
        self.ready = False

    async def __aenter__(self):
        if not self.path.is_file():
            raise SessionUnavailable("Telegram session is missing; run scripts/login.py manually")
        self.lock.__enter__()
        try:
            self.state = RpcState(self.state_path, load_limits(self.config_path))
            if self.state.halted():
                raise SessionUnavailable("Telegram operations are HALTED; manual recovery required")
            values = dotenv_values(PROJECT_ROOT / ".env", interpolate=False)
            api_id = int(values.get("TELEGRAM_API_ID") or "0")
            api_hash = values.get("TELEGRAM_API_HASH")
            if api_id <= 0 or not api_hash:
                raise SessionUnavailable("Configure API credentials in the local .env file")
            logger = logging.getLogger("telegram_session")
            logger.setLevel(logging.CRITICAL + 1)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
            self._client = _Client(
                str(self.path), api_id, api_hash, request_retries=0,
                connection_retries=0, auto_reconnect=False, flood_sleep_threshold=0,
                raise_last_call_error=True, receive_updates=False, catch_up=False,
                base_logger=logger,
            )
            self.path.chmod(0o600)
            if not self._client.session.auth_key:
                raise SessionUnavailable("Telegram session is unauthorized; run scripts/login.py manually")
            self.executor = RpcExecutor(self.state, self._client._sender.send)
            self._client._sender.send = self._intercept
            self._client._sender._keepalive_ping = self._keepalive_ping
            await self._client.connect()
            # A raw authenticated RPC avoids is_user_authorized swallowing RPC errors.
            token = OPERATION.set("session_check")
            try:
                await self._client(functions.updates.GetStateRequest())
            finally:
                OPERATION.reset(token)
            self.ready = True
            return self
        except RpcRejected as exc:
            await self.close()
            if exc.result.get("error") in {"AuthKeyUnregisteredError", "SessionRevokedError", "SessionExpiredError", "UnauthorizedError"}:
                raise SessionUnavailable("Telegram session is unauthorized; manual login and recovery required") from None
            raise RuntimeError(f"Telegram session initialization failed: {exc.result}") from None
        except SessionUnavailable:
            await self.close()
            raise
        except BaseException as exc:
            try:
                if self.state:
                    self.state.halt("session_initialization_error")
            except Exception:
                pass
            finally:
                await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError("Telegram session initialization failed; manual recovery required") from None

    def _keepalive_ping(self, ping_id):
        """Do not declare a missing pong while its accounted ping is still pending."""
        if self._keepalive_future is None or self._keepalive_future.done():
            MTProtoSender._keepalive_ping(self._client._sender, ping_id)

    def _intercept(self, request, ordered=False):
        """Keep Telethon's ping marker consistent with local pre-dispatch rejection."""
        future = self.executor.intercept(request, ordered=ordered)
        sender = self._client._sender
        if isinstance(request, functions.PingRequest) and sender._ping == request.ping_id:
            self._keepalive_future = future
            def clear_unsent_ping(done):
                if done.cancelled():
                    return
                exc = done.exception()
                # A Telegram FloodWait also returns rate_limited, but has an error
                # code and an audited dispatch. Leave sent-ping failure detection intact.
                if (isinstance(exc, RpcRejected)
                        and exc.result.get("status") == "rate_limited"
                        and "error" not in exc.result
                        and sender._ping == request.ping_id):
                    sender._ping = None

            future.add_done_callback(clear_unsent_ping)
        return future

    async def execute(self, operation, request):
        if not self.ready:
            return {"status": "unavailable"}
        # No callbacks, iterators, batch requests, or automatic pagination.
        if not isinstance(request, TLRequest):
            return {"status": "invalid_request"}
        token = OPERATION.set(operation)
        try:
            return {"status": "ok", "data": await self._client(request)}
        except RpcRejected as exc:
            return exc.result
        except asyncio.CancelledError:
            self.state.halt("operation_cancelled")
            raise
        except BaseException:
            self.state.halt("operation_error")
            return {"status": "halted"}
        finally:
            OPERATION.reset(token)

    def cached_peer(self, chat_id):
        """Resolve a marked Telegram peer ID locally, without any network request."""
        try:
            raw_id, peer_type = utils.resolve_id(chat_id)
            peer = self._client.session.get_input_entity(peer_type(raw_id))
        except ValueError:
            return None
        return peer

    async def close(self):
        self.ready = False
        try:
            try:
                if self.executor:
                    await self.executor.close()
            finally:
                if self._client:
                    await self._client.disconnect()
        finally:
            try:
                if self.state:
                    self.state.close()
                    self.state = None
            finally:
                self.lock.__exit__(None, None, None)

    async def __aexit__(self, *args):
        await self.close()
