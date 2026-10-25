"""Single sender queue, persistent dispatch accounting, and fail-closed limits."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import math
import json
import uuid
from pathlib import Path
import re
import sqlite3
import time

import yaml
from telethon import errors, functions
from telethon.tl.tlobject import TLRequest

OPERATION = ContextVar("telegram_operation", default="internal")
OPERATION_ID = ContextVar("telegram_operation_id", default=None)
EXPECTED_ERRORS = {
    "ChannelPrivateError": "inaccessible",
    "ChannelInvalidError": "inaccessible",
    "ChatAdminRequiredError": "inaccessible",
    "ChatIdInvalidError": "inaccessible",
    "PeerIdInvalidError": "inaccessible",
    "UserBannedInChannelError": "inaccessible",
    "MessageIdInvalidError": "missing",
    "MsgIdInvalidError": "missing",
    "TopicIdInvalidError": "missing",
}
WINDOWS = {"minute": 60, "hour": 3600, "day": 86400}


def load_limits(path):
    with open(path) as file:
        config = yaml.safe_load(file)
    if "max_wait_seconds" in config:
        raise ValueError("max_wait_seconds is no longer supported; use auto_waiting_max")
    def number(value, minimum=0):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
            raise ValueError("Invalid rate limit configuration")
    for key in ("auto_waiting_max", "rpc_timeout_seconds", "queue_capacity"):
        number(config[key], 0 if key == "auto_waiting_max" else 1)
    if not isinstance(config["queue_capacity"], int):
        raise ValueError("Queue capacity must be an integer")
    limits = config["limits"]
    for group in [limits["keepalive"], limits["global"], limits["default"], *limits["operations"].values()]:
        for window in WINDOWS:
            number(group[window], 1)
            if not isinstance(group[window], int):
                raise ValueError("Window limits must be integers")
    number(config["cooldown"]["window_seconds"], 1)
    previous_count = previous_seconds = 0
    for tier in config["cooldown"]["tiers"]:
        number(tier["requests"], 1)
        number(tier["seconds"])
        if not isinstance(tier["requests"], int) or tier["requests"] <= previous_count or tier["seconds"] < previous_seconds:
            raise ValueError("Cooldown tiers must be progressive")
        previous_count, previous_seconds = tier["requests"], tier["seconds"]
    return config


class RpcState:
    def __init__(self, path: Path, config, clock=time.time):
        self.config, self.clock = config, clock
        self.failed = False
        self.halt_path = path.with_suffix(path.suffix + ".halted")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK(id=1), halted INTEGER NOT NULL,
                reason TEXT, cooldown REAL NOT NULL, flood REAL NOT NULL, last_time REAL NOT NULL);
            INSERT OR IGNORE INTO state VALUES (1,0,NULL,0,0,0);
            CREATE TABLE IF NOT EXISTS rpc_audit (
                id INTEGER PRIMARY KEY, operation TEXT NOT NULL, rpc TEXT NOT NULL,
                queued_at REAL NOT NULL, dispatched_at REAL NOT NULL,
                wait_seconds REAL NOT NULL, completed_at REAL, outcome TEXT NOT NULL,
                error TEXT);
            CREATE INDEX IF NOT EXISTS rpc_time ON rpc_audit(dispatched_at);
        ''')
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(rpc_audit)")}
        if "operation_id" not in columns:
            self.db.execute("ALTER TABLE rpc_audit ADD COLUMN operation_id TEXT")
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS rpc_operation_id ON rpc_audit(operation_id);
            CREATE TABLE IF NOT EXISTS tool_audit (
                operation_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                parameters TEXT NOT NULL, started_at REAL NOT NULL, completed_at REAL,
                wait_seconds REAL NOT NULL DEFAULT 0,
                execution_seconds REAL NOT NULL DEFAULT 0, rpc_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, error TEXT);
        """)
        tool_columns = {row[1] for row in self.db.execute("PRAGMA table_info(tool_audit)")}
        if "rejected_wait_seconds" not in tool_columns:
            self.db.execute("ALTER TABLE tool_audit ADD COLUMN rejected_wait_seconds REAL NOT NULL DEFAULT 0")
        if not self.halted() and (
            self.db.execute("SELECT 1 FROM rpc_audit WHERE outcome='in_flight'").fetchone()
            or self.db.execute("SELECT 1 FROM tool_audit WHERE status='in_flight'").fetchone()
        ):
            self.halt("uncertain_previous_operation")

    def halt(self, reason):
        # Latch first, including when both persistence mechanisms are unavailable.
        import os
        self.failed = True
        marker_saved = False
        try:
            fd = os.open(self.halt_path, os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.fsync(fd)
                marker_saved = True
            finally:
                os.close(fd)
        except OSError:
            pass
        try:
            with self.db:
                self.db.execute("UPDATE state SET halted=1, reason=? WHERE id=1", (reason,))
        except sqlite3.Error:
            if not marker_saved:
                raise RuntimeError("HALTED in memory; durable storage unavailable") from None

    def begin_tool(self, operation, parameters):
        identifier = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                "INSERT INTO tool_audit(operation_id,operation,parameters,started_at,status) VALUES(?,?,?,?,'in_flight')",
                (identifier, operation, json.dumps(parameters, sort_keys=True), self.clock()),
            )
        return identifier

    def add_rejected_wait(self, identifier, seconds):
        if identifier:
            with self.db:
                self.db.execute("UPDATE tool_audit SET rejected_wait_seconds=rejected_wait_seconds+? WHERE operation_id=?", (seconds, identifier))

    def finish_tool(self, identifier, status, error=None):
        now = self.clock()
        count, wait = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(wait_seconds),0) FROM rpc_audit WHERE operation_id=?", (identifier,),
        ).fetchone()
        extra_wait = self.db.execute("SELECT rejected_wait_seconds FROM tool_audit WHERE operation_id=?", (identifier,)).fetchone()[0]
        wait += extra_wait
        with self.db:
            self.db.execute(
                "UPDATE tool_audit SET completed_at=?,wait_seconds=?,execution_seconds=MAX(0,?-started_at-?),rpc_count=?,status=?,error=? WHERE operation_id=?",
                (now, wait, now, wait, count, status, error, identifier),
            )

    def inspect(self):
        reason, cooldown, flood = self.db.execute("SELECT reason,cooldown,flood FROM state").fetchone()
        return {"status": "HALTED" if self.halted() else "READY", "reason": reason,
                "cooldown_until": cooldown, "flood_until": flood}

    def recover(self):
        # Retain rate history and all cooldown/FloodWait deadlines.
        with self.db:
            self.db.execute("UPDATE rpc_audit SET outcome='uncertain' WHERE outcome='in_flight'")
            self.db.execute("UPDATE state SET halted=0, reason=NULL WHERE id=1")
            self.db.execute("UPDATE tool_audit SET status='uncertain' WHERE status='in_flight'")
        self.halt_path.unlink(missing_ok=True)
        self.failed = False

    def halted(self):
        return self.failed or self.halt_path.exists() or bool(self.db.execute("SELECT halted FROM state WHERE id=1").fetchone()[0])

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            self.halt("invalid_clock")
            raise ValueError("Clock must be finite")
        return now

    def wait(self, operation):
        now = self._now()
        with self.db:
            return self._wait_at(operation, now)

    def _wait_at(self, operation, now):
        cooldown, flood, last_time = self.db.execute("SELECT cooldown,flood,last_time FROM state").fetchone()
        if now < last_time:
            self.halt("clock_moved_backwards")
        self.db.execute("UPDATE state SET last_time=MAX(last_time,?)", (now,))
        due = max(now, cooldown, flood)
        limits = self.config["limits"]
        groups = (("keepalive", limits["keepalive"]),) if operation == "keepalive" else (
            (None, limits["global"]), (operation, limits["operations"].get(operation, limits["default"])))
        for name, group in groups:
            for window, seconds in WINDOWS.items():
                sql = "SELECT dispatched_at FROM rpc_audit WHERE dispatched_at>?"
                args = [now - seconds]
                if name is not None:
                    sql += " AND operation=?"
                    args.append(name)
                else:
                    sql += " AND operation!='keepalive'"
                sql += " ORDER BY dispatched_at DESC LIMIT 1 OFFSET ?"
                args.append(group[window] - 1)
                row = self.db.execute(sql, args).fetchone()
                if row:
                    due = max(due, row[0] + seconds)
        return max(0, due - now)

    def dispatch(self, operation, rpc, queued, operation_id=None):
        now = self._now()
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            wait = self._wait_at(operation, now)
            if self.halted():
                raise RpcRejected({"status": "halted"})
            if wait > 0:
                raise RpcRejected(rate_limited_result(wait, now))
            cursor = self.db.execute(
                "INSERT INTO rpc_audit(operation,rpc,queued_at,dispatched_at,wait_seconds,outcome,operation_id) VALUES(?,?,?,?,?,'in_flight',?)",
                (operation, rpc, queued, now, max(0, now - queued), operation_id),
            )
            cooldown = self.config["cooldown"]
            count = self.db.execute("SELECT COUNT(*) FROM rpc_audit WHERE dispatched_at>?", (now - cooldown["window_seconds"],)).fetchone()[0]
            delay = max((tier["seconds"] for tier in cooldown["tiers"] if count >= tier["requests"]), default=0)
            self.db.execute("UPDATE state SET cooldown=MAX(cooldown,?),last_time=?", (now + delay, now))
        return cursor.lastrowid

    def preserve_flood(self, seconds):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Invalid FloodWait duration")
        now = self._now()
        with self.db:
            self.db.execute("UPDATE state SET flood=MAX(flood,?)", (now + seconds,))

    def finish(self, identifier, outcome, error=None, flood_seconds=0):
        now = self.clock()
        with self.db:
            self.db.execute("UPDATE rpc_audit SET completed_at=?,outcome=?,error=? WHERE id=?", (now, outcome, error, identifier))
            if flood_seconds:
                self.db.execute("UPDATE state SET flood=MAX(flood,?)", (now + flood_seconds,))

    def close(self):
        self.db.close()


def rate_limited_result(wait, now):
    return {"status": "rate_limited", "estimated_wait_seconds": wait,
            "retry_after": wait,
            "retry_at": datetime.fromtimestamp(now + wait, timezone.utc).isoformat()}


class RpcRejected(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result["status"])


class RpcExecutor:
    def __init__(self, state, send, sleep=asyncio.sleep, recover_keepalive=None):
        self.state, self.send, self.sleep = state, send, sleep
        self.recover_keepalive = recover_keepalive
        self.recovering = False
        self.queue = asyncio.Queue(maxsize=state.config["queue_capacity"])
        self.worker = None
        self.pending = 0
        self.closed = False
        self.failed = False
        self.active_future = None

    def _caller_done(self, future):
        if future.cancelled():
            if future is self.active_future:
                self._stop("in_flight_cancellation")
        else:
            # Internal fire-and-forget calls (for example keepalive) may not await it.
            future.exception()

    def _drain(self):
        while not self.queue.empty():
            *_, future = self.queue.get_nowait()
            if not future.done():
                future.set_exception(self.rejection("halted"))
            self.pending -= 1
            self.queue.task_done()

    def _worker_done(self, task):
        error = None if task.cancelled() else task.exception()
        if not self.closed or error is not None:
            self._stop("worker_terminated")
        self._drain()

    def _stop(self, reason):
        self.failed = True
        try:
            self.state.halt(reason)
        except Exception:
            pass  # In-memory latch remains set even if durable storage is unavailable.

    def _excessive_wait(self, queued, wait):
        budget = self.state.config["auto_waiting_max"]
        if budget == 0:
            # Admission has already rejected occupied queues. Never sleep for limits.
            return wait > 0
        return self.state.clock() - queued + wait > budget

    def rejection(self, status, wait=0):
        result = {"status": status}
        if status == "rate_limited":
            result.update(rate_limited_result(wait, self.state.clock()))
        return RpcRejected(result)

    def intercept(self, request, ordered=False, background_keepalive=False):
        """Match sender.send's Future contract; never enqueue directly to Telethon."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.add_done_callback(self._caller_done)
        try:
            if self.closed or self.failed or self.state.halted():
                raise self.rejection("halted")
            if self.recovering:
                raise self.rejection("unavailable")
            if not isinstance(request, TLRequest):
                raise ValueError("Exactly one Telegram request is required")
            inner = request
            while hasattr(inner, "query"):
                inner = inner.query
            if type(inner).__module__ == "telethon.tl.functions.auth":
                raise ValueError("Authentication RPCs are prohibited")
            operation = OPERATION.get()
            if type(request) is functions.PingRequest:
                operation = "keepalive"
            elif operation == "keepalive":
                raise ValueError("The keepalive operation is reserved for PingRequest")
            if not re.fullmatch(r"[a-z_]{1,64}", operation):
                raise ValueError("Invalid operation label")
            wait = self.state.wait(operation)
            # Reserve a full RPC timeout per predecessor; recheck actual limits at dispatch.
            wait += self.pending * self.state.config["rpc_timeout_seconds"]
            if self.state.halted():
                raise self.rejection("halted")
            if wait > self.state.config["auto_waiting_max"] or self.queue.full():
                raise self.rejection("rate_limited", wait)
            self.queue.put_nowait((request, ordered, operation, OPERATION_ID.get(), self.state.clock(),
                                   background_keepalive and type(request) is functions.PingRequest, future))
            self.pending += 1
            if self.worker is None:
                self.worker = loop.create_task(self._work())
                self.worker.add_done_callback(self._worker_done)
        except RpcRejected as exc:
            future.set_exception(exc)
        except Exception:
            self.failed = True
            self._stop("dispatch_admission_error")
            future.set_exception(self.rejection("halted"))
        return future

    async def _work(self):
        while True:
            request, ordered, operation, operation_id, queued, background_keepalive, future = await self.queue.get()
            identifier = None
            try:
                if future.cancelled():
                    continue
                if self.failed or self.state.halted() or self.closed:
                    raise self.rejection("halted")
                wait = self.state.wait(operation)
                if self.state.halted():
                    raise self.rejection("halted")
                if self._excessive_wait(queued, wait):
                    raise self.rejection("rate_limited", wait)
                while wait > 0:
                    await self.sleep(wait)
                    wait = self.state.wait(operation)
                    if self.state.halted():
                        raise self.rejection("halted")
                    if self._excessive_wait(queued, wait):
                        raise self.rejection("rate_limited", wait)
                if future.cancelled():
                    continue
                inner = request
                while isinstance(getattr(inner, "query", None), TLRequest):
                    inner = inner.query
                identifier = self.state.dispatch(operation, type(inner).__name__, queued, operation_id)
                self.active_future = future
                result = await asyncio.wait_for(self.send(request, ordered=ordered), self.state.config["rpc_timeout_seconds"])
                self.state.finish(identifier, "ok")
                if not future.done():
                    future.set_result(result)
            except RpcRejected as exc:
                try:
                    self.state.add_rejected_wait(operation_id, max(0, self.state.clock() - queued))
                except Exception:
                    self._stop("audit_failure")
                    exc = self.rejection("halted")
                if not future.done():
                    future.set_exception(exc)
            except (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.SlowModeWaitError, errors.FloodTestPhoneWaitError) as exc:
                try:
                    self.state.preserve_flood(exc.seconds)
                    self.state.finish(identifier, "flood_wait", type(exc).__name__)
                    result = self.rejection("rate_limited", max(exc.seconds, self.state.wait(operation)))
                    result.result["error"] = type(exc).__name__
                except Exception:
                    self.failed = True
                    self._stop("audit_failure")
                    result = self.rejection("halted")
                if not future.done():
                    future.set_exception(result)
            except errors.RPCError as exc:
                error = type(exc).__name__
                status = EXPECTED_ERRORS.get(error)
                if status is None:
                    self.failed = True
                    self._stop("critical_rpc_error")
                    status = "halted"
                try:
                    self.state.finish(identifier, status, error)
                except Exception:
                    self.failed = True
                    self._stop("audit_failure")
                    status = "halted"
                if not future.done():
                    future.set_exception(RpcRejected({"status": status, "error": error}))
            except BaseException as exc:
                recovery_failed = False
                failure_audited = False
                dispatch_error = type(exc).__name__
                if (background_keepalive and isinstance(exc, ConnectionError)
                        and identifier and self.recover_keepalive is not None
                        and not self.failed and not self.closed and not self.state.halted()):
                    self.recovering = True
                    try:
                        # Preserve the failed dispatch; recovery never replays it.
                        self.state.finish(identifier, "error", type(exc).__name__)
                        failure_audited = True
                        async with asyncio.timeout(120):
                            recovered = await self.recover_keepalive()
                        if recovered and not self.failed and not self.state.halted():
                            if not future.done():
                                future.set_exception(RpcRejected({"status": "unavailable", "error": type(exc).__name__}))
                            continue
                    except asyncio.CancelledError as cancelled:
                        exc = cancelled
                    except Exception:
                        pass  # Fail closed on timeout, audit failure, or unexpected recovery error.
                    finally:
                        self.recovering = False
                    recovery_failed = True
                self.failed = True
                try:
                    self._stop("keepalive_reconnect_failed" if recovery_failed else
                               "uncertain_dispatch" if identifier else "executor_error")
                    if identifier and not failure_audited:
                        try:
                            self.state.finish(identifier, "error", dispatch_error)
                        except Exception:
                            pass  # HALTED is already durable; do not strand queued callers.
                finally:
                    if not future.done():
                        rejection = self.rejection("halted")
                        rejection.result["error"] = type(exc).__name__
                        future.set_exception(rejection)
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
            finally:
                if self.active_future is future:
                    self.active_future = None
                self.pending -= 1
                self.queue.task_done()

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            except Exception:
                self._stop("worker_terminated")
        self._drain()
