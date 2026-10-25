"""Non-interactive session owner for the shared MCP runtime."""

import logging
import asyncio

from dotenv import dotenv_values
from telethon import TelegramClient, functions, utils
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.tlobject import TLRequest

from telegram_mcp.paths import PROJECT_ROOT, SESSION_PATH
from telegram_mcp.rpc import OPERATION, RpcExecutor, RpcRejected, RpcState, load_limits, rate_limited_result
from telegram_mcp.session_lock import SessionLock
from telegram_mcp.message_cache import MessageCache


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
        self.cache = None
        self._keepalive_future = None
        self._scheduling_keepalive = False
        self._startup_limited = False
        self._startup_operation = "internal"
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
            self.cache = MessageCache(self.state_path.with_name("messages.sqlite3"))
            self.executor = RpcExecutor(self.state, self._client._sender.send,
                                        recover_keepalive=self._recover_keepalive)
            self._client._sender.send = self._intercept
            self._client._sender._keepalive_ping = self._keepalive_ping
            await self._client.connect()
            # A raw authenticated RPC avoids is_user_authorized swallowing RPC errors.
            self._startup_operation = "session_check"
            token = OPERATION.set("session_check")
            try:
                await self._client(functions.updates.GetStateRequest())
            finally:
                OPERATION.reset(token)
            self.ready = True
            return self
        except RpcRejected as exc:
            if exc.result.get("status") == "rate_limited" and "error" not in exc.result:
                # A local quota rejection is not a broken MCP transport. Keep
                # SQLite/auditing/session ownership alive, but stop Telegram.
                try:
                    if self.executor:
                        await self.executor.close()
                    if self._client:
                        await self._client.disconnect()
                except BaseException:
                    await self.close()
                    raise
                self._startup_limited = True
                logging.getLogger(__name__).warning(
                    "Telegram startup deferred by local rate limit; MCP/cache remain available. Reload after %s",
                    exc.result.get("retry_at"))
                return self
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
            self._scheduling_keepalive = True
            try:
                MTProtoSender._keepalive_ping(self._client._sender, ping_id)
            finally:
                self._scheduling_keepalive = False

    async def _recover_keepalive(self):
        """Reconnect transport only; the executor holds the queue and 120-second deadline."""
        client, sender = self._client, self._client._sender
        logger = logging.getLogger("telegram_mcp.keepalive")
        # Cancel both loops before yielding; no update RPC may enter during recovery.
        for task in (client._updates_handle, client._keepalive_handle):
            if task is not None:
                task.cancel()
        for attempt in range(1, 6):
            logger.warning("Keepalive transport recovery attempt %d/5", attempt)
            await client._disconnect()  # Telethon disconnects without closing the session.
            if sender._disconnected.done() and not sender._disconnected.cancelled():
                sender._disconnected.exception()  # Observe the already-handled disconnect before connect replaces it.
            if not sender.auth_key or self.state.halted():
                return False
            # Telethon's automatic reconnect replays pending states and calls get_me.
            # Use transport connect instead, discarding the failed ping and old housekeeping.
            sender._send_queue._deque.clear()
            sender._pending_ack.clear()
            sender._state.reset()
            sender._ping = None
            sender._reconnecting = False
            connection = client._connection(
                client.session.server_address, client.session.port, client.session.dc_id,
                loggers=client._log, proxy=client._proxy, local_addr=client._local_addr)
            try:
                # connection_retries=0 gives one Telethon connection attempt per call.
                await asyncio.wait_for(sender.connect(connection), timeout=20)
            except (OSError, asyncio.TimeoutError) as exc:
                logger.warning("Keepalive transport recovery attempt %d failed: %s", attempt, type(exc).__name__)
                if attempt < 5:
                    await asyncio.sleep(5)
            else:
                if not sender._transport_connected() or self.state.halted() or self.executor.closed:
                    return False
                loop = asyncio.get_running_loop()
                client._updates_handle = loop.create_task(client._update_loop())
                client._keepalive_handle = loop.create_task(client._keepalive_loop())
                logger.warning("Keepalive transport recovery succeeded on attempt %d", attempt)
                return True
        return False

    def _intercept(self, request, ordered=False):
        """Keep Telethon's ping marker consistent with local pre-dispatch rejection."""
        future = self.executor.intercept(request, ordered=ordered,
                                         background_keepalive=self._scheduling_keepalive)
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
            if self._startup_limited:
                if self.state.halted():
                    return {"status": "halted"}
                wait = self.state.wait(self._startup_operation)
                if self.state.halted():
                    return {"status": "halted"}
                return {**rate_limited_result(wait, self.state.clock()),
                        "status": "rate_limited" if wait > 0 else "unavailable",
                        "reason": "startup_rate_limited", "manual_reload_required": True,
                        "hint": "Run /reload-mcp after retry_at to complete Telegram startup. No automatic connection retry."}
            return {"status": "unavailable"}
        if self.executor.recovering:
            return {"status": "halted" if self.state.halted() else "unavailable"}
        # No callbacks, iterators, batch requests, or automatic pagination.
        if not isinstance(request, TLRequest):
            return {"status": "invalid_request"}
        token = OPERATION.set(operation)
        try:
            data = await self._client(request)
            if operation in {"list_dialogs", "search_chat", "get_message", "get_replies", "read_around"} and getattr(self, "cache", None) is not None:
                messages = () if operation == "list_dialogs" else getattr(data, "messages", ())
                thread = (utils.get_peer_id(request.peer), request.msg_id) if isinstance(request, functions.messages.GetRepliesRequest) else None
                self.cache.save(messages, [*getattr(data, "chats", ()), *getattr(data, "users", ())], thread=thread)
            return {"status": "ok", "data": data}
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
                    try:
                        if self.cache:
                            self.cache.close()
                            self.cache = None
                    finally:
                        self.state.close()
                        self.state = None
            finally:
                self.lock.__exit__(None, None, None)

    async def __aexit__(self, *args):
        await self.close()
