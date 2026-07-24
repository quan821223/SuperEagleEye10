import argparse
import json
import os
import logging
from logging.handlers import TimedRotatingFileHandler
from contextlib import contextmanager
import numpy as np
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

# OpenCV's native (C++) logger writes straight to the OS stderr, bypassing
# Python's `logging`/`sys.stderr` entirely, so none of the console-filtering
# in `configure_runtime_logging()` can touch it. On machines where a
# configured device_index doesn't correspond to a real camera, every
# VideoCapture open attempt (including our own reconnect retries) prints raw
# "[ WARN:...] VIDEOIO(...): backend is generally available but can't be used
# to capture by index" / obsensor "Camera index out of range" lines straight
# to the console. Silence that native logger; our own LOGGER already records
# camera_open_failed/camera_open_retry for the same events.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

import cv2

try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except AttributeError:
    pass

# --- Optional native Windows camera control -------------------------------
#
# OpenCV's `VideoCapture.set()` on a live MSMF/DSHOW stream is unreliable for
# brightness/focus on many UVC drivers, and manual focus gets silently
# overridden while autofocus is on. DirectShow's IAMVideoProcAmp (brightness)
# and IAMCameraControl (focus) talk to the driver directly and are what
# native "camera settings" utilities use. There is no public typelib for
# these two interfaces (they predate typelib-based automation), so they are
# declared by hand via `comtypes` rather than generated — this also avoids
# comtypes.client's runtime typelib codegen/caching, which would be fragile
# in a frozen PyInstaller build. Everything here is best-effort: any failure
# (comtypes missing, device not matched, COM error) falls back to the
# existing OpenCV-based property path in `CameraSession`.
try:
    import ctypes as _ctypes
    from ctypes import HRESULT as _HRESULT, POINTER as _POINTER, c_long as _c_long, c_ulong as _c_ulong, c_ulonglong as _c_ulonglong, c_int as _c_int
    import comtypes
    from comtypes import GUID, COMMETHOD, IUnknown, IPersist, CoCreateInstance
    from comtypes.persist import IPropertyBag

    _DSHOW_CONTROL_AVAILABLE = True
except Exception:
    _DSHOW_CONTROL_AVAILABLE = False

if _DSHOW_CONTROL_AVAILABLE:
    _CLSID_SYSTEM_DEVICE_ENUM = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
    _CLSID_VIDEO_INPUT_DEVICE_CATEGORY = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")
    _IID_ICREATE_DEV_ENUM = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")

    DSHOW_CAMERA_CONTROL_EXPOSURE = 4
    DSHOW_CAMERA_CONTROL_FOCUS = 6
    DSHOW_CAMERA_CONTROL_FLAGS_AUTO = 0x0001
    DSHOW_CAMERA_CONTROL_FLAGS_MANUAL = 0x0002

    class _IBaseFilter(IUnknown):
        # Only ever used as a QueryInterface waypoint to IAMVideoProcAmp /
        # IAMCameraControl; its own methods are intentionally not declared
        # since nothing here calls them.
        _iid_ = GUID("{56A86895-0AD4-11CE-B03A-0020AF0BA770}")

    class _IEnumMoniker(IUnknown):
        _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "Next",
                      (["in"], _c_ulong, "celt"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "rgelt"),
                      (["out"], _POINTER(_c_ulong), "pceltFetched")),
        ]

    class _IPersistStream(IPersist):
        # IMoniker really derives IUnknown -> IPersist -> IPersistStream ->
        # IMoniker. Skipping these two intermediate interfaces shifts every
        # IMoniker vtable slot below (BindToObject/BindToStorage would land
        # on IPersist::GetClassID's slot instead), so they must be declared
        # even though nothing here calls them directly.
        _iid_ = GUID("{00000109-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "IsDirty"),
            COMMETHOD([], _HRESULT, "Load", (["in"], _POINTER(IUnknown), "pstm")),
            COMMETHOD([], _HRESULT, "Save", (["in"], _POINTER(IUnknown), "pstm"), (["in"], _c_int, "fClearDirty")),
            COMMETHOD([], _HRESULT, "GetSizeMax", (["out"], _POINTER(_c_ulonglong), "pcbSize")),
        ]

    class _IMoniker(_IPersistStream):
        _iid_ = GUID("{0000000f-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "BindToObject",
                      (["in"], _POINTER(IUnknown), "pbc"),
                      (["in"], _POINTER(IUnknown), "pmkToLeft"),
                      (["in"], _POINTER(GUID), "riid"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "ppvResult")),
            COMMETHOD([], _HRESULT, "BindToStorage",
                      (["in"], _POINTER(IUnknown), "pbc"),
                      (["in"], _POINTER(IUnknown), "pmkToLeft"),
                      (["in"], _POINTER(GUID), "riid"),
                      (["out"], _POINTER(_POINTER(IUnknown)), "ppvObj")),
        ]

    class _ICreateDevEnum(IUnknown):
        _iid_ = _IID_ICREATE_DEV_ENUM
        _methods_ = [
            COMMETHOD([], _HRESULT, "CreateClassEnumerator",
                      (["in"], _POINTER(GUID), "clsidDeviceClass"),
                      (["out"], _POINTER(_POINTER(_IEnumMoniker)), "ppEnumMoniker"),
                      (["in"], _c_ulong, "dwFlags")),
        ]

    class _IAMCameraControl(IUnknown):
        _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
        _methods_ = [
            COMMETHOD([], _HRESULT, "GetRange",
                      (["in"], _c_long, "Property"),
                      (["out"], _POINTER(_c_long), "pMin"),
                      (["out"], _POINTER(_c_long), "pMax"),
                      (["out"], _POINTER(_c_long), "pSteppingDelta"),
                      (["out"], _POINTER(_c_long), "pDefault"),
                      (["out"], _POINTER(_c_long), "pCapsFlags")),
            COMMETHOD([], _HRESULT, "Set",
                      (["in"], _c_long, "Property"), (["in"], _c_long, "lValue"), (["in"], _c_long, "Flags")),
            COMMETHOD([], _HRESULT, "Get",
                      (["in"], _c_long, "Property"), (["out"], _POINTER(_c_long), "lValue"), (["out"], _POINTER(_c_long), "Flags")),
        ]

    # CAMERA_PROP_SPECS name -> (COM interface, property id, manual flag, auto flag)
    #
    # "brightness" is deliberately mapped to IAMCameraControl::Exposure, not
    # IAMVideoProcAmp::Brightness. Measured on real hardware (Microsoft LifeCam
    # Cinema): Set()/Get() on VideoProcAmp Brightness round-trips fine (the
    # driver echoes back whatever value you write) but never changes a single
    # captured pixel — mean frame brightness stayed ~25-28 across the full
    # 30-255 range. GetRange() also reported capsFlags=0 (neither Auto nor
    # Manual advertised), consistent with the property being an unwired
    # legacy shim on this driver. CameraControl Exposure, by contrast, swung
    # mean frame brightness from ~9 (min) to ~229 (max) - a real, dramatic
    # effect - so it is used as the actual "brightness" control instead. This
    # is a known quirk on many UVC webcams, not specific to one camera model.
    _DSHOW_PROPERTY_MAP = {
        "brightness": (_IAMCameraControl, DSHOW_CAMERA_CONTROL_EXPOSURE, DSHOW_CAMERA_CONTROL_FLAGS_MANUAL, DSHOW_CAMERA_CONTROL_FLAGS_AUTO),
        "focus": (_IAMCameraControl, DSHOW_CAMERA_CONTROL_FOCUS, DSHOW_CAMERA_CONTROL_FLAGS_MANUAL, DSHOW_CAMERA_CONTROL_FLAGS_AUTO),
    }

    def _dshow_create_bind_ctx():
        ptr = _ctypes.c_void_p()
        hr = _ctypes.windll.ole32.CreateBindCtx(0, _ctypes.byref(ptr))
        if hr != 0 or not ptr:
            raise OSError(f"CreateBindCtx failed hr={hr}")
        return _ctypes.cast(ptr, _POINTER(IUnknown))

    def _dshow_moniker_name(moniker, bind_ctx) -> str:
        raw = moniker.BindToStorage(bind_ctx, None, IPropertyBag._iid_)
        return raw.QueryInterface(IPropertyBag).Read("FriendlyName", pErrorLog=None)

    def find_dshow_video_filter(descriptor_friendly_name: str, fallback_index: int):
        """Enumerate DirectShow video-input monikers and bind the one matching
        `descriptor_friendly_name` (preferred) or `fallback_index`
        (positional fallback, since MSMF/DSHOW enumeration order can differ
        from the app's own device_index in rare cases) to IBaseFilter.
        Caller's thread must have called `comtypes.CoInitialize()` first."""
        bind_ctx = _dshow_create_bind_ctx()
        dev_enum = CoCreateInstance(_CLSID_SYSTEM_DEVICE_ENUM, interface=_ICreateDevEnum)
        enum_moniker = dev_enum.CreateClassEnumerator(_CLSID_VIDEO_INPUT_DEVICE_CATEGORY, 0)
        if not enum_moniker:
            return None
        candidates = []
        while True:
            moniker, fetched = enum_moniker.Next(1)
            if fetched == 0 or moniker is None:
                break
            moniker = moniker.QueryInterface(_IMoniker)
            try:
                name = _dshow_moniker_name(moniker, bind_ctx)
            except Exception:
                name = ""
            candidates.append((moniker, name))
        target_moniker = None
        if descriptor_friendly_name:
            for moniker, name in candidates:
                if name and name.strip().lower() == descriptor_friendly_name.strip().lower():
                    target_moniker = moniker
                    break
        if target_moniker is None and 0 <= fallback_index < len(candidates):
            target_moniker = candidates[fallback_index][0]
        if target_moniker is None:
            return None
        return target_moniker.BindToObject(bind_ctx, None, _IBaseFilter._iid_).QueryInterface(_IBaseFilter)

    class DirectShowPropertyController:
        """Per-device wrapper around IAMCameraControl / IAMVideoProcAmp.

        Must only be used from the thread that created it (COM STA rules) -
        in `CameraSession` that is always the preview thread."""

        def __init__(self, base_filter):
            self._base_filter = base_filter
            self._interfaces: Dict[type, object] = {}

        def _interface_for(self, name: str):
            interface_cls = _DSHOW_PROPERTY_MAP[name][0]
            if interface_cls not in self._interfaces:
                self._interfaces[interface_cls] = self._base_filter.QueryInterface(interface_cls)
            return self._interfaces[interface_cls]

        def get_range(self, name: str):
            prop_id = _DSHOW_PROPERTY_MAP[name][1]
            return self._interface_for(name).GetRange(prop_id)

        def set_manual(self, name: str, value: int) -> float:
            _, prop_id, manual_flag, _ = _DSHOW_PROPERTY_MAP[name]
            iface = self._interface_for(name)
            iface.Set(prop_id, int(value), manual_flag)
            reported, _flags = iface.Get(prop_id)
            return float(reported)

        def set_auto(self, name: str) -> None:
            _, prop_id, _, auto_flag = _DSHOW_PROPERTY_MAP[name]
            iface = self._interface_for(name)
            current, _flags = iface.Get(prop_id)
            iface.Set(prop_id, current, auto_flag)

ACK_BYTES = bytes([0xC3, 0x0D, 0x0A])
FRAME_STX = 0xC3
FRAME_VERSION = 0x01
FRAME_ETX = 0x0A
HEARTBEAT_TIMEOUT_SEC = 3.0
DEFAULT_GRPC_PORT = 50051
GRPC_PORT_MIN = 50051
GRPC_PORT_MAX = 50060
GRPC_PORT = DEFAULT_GRPC_PORT
PROTO_FILE_NAME = "SC_communication_gRPC.proto"
CAMERA_MAP_FILE_NAME = "camera_map.json"
VERSION_FILE_NAME = "version.json"
SHARED_SECRET_ENV_VAR = "SEE_SUPER_EAGLE_EYE_SECRET"
SHARED_SECRET_FILE_NAME = "SuperEagleEye.secret"
APP_RUNTIME_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SEE" / "runtime"
LEGACY_SECRET_PATH = Path(__file__).resolve().parent / SHARED_SECRET_FILE_NAME
CRASH_LOG_FILE_NAME = "SuperEagleEye.crash.log"
SINGLE_INSTANCE_MUTEX_NAME = "Local\\SuperEagleEye_SEE10_Runtime"
_SINGLE_INSTANCE_MUTEX = None
LOGGER = logging.getLogger("SuperEagleEye")

QUERY_MSG_TYPES = {
    "GET_STATUS": 0x11,
    "LIST_CAMERAS": 0x12,
    "LIST_DEVICES": 0x15,
    "GET_CAMERA_CONFIG": 0x13,
    "GET_RECORD_STATE": 0x14,
    "GET_RUNTIME_INFO": 0x16,
}

RESULT_CODES = {
    "OK": 0x00,
    "INVALID_COMMAND": 0x01,
    "INVALID_ARGUMENT": 0x02,
    "CAMERA_NOT_FOUND": 0x03,
    "CAMERA_BUSY": 0x04,
    "INTERNAL_ERROR": 0x05,
    "AUTH_FAILED": 0x06,
    "NO_FRAME_YET": 0x07,
}

MANAGER_LOCK_TIMEOUT_SEC = 0.25
SESSION_LOCK_TIMEOUT_SEC = 0.25


def runtime_secret_path() -> Path:
    return APP_RUNTIME_DIR / SHARED_SECRET_FILE_NAME


def normalize_instance_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return normalized or "default"


def parse_device_indexes(value: str) -> Optional[List[int]]:
    raw = str(value or "").strip()
    if not raw:
        return None
    indexes = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        indexes.append(coerce_int(item, "device_indexes", min_value=0))
    return sorted(set(indexes))


def format_runtime_title(instance_id: str, grpc_port: int, device_indexes: Optional[List[int]], save_path: Path) -> str:
    device_label = ",".join(str(index) for index in device_indexes) if device_indexes is not None else "all"
    return f"instance={instance_id} grpc_port={grpc_port} device_indexes={device_label} save_path={save_path}"


def format_runtime_file_tag(grpc_port: int) -> str:
    return f"port{grpc_port}"


def get_runtime_log_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent / "logs"


@contextmanager
def acquired_lock(lock: threading.RLock, timeout_sec: float, busy_code: str, busy_message: str):
    acquired = lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise CommandError(busy_code, busy_message)
    try:
        yield
    finally:
        lock.release()


def _decode_shell_output(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8", "cp950"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def build_runtime_log_path(instance_id: str, grpc_port: int) -> Path:
    safe_instance_id = normalize_instance_id(instance_id)
    return get_runtime_log_dir() / f"SuperEagleEye_{safe_instance_id}_grpc{grpc_port}.log"


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


def resolve_shared_secret(cli_value: str) -> str:
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

    if LEGACY_SECRET_PATH.exists():
        try:
            legacy = LEGACY_SECRET_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            LOGGER.warning("shared_secret_legacy_read_failed path=%s error=%s", LEGACY_SECRET_PATH, exc)
            legacy = ""
        if legacy:
            _persist_secret(legacy)
            return legacy

    generated = uuid.uuid4().hex + uuid.uuid4().hex
    _persist_secret(generated)
    return generated


def ensure_proto_generated(base_dir: Path) -> None:
    if getattr(sys, "frozen", False):
        return

    proto_path = base_dir / PROTO_FILE_NAME
    pb2_path = base_dir / "SC_communication_gRPC_pb2.py"
    pb2_grpc_path = base_dir / "SC_communication_gRPC_pb2_grpc.py"
    pb2_files_exist = pb2_path.exists() and pb2_grpc_path.exists()
    if pb2_files_exist and (
        pb2_path.stat().st_mtime >= proto_path.stat().st_mtime
        and pb2_grpc_path.stat().st_mtime >= proto_path.stat().st_mtime
    ):
        return

    try:
        import grpc_tools.protoc  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Missing grpc_tools. Install with: python -m pip install grpcio grpcio-tools protobuf"
        ) from exc

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{base_dir}",
        f"--python_out={base_dir}",
        f"--grpc_python_out={base_dir}",
        str(proto_path),
    ]
    # Do not let the host console code page crash proto generation on Windows.
    result = subprocess.run(cmd, capture_output=True, check=False)
    stdout = _decode_shell_output(result.stdout).strip()
    stderr = _decode_shell_output(result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"Failed to generate gRPC Python files: {stderr or stdout}")


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_runtime_version(base_dir: Path) -> Dict[str, str]:
    version_path = base_dir / VERSION_FILE_NAME
    fallback = {
        "runtime_name": "SEE_1.0",
        "version": "unknown",
        "min_see_version": "unknown",
        "min_supercarter_version": "unknown",
    }
    if not version_path.exists():
        return fallback
    try:
        payload = json.loads(version_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return fallback
        return {
            "runtime_name": str(payload.get("runtime_name", fallback["runtime_name"])),
            "version": str(payload.get("version", fallback["version"])),
            "min_see_version": str(payload.get("min_see_version", fallback["min_see_version"])),
            "min_supercarter_version": str(payload.get("min_supercarter_version", fallback["min_supercarter_version"])),
        }
    except Exception:
        return fallback


def get_crash_log_paths() -> List[Path]:
    candidates: List[Path] = []
    for directory in (APP_RUNTIME_DIR, get_base_dir()):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        candidates.append(directory / CRASH_LOG_FILE_NAME)
    return candidates


def write_crash_log(exc: BaseException) -> List[Path]:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = [
        f"[{timestamp}] SuperEagleEye crashed",
        f"python: {sys.version}",
        f"executable: {sys.executable}",
        f"base_dir: {get_base_dir()}",
        "",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip(),
        "",
    ]
    content = "\n".join(payload)
    written_paths: List[Path] = []
    for path in get_crash_log_paths():
        try:
            path.write_text(content, encoding="utf-8")
            written_paths.append(path)
        except OSError:
            continue
    return written_paths


def pause_on_fatal_error(log_paths: List[Path]) -> None:
    if not getattr(sys, "frozen", False):
        return

    print("SuperEagleEye encountered a fatal error.")
    for path in log_paths:
        print(f"Crash log written to: {path}")
    print("Press Enter to close this window...")
    try:
        input()
    except EOFError:
        time.sleep(10)


BASE_DIR = get_base_dir()
ensure_proto_generated(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import grpc  # noqa: E402
from concurrent import futures  # noqa: E402
import SC_communication_gRPC_pb2 as pb2  # noqa: E402
import SC_communication_gRPC_pb2_grpc as pb2_grpc  # noqa: E402


@dataclass
class CameraConfig:
    width: int = 640
    height: int = 480
    fps: int = 20
    recording_duration: int = 60
    max_folder_size_gb: int = 10


@dataclass
class CameraDescriptor:
    device_index: int
    friendly_name: str
    is_external: bool
    device_id: str = ""
    pnp_device_id: str = ""
    location_information: str = ""
    manufacturer: str = ""
    connected: bool = True


class CommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_grpc_port(value) -> Tuple[int, bool]:
    try:
        port = int(str(value).strip())
    except Exception:
        return DEFAULT_GRPC_PORT, True
    if port < GRPC_PORT_MIN or port > GRPC_PORT_MAX:
        return DEFAULT_GRPC_PORT, True
    return port, False


def file_timestamp_with_millis() -> str:
    millis = int((time.time() % 1) * 1000)
    return f"{time.strftime('%y%m%d_%H%M%S')}_{millis:03d}"


class RecordingSession:
    def __init__(self, output_dir: Path, file_prefix: str, frame_size: Tuple[int, int], fps: int, duration_sec: int, file_tag: str = ""):
        self.output_dir = output_dir
        self.file_prefix = file_prefix
        self.file_tag = file_tag
        self.frame_size = frame_size
        self.fps = fps
        self.duration_sec = duration_sec
        self.writer = None
        self.started_at = 0.0
        self.segment_started_at = 0.0
        self.segment_index = 0
        self.lock = threading.Lock()
        self._open_writer()

    def _open_writer(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.segment_index += 1
        timestamp = file_timestamp_with_millis()
        file_tag = f"_{self.file_tag}" if self.file_tag else ""
        path = self.output_dir / f"{timestamp}{file_tag}_{self.file_prefix}_{self.segment_index}.mp4"
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(str(path), fourcc, self.fps, self.frame_size)
        if self.writer is None or not self.writer.isOpened():
            raise CommandError("INTERNAL_ERROR", f"Failed to open video writer for {path}")
        self.segment_started_at = time.time()
        if self.started_at == 0.0:
            self.started_at = self.segment_started_at

    def write(self, frame) -> None:
        with self.lock:
            if self.writer is None:
                return
            if time.time() - self.segment_started_at >= self.duration_sec:
                self.writer.release()
                self._open_writer()
            self.writer.write(frame)

    def stop(self) -> None:
        with self.lock:
            if self.writer is not None:
                self.writer.release()
                self.writer = None


class CameraSession:
    MAX_OPEN_RETRIES = 5
    READ_FAILURE_LOG_THRESHOLD = 3
    UNAVAILABLE_FRAME_TEXT = "NO SIGNAL"
    INITIAL_OPEN_WAIT_SEC = 1.5
    PROP_COMMIT_DEBOUNCE_SEC = 0.4
    CAMERA_PROP_SPECS = [
        {"name": "brightness", "cap_prop": cv2.CAP_PROP_BRIGHTNESS, "max": 100, "default": 50, "scale": 100.0, "offset": 0.0, "auto_disable_prop": None},
        {"name": "focus", "cap_prop": cv2.CAP_PROP_FOCUS, "max": 255, "default": 0, "scale": 1.0, "offset": 0.0, "auto_disable_prop": cv2.CAP_PROP_AUTOFOCUS},
    ]

    def __init__(
        self,
        camera_id: str,
        descriptor: CameraDescriptor,
        config: CameraConfig,
        output_root: Path,
        shutdown_callback: Optional[Callable[[], None]] = None,
        get_backend_index: Optional[Callable[[], int]] = None,
        request_backend_failover: Optional[Callable[[int], bool]] = None,
        runtime_title: str = "",
        file_tag: str = "",
    ):
        self.camera_id = camera_id
        self.descriptor = descriptor
        self.config = config
        self.output_root = output_root
        self.shutdown_callback = shutdown_callback
        self.get_backend_index = get_backend_index
        self.request_backend_failover = request_backend_failover
        self.capture = None
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.latest_frame = None
        self.recording: Optional[RecordingSession] = None
        self.opened = False
        self.runtime_title = runtime_title
        self.file_tag = file_tag
        self.window_name = f"SuperEagleEye::{camera_id}" + (f" [{runtime_title}]" if runtime_title else "")
        self.open_retry_count = 0
        self.open_retry_exhausted = False
        self.backend_index = 0
        self.active_backend_name = ""
        self.read_failure_count = 0
        self.reconnecting_logged = False
        self.initial_open_event = threading.Event()
        self.controls_enabled = False
        self.camera_prop_cache = {spec["name"]: int(spec["default"]) for spec in self.CAMERA_PROP_SPECS}
        self.camera_prop_reported: Dict[str, float] = {}
        self.camera_prop_ranges: Dict[str, Tuple[int, int]] = {}
        self.camera_prop_native_defaults: Dict[str, int] = {}
        self._camera_prop_last_applied = dict(self.camera_prop_cache)
        self._camera_prop_user_modified = False
        self._camera_prop_modified_names: Set[str] = set()
        self._camera_prop_pending_commit_at: Optional[float] = None
        self._capture_reopen_requested = False
        self._unavailable_logged = False
        self._dshow_control: Optional["DirectShowPropertyController"] = None
        self._dshow_available: Optional[bool] = None
        self._panel_pending_values: Dict[str, int] = {}
        self._panel_value_lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.initial_open_event.clear()
            self.thread = threading.Thread(target=self._run, name=f"camera-{self.camera_id}", daemon=True)
            self.thread.start()
        LOGGER.info(
            "camera_session_start camera_id=%s device_index=%s friendly_name=%s connected=%s",
            self.camera_id,
            self.descriptor.device_index,
            self.descriptor.friendly_name,
            self.descriptor.connected,
        )

    def wait_for_initial_open(self, timeout_sec: float = INITIAL_OPEN_WAIT_SEC) -> bool:
        return self.initial_open_event.wait(timeout_sec)

    @staticmethod
    def _backend_candidates_full():
        return [
            (cv2.CAP_MSMF, "CAP_MSMF"),
            (cv2.CAP_DSHOW, "CAP_DSHOW"),
        ]

    def _backend_candidates(self):
        candidates = self._backend_candidates_full()
        if self.get_backend_index is None:
            return candidates
        selected_index = max(0, min(self.get_backend_index(), len(candidates) - 1))
        return [candidates[selected_index]]

    def _advance_backend(self):
        current_index = self.get_backend_index() if self.get_backend_index is not None else self.backend_index
        if self.request_backend_failover is not None and self.request_backend_failover(current_index):
            next_index = self.get_backend_index() if self.get_backend_index is not None else current_index + 1
            next_backend_name = self._backend_candidates_full()[next_index][1]
            LOGGER.warning(
                "camera_backend_failover camera_id=%s next_backend=%s",
                self.camera_id,
                next_backend_name,
            )

    def _request_capture_reopen(self) -> None:
        """Ask the preview thread to release and reopen the capture on its next
        iteration, instead of mutating a live capture from another thread."""
        self._capture_reopen_requested = True

    def _open_capture(self):
        if self.descriptor.device_index < 0:
            raise CommandError("INVALID_ARGUMENT", f"device_index must be >= 0, got {self.descriptor.device_index}")

        backend_candidates = self._backend_candidates()
        for candidate_index, (backend, backend_name) in enumerate(backend_candidates):
            LOGGER.info(
                "camera_open_attempt camera_id=%s device_index=%s backend=%s",
                self.camera_id,
                self.descriptor.device_index,
                backend_name,
            )
            capture = cv2.VideoCapture(self.descriptor.device_index, backend) if backend is not None else cv2.VideoCapture(self.descriptor.device_index)
            if not capture.isOpened():
                capture.release()
                LOGGER.warning(
                    "camera_open_failed camera_id=%s device_index=%s backend=%s",
                    self.camera_id,
                    self.descriptor.device_index,
                    backend_name,
                )
                continue

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            capture.set(cv2.CAP_PROP_FPS, self.config.fps)
            if self.controls_enabled and self._camera_prop_user_modified:
                self._apply_camera_properties(capture, force=True)
            self.backend_index = self.get_backend_index() if self.get_backend_index is not None else candidate_index
            self.active_backend_name = backend_name
            if self.reconnecting_logged:
                LOGGER.info(
                    "camera_reopened camera_id=%s device_index=%s backend=%s",
                    self.camera_id,
                    self.descriptor.device_index,
                    backend_name,
                    extra={"console": True},
                )
            else:
                LOGGER.info(
                    "camera_opened camera_id=%s device_index=%s backend=%s",
                    self.camera_id,
                    self.descriptor.device_index,
                    backend_name,
                    extra={"console": True},
                )
            return capture

        self.backend_index = self.get_backend_index() if self.get_backend_index is not None else 0
        self.active_backend_name = ""
        LOGGER.error(
            "camera_open_exhausted camera_id=%s device_index=%s",
            self.camera_id,
            self.descriptor.device_index,
        )
        return cv2.VideoCapture()

    def _run(self) -> None:
        LOGGER.info(
            "camera_preview_thread_start camera_id=%s device_index=%s friendly_name=%s",
            self.camera_id,
            self.descriptor.device_index,
            self.descriptor.friendly_name,
        )
        try:
            while not self.stop_event.is_set():
                if not self.descriptor.connected or self.descriptor.device_index < 0:
                    if not self._unavailable_logged:
                        LOGGER.warning(
                            "camera_unavailable camera_id=%s device_index=%s connected=%s",
                            self.camera_id,
                            self.descriptor.device_index,
                            self.descriptor.connected,
                            extra={"console": True},
                        )
                        self._unavailable_logged = True
                    self._set_unavailable_state()
                    self._show_unavailable_frame()
                    time.sleep(0.2)
                    continue
                self._unavailable_logged = False

                if self._capture_reopen_requested:
                    if self.capture is not None:
                        self.capture.release()
                        self.capture = None
                        self.opened = False
                    self._capture_reopen_requested = False

                if self.capture is None or not self.capture.isOpened():
                    try:
                        self.capture = self._open_capture()
                    except CommandError as exc:
                        LOGGER.error(
                            "camera_open_rejected camera_id=%s code=%s message=%s",
                            self.camera_id,
                            exc.code,
                            exc.message,
                            extra={"console": True},
                        )
                        self.open_retry_exhausted = True
                        self.opened = False
                        break
                    except Exception as exc:
                        LOGGER.exception("camera_open_exception camera_id=%s", self.camera_id, extra={"console": True})
                        self.open_retry_exhausted = True
                        self.opened = False
                        break

                    self.opened = self.capture.isOpened()
                    if not self.opened:
                        self.open_retry_count += 1
                        LOGGER.warning(
                            "camera_open_retry camera_id=%s device_index=%s attempt=%s max_attempts=%s",
                            self.camera_id,
                            self.descriptor.device_index,
                            self.open_retry_count,
                            self.MAX_OPEN_RETRIES,
                        )
                        if self.open_retry_count >= self.MAX_OPEN_RETRIES:
                            LOGGER.error(
                                "camera_open_retry_exhausted camera_id=%s device_index=%s",
                                self.camera_id,
                                self.descriptor.device_index,
                            )
                            self.open_retry_count = 0
                        time.sleep(1.0)
                        continue

                    self.open_retry_count = 0
                    self.open_retry_exhausted = False
                    if self.reconnecting_logged:
                        LOGGER.info("camera_preview_recovered camera_id=%s", self.camera_id, extra={"console": True})
                    else:
                        LOGGER.info("camera_preview_opened camera_id=%s", self.camera_id, extra={"console": True})
                    self.read_failure_count = 0
                    self.reconnecting_logged = False
                    self.initial_open_event.set()
                ok, frame = self.capture.read()
                if not ok:
                    if self.stop_event.is_set():
                        break
                    self._set_unavailable_state()
                    self.read_failure_count += 1
                    if self.read_failure_count >= self.READ_FAILURE_LOG_THRESHOLD and not self.reconnecting_logged:
                        LOGGER.warning(
                            "camera_frame_read_failed camera_id=%s backend=%s read_failure_count=%s",
                            self.camera_id,
                            self.active_backend_name or "unknown",
                            self.read_failure_count,
                            extra={"console": True},
                        )
                        self.reconnecting_logged = True
                    if self.capture is not None:
                        self.capture.release()
                    self.capture = None
                    self._show_unavailable_frame()
                    self._advance_backend()
                    time.sleep(0.5)
                    continue

                frame = self._decorate(frame)
                self._sync_controls_with_camera()
                with self.lock:
                    self.latest_frame = frame.copy()
                    recording = self.recording
                if recording is not None:
                    recording.write(frame)

                try:
                    cv2.imshow(self.window_name, frame)
                    key = cv2.waitKey(1) & 0xFF
                except cv2.error as exc:
                    LOGGER.exception("camera_window_error camera_id=%s", self.camera_id, extra={"console": True})
                    self.stop_event.set()
                    break

                if key in (27, ord("q"), ord("Q")):
                    self.stop_event.set()
                    if self.shutdown_callback is not None:
                        threading.Thread(target=self.shutdown_callback, daemon=True).start()
                    break
        finally:
            self.initial_open_event.set()
            LOGGER.info("camera_preview_thread_stop camera_id=%s", self.camera_id)
            self._cleanup_capture()

    def _decorate(self, frame):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, f"{self.camera_id} {timestamp}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        if self.runtime_title:
            self._put_fitted_text(frame, self.runtime_title, (10, 58), 0.48, (0, 255, 255), 1)
        return frame

    @staticmethod
    def _fit_text_to_width(text: str, font, scale: float, thickness: int, max_width: int) -> str:
        if max_width <= 0:
            return ""
        if cv2.getTextSize(text, font, scale, thickness)[0][0] <= max_width:
            return text
        suffix = "..."
        low = 0
        high = len(text)
        while low < high:
            mid = (low + high + 1) // 2
            candidate = text[:mid].rstrip() + suffix
            if cv2.getTextSize(candidate, font, scale, thickness)[0][0] <= max_width:
                low = mid
            else:
                high = mid - 1
        return text[:low].rstrip() + suffix

    def _put_fitted_text(self, frame, text: str, origin: Tuple[int, int], scale: float, color: Tuple[int, int, int], thickness: int) -> None:
        font = cv2.FONT_HERSHEY_SIMPLEX
        max_width = max(0, frame.shape[1] - origin[0] - 10)
        fitted = self._fit_text_to_width(text, font, scale, thickness, max_width)
        cv2.putText(frame, fitted, origin, font, scale, color, thickness)

    def _set_unavailable_state(self) -> None:
        with self.lock:
            self.opened = False
            self.latest_frame = None
            if self.recording is not None:
                self.recording.stop()
                self.recording = None

    def _build_unavailable_frame(self):
        frame = np.zeros((self.config.height, self.config.width, 3), dtype=np.uint8)
        title = f"{self.camera_id} {self.UNAVAILABLE_FRAME_TEXT}"
        subtitle = self.descriptor.friendly_name or "camera disconnected"
        cv2.putText(frame, title, (20, max(40, self.config.height // 2 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        cv2.putText(frame, subtitle, (20, max(70, self.config.height // 2 + 30)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)
        if self.runtime_title:
            self._put_fitted_text(frame, self.runtime_title, (20, 30), 0.48, (0, 255, 255), 1)
        return frame

    def _show_unavailable_frame(self) -> None:
        try:
            frame = self._build_unavailable_frame()
            cv2.imshow(self.window_name, frame)
            cv2.waitKey(1)
        except cv2.error as exc:
            LOGGER.exception("camera_unavailable_window_error camera_id=%s", self.camera_id, extra={"console": True})
            self.stop_event.set()

    def _ensure_dshow_control(self) -> bool:
        """Lazily bind this session's device to native DirectShow property
        control. Must only be called from the preview thread (COM STA rules;
        the resulting controller is only ever used on that same thread)."""
        if self._dshow_available is not None:
            return self._dshow_available
        if not _DSHOW_CONTROL_AVAILABLE:
            self._dshow_available = False
            return False
        try:
            comtypes.CoInitialize()
            base_filter = find_dshow_video_filter(self.descriptor.friendly_name, self.descriptor.device_index)
            if base_filter is None:
                raise RuntimeError("no matching DirectShow video input device found")
            controller = DirectShowPropertyController(base_filter)
            for spec in self.CAMERA_PROP_SPECS:
                name = spec["name"]
                range_min, range_max, _step, default, _caps = controller.get_range(name)
                self.camera_prop_ranges[name] = (int(range_min), int(range_max))
                self.camera_prop_native_defaults[name] = int(default)
                # CAMERA_PROP_SPECS["default"] is a static placeholder tuned
                # for the old 0-100/0-255 OpenCV ranges (e.g. brightness=50);
                # it is meaningless once the real native range is known (e.g.
                # exposure -11..1) and would otherwise make the slider open
                # sitting at the wrong position. Only seed it in if the user
                # has not already changed this property.
                if name not in self._camera_prop_modified_names:
                    self.camera_prop_cache[name] = int(default)
            self._dshow_control = controller
            self._dshow_available = True
            LOGGER.info(
                "camera_native_control_available camera_id=%s friendly_name=%s",
                self.camera_id,
                self.descriptor.friendly_name,
                extra={"console": True},
            )
        except Exception as exc:
            self._dshow_control = None
            self._dshow_available = False
            LOGGER.warning(
                "camera_native_control_unavailable camera_id=%s reason=%s falling_back_to_opencv=true",
                self.camera_id,
                exc,
                extra={"console": True},
            )
        return self._dshow_available

    def _apply_camera_property_native(self, spec: Dict[str, object], raw: int) -> bool:
        # Unlike the OpenCV fallback, `raw` here is already in the DirectShow
        # property's native units (it came from a slider ranged with
        # `camera_prop_ranges`, i.e. real `GetRange()` values) - it must be
        # passed straight through. Do NOT apply `spec["scale"]`/`["offset"]`:
        # those only exist to normalize into OpenCV's 0.0-1.0 CAP_PROP range
        # and previously crushed every native value (e.g. exposure -11..1
        # divided by scale=100) down to 0, so brightness looked "stuck".
        name = spec["name"]
        try:
            reported = self._dshow_control.set_manual(name, raw)
            self.camera_prop_reported[name] = reported
            LOGGER.info(
                "camera_property_apply camera_id=%s name=%s backend=dshow raw=%s reported=%s",
                self.camera_id,
                name,
                raw,
                reported,
            )
            return True
        except Exception:
            LOGGER.exception(
                "camera_property_apply_native_failed camera_id=%s name=%s falling_back_to_opencv=true",
                self.camera_id,
                name,
                extra={"console": True},
            )
            return False

    def _apply_camera_property_opencv(self, capture, spec: Dict[str, object], raw: int) -> None:
        name = spec["name"]
        value = raw / float(spec["scale"]) + float(spec["offset"])
        try:
            auto_disable_prop = spec.get("auto_disable_prop")
            if auto_disable_prop is not None:
                capture.set(auto_disable_prop, 0)
            applied = capture.set(spec["cap_prop"], float(value))
            reported = capture.get(spec["cap_prop"])
            self.camera_prop_reported[name] = reported
            LOGGER.info(
                "camera_property_apply camera_id=%s name=%s backend=opencv raw=%s value=%s applied=%s reported=%s",
                self.camera_id,
                name,
                raw,
                value,
                applied,
                reported,
            )
        except Exception:
            LOGGER.exception("camera_property_apply_failed camera_id=%s name=%s", self.camera_id, name, extra={"console": True})

    def _apply_camera_properties(self, capture, force: bool = False) -> None:
        for spec in self.CAMERA_PROP_SPECS:
            name = spec["name"]
            current_raw = int(self.camera_prop_cache.get(name, spec["default"]))
            if name not in self._camera_prop_modified_names:
                continue
            if not force and self._camera_prop_last_applied.get(name) == current_raw:
                continue
            applied_native = self._ensure_dshow_control() and self._apply_camera_property_native(spec, current_raw)
            if not applied_native:
                self._apply_camera_property_opencv(capture, spec, current_raw)
            self._camera_prop_last_applied[name] = current_raw

    def request_property_value(self, name: str, raw_value: int) -> None:
        """Thread-safe entry point for the controls UI (any thread) to report
        a user-driven slider change. Picked up by `_sync_controls_with_camera()`
        on the preview thread, which owns `capture`/`_dshow_control`."""
        if not self.controls_enabled:
            return
        with self._panel_value_lock:
            self._panel_pending_values[name] = int(raw_value)

    def _reset_controls_to_driver_defaults(self) -> None:
        for spec in self.CAMERA_PROP_SPECS:
            name = spec["name"]
            self.camera_prop_cache[name] = self.camera_prop_native_defaults.get(name, int(spec["default"]))
        self._camera_prop_modified_names.clear()
        self._camera_prop_user_modified = False
        self._camera_prop_last_applied = dict(self.camera_prop_cache)
        self._camera_prop_pending_commit_at = None
        self.camera_prop_reported = {}
        with self._panel_value_lock:
            self._panel_pending_values.clear()
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self.opened = False
        self.latest_frame = None
        LOGGER.warning("camera_controls_reset_to_driver_defaults camera_id=%s", self.camera_id, extra={"console": True})

    def reset_camera_properties(self) -> Dict[str, object]:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            self._reset_controls_to_driver_defaults()
            return {
                "camera_id": self.camera_id,
                "reset_to_driver_defaults": True,
                "properties": dict(self.camera_prop_cache),
                "properties_reported": dict(self.camera_prop_reported),
            }

    def _sync_controls_with_camera(self) -> None:
        if not self.controls_enabled:
            return
        if self.capture is None or not self.capture.isOpened():
            return
        # Probe native control as soon as the panel is open (not only once a
        # value changes) so `camera_prop_ranges` is populated promptly for
        # the controls UI to read real hardware-reported slider bounds.
        self._ensure_dshow_control()

        with self._panel_value_lock:
            pending = dict(self._panel_pending_values)
            self._panel_pending_values.clear()
        changed = False
        for spec in self.CAMERA_PROP_SPECS:
            name = spec["name"]
            if name not in pending:
                continue
            raw = pending[name]
            old_raw = int(self.camera_prop_cache.get(name, spec["default"]))
            if raw != old_raw:
                self.camera_prop_cache[name] = int(raw)
                self._camera_prop_user_modified = True
                self._camera_prop_modified_names.add(name)
                changed = True
                LOGGER.info(
                    "camera_property_changed camera_id=%s name=%s old_raw=%s new_raw=%s",
                    self.camera_id,
                    name,
                    old_raw,
                    raw,
                )
        if changed and self._ensure_dshow_control():
            # Native DirectShow control is a lightweight COM call, not a
            # capture teardown: apply immediately, no debounce/reopen needed.
            self._camera_prop_pending_commit_at = None
            self._apply_camera_properties(self.capture, force=False)
        elif changed:
            # OpenCV fallback: live capture.set() on a running preview stream
            # is unreliable across backends/drivers. Debounce so a slider drag
            # doesn't reopen the capture on every tick, then commit via
            # release+reopen (same path as reset/`apply_config`) so the driver
            # actually applies the value.
            self._camera_prop_pending_commit_at = time.time() + self.PROP_COMMIT_DEBOUNCE_SEC
        elif (
            self._camera_prop_pending_commit_at is not None
            and time.time() >= self._camera_prop_pending_commit_at
        ):
            self._camera_prop_pending_commit_at = None
            LOGGER.info(
                "camera_property_committed camera_id=%s properties=%s",
                self.camera_id,
                dict(self.camera_prop_cache),
                extra={"console": True},
            )
            self._request_capture_reopen()

    def open_controls_panel(self) -> Dict[str, object]:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            if self.capture is None or not self.capture.isOpened():
                raise CommandError("NO_FRAME_YET", f"{self.camera_id} is not opened yet")
            self.controls_enabled = True
            return {
                "camera_id": self.camera_id,
                "controls_panel_open": True,
                "message": "controls panel opening",
                "properties": dict(self.camera_prop_cache),
                "properties_reported": dict(self.camera_prop_reported),
                "properties_ranges": dict(self.camera_prop_ranges),
            }

    def close_controls_panel(self) -> Dict[str, object]:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            self.controls_enabled = False
            with self._panel_value_lock:
                self._panel_pending_values.clear()
            return {
                "camera_id": self.camera_id,
                "controls_panel_open": False,
                "message": "controls panel closed",
            }

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.recording is not None:
                self.recording.stop()
                self.recording = None
            if self.capture is not None:
                self.capture.release()
                self.capture = None
        LOGGER.info("camera_session_stop_requested camera_id=%s", self.camera_id)

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                LOGGER.warning("camera_stop_timeout camera_id=%s", self.camera_id)

    def _cleanup_capture(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        self.opened = False
        try:
            cv2.destroyWindow(self.window_name)
        except cv2.error:
            pass
        self.controls_enabled = False

    def update_output_root(self, output_root: Path) -> None:
        if not self.lock.acquire(timeout=SESSION_LOCK_TIMEOUT_SEC):
            LOGGER.warning("camera_output_root_update_skipped camera_id=%s busy=true", self.camera_id)
            return
        try:
            self.output_root = output_root
        finally:
            self.lock.release()

    def force_reopen(self) -> None:
        if not self.lock.acquire(timeout=SESSION_LOCK_TIMEOUT_SEC):
            LOGGER.warning("camera_force_reopen_skipped camera_id=%s busy=true", self.camera_id)
            return
        try:
            if self.capture is not None:
                self.capture.release()
                self.capture = None
            self.opened = False
            self.latest_frame = None
            self.open_retry_exhausted = False
            self.open_retry_count = 0
            self.read_failure_count = 0
            self.reconnecting_logged = False
            self.backend_index = 0
            self.active_backend_name = ""
            self.initial_open_event.clear()
            self._dshow_available = None
            self._dshow_control = None
        finally:
            self.lock.release()
        LOGGER.warning(
            "camera_force_reopen camera_id=%s device_index=%s connected=%s",
            self.camera_id,
            self.descriptor.device_index,
            self.descriptor.connected,
        )

    def update_descriptor(self, descriptor: CameraDescriptor) -> None:
        if not self.lock.acquire(timeout=SESSION_LOCK_TIMEOUT_SEC):
            LOGGER.warning(
                "camera_descriptor_update_skipped camera_id=%s busy=true new_device_index=%s",
                self.camera_id,
                descriptor.device_index,
            )
            return
        try:
            old_device_index = self.descriptor.device_index
            old_device_id = self.descriptor.device_id
            old_pnp_device_id = self.descriptor.pnp_device_id
            old_connected = self.descriptor.connected
            device_changed = (
                old_device_index != descriptor.device_index
                or old_device_id != descriptor.device_id
                or old_pnp_device_id != descriptor.pnp_device_id
                or old_connected != descriptor.connected
            )
            self.descriptor = descriptor
            self.open_retry_exhausted = False
            self.open_retry_count = 0
            self.read_failure_count = 0
            self.reconnecting_logged = False
            self.backend_index = 0
            self.active_backend_name = ""
            self.initial_open_event.clear()
            if device_changed:
                if self.capture is not None:
                    self.capture.release()
                    self.capture = None
                self.opened = False
                self.latest_frame = None
                self._dshow_available = None
                self._dshow_control = None
        finally:
            self.lock.release()
        if device_changed:
            LOGGER.warning(
                "camera_descriptor_changed camera_id=%s old_device_index=%s new_device_index=%s old_device_id=%s new_device_id=%s old_connected=%s new_connected=%s",
                self.camera_id,
                old_device_index,
                descriptor.device_index,
                old_device_id,
                descriptor.device_id,
                old_connected,
                descriptor.connected,
            )
        else:
            LOGGER.info(
                "camera_descriptor_refreshed camera_id=%s device_index=%s connected=%s",
                self.camera_id,
                descriptor.device_index,
                descriptor.connected,
            )

    def snapshot(self, output_path: Optional[Path] = None, command_name: Optional[str] = None) -> Path:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            if not self.opened or self.latest_frame is None:
                LOGGER.warning(
                    "camera_snapshot_no_frame camera_id=%s opened=%s has_frame=%s",
                    self.camera_id,
                    self.opened,
                    self.latest_frame is not None,
                )
                raise CommandError("NO_FRAME_YET", f"{self.camera_id} has no frame yet")
            frame = self.latest_frame.copy()
        target = output_path or self._default_snapshot_path(command_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        ok = cv2.imwrite(str(target), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise CommandError("INTERNAL_ERROR", f"Failed to write snapshot to {target}")
        LOGGER.info("camera_snapshot_saved camera_id=%s path=%s", self.camera_id, target)
        return target

    def _default_snapshot_path(self, command_name: Optional[str]) -> Path:
        timestamp = file_timestamp_with_millis()
        safe_command_name = self._sanitize_file_component(command_name or self.camera_id or "SNAPSHOT")
        file_tag = f"_{self.file_tag}" if self.file_tag else ""
        return self.output_root / f"{timestamp}{file_tag}_{safe_command_name}.jpg"

    @staticmethod
    def _sanitize_file_component(value: str) -> str:
        normalized = re.sub(r'[<>:"/\\\\|?*]+', "_", str(value).strip())
        normalized = re.sub(r"_+", "_", normalized).strip("._ ")
        return normalized or "SNAPSHOT"

    def start_recording(self, duration_sec: Optional[int] = None, output_dir: Optional[Path] = None, file_prefix: Optional[str] = None) -> None:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            if not self.opened or self.latest_frame is None:
                LOGGER.warning(
                    "camera_recording_no_frame camera_id=%s opened=%s has_frame=%s",
                    self.camera_id,
                    self.opened,
                    self.latest_frame is not None,
                )
                raise CommandError("NO_FRAME_YET", f"{self.camera_id} has no frame yet")
            if self.recording is not None:
                raise CommandError("CAMERA_BUSY", f"{self.camera_id} is already recording")
            self.recording = RecordingSession(
                output_dir or self.output_root,
                file_prefix or self.camera_id,
                (self.config.width, self.config.height),
                self.config.fps,
                duration_sec or self.config.recording_duration,
                file_tag=self.file_tag,
            )
        LOGGER.info(
            "camera_recording_started camera_id=%s duration_sec=%s output_dir=%s file_prefix=%s",
            self.camera_id,
            duration_sec or self.config.recording_duration,
            output_dir or self.output_root,
            file_prefix or self.camera_id,
        )

    def stop_recording(self) -> None:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            if self.recording is None:
                raise CommandError("INVALID_ARGUMENT", f"{self.camera_id} is not recording")
            self.recording.stop()
            self.recording = None
        LOGGER.info("camera_recording_stopped camera_id=%s", self.camera_id)

    def apply_config(self, new_values: Dict[str, int]) -> None:
        validated: Dict[str, int] = {}
        for key, value in new_values.items():
            if not hasattr(self.config, key):
                continue
            minimum = 0 if key == "max_folder_size_gb" else 1
            validated[key] = coerce_int(value, key, min_value=minimum)

        capture_affecting = {"width", "height", "fps"}
        needs_reopen = bool(capture_affecting & validated.keys())
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            for key, value in validated.items():
                setattr(self.config, key, value)
            # Never mutate a live `self.capture` from this thread: the preview
            # thread owns it and may be mid-`read()`. Ask it to release and
            # reopen with the new config instead (same pattern as reset).
            if needs_reopen and self.capture is not None:
                self._request_capture_reopen()
        LOGGER.info("camera_config_updated camera_id=%s values=%s", self.camera_id, validated, extra={"console": True})

    def status(self) -> Dict[str, object]:
        with acquired_lock(
            self.lock,
            SESSION_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            f"{self.camera_id} is busy recovering hardware state",
        ):
            return {
                "camera_id": self.camera_id,
                "device_index": self.descriptor.device_index,
                "friendly_name": self.descriptor.friendly_name,
                "is_external": self.descriptor.is_external,
                "connected": self.descriptor.connected,
                "opened": self.opened,
                "recording": self.recording is not None,
                "width": self.config.width,
                "height": self.config.height,
                "fps": self.config.fps,
                "recording_duration": self.config.recording_duration,
                "max_folder_size_gb": self.config.max_folder_size_gb,
                "camera_properties": dict(self.camera_prop_cache),
                "camera_properties_reported": dict(self.camera_prop_reported),
                "camera_properties_ranges": dict(self.camera_prop_ranges),
                "controls_panel_open": self.controls_enabled,
            }


class CameraControlsUI:
    """Tk-based controls panel, replacing the old cv2 trackbar window.

    Owns a single process-wide Tk root/mainloop on its own daemon thread.
    Tkinter widgets may only be touched from that thread, so every other
    thread talks to it exclusively through `_queue` (never calls Tk APIs
    directly), and the Tk thread talks back to `CameraSession` only through
    methods that are already documented as thread-safe on their own
    (`request_property_value()`, `reset_camera_properties()`,
    `close_controls_panel()`).
    """

    REFRESH_INTERVAL_MS = 200
    QUEUE_POLL_INTERVAL_MS = 50

    def __init__(self) -> None:
        self._queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._root = None
        self._panels: Dict[str, object] = {}
        self._ready_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="camera-controls-ui", daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=5)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.put(("shutdown", None))
        self._thread.join(timeout=3)

    def open_panel(self, session: "CameraSession") -> None:
        self._queue.put(("open", session))

    def close_panel(self, camera_id: str) -> None:
        self._queue.put(("close", camera_id))

    # --- Tk thread only below this point -----------------------------

    def _run(self) -> None:
        import tkinter as tk

        self._tk = tk
        root = tk.Tk()
        root.withdraw()
        self._root = root
        self._ready_event.set()
        root.after(self.QUEUE_POLL_INTERVAL_MS, self._drain_queue)
        root.mainloop()

    def _drain_queue(self) -> None:
        try:
            while True:
                action, payload = self._queue.get_nowait()
                if action == "open":
                    self._open_panel_on_tk_thread(payload)
                elif action == "close":
                    self._close_panel_on_tk_thread(payload)
                elif action == "shutdown":
                    for camera_id in list(self._panels):
                        self._close_panel_on_tk_thread(camera_id)
                    self._root.quit()
                    return
        except queue.Empty:
            pass
        self._root.after(self.QUEUE_POLL_INTERVAL_MS, self._drain_queue)

    def _open_panel_on_tk_thread(self, session: "CameraSession") -> None:
        tk = self._tk
        camera_id = session.camera_id
        existing = self._panels.get(camera_id)
        if existing is not None:
            existing["toplevel"].lift()
            return

        toplevel = tk.Toplevel(self._root)
        toplevel.title(f"{camera_id} controls")

        def handle_close() -> None:
            session.close_controls_panel()
            self._close_panel_on_tk_thread(camera_id)

        toplevel.protocol("WM_DELETE_WINDOW", handle_close)

        sliders: Dict[str, object] = {}
        labels: Dict[str, object] = {}
        for spec in session.CAMERA_PROP_SPECS:
            name = spec["name"]
            range_min, range_max = session.camera_prop_ranges.get(name, (0, int(spec["max"])))
            row = tk.Frame(toplevel)
            row.pack(fill="x", padx=8, pady=4)
            tk.Label(row, text=name, width=12, anchor="w").pack(side="left")
            label = tk.Label(row, text="", width=16, anchor="w")
            label.pack(side="right")

            def on_change(value, _name=name):
                session.request_property_value(_name, int(float(value)))

            # Create without `command` so the initial `.set()` below (and any
            # later silent range/clamp correction) can never masquerade as a
            # user-driven request; `command` is attached only afterward, so
            # only real user drags reach `request_property_value()`.
            scale = tk.Scale(
                toplevel,
                from_=range_min,
                to=range_max,
                orient="horizontal",
                length=260,
            )
            scale.set(int(session.camera_prop_cache.get(name, spec["default"])))
            scale.config(command=on_change)
            scale.pack(fill="x", padx=8)
            sliders[name] = scale
            labels[name] = label

        def handle_reset() -> None:
            session.reset_camera_properties()
            for spec in session.CAMERA_PROP_SPECS:
                name = spec["name"]
                scale = sliders[name]
                cmd = scale["command"]
                scale["command"] = ""
                scale.set(int(spec["default"]))
                scale["command"] = cmd

        button_row = tk.Frame(toplevel)
        button_row.pack(fill="x", padx=8, pady=8)
        tk.Button(button_row, text="Reset to driver defaults", command=handle_reset).pack(side="left")
        tk.Button(button_row, text="Close", command=handle_close).pack(side="right")

        panel = {"toplevel": toplevel, "sliders": sliders, "labels": labels}
        self._panels[camera_id] = panel
        self._refresh_panel(session, camera_id)

    def _refresh_panel(self, session: "CameraSession", camera_id: str) -> None:
        panel = self._panels.get(camera_id)
        if panel is None:
            return
        for spec in session.CAMERA_PROP_SPECS:
            name = spec["name"]
            range_min, range_max = session.camera_prop_ranges.get(name, (0, int(spec["max"])))
            requested_raw = int(session.camera_prop_cache.get(name, spec["default"]))
            requested_display = min(range_max, max(range_min, requested_raw))
            reported = session.camera_prop_reported.get(name)
            reported_text = f"{reported:.1f}" if isinstance(reported, (int, float)) else "-"
            panel["labels"][name].config(text=f"req={requested_display}  actual={reported_text}")
            scale = panel["sliders"][name]
            if (float(scale.cget("from")), float(scale.cget("to"))) != (float(range_min), float(range_max)):
                # Reconfiguring `from_`/`to` on a Tk Scale silently clamps an
                # out-of-range current value AND fires `command` as a side
                # effect - detach it first so a range correction (e.g. the
                # static 0-100 fallback getting replaced by the real
                # hardware range once probed) never gets applied to the
                # camera as if the user had dragged the slider.
                cmd = scale["command"]
                scale["command"] = ""
                scale.config(from_=range_min, to=range_max)
                scale.set(requested_display)
                scale["command"] = cmd
        self._root.after(self.REFRESH_INTERVAL_MS, self._refresh_panel, session, camera_id)

    def _close_panel_on_tk_thread(self, camera_id: str) -> None:
        panel = self._panels.pop(camera_id, None)
        if panel is not None:
            try:
                panel["toplevel"].destroy()
            except Exception:
                pass


class CameraManager:
    HOTPLUG_POLL_INTERVAL_SEC = 2.0
    HOTPLUG_SETTLE_SEC = 1.0
    WINDOWS_DEVICE_QUERY_TIMEOUT_SEC = 5.0
    WINDOWS_DEVICE_QUERY_MAX_CONSECUTIVE_FAILURES = 3
    MAX_CAMERA_PROBE_COUNT = 6
    CAMERA_PROBE_BUFFER = 2
    MAX_LOGICAL_CAMERAS = 10
    BACKEND_CANDIDATES = [
        (cv2.CAP_MSMF, "CAP_MSMF"),
        (cv2.CAP_DSHOW, "CAP_DSHOW"),
    ]

    def __init__(
        self,
        config: CameraConfig,
        output_dir: Path,
        map_path: Path,
        controls_ui: "CameraControlsUI",
        allowed_device_indexes: Optional[List[int]] = None,
        runtime_title: str = "",
        file_tag: str = "",
    ):
        self.default_config = config
        self.output_dir = output_dir
        self.map_path = map_path
        self.controls_ui = controls_ui
        self.allowed_device_indexes = set(allowed_device_indexes) if allowed_device_indexes else None
        self.runtime_title = runtime_title
        self.file_tag = file_tag
        self.sessions: Dict[str, CameraSession] = {}
        self.logical_slots: Dict[str, CameraDescriptor] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.hotplug_thread: Optional[threading.Thread] = None
        self.shutdown_callback: Optional[Callable[[], None]] = None
        self.global_backend_index = 0
        self._last_windows_devices: List[Dict[str, str]] = []
        self._last_windows_query_ok = True
        self._windows_query_consecutive_failures = 0
        self._last_logged_windows_device_snapshot: Optional[Tuple[Tuple[str, str, str, str, str], ...]] = None
        self.alias_config = self._load_alias_config()
        self.descriptors = self._filter_allowed_descriptors(self._discover_descriptors())
        self.last_windows_device_snapshot = self._windows_device_signature(self._query_windows_camera_devices())
        self._initialize_logical_slots()
        self._open_default_cameras()
        self._start_hotplug_monitor()
        LOGGER.info(
            "camera_manager_initialized output_dir=%s allowed_device_indexes=%s discovered_devices=%s",
            self.output_dir,
            sorted(self.allowed_device_indexes) if self.allowed_device_indexes is not None else "all",
            len(self.descriptors),
        )

    def _filter_allowed_descriptors(self, descriptors: List[CameraDescriptor]) -> List[CameraDescriptor]:
        if self.allowed_device_indexes is None:
            return descriptors
        return [descriptor for descriptor in descriptors if descriptor.device_index in self.allowed_device_indexes]

    def _load_alias_config(self) -> Dict[str, object]:
        if not self.map_path.exists():
            return self._default_alias_config()
        with self.map_path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            payload = self._default_alias_config()
        aliases = payload.get("aliases", {})
        if not isinstance(aliases, dict):
            aliases = {}
        for slot_index in range(self.MAX_LOGICAL_CAMERAS):
            aliases.setdefault(self._slot_name(slot_index), self._default_alias_entry())
        payload["aliases"] = aliases
        return payload

    def _save_alias_config(self) -> None:
        self.map_path.write_text(json.dumps(self.alias_config, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def _default_alias_entry(cls) -> Dict[str, object]:
        return {
            "preferred_name_keywords": [],
            "device_index": None,
            "binding_key": "",
            "friendly_name": "",
            "device_id": "",
            "pnp_device_id": "",
            "location_information": "",
            "manufacturer": "",
        }

    @classmethod
    def _default_alias_config(cls) -> Dict[str, object]:
        aliases = {}
        for slot_index in range(cls.MAX_LOGICAL_CAMERAS):
            aliases[cls._slot_name(slot_index)] = cls._default_alias_entry()
        aliases["cam0"]["preferred_name_keywords"] = ["logitech", "usb", "webcam", "hd pro"]
        return {"aliases": aliases}

    def _discover_descriptors(
        self,
        *,
        windows_devices: Optional[List[Dict[str, str]]] = None,
        preserved_descriptors: Optional[List[CameraDescriptor]] = None,
    ) -> List[CameraDescriptor]:
        windows_devices = windows_devices if windows_devices is not None else self._query_windows_camera_devices()
        names = [device.get("friendly_name", "") for device in windows_devices if device.get("friendly_name")]
        descriptors = []
        probe_count = min(
            self.MAX_CAMERA_PROBE_COUNT,
            max(len(windows_devices) + self.CAMERA_PROBE_BUFFER, 2),
        )

        previous_log_level = self._set_opencv_probe_log_level()
        try:
            for idx in range(probe_count):
                cap = None
                try:
                    cap = self._probe_capture(idx)
                    if not cap.isOpened():
                        LOGGER.warning("camera_probe_unopened index=%s", idx)
                        continue

                    device_info = windows_devices[idx] if idx < len(windows_devices) else {}
                    name = device_info.get("friendly_name") or (names[idx] if idx < len(names) else f"Camera {idx}")
                    is_external = any(token in name.lower() for token in ["usb", "logitech", "webcam", "hd pro", "external"])
                    descriptors.append(CameraDescriptor(
                        idx,
                        name,
                        is_external,
                        device_id=str(device_info.get("device_id", "")),
                        pnp_device_id=str(device_info.get("pnp_device_id", "")),
                        location_information=str(device_info.get("location_information", "")),
                        manufacturer=str(device_info.get("manufacturer", "")),
                        ))
                except cv2.error as exc:
                    LOGGER.warning("camera_probe_cv_error index=%s error=%s", idx, exc)
                    continue
                except Exception as exc:
                    LOGGER.exception("camera_probe_exception index=%s", idx)
                    continue
                finally:
                    if cap is not None:
                        try:
                            cap.release()
                        except Exception:
                            pass
        finally:
            self._restore_opencv_probe_log_level(previous_log_level)
        return descriptors

    @staticmethod
    def _probe_capture(device_index: int):
        for backend in (cv2.CAP_MSMF, cv2.CAP_DSHOW, None):
            cap = None
            try:
                cap = cv2.VideoCapture(device_index, backend) if backend is not None else cv2.VideoCapture(device_index)
                if cap.isOpened():
                    return cap
            except cv2.error:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                continue
            except Exception:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
                continue
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        return cv2.VideoCapture()

    @staticmethod
    def _set_opencv_probe_log_level() -> Optional[int]:
        if not hasattr(cv2, "setLogLevel"):
            return None
        previous_level = None
        if hasattr(cv2, "getLogLevel"):
            try:
                previous_level = cv2.getLogLevel()
            except Exception:
                previous_level = None
        try:
            cv2.setLogLevel(0)
        except Exception:
            return previous_level
        return previous_level

    @staticmethod
    def _restore_opencv_probe_log_level(previous_level: Optional[int]) -> None:
        if previous_level is None or not hasattr(cv2, "setLogLevel"):
            return
        try:
            cv2.setLogLevel(previous_level)
        except Exception:
            pass

    @staticmethod
    def _descriptor_key(descriptor: CameraDescriptor) -> str:
        parts = []
        for candidate in (
            descriptor.pnp_device_id,
            descriptor.device_id,
            descriptor.location_information,
            descriptor.manufacturer,
            descriptor.friendly_name,
        ):
            normalized = str(candidate or "").strip().lower()
            if normalized:
                parts.append(normalized)
        if parts:
            return "|".join(parts)
        return f"index:{descriptor.device_index}"

    @staticmethod
    def _descriptor_signature(descriptor: CameraDescriptor) -> Tuple[object, ...]:
        return (
            descriptor.device_index,
            descriptor.friendly_name,
            descriptor.is_external,
            descriptor.device_id,
            descriptor.pnp_device_id,
            descriptor.location_information,
            descriptor.manufacturer,
        )

    @classmethod
    def _descriptor_signatures(cls, descriptors: List[CameraDescriptor]) -> Tuple[Tuple[object, ...], ...]:
        return tuple(sorted(cls._descriptor_signature(descriptor) for descriptor in descriptors))

    @staticmethod
    def _device_info_key(device_info: Dict[str, str]) -> str:
        parts = []
        for candidate in (
            device_info.get("pnp_device_id", ""),
            device_info.get("device_id", ""),
            device_info.get("location_information", ""),
            device_info.get("manufacturer", ""),
            device_info.get("friendly_name", ""),
        ):
            normalized = str(candidate or "").strip().lower()
            if normalized:
                parts.append(normalized)
        if parts:
            return "|".join(parts)
        return ""

    @staticmethod
    def _windows_device_signature(devices: List[Dict[str, str]]) -> Tuple[Tuple[str, str, str, str, str], ...]:
        # Get-CimInstance does not guarantee stable ordering between polls, so
        # sort the signature to avoid treating a reordered-but-unchanged
        # device list as a real change.
        return tuple(
            sorted(
                (
                    str(item.get("friendly_name", "") or ""),
                    str(item.get("device_id", "") or ""),
                    str(item.get("pnp_device_id", "") or ""),
                    str(item.get("location_information", "") or ""),
                    str(item.get("manufacturer", "") or ""),
                )
                for item in devices
                if isinstance(item, dict)
            )
        )

    def _query_windows_camera_devices(self) -> List[Dict[str, str]]:
        script = (
            "try { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.Service -match 'usbvideo|ksthunk' } | "
            "Select-Object @{Name='friendly_name';Expression={$_.Name}}, "
            "@{Name='device_id';Expression={$_.DeviceID}}, "
            "@{Name='pnp_device_id';Expression={$_.PNPDeviceID}}, "
            "@{Name='location_information';Expression={$_.LocationInformation}}, "
            "@{Name='manufacturer';Expression={$_.Manufacturer}} | ConvertTo-Json -Depth 3 "
            "} catch { '[]' }"
        )
        try:
            command = ["powershell", "-NoProfile", "-Command", script]
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=self.WINDOWS_DEVICE_QUERY_TIMEOUT_SEC,
            )
            stdout = _decode_shell_output(result.stdout).strip()
            stderr = _decode_shell_output(result.stderr).strip()
            if result.returncode != 0:
                self._last_windows_query_ok = False
                self._windows_query_consecutive_failures += 1
                LOGGER.warning("windows_device_query_failed returncode=%s stderr=%s", result.returncode, stderr)
                return list(self._last_windows_devices)
            payload = stdout
            if not payload:
                self._last_windows_query_ok = False
                self._windows_query_consecutive_failures += 1
                LOGGER.warning("windows_device_query_empty")
                return list(self._last_windows_devices)
            data = json.loads(payload)
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                self._last_windows_query_ok = False
                self._windows_query_consecutive_failures += 1
                LOGGER.warning("windows_device_query_non_list")
                return list(self._last_windows_devices)
            normalized = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                normalized.append({
                    "friendly_name": str(item.get("friendly_name", "") or ""),
                    "device_id": str(item.get("device_id", "") or ""),
                    "pnp_device_id": str(item.get("pnp_device_id", "") or ""),
                    "location_information": str(item.get("location_information", "") or ""),
                    "manufacturer": str(item.get("manufacturer", "") or ""),
                })
            self._last_windows_devices = normalized
            self._last_windows_query_ok = True
            self._windows_query_consecutive_failures = 0
            snapshot = self._windows_device_signature(normalized)
            if snapshot != self._last_logged_windows_device_snapshot:
                LOGGER.info(
                    "windows_device_query_devices devices=%s",
                    [
                        {
                            "friendly_name": d.get("friendly_name", ""),
                            "device_id": d.get("device_id", ""),
                            "pnp_device_id": d.get("pnp_device_id", ""),
                            "location_information": d.get("location_information", ""),
                            "manufacturer": d.get("manufacturer", ""),
                        }
                        for d in normalized
                    ],
                    extra={"console": True},
                )
                LOGGER.info("windows_device_query_success count=%s", len(normalized))
                self._last_logged_windows_device_snapshot = snapshot
            else:
                LOGGER.debug("windows_device_query_success count=%s unchanged=true", len(normalized))
            return normalized
        except subprocess.TimeoutExpired:
            self._last_windows_query_ok = False
            self._windows_query_consecutive_failures += 1
            LOGGER.warning(
                "windows_device_query_timeout timeout_sec=%s fail_count=%s fallback_count=%s fallback_devices=%s",
                self.WINDOWS_DEVICE_QUERY_TIMEOUT_SEC,
                self._windows_query_consecutive_failures,
                len(self._last_windows_devices),
                [
                    {
                        "friendly_name": d.get("friendly_name", ""),
                        "device_id": d.get("device_id", ""),
                        "pnp_device_id": d.get("pnp_device_id", ""),
                        "location_information": d.get("location_information", ""),
                        "manufacturer": d.get("manufacturer", ""),
                    }
                    for d in self._last_windows_devices
                ],
            )
            return list(self._last_windows_devices)
        except Exception:
            self._last_windows_query_ok = False
            self._windows_query_consecutive_failures += 1
            LOGGER.exception("windows_device_query_exception")
            return list(self._last_windows_devices)

    def _pick_default_descriptor(self) -> CameraDescriptor:
        aliases = self.alias_config.get("aliases", {}) if isinstance(self.alias_config, dict) else {}
        cam0_rule = aliases.get("cam0", {}) if isinstance(aliases, dict) else {}
        preferred_keywords = [k.lower() for k in cam0_rule.get("preferred_name_keywords", [])]
        preferred_index = cam0_rule.get("device_index")
        if isinstance(preferred_index, int):
            for descriptor in self.descriptors:
                if descriptor.device_index == preferred_index:
                    return descriptor
        if not self.descriptors:
            return CameraDescriptor(-1, "No Camera Detected", False, connected=False)
        if len(self.descriptors) == 1:
            for descriptor in self.descriptors:
                if preferred_keywords and any(keyword in descriptor.friendly_name.lower() for keyword in preferred_keywords):
                    return descriptor
            for descriptor in self.descriptors:
                if descriptor.is_external:
                    return descriptor
        return self.descriptors[0]

    def _initialize_logical_slots(self) -> None:
        aliases = self.alias_config.get("aliases", {}) if isinstance(self.alias_config, dict) else {}
        for slot_index in range(self.MAX_LOGICAL_CAMERAS):
            camera_id = self._slot_name(slot_index)
            rule = aliases.get(camera_id, {})
            self.logical_slots[camera_id] = CameraDescriptor(
                -1,
                str(rule.get("friendly_name", "") or camera_id),
                False,
                device_id=str(rule.get("device_id", "") or ""),
                pnp_device_id=str(rule.get("pnp_device_id", "") or ""),
                location_information=str(rule.get("location_information", "") or ""),
                manufacturer=str(rule.get("manufacturer", "") or ""),
                connected=False,
            )

    def _slot_rule(self, camera_id: str) -> Dict[str, object]:
        aliases = self.alias_config.get("aliases", {}) if isinstance(self.alias_config, dict) else {}
        rule = aliases.get(camera_id, {})
        if not isinstance(rule, dict):
            rule = self._default_alias_entry()
            aliases[camera_id] = rule
        return rule

    def _descriptor_matches_rule(self, descriptor: CameraDescriptor, rule: Dict[str, object]) -> bool:
        binding_key = str(rule.get("binding_key", "") or "").strip().lower()
        if binding_key:
            return self._descriptor_key(descriptor) == binding_key
        for field_name in ("pnp_device_id", "device_id", "location_information", "friendly_name", "manufacturer"):
            expected = str(rule.get(field_name, "") or "").strip().lower()
            actual = str(getattr(descriptor, field_name, "") or "").strip().lower()
            if expected and actual and expected != actual:
                return False
        return any(str(rule.get(field_name, "") or "").strip() for field_name in ("pnp_device_id", "device_id", "location_information", "friendly_name"))

    def _claim_slot(self, camera_id: str, descriptor: CameraDescriptor) -> None:
        rule = self._slot_rule(camera_id)
        rule["binding_key"] = self._descriptor_key(descriptor)
        rule["friendly_name"] = descriptor.friendly_name
        rule["device_id"] = descriptor.device_id
        rule["pnp_device_id"] = descriptor.pnp_device_id
        rule["location_information"] = descriptor.location_information
        rule["manufacturer"] = descriptor.manufacturer

    def _open_default_cameras(self) -> None:
        changed = self._refresh_discovery_locked()
        if changed:
            self._save_alias_config()
        LOGGER.info("camera_manager_open_defaults changed=%s", changed)

        opened_any = False
        for slot_index in range(self.MAX_LOGICAL_CAMERAS):
            camera_id = self._slot_name(slot_index)
            descriptor = self.logical_slots.get(camera_id)
            if descriptor is None or not descriptor.connected:
                continue
            LOGGER.info(
                "camera_default_open camera_id=%s device_index=%s friendly_name=%s",
                camera_id,
                descriptor.device_index,
                descriptor.friendly_name,
            )
            self.open_camera(camera_id, refresh=False, wait_initial=True)
            opened_any = True

        if not opened_any:
            LOGGER.warning("camera_manager_no_defaults_discovered")

    @staticmethod
    def _slot_name(slot_index: int) -> str:
        return f"cam{slot_index}"

    def _next_camera_id_locked(self) -> Optional[str]:
        used_indexes = set()
        for camera_id in self.logical_slots.keys():
            match = re.fullmatch(r"cam(\d+)", camera_id)
            if match:
                used_indexes.add(int(match.group(1)))
        for next_index in range(self.MAX_LOGICAL_CAMERAS):
            if next_index not in used_indexes:
                return self._slot_name(next_index)
        return None

    def _refresh_discovery_locked(self, windows_devices: Optional[List[Dict[str, str]]] = None) -> bool:
        if windows_devices is None:
            windows_devices = self._query_windows_camera_devices()
        self.last_windows_device_snapshot = self._windows_device_signature(windows_devices)
        latest_descriptors = self._filter_allowed_descriptors(self._discover_descriptors(
            windows_devices=windows_devices,
            preserved_descriptors=[],
        ))
        changed = self._descriptor_signatures(self.descriptors) != self._descriptor_signatures(latest_descriptors)
        self.descriptors = latest_descriptors
        aliases_changed = False
        LOGGER.info(
            "camera_discovery_refresh windows_devices=%s descriptors=%s changed=%s",
            len(windows_devices),
            len(latest_descriptors),
            changed,
        )

        for slot_index in range(self.MAX_LOGICAL_CAMERAS):
            camera_id = self._slot_name(slot_index)
            session = self.sessions.get(camera_id)
            if slot_index < len(latest_descriptors):
                descriptor = latest_descriptors[slot_index]
                self.logical_slots[camera_id] = descriptor
                self._claim_slot(camera_id, descriptor)
                aliases_changed = True
                if session is not None:
                    session.update_descriptor(descriptor)
            else:
                current_descriptor = self.logical_slots.get(camera_id, CameraDescriptor(-1, camera_id, False, connected=False))
                offline_descriptor = CameraDescriptor(
                    -1,
                    current_descriptor.friendly_name,
                    current_descriptor.is_external,
                    device_id=current_descriptor.device_id,
                    pnp_device_id=current_descriptor.pnp_device_id,
                    location_information=current_descriptor.location_information,
                    manufacturer=current_descriptor.manufacturer,
                    connected=False,
                )
                self.logical_slots[camera_id] = offline_descriptor
                if session is not None:
                    session.update_descriptor(offline_descriptor)

        if aliases_changed:
            self._save_alias_config()
            LOGGER.info("camera_alias_config_saved aliases_changed=%s", aliases_changed)

        if changed:
            LOGGER.warning("camera_discovery_changed forcing_session_reopen=%s", len(self.sessions), extra={"console": True})
            for session in self.sessions.values():
                session.force_reopen()

        return changed

    def _refresh_discovery(self) -> bool:
        with self.lock:
            return self._refresh_discovery_locked()

    def _refresh_if_windows_devices_changed_locked(self) -> bool:
        windows_devices = self._query_windows_camera_devices()
        snapshot = self._windows_device_signature(windows_devices)
        if snapshot == self.last_windows_device_snapshot:
            return False
        return self._refresh_discovery_locked(windows_devices)

    def _apply_windows_device_presence_locked(self, windows_devices: List[Dict[str, str]]) -> bool:
        self.last_windows_device_snapshot = self._windows_device_signature(windows_devices)
        connected_device_keys = {
            self._device_info_key(device)
            for device in windows_devices
            if self._device_info_key(device)
        }
        changed = False
        for camera_id, descriptor in list(self.logical_slots.items()):
            is_connected = self._descriptor_key(descriptor) in connected_device_keys
            if descriptor.connected == is_connected:
                continue
            updated_descriptor = CameraDescriptor(
                descriptor.device_index if is_connected else -1,
                descriptor.friendly_name,
                descriptor.is_external,
                device_id=descriptor.device_id,
                pnp_device_id=descriptor.pnp_device_id,
                location_information=descriptor.location_information,
                manufacturer=descriptor.manufacturer,
                connected=is_connected,
            )
            self.logical_slots[camera_id] = updated_descriptor
            session = self.sessions.get(camera_id)
            if session is not None:
                session.update_descriptor(updated_descriptor)
            changed = True
            LOGGER.warning(
                "camera_presence_changed camera_id=%s device_index=%s connected=%s",
                camera_id,
                updated_descriptor.device_index,
                updated_descriptor.connected,
            )
        return changed

    def rescan_devices(self) -> Dict[str, object]:
        windows_devices = self._query_windows_camera_devices()
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            changed = self._refresh_discovery_locked(windows_devices)
            devices = self._list_devices_locked()
        LOGGER.info("camera_rescan_completed changed=%s device_count=%s", changed, len(devices), extra={"console": bool(changed)})
        return {
            "changed": changed,
            "device_count": len(devices),
            "devices": devices,
        }

    def refresh_cameras(self) -> Dict[str, object]:
        windows_devices = self._query_windows_camera_devices()
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            self.global_backend_index = 0
            changed = self._refresh_discovery_locked(windows_devices)
            sessions_to_restart: List[CameraSession] = []
            for slot_index in range(self.MAX_LOGICAL_CAMERAS):
                camera_id = self._slot_name(slot_index)
                descriptor = self.logical_slots.get(camera_id)
                if descriptor is None or not descriptor.connected:
                    continue
                session = self.sessions.get(camera_id)
                if session is None:
                    session = self._create_session_locked(camera_id, descriptor)
                else:
                    session.force_reopen()
                sessions_to_restart.append(session)
            devices = self._list_devices_locked()
        LOGGER.warning(
            "camera_refresh_requested changed=%s device_count=%s active_sessions=%s",
            changed,
            len(devices),
            len(sessions_to_restart),
        )
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            cameras = [session.status() for session in self.sessions.values()]
        LOGGER.info("camera_refresh_completed cameras=%s", len(cameras))
        return {
            "changed": changed,
            "device_count": len(devices),
            "devices": devices,
            "cameras": cameras,
        }

    def _hotplug_monitor_loop(self) -> None:
        while not self.stop_event.wait(self.HOTPLUG_POLL_INTERVAL_SEC):
            try:
                windows_devices = self._query_windows_camera_devices()
                if (
                    not self._last_windows_query_ok
                    and self._windows_query_consecutive_failures < self.WINDOWS_DEVICE_QUERY_MAX_CONSECUTIVE_FAILURES
                ):
                    LOGGER.warning(
                        "camera_hotplug_query_debounced fail_count=%s threshold=%s",
                        self._windows_query_consecutive_failures,
                        self.WINDOWS_DEVICE_QUERY_MAX_CONSECUTIVE_FAILURES,
                    )
                    continue
                snapshot = self._windows_device_signature(windows_devices)
                descriptor_count_mismatch = (
                    self._last_windows_query_ok
                    and self.allowed_device_indexes is None
                    and len(windows_devices) != len(self.descriptors)
                )
                if snapshot == self.last_windows_device_snapshot and not descriptor_count_mismatch:
                    continue
                if snapshot != self.last_windows_device_snapshot and self.HOTPLUG_SETTLE_SEC > 0:
                    if self.stop_event.wait(self.HOTPLUG_SETTLE_SEC):
                        break
                LOGGER.warning(
                    "camera_hotplug_change_detected windows_devices=%s descriptor_count_mismatch=%s",
                    len(windows_devices),
                    descriptor_count_mismatch,
                    extra={"console": True},
                )
                if not self.lock.acquire(timeout=MANAGER_LOCK_TIMEOUT_SEC):
                    LOGGER.warning("camera_hotplug_refresh_skipped manager_busy=true")
                    continue
                try:
                    self._refresh_discovery_locked(windows_devices)
                finally:
                    self.lock.release()
            except Exception:
                LOGGER.exception("camera_hotplug_refresh_failed", extra={"console": True})

    def _start_hotplug_monitor(self) -> None:
        if self.hotplug_thread is not None and self.hotplug_thread.is_alive():
            return
        self.hotplug_thread = threading.Thread(
            target=self._hotplug_monitor_loop,
            name="camera-hotplug-monitor",
            daemon=True,
        )
        self.hotplug_thread.start()
        LOGGER.info("camera_hotplug_monitor_started")

    def _get_slot_descriptor(self, camera_id: str) -> CameraDescriptor:
        descriptor = self.logical_slots.get(camera_id)
        if descriptor is None:
            raise CommandError("CAMERA_NOT_FOUND", f"{camera_id} is not assigned")
        return descriptor

    def set_shutdown_callback(self, shutdown_callback: Callable[[], None]) -> None:
        self.shutdown_callback = shutdown_callback
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            for session in self.sessions.values():
                session.shutdown_callback = shutdown_callback

    def set_output_dir(self, output_dir: Path) -> Path:
        resolved = output_dir.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self.output_dir = resolved
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            for session in self.sessions.values():
                session.update_output_root(resolved)
        return resolved

    def _get_backend_index(self) -> int:
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            return self.global_backend_index

    def _request_backend_failover(self, current_index: int) -> bool:
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            if current_index != self.global_backend_index:
                return False
            if not self.BACKEND_CANDIDATES:
                return False
            self.global_backend_index = (self.global_backend_index + 1) % len(self.BACKEND_CANDIDATES)
            for session in self.sessions.values():
                session.force_reopen()
            LOGGER.warning(
                "camera_backend_global_failover current_index=%s next_index=%s session_count=%s",
                current_index,
                self.global_backend_index,
                len(self.sessions),
            )
            return True

    def _create_session_locked(self, camera_id: str, descriptor: CameraDescriptor) -> CameraSession:
        duplicated_camera = next(
            (
                session
                for session in self.sessions.values()
                if session.descriptor.connected
                and descriptor.connected
                and session.descriptor.device_index == descriptor.device_index
            ),
            None,
        )
        if duplicated_camera is not None:
            LOGGER.error(
                "camera_device_busy camera_id=%s device_index=%s already_opened_by=%s",
                camera_id,
                descriptor.device_index,
                duplicated_camera.camera_id,
            )
            raise CommandError(
                "CAMERA_BUSY",
                f"{camera_id} is assigned to device_index {descriptor.device_index}, already opened by {duplicated_camera.camera_id}",
            )

        session = CameraSession(
            camera_id,
            descriptor,
            CameraConfig(**vars(self.default_config)),
            self.output_dir,
            shutdown_callback=self.shutdown_callback,
                get_backend_index=self._get_backend_index,
                request_backend_failover=self._request_backend_failover,
                runtime_title=self.runtime_title,
                file_tag=self.file_tag,
            )
        self.sessions[camera_id] = session
        session.start()
        LOGGER.info(
            "camera_session_created camera_id=%s device_index=%s friendly_name=%s",
            camera_id,
            descriptor.device_index,
            descriptor.friendly_name,
        )
        return session

    def open_camera(self, camera_id: str, refresh: bool = True, wait_initial: bool = False) -> Dict[str, object]:
        windows_devices = self._query_windows_camera_devices() if refresh else None
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            if refresh:
                self._refresh_discovery_locked(windows_devices)
            existing_session = self.sessions.get(camera_id)
            if existing_session is not None:
                LOGGER.info("camera_open_existing camera_id=%s", camera_id)
                return existing_session.status()

            descriptor = self._get_slot_descriptor(camera_id)
            session = self._create_session_locked(camera_id, descriptor)
        if wait_initial:
            session.wait_for_initial_open()
        LOGGER.info("camera_open_completed camera_id=%s wait_initial=%s", camera_id, wait_initial)
        return session.status()

    def swap_cameras(self, camera_id_a: str, camera_id_b: str) -> Dict[str, object]:
        if camera_id_a == camera_id_b:
            raise CommandError("INVALID_ARGUMENT", "swap requires two different camera ids")

        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            session_a = self.sessions.get(camera_id_a)
            session_b = self.sessions.get(camera_id_b)
            if session_a is None:
                raise CommandError("CAMERA_NOT_FOUND", f"{camera_id_a} not found")
            if session_b is None:
                raise CommandError("CAMERA_NOT_FOUND", f"{camera_id_b} not found")
            if session_a.recording is not None or session_b.recording is not None:
                raise CommandError("CAMERA_BUSY", "stop recording before swapping cameras")

            descriptor_a = session_a.descriptor
            descriptor_b = session_b.descriptor
            config_a = CameraConfig(**vars(session_a.config))
            config_b = CameraConfig(**vars(session_b.config))
            output_root_a = session_a.output_root
            output_root_b = session_b.output_root
            self.logical_slots[camera_id_a] = descriptor_b
            self.logical_slots[camera_id_b] = descriptor_a
            self.sessions.pop(camera_id_a, None)
            self.sessions.pop(camera_id_b, None)

        session_a.stop()
        session_b.stop()

        new_session_a = CameraSession(
            camera_id_a,
            descriptor_b,
            config_a,
            output_root_a,
            shutdown_callback=self.shutdown_callback,
            get_backend_index=self._get_backend_index,
            request_backend_failover=self._request_backend_failover,
            runtime_title=self.runtime_title,
            file_tag=self.file_tag,
        )
        new_session_b = CameraSession(
            camera_id_b,
            descriptor_a,
            config_b,
            output_root_b,
            shutdown_callback=self.shutdown_callback,
            get_backend_index=self._get_backend_index,
            request_backend_failover=self._request_backend_failover,
            runtime_title=self.runtime_title,
            file_tag=self.file_tag,
        )

        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            self.sessions[camera_id_a] = new_session_a
            self.sessions[camera_id_b] = new_session_b

        new_session_a.start()
        new_session_b.start()

        return {
            "swapped": True,
            "camera_a": new_session_a.status(),
            "camera_b": new_session_b.status(),
        }

    def close_camera(self, camera_id: str) -> Dict[str, object]:
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            session = self.sessions.pop(camera_id, None)
        if session is None:
            raise CommandError("CAMERA_NOT_FOUND", f"{camera_id} not found")
        session.stop()
        self.controls_ui.close_panel(camera_id)
        LOGGER.info("camera_closed camera_id=%s", camera_id)
        return {"camera_id": camera_id, "closed": True}

    def get_session(self, camera_id: str) -> CameraSession:
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            session = self.sessions.get(camera_id)
        if session is None:
            raise CommandError("CAMERA_NOT_FOUND", f"{camera_id} not found")
        return session

    def open_camera_panel(self, camera_id: str) -> Dict[str, object]:
        session = self.get_session(camera_id)
        result = session.open_controls_panel()
        self.controls_ui.open_panel(session)
        LOGGER.info("camera_controls_open_requested camera_id=%s", camera_id)
        return result

    def close_camera_panel(self, camera_id: str) -> Dict[str, object]:
        session = self.get_session(camera_id)
        result = session.close_controls_panel()
        self.controls_ui.close_panel(camera_id)
        LOGGER.info("camera_controls_close_requested camera_id=%s", camera_id)
        return result

    def reset_camera_properties(self, camera_id: str) -> Dict[str, object]:
        session = self.get_session(camera_id)
        result = session.reset_camera_properties()
        LOGGER.warning("camera_properties_reset_requested camera_id=%s", camera_id)
        return result

    def capture_snapshot(self, camera_id: str, output_path: Optional[Path] = None, command_name: Optional[str] = None) -> Dict[str, object]:
        normalized = (camera_id or "cam0").strip().lower()
        started_at = time.perf_counter()
        LOGGER.info(
            "camera_snapshot_requested camera_id=%s normalized=%s output_path=%s command_name=%s",
            camera_id,
            normalized,
            output_path,
            command_name,
        )
        if normalized != "all":
            session = self.get_session(camera_id)
            snapshot_path = session.snapshot(output_path, command_name=command_name)
            result = {"camera_id": camera_id, "snapshot_path": str(snapshot_path)}
            LOGGER.info(
                "camera_snapshot_completed camera_id=%s duration_ms=%s snapshot_path=%s",
                camera_id,
                int((time.perf_counter() - started_at) * 1000),
                snapshot_path,
            )
            return result

        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            sessions = list(self.sessions.values())
        if not sessions:
            raise CommandError("CAMERA_NOT_FOUND", "No opened cameras available for snapshot")

        results = []
        for session in sessions:
            snapshot_path = session.snapshot(None, command_name=f"{command_name}_{session.camera_id}" if command_name else session.camera_id)
            results.append({
                "camera_id": session.camera_id,
                "snapshot_path": str(snapshot_path),
            })
        result = {
            "camera_id": "all",
            "snapshots": results,
            "count": len(results),
        }
        LOGGER.info(
            "camera_snapshot_completed camera_id=all duration_ms=%s count=%s",
            int((time.perf_counter() - started_at) * 1000),
            len(results),
        )
        return result

    def start_recording(self, camera_id: str, duration_sec: int, output_dir: Optional[Path] = None, file_prefix: Optional[str] = None) -> Dict[str, object]:
        normalized = (camera_id or "cam0").strip().lower()
        if normalized != "all":
            session = self.get_session(camera_id)
            session.start_recording(duration_sec=duration_sec, output_dir=output_dir, file_prefix=file_prefix or camera_id)
            return {"camera_id": camera_id, "recording": True, "duration_sec": duration_sec}

        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            sessions = list(self.sessions.values())
        if not sessions:
            raise CommandError("CAMERA_NOT_FOUND", "No opened cameras available for recording")

        results = []
        for session in sessions:
            session.start_recording(
                duration_sec=duration_sec,
                output_dir=output_dir,
                file_prefix=f"{file_prefix}_{session.camera_id}" if file_prefix else session.camera_id,
            )
            results.append({
                "camera_id": session.camera_id,
                "recording": True,
                "duration_sec": duration_sec,
            })
        return {
            "camera_id": "all",
            "recording": True,
            "duration_sec": duration_sec,
            "cameras": results,
            "count": len(results),
        }

    def stop_recording(self, camera_id: str) -> Dict[str, object]:
        normalized = (camera_id or "cam0").strip().lower()
        if normalized != "all":
            session = self.get_session(camera_id)
            session.stop_recording()
            return {"camera_id": camera_id, "recording": False}

        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            sessions = list(self.sessions.values())
        if not sessions:
            raise CommandError("CAMERA_NOT_FOUND", "No opened cameras available for recording stop")

        results = []
        for session in sessions:
            session.stop_recording()
            results.append({
                "camera_id": session.camera_id,
                "recording": False,
            })
        return {
            "camera_id": "all",
            "recording": False,
            "cameras": results,
            "count": len(results),
        }

    def list_cameras(self) -> List[Dict[str, object]]:
        windows_devices = self._query_windows_camera_devices()
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            if self._windows_device_signature(windows_devices) != self.last_windows_device_snapshot:
                self._refresh_discovery_locked(windows_devices)
            cameras = [session.status() for session in self.sessions.values()]
        LOGGER.info("camera_list_cameras count=%s", len(cameras))
        return cameras

    def _list_devices_locked(self) -> List[Dict[str, object]]:
        bound_device_indexes = {
            session.descriptor.device_index: session.camera_id
            for session in self.sessions.values()
            if session.descriptor.connected and session.descriptor.device_index >= 0
        }

        assigned_slots = {
            descriptor.device_index: camera_id
            for camera_id, descriptor in self.logical_slots.items()
            if descriptor.connected and descriptor.device_index >= 0
        }

        devices = []
        for camera_index, descriptor in enumerate(self.descriptors):
            devices.append({
                "camera_index": camera_index,
                "device_index": descriptor.device_index,
                "friendly_name": descriptor.friendly_name,
                "device_id": descriptor.device_id,
                "pnp_device_id": descriptor.pnp_device_id,
                "location_information": descriptor.location_information,
                "manufacturer": descriptor.manufacturer,
                "is_external": descriptor.is_external,
                "assigned_camera_id": assigned_slots.get(descriptor.device_index, ""),
                "opened_camera_id": bound_device_indexes.get(descriptor.device_index, ""),
            })
        return devices

    def list_devices(self) -> List[Dict[str, object]]:
        windows_devices = self._query_windows_camera_devices()
        with acquired_lock(
            self.lock,
            MANAGER_LOCK_TIMEOUT_SEC,
            "CAMERA_BUSY",
            "Camera manager is busy recovering hardware state",
        ):
            if self._windows_device_signature(windows_devices) != self.last_windows_device_snapshot:
                self._refresh_discovery_locked(windows_devices)
            devices = self._list_devices_locked()
        LOGGER.info("camera_list_devices count=%s", len(devices))
        return devices

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.stop()
        if self.hotplug_thread and self.hotplug_thread.is_alive():
            self.hotplug_thread.join(timeout=2)
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
        self.controls_ui.stop()
        LOGGER.info("camera_manager_shutdown sessions=%s", len(sessions), extra={"console": True})


class CommandRouter:
    def __init__(self, camera_manager: CameraManager, output_dir: Path, auth_token: str, runtime_info: Optional[Dict[str, object]] = None):
        self.camera_manager = camera_manager
        self.output_dir = output_dir
        self.auth_token = auth_token.strip()
        self.runtime_info = runtime_info or {}
        self.set_grpc_port_callback: Optional[Callable[[int, bool], Dict[str, object]]] = None
        self.started_at = time.time()
        self.last_see_seen = 0.0
        self.connection_state = "waiting_reconnect"
        self.running = True
        self.monitor_thread = threading.Thread(target=self._connection_monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.camera_manager.set_shutdown_callback(self.shutdown)
        LOGGER.info("command_router_initialized output_dir=%s", self.output_dir, extra={"console": True})

    def validate_auth(self, token: str) -> None:
        if token != self.auth_token:
            raise CommandError("AUTH_FAILED", "Invalid SuperEagleEye auth token")

    def heartbeat(self, client_id: str, auth_token: str) -> Dict[str, object]:
        self.validate_auth(auth_token)
        self.last_see_seen = time.time()
        self.connection_state = "connected"
        result = {"connected": True, "client_id": client_id, "ack_hex": bytes_to_hex(ACK_BYTES)}
        LOGGER.info("grpc_heartbeat client_id=%s connected=%s", client_id, result["connected"])
        return result

    def _connection_monitor_loop(self) -> None:
        last_state = self.connection_state
        while self.running:
            if self.last_see_seen == 0.0 or time.time() - self.last_see_seen > HEARTBEAT_TIMEOUT_SEC:
                self.connection_state = "waiting_reconnect"
            else:
                self.connection_state = "connected"
            if self.connection_state != last_state:
                LOGGER.info("grpc_connection_state_changed state=%s", self.connection_state)
                last_state = self.connection_state
            time.sleep(1.0)

    def shutdown(self) -> None:
        self.running = False
        self.camera_manager.shutdown()
        LOGGER.info("command_router_shutdown", extra={"console": True})

    def execute(self, command: str, camera_id: str = "cam0", args: Optional[Dict[str, object]] = None, source: str = "cli") -> Dict[str, object]:
        args = args or {}
        normalized = command.strip().upper()
        LOGGER.info(
            "command_execute_requested source=%s command=%s camera_id=%s args=%s",
            source,
            normalized,
            camera_id or "cam0",
            args,
        )
        try:
            payload = self._handle_command(normalized, camera_id or "cam0", args, source)
            result = {
                "success": True,
                "code": "OK",
                "message": f"{normalized} completed",
                "payload": payload,
                "ack": ACK_BYTES,
                "source": source,
            }
            LOGGER.info("command_execute_completed command=%s camera_id=%s code=%s", normalized, camera_id or "cam0", result["code"])
            return result
        except CommandError as exc:
            result = {
                "success": False,
                "code": exc.code,
                "message": exc.message,
                "payload": {},
                "ack": ACK_BYTES,
                "source": source,
            }
            LOGGER.warning("command_execute_failed command=%s camera_id=%s code=%s message=%s", normalized, camera_id or "cam0", exc.code, exc.message)
            return result
        except Exception as exc:
            result = {
                "success": False,
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "payload": {},
                "ack": ACK_BYTES,
                "source": source,
            }
            LOGGER.exception("command_execute_exception command=%s camera_id=%s", normalized, camera_id or "cam0")
            return result

    def query(self, query_name: str, camera_id: str = "cam0", args: Optional[Dict[str, object]] = None, source: str = "cli") -> Dict[str, object]:
        args = args or {}
        normalized = query_name.strip().upper()
        LOGGER.info(
            "query_requested source=%s query=%s camera_id=%s args=%s",
            source,
            normalized,
            camera_id or "cam0",
            args,
        )
        try:
            payload = self._handle_query(normalized, camera_id or "cam0", args)
            result = {
                "success": True,
                "code": "OK",
                "message": f"{normalized} completed",
                "payload": payload,
                "source": source,
            }
            LOGGER.info("query_completed query=%s camera_id=%s code=%s", normalized, camera_id or "cam0", result["code"])
            return result
        except CommandError as exc:
            result = {
                "success": False,
                "code": exc.code,
                "message": exc.message,
                "payload": {},
                "source": source,
            }
            LOGGER.warning("query_failed query=%s camera_id=%s code=%s message=%s", normalized, camera_id or "cam0", exc.code, exc.message)
            return result
        except Exception as exc:
            result = {
                "success": False,
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "payload": {},
                "source": source,
            }
            LOGGER.exception("query_exception query=%s camera_id=%s", normalized, camera_id or "cam0")
            return result

    def _handle_command(self, command: str, camera_id: str, args: Dict[str, object], source: str) -> Dict[str, object]:
        if command == "PING":
            return {"ack_hex": bytes_to_hex(ACK_BYTES), "connection_state": self.connection_state}
        if command == "SET_GRPC_PORT":
            port, defaulted = normalize_grpc_port(args.get("port"))
            if self.set_grpc_port_callback is None:
                return {
                    "grpc_port": port,
                    "defaulted": defaulted,
                    "range": f"{GRPC_PORT_MIN}-{GRPC_PORT_MAX}",
                    "message": "gRPC port callback is not configured",
                }
            payload = self.set_grpc_port_callback(port, source.lower() != "cli")
            payload["defaulted"] = defaulted
            payload["range"] = f"{GRPC_PORT_MIN}-{GRPC_PORT_MAX}"
            return payload
        if command == "OPEN_CAMERA":
            return self.camera_manager.open_camera(camera_id, wait_initial=False)
        if command == "CLOSE_CAMERA":
            return self.camera_manager.close_camera(camera_id)
        if command == "OPEN_CAMERA_PANEL":
            return self.camera_manager.open_camera_panel(camera_id)
        if command == "CLOSE_CAMERA_PANEL":
            return self.camera_manager.close_camera_panel(camera_id)
        if command == "RESET_CAMERA_PROPERTIES":
            return self.camera_manager.reset_camera_properties(camera_id)
        if command == "SHOW_RUNTIME_INFO":
            return self._runtime_info_payload()
        if command == "SCAN_DEVICES":
            return self.camera_manager.rescan_devices()
        if command == "REFRESH_CAMERAS":
            return self.camera_manager.refresh_cameras()
        if command == "SWAP_CAMERAS":
            source_camera_id = str(args.get("source_camera_id") or camera_id or "").strip()
            target_camera_id = str(args.get("target_camera_id") or "").strip()
            if not source_camera_id or not target_camera_id:
                raise CommandError("INVALID_ARGUMENT", "swap requires source_camera_id and target_camera_id")
            return self.camera_manager.swap_cameras(source_camera_id, target_camera_id)
        if command == "START_RECORD":
            default_duration = self.camera_manager.get_session(camera_id).config.recording_duration if (camera_id or "").strip().lower() != "all" else self.camera_manager.default_config.recording_duration
            duration_sec = coerce_int(args.get("duration_sec", default_duration), "duration_sec", min_value=1)
            output_dir = Path(args["output_dir"]) if args.get("output_dir") else None
            file_prefix = str(args.get("file_prefix", camera_id))
            return self.camera_manager.start_recording(camera_id, duration_sec=duration_sec, output_dir=output_dir, file_prefix=file_prefix)
        if command == "STOP_RECORD":
            return self.camera_manager.stop_recording(camera_id)
        if command == "CAPTURE_SNAPSHOT":
            output_path = Path(args["output_path"]) if args.get("output_path") else None
            command_name = str(args.get("command_name") or "").strip() or None
            return self.camera_manager.capture_snapshot(camera_id, output_path, command_name=command_name)
        if command == "SET_CAMERA_CONFIG":
            session = self.camera_manager.get_session(camera_id)
            allowed = {k: args[k] for k in ["width", "height", "fps", "recording_duration", "max_folder_size_gb"] if k in args}
            if not allowed:
                raise CommandError("INVALID_ARGUMENT", "No camera settings provided")
            session.apply_config(allowed)
            return session.status()
        if command == "SET_OUTPUT_ROOT":
            raw_output_dir = str(args.get("output_dir") or args.get("save_path") or "").strip()
            if not raw_output_dir:
                raise CommandError("INVALID_ARGUMENT", "Missing output_dir")
            resolved = self.camera_manager.set_output_dir(Path(raw_output_dir))
            return {"output_dir": str(resolved)}
        if command == "OPEN_OUTPUT_FOLDER":
            output_dir = self.camera_manager.output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            opened = False
            if source.lower() == "cli":
                try:
                    os.startfile(str(output_dir))
                    opened = True
                except Exception as exc:
                    raise CommandError("INTERNAL_ERROR", f"Failed to open output folder '{output_dir}': {exc}") from exc
            print(f"[SuperEagleEye] output folder: {output_dir}")
            return {
                "output_dir": str(output_dir),
                "actual_path": str(output_dir),
                "opened": opened,
            }
        if command == "SHUTDOWN":
            self.shutdown()
            return {"shutdown": True}
        raise CommandError("INVALID_COMMAND", f"Unsupported command: {command}")

    def _handle_query(self, query_name: str, camera_id: str, args: Dict[str, object]) -> Dict[str, object]:
        if query_name == "GET_STATUS":
            cameras = self.camera_manager.list_cameras()
            return {
                "connection_state": self.connection_state,
                "uptime_sec": int(time.time() - self.started_at),
                "camera_count": len(cameras),
                "default_camera_id": "cam0",
                "recording_cameras": [camera["camera_id"] for camera in cameras if camera["recording"]],
            }
        if query_name == "LIST_CAMERAS":
            return {"cameras": self.camera_manager.list_cameras()}
        if query_name == "LIST_DEVICES":
            return {"devices": self.camera_manager.list_devices()}
        if query_name == "GET_CAMERA_CONFIG":
            return self.camera_manager.get_session(camera_id).status()
        if query_name == "GET_RECORD_STATE":
            session = self.camera_manager.get_session(camera_id)
            return {"camera_id": camera_id, "recording": session.status()["recording"]}
        if query_name == "GET_RUNTIME_INFO":
            return self._runtime_info_payload()
        raise CommandError("INVALID_COMMAND", f"Unsupported query: {query_name}")

    def _runtime_info_payload(self) -> Dict[str, object]:
        cameras = self.camera_manager.list_cameras()
        devices = self.camera_manager.list_devices()
        return {
            "runtime": dict(self.runtime_info),
            "connection_state": self.connection_state,
            "uptime_sec": int(time.time() - self.started_at),
            "cameras": cameras,
            "devices": devices,
            "paths": {
                "base_dir": str(BASE_DIR),
                "output_dir": str(self.camera_manager.output_dir.resolve()),
                "log_dir": str(get_runtime_log_dir().resolve()),
                "camera_map": str((BASE_DIR / CAMERA_MAP_FILE_NAME).resolve()),
                "version_file": str((BASE_DIR / VERSION_FILE_NAME).resolve()),
            },
            "environment": {
                "platform": sys.platform,
                "python": sys.version,
                "opencv": cv2.__version__,
                "frozen": bool(getattr(sys, "frozen", False)),
            },
        }


class See10Service(pb2_grpc.SC_communication_gRPCServicer):
    def __init__(self, router: CommandRouter):
        self.router = router
        LOGGER.info("grpc_service_initialized")

    def Heartbeat(self, request, context):
        try:
            LOGGER.info("grpc_heartbeat_request client_id=%s", request.client_id)
            payload = self.router.heartbeat(request.client_id, request.auth_token)
        except CommandError as exc:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(exc.message)
            LOGGER.warning("grpc_heartbeat_failed client_id=%s code=%s message=%s", request.client_id, exc.code, exc.message)
            return pb2.HeartbeatResponse(connected=False, ack=b"", server_time=current_millis(), message=exc.message)
        LOGGER.info("grpc_heartbeat_response client_id=%s connected=%s", request.client_id, True)
        return pb2.HeartbeatResponse(
            connected=True,
            ack=ACK_BYTES,
            server_time=current_millis(),
            message=json.dumps(payload, ensure_ascii=False),
        )

    def ExecuteCameraCommand(self, request, context):
        try:
            self.router.validate_auth(request.auth_token)
            args = parse_json_dict(request.args_json)
            LOGGER.info(
                "grpc_execute_request request_id=%s source=%s command=%s camera_id=%s args_json=%s",
                request.request_id,
                request.source,
                request.command,
                request.camera_id,
                request.args_json,
            )
            result = self.router.execute(request.command, request.camera_id or "cam0", args, source=request.source or "grpc")
        except CommandError as exc:
            if exc.code == "AUTH_FAILED":
                context.set_code(grpc.StatusCode.UNAUTHENTICATED)
                context.set_details(exc.message)
            result = {"success": False, "code": exc.code, "message": exc.message, "payload": {}, "ack": b"", "source": "grpc"}
            LOGGER.warning("grpc_execute_failed request_id=%s code=%s message=%s", request.request_id, exc.code, exc.message)
        payload_json = json.dumps(result["payload"], ensure_ascii=False)
        LOGGER.info(
            "grpc_execute_response request_id=%s success=%s code=%s message=%s",
            request.request_id,
            result["success"],
            result["code"],
            result["message"],
        )
        return pb2.CommandReply(
            success=result["success"],
            request_id=request.request_id,
            ack=result["ack"],
            result_code=RESULT_CODES[result["code"]],
            message=result["message"],
            response_frame=b"",
            payload_json=payload_json,
            server_time=current_millis(),
        )

    def QueryCameraState(self, request, context):
        try:
            self.router.validate_auth(request.auth_token)
            args = parse_json_dict(request.args_json)
            LOGGER.info(
                "grpc_query_request request_id=%s query=%s camera_id=%s args_json=%s",
                request.request_id,
                request.query,
                request.camera_id,
                request.args_json,
            )
            result = self.router.query(request.query, request.camera_id or "cam0", args, source="grpc")
        except CommandError as exc:
            if exc.code == "AUTH_FAILED":
                context.set_code(grpc.StatusCode.UNAUTHENTICATED)
                context.set_details(exc.message)
            result = {"success": False, "code": exc.code, "message": exc.message, "payload": {}, "source": "grpc"}
            LOGGER.warning("grpc_query_failed request_id=%s code=%s message=%s", request.request_id, exc.code, exc.message)
        payload_json = json.dumps(result["payload"], ensure_ascii=False)
        response_frame = build_query_frame(request.request_id, request.query, result["code"], payload_json)
        LOGGER.info(
            "grpc_query_response request_id=%s success=%s code=%s message=%s",
            request.request_id,
            result["success"],
            result["code"],
            result["message"],
        )
        return pb2.QueryReply(
            success=result["success"],
            request_id=request.request_id,
            result_code=RESULT_CODES[result["code"]],
            message=result["message"],
            response_frame=response_frame,
            payload_json=payload_json,
            server_time=current_millis(),
        )

    def SendMessage(self, request, context):
        LOGGER.info("grpc_send_message_request sender=%s message=%s", request.sender, request.message)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Use ExecuteCameraCommand instead")
        return pb2.MessageResponse()

    def SubscribeMessages(self, request, context):
        LOGGER.info("grpc_subscribe_messages_request client_id=%s", request.client_id)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.MessageResponse()

    def SubscribeUpdates(self, request, context):
        LOGGER.info("grpc_subscribe_updates_request client_id=%s", request.client_id)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.MessageResponse()

    def SendImage(self, request, context):
        LOGGER.info("grpc_send_image_request file_name=%s width=%s height=%s", request.file_name, request.width, request.height)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.ImageResponse()

    def CommandMessage(self, request, context):
        LOGGER.info("grpc_command_message_request sender=%s message=%s", request.sender, request.message)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Use ExecuteCameraCommand instead")
        yield pb2.MsgResponse()


def current_millis() -> int:
    return int(time.time() * 1000)


def parse_json_dict(raw: str) -> Dict[str, object]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise CommandError("INVALID_ARGUMENT", "args_json must be a JSON object")
    return data


def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{item:02X}" for item in data)


def build_query_frame(request_id: str, query_name: str, code: str, payload_json: str) -> bytes:
    msg_type = QUERY_MSG_TYPES.get(query_name.upper(), 0x7F)
    result_code = RESULT_CODES[code]
    payload = payload_json.encode("utf-8")
    request_bytes = int(uuid.UUID(request_id).int & 0xFFFFFFFF).to_bytes(4, "big") if _looks_like_uuid(request_id) else hash(request_id).to_bytes(8, "big", signed=True)[-4:]
    return bytes([FRAME_STX, FRAME_VERSION, msg_type]) + request_bytes + bytes([result_code]) + len(payload).to_bytes(4, "big") + payload + bytes([FRAME_ETX])


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except Exception:
        return False


def coerce_int(value, name: str, *, min_value: Optional[int] = None) -> int:
    if value is None:
        raise CommandError("INVALID_ARGUMENT", f"Missing {name}")
    try:
        parsed = int(value)
    except Exception as exc:
        raise CommandError("INVALID_ARGUMENT", f"{name} must be an integer") from exc
    if min_value is not None and parsed < min_value:
        raise CommandError("INVALID_ARGUMENT", f"{name} must be >= {min_value}")
    return parsed


def parse_cli_line(line: str) -> Tuple[str, str, Dict[str, object], bool]:
    parts = [item for item in line.strip().split() if item]
    if not parts:
        raise CommandError("INVALID_ARGUMENT", "Empty command")
    cmd = parts[0].lower()
    if cmd == "ping":
        return "PING", "cam0", {}, False
    if cmd == "status":
        return "GET_STATUS", "cam0", {}, True
    if cmd == "list":
        return "LIST_CAMERAS", "cam0", {}, True
    if cmd == "list_devices":
        return "LIST_DEVICES", "cam0", {}, True
    if cmd == "info" or cmd == "runtime_info":
        return "GET_RUNTIME_INFO", "cam0", {}, True
    if cmd == "scan_devices" or cmd == "rescan":
        return "SCAN_DEVICES", "cam0", {}, False
    if cmd == "refresh_cameras" or cmd == "refresh":
        return "REFRESH_CAMERAS", "cam0", {}, False
    if cmd == "set_grpc_port" or cmd == "grpc_port":
        port = parts[1] if len(parts) > 1 else ""
        return "SET_GRPC_PORT", "cam0", {"port": port}, False
    if cmd == "snapshot":
        return "CAPTURE_SNAPSHOT", parts[1] if len(parts) > 1 else "cam0", {}, False
    if cmd == "open_output_folder":
        return "OPEN_OUTPUT_FOLDER", "cam0", {}, False
    if cmd == "open":
        if len(parts) < 2:
            raise CommandError("INVALID_ARGUMENT", "Usage: open <camera_id>")
        return "OPEN_CAMERA", parts[1], {}, False
    if cmd == "panel" or cmd == "open_panel":
        if len(parts) < 2:
            raise CommandError("INVALID_ARGUMENT", "Usage: panel <camera_id>")
        return "OPEN_CAMERA_PANEL", parts[1], {}, False
    if cmd == "close_panel":
        if len(parts) < 2:
            raise CommandError("INVALID_ARGUMENT", "Usage: close_panel <camera_id>")
        return "CLOSE_CAMERA_PANEL", parts[1], {}, False
    if cmd in {"reset_panel", "reset_camera_properties", "reset_properties"}:
        if len(parts) < 2:
            raise CommandError("INVALID_ARGUMENT", f"Usage: {cmd} <camera_id>")
        return "RESET_CAMERA_PROPERTIES", parts[1], {}, False
    if cmd == "close":
        if len(parts) < 2:
            raise CommandError("INVALID_ARGUMENT", "Usage: close <camera_id>")
        return "CLOSE_CAMERA", parts[1], {}, False
    if cmd == "swap" or cmd == "change":
        if len(parts) < 3:
            raise CommandError("INVALID_ARGUMENT", "Usage: swap <camera_id_a> <camera_id_b>")
        return "SWAP_CAMERAS", parts[1], {"source_camera_id": parts[1], "target_camera_id": parts[2]}, False
    if cmd == "record_start":
        camera_id = parts[1] if len(parts) > 1 else "cam0"
        duration_sec = coerce_int(parts[2], "duration_sec", min_value=1) if len(parts) > 2 else 60
        return "START_RECORD", camera_id, {"duration_sec": duration_sec}, False
    if cmd == "record_stop":
        return "STOP_RECORD", parts[1] if len(parts) > 1 else "cam0", {}, False
    if cmd == "config":
        return "GET_CAMERA_CONFIG", parts[1] if len(parts) > 1 else "cam0", {}, True
    if cmd == "record_state":
        return "GET_RECORD_STATE", parts[1] if len(parts) > 1 else "cam0", {}, True
    if cmd == "set":
        if len(parts) < 3:
            raise CommandError("INVALID_ARGUMENT", "Usage: set <camera_id> key=value ...")
        args = {}
        for item in parts[2:]:
            if "=" not in item:
                raise CommandError("INVALID_ARGUMENT", f"Invalid setting: {item}")
            key, value = item.split("=", 1)
            args[key] = coerce_int(value, key, min_value=0 if key == "max_folder_size_gb" else 1)
        return "SET_CAMERA_CONFIG", parts[1], args, False
    if cmd == "quit" or cmd == "exit":
        return "SHUTDOWN", "cam0", {}, False
    raise CommandError("INVALID_COMMAND", f"Unsupported CLI command: {cmd}")


def print_cli_help() -> None:
    rows = [
        ("help | ? | commands", "Show this command reference."),
        ("status", "Show runtime connection status."),
        ("list", "List opened logical cameras."),
        ("list_devices", "List discovered camera indexes, friendly names, and current bindings."),
        ("info | runtime_info", "Show runtime version, paths, environment, cameras, and devices."),
        ("scan_devices | rescan", "Manually rescan camera devices and refresh logical slot bindings."),
        ("refresh_cameras | refresh", "Rescan devices, rebuild logical slots, open connected cameras, and reopen existing sessions."),
        (
            "set_grpc_port | grpc_port [port]",
            f"Rebind the current gRPC listener. Allowed range: {GRPC_PORT_MIN}-{GRPC_PORT_MAX}. Invalid input falls back to {DEFAULT_GRPC_PORT}.",
        ),
        ("config [camera_id]", "Show camera config. Default: cam0"),
        ("record_state [camera_id]", "Show recording state. Default: cam0"),
        ("snapshot [camera_id]", "Save one frame to disk. Default: cam0"),
        ("open_output_folder", "Print and open the actual current output folder in File Explorer."),
        ("open <camera_id>", "Open the assigned logical camera slot again."),
        ("panel | open_panel <camera_id>", "Open the OpenCV controls panel for a camera."),
        ("close_panel <camera_id>", "Close the OpenCV controls panel for a camera."),
        ("reset_panel | reset_camera_properties <camera_id>", "Clear modified camera properties and reopen with driver defaults."),
        ("close <camera_id>", "Close a camera preview window."),
        ("swap <camera_id_a> <camera_id_b>", "Swap the hardware bindings behind two logical camera ids. Alias: change"),
        ("record_start [camera_id] [duration_sec]", "Start recording. Defaults: cam0, 60"),
        ("record_stop [camera_id]", "Stop recording. Default: cam0"),
        (
            "set <camera_id> key=value ...",
            "Update config keys: width height fps recording_duration max_folder_size_gb. Example: set cam0 width=1280 height=720 fps=20",
        ),
        ("exit | quit", "Shut down SuperEagleEye."),
    ]
    command_width = max(len(command) for command, _ in rows) + 2

    print("SuperEagleEye CLI commands:")
    print()
    for command, description in rows:
        print(f"  {command:<{command_width}}{description}")


def maybe_prompt_grpc_port(line: str) -> str:
    parts = [item for item in line.strip().split() if item]
    if not parts or parts[0].lower() not in {"set_grpc_port", "grpc_port"} or len(parts) > 1:
        return line

    print(f"gRPC port can be set from {GRPC_PORT_MIN} to {GRPC_PORT_MAX}.")
    print(f"Invalid input will use the default port: {DEFAULT_GRPC_PORT}.")
    try:
        raw_port = input("Enter gRPC port number: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw_port = ""
    return f"{parts[0]} {raw_port}"


def cli_loop(router: CommandRouter):
    print("SuperEagleEye interactive terminal ready. Type help for commands.")
    while router.running:
        try:
            line = input("SEE> ").strip()
        except (EOFError, KeyboardInterrupt):
            line = "exit"
        if not line:
            continue
        if line.lower() in {"help", "?", "commands"}:
            print_cli_help()
            continue
        try:
            line = maybe_prompt_grpc_port(line)
            command, camera_id, args, is_query = parse_cli_line(line)
            if is_query:
                result = router.query(command, camera_id, args)
                payload_json = json.dumps(result["payload"], ensure_ascii=False, indent=2)
                frame = build_query_frame(str(uuid.uuid4()), command, result["code"], json.dumps(result["payload"], ensure_ascii=False))
                print(payload_json)
                print(f"frame: {bytes_to_hex(frame)}")
            else:
                result = router.execute(command, camera_id, args)
                print(f"ack: {bytes_to_hex(result['ack'])}")
                if result["payload"]:
                    print(json.dumps(result["payload"], ensure_ascii=False, indent=2))
                if command == "SHUTDOWN":
                    break
        except CommandError as exc:
            print(f"error[{exc.code}]: {exc.message}")
        except Exception as exc:
            print(f"error: {exc}")


def run_until_shutdown(router: CommandRouter) -> None:
    stdin = getattr(sys, "stdin", None)
    interactive = bool(stdin) and stdin.isatty()
    if interactive:
        cli_loop(router)
        return

    print("SuperEagleEye running without interactive stdin; CLI disabled.")
    try:
        while router.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        router.shutdown()


def create_default_camera_map(path: Path) -> None:
    if path.exists():
        return
    payload = CameraManager._default_alias_config()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class GrpcServerController:
    def __init__(self, router: CommandRouter, initial_port: int):
        self.router = router
        self.port = initial_port
        self.server = None
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            if self.server is not None:
                return
            self.server = self._create_server(self.port)
            LOGGER.info("grpc_server_listening port=%s", self.port, extra={"console": True})

    def stop(self) -> None:
        with self.lock:
            if self.server is None:
                return
            self.server.stop(0)
            self.server = None

    def set_port(self, port: int, defer_restart: bool = False) -> Dict[str, object]:
        with self.lock:
            previous_port = self.port
            if previous_port == port:
                return {
                    "grpc_port": self.port,
                    "previous_grpc_port": previous_port,
                    "restarted": False,
                    "message": f"gRPC port is already {self.port}",
                }
            self.port = port

        if defer_restart:
            threading.Thread(target=self._restart_after_reply, daemon=True).start()
            return {
                "grpc_port": port,
                "previous_grpc_port": previous_port,
                "restarted": False,
                "restart_deferred": True,
                "message": f"gRPC listener will restart on port {port}",
            }

        self.restart()
        return {
            "grpc_port": port,
            "previous_grpc_port": previous_port,
            "restarted": True,
            "message": f"gRPC listener restarted on port {port}",
        }

    def restart(self) -> None:
        with self.lock:
            if self.server is not None:
                self.server.stop(0)
                self.server = None
            self.server = self._create_server(self.port)
            LOGGER.info("grpc_server_listening port=%s", self.port, extra={"console": True})

    def _restart_after_reply(self) -> None:
        time.sleep(0.2)
        self.restart()

    def _create_server(self, port: int):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        pb2_grpc.add_SC_communication_gRPCServicer_to_server(See10Service(self.router), server)
        bound_port = server.add_insecure_port(f"127.0.0.1:{port}")
        if bound_port == 0:
            raise CommandError("INTERNAL_ERROR", f"Failed to bind gRPC port {port}")
        server.start()
        return server


def basic_options():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--frame_width", type=int, default=640, help="Output video frame width in pixels.")
    parser.add_argument("--frame_height", type=int, default=480, help="Output video frame height in pixels.")
    parser.add_argument("--frame_rate", type=int, default=20, help="Frames per second (FPS) for the output video.")
    parser.add_argument("--recording_duration", type=int, default=60, help="Maximum recording duration in seconds per segment.")
    parser.add_argument("--max_foldersize", type=int, default=10, help="Maximum allowed size (GB) for the output video folder.")
    parser.add_argument("--grpc_port", type=str, default=str(DEFAULT_GRPC_PORT), help=f"gRPC port. Valid range: {GRPC_PORT_MIN}-{GRPC_PORT_MAX}.")
    parser.add_argument("--instance_id", type=str, default="default", help="Runtime instance id. Use a different value only for intentional multi-instance runs.")
    parser.add_argument("--device_indexes", type=str, default="", help="Comma-separated OpenCV device indexes this instance may use, for example: 0 or 1,2.")
    parser.add_argument("--auth_token", type=str, default=os.environ.get(SHARED_SECRET_ENV_VAR, ""), help="Shared secret required for gRPC control.")
    return parser


def data_options(parser):
    parser.add_argument("--save_path", type=str, default="./videos", help="Path to save output snapshots and video files.")
    parser.add_argument("--file_path", type=str, default="SuperEagleEye.py", help="Current script path, kept for compatibility.")
    return parser


def main():
    parser = data_options(basic_options())
    opt, _ = parser.parse_known_args()
    instance_id = normalize_instance_id(opt.instance_id)
    grpc_port, defaulted_grpc_port = normalize_grpc_port(opt.grpc_port)
    log_path = configure_runtime_logging(instance_id, grpc_port)
    LOGGER.info("logging_initialized path=%s", log_path, extra={"console": True})
    if not acquire_single_instance_lock(instance_id):
        LOGGER.warning("another_runtime_instance_running instance_id=%s", instance_id, extra={"console": True})
        return

    output_dir = Path(opt.save_path).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    create_default_camera_map(BASE_DIR / CAMERA_MAP_FILE_NAME)
    runtime_version = load_runtime_version(BASE_DIR)

    LOGGER.info(
        "runtime_version runtime_name=%s version=%s min_see_version=%s min_supercarter_version=%s",
        runtime_version["runtime_name"],
        runtime_version["version"],
        runtime_version["min_see_version"],
        runtime_version["min_supercarter_version"],
        extra={"console": True},
    )

    camera_config = CameraConfig(
        width=opt.frame_width,
        height=opt.frame_height,
        fps=opt.frame_rate,
        recording_duration=opt.recording_duration,
        max_folder_size_gb=opt.max_foldersize,
    )
    auth_token = resolve_shared_secret(opt.auth_token)
    if not auth_token:
        raise RuntimeError("SuperEagleEye shared secret is missing. Launch from SEE, set SEE_SUPER_EAGLE_EYE_SECRET, or provide --auth_token.")

    if defaulted_grpc_port:
        LOGGER.warning("invalid_grpc_port value=%s default_port=%s", opt.grpc_port, DEFAULT_GRPC_PORT, extra={"console": True})

    allowed_device_indexes = parse_device_indexes(opt.device_indexes)
    if allowed_device_indexes is not None:
        LOGGER.info("instance_device_indexes instance_id=%s device_indexes=%s", instance_id, allowed_device_indexes, extra={"console": True})

    runtime_title = format_runtime_title(instance_id, grpc_port, allowed_device_indexes, output_dir)
    runtime_file_tag = format_runtime_file_tag(grpc_port)
    LOGGER.info("runtime_title %s", runtime_title, extra={"console": True})
    LOGGER.info("runtime_startup output_dir=%s grpc_port=%s instance_id=%s", output_dir, grpc_port, instance_id, extra={"console": True})

    controls_ui = CameraControlsUI()
    controls_ui.start()

    camera_manager = CameraManager(
        camera_config,
        output_dir,
        BASE_DIR / CAMERA_MAP_FILE_NAME,
        controls_ui,
        allowed_device_indexes=allowed_device_indexes,
        runtime_title=runtime_title,
        file_tag=runtime_file_tag,
    )
    router = CommandRouter(
        camera_manager,
        output_dir,
        auth_token,
        runtime_info={
            "runtime_name": runtime_version["runtime_name"],
            "version": runtime_version["version"],
            "min_see_version": runtime_version["min_see_version"],
            "min_supercarter_version": runtime_version["min_supercarter_version"],
            "instance_id": instance_id,
            "grpc_port": grpc_port,
            "device_indexes": allowed_device_indexes or [],
            "runtime_title": runtime_title,
            "file_tag": runtime_file_tag,
        },
    )
    grpc_controller = GrpcServerController(router, grpc_port)
    router.set_grpc_port_callback = grpc_controller.set_port
    grpc_controller.start()

    try:
        run_until_shutdown(router)
    finally:
        router.shutdown()
        grpc_controller.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        paths = write_crash_log(exc)
        traceback.print_exc()
        pause_on_fatal_error(paths)
        raise
