"""Shared command error type and the lock-acquire-or-raise helper built on it.

Split out on its own because almost every other module (camera session,
camera manager, command router, gRPC service, CLI) needs `CommandError`
without needing anything else, so keeping it dependency-free avoids
circular imports.
"""

import threading
from contextlib import contextmanager


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@contextmanager
def acquired_lock(lock: threading.RLock, timeout_sec: float, busy_code: str, busy_message: str):
    acquired = lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise CommandError(busy_code, busy_message)
    try:
        yield
    finally:
        lock.release()
