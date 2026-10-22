"""Cooperating processes share a POSIX advisory lock, released by the kernel."""

import fcntl
import os
from pathlib import Path

from telegram_mcp.paths import SESSION_PATH


class SessionLock:
    def __init__(self, session_path: Path = SESSION_PATH):
        self.path = session_path.with_suffix(".session.lock")
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError("Telegram session is already owned by another process") from None
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
