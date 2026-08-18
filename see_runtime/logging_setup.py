"""Runtime logging setup, stdout/stderr redirection to the log file, the
single-instance mutex, and shared-secret resolution. See
`doc/logging-runtime-state.md`.
"""

import logging
import os
import sys
import threading
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Tuple

from see_runtime.constants import APP_RUNTIME_DIR, SHARED_SECRET_ENV_VAR, SINGLE_INSTANCE_MUTEX_NAME
from see_runtime.runtime_paths import build_runtime_log_path, normalize_instance_id, runtime_secret_path

LOGGER = logging.getLogger("SuperEagleEye")

_SINGLE_INSTANCE_MUTEX = None


class _LoggerStream:
    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""

    def write(self, message: str) -> int:
        if not message:
            return 0
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self.logger.log(self.level, line, extra={"console": True})
        return len(message)

    def flush(self) -> None:
        line = self._buffer.rstrip("\r")
        self._buffer = ""
        if line:
            self.logger.log(self.level, line, extra={"console": True})


class _ConsoleVisibilityFilter(logging.Filter):
    """Console handler filter.

    Only records explicitly marked ``console=True`` (interactive CLI output
    via `_LoggerStream`, or a `LOGGER.*` call tagged `extra={"console": True}`)
    reach the console. Everything else still reaches the file handler, which
    has no filter attached. A small set of legacy prefixes keep their
    original "print once per runtime" behavior for backward compatibility.
    """

    def __init__(self, once_prefixes: Tuple[str, ...]):
        super().__init__()
        self.once_prefixes = once_prefixes
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "console", False):
            return True
        message = record.getMessage()
        key = next((prefix for prefix in self.once_prefixes if message.startswith(prefix)), None)
        if key is None:
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


def configure_runtime_logging(instance_id: str, grpc_port: int) -> Path:
    log_path = build_runtime_log_path(instance_id, grpc_port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = LOGGER
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
        delay=False,
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.__stdout__)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(
        _ConsoleVisibilityFilter(
            (
                "grpc_connection_state_changed",
                "grpc_heartbeat_request",
                "grpc_heartbeat_response",
                "grpc_heartbeat client_id=",
            )
        )
    )
    logger.addHandler(console_handler)

    sys.stdout = _LoggerStream(logger, logging.INFO)
    sys.stderr = _LoggerStream(logger, logging.ERROR)
    return log_path


def acquire_single_instance_lock(instance_id: str = "default") -> bool:
    global _SINGLE_INSTANCE_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        mutex_name = f"{SINGLE_INSTANCE_MUTEX_NAME}_{normalize_instance_id(instance_id)}"
        mutex = kernel32.CreateMutexW(None, False, mutex_name)
        if not mutex:
            return True
        _SINGLE_INSTANCE_MUTEX = mutex
        return ctypes.get_last_error() != 183
    except Exception as exc:
        print(f"[SuperEagleEye] single-instance check failed: {exc}; continuing")
        return True


def resolve_shared_secret(cli_value: str, legacy_secret_path: Path) -> str:
    def _persist_secret(secret_value: str) -> None:
        APP_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        try:
            runtime_secret_path().write_text(secret_value, encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("shared_secret_persist_failed path=%s error=%s", runtime_secret_path(), exc)

    if cli_value and cli_value.strip():
        secret = cli_value.strip()
        _persist_secret(secret)
        return secret

    env_value = os.environ.get(SHARED_SECRET_ENV_VAR, "").strip()
    if env_value:
        _persist_secret(env_value)
        return env_value

    secret_path = runtime_secret_path()
    if secret_path.exists():
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        except OSError as exc:
            LOGGER.warning("shared_secret_read_failed path=%s error=%s", secret_path, exc)

    if legacy_secret_path.exists():
        try:
            legacy = legacy_secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            LOGGER.warning("shared_secret_legacy_read_failed path=%s error=%s", legacy_secret_path, exc)
            legacy = ""
        if legacy:
            _persist_secret(legacy)
            return legacy

    generated = uuid.uuid4().hex + uuid.uuid4().hex
    _persist_secret(generated)
    return generated
