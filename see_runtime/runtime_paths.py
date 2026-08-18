"""Runtime path/identity helpers: instance id, device index parsing, log/crash
log locations, and version.json loading. Corresponds to `doc/runtime-bootstrap.md`
and `doc/logging-runtime-state.md`.
"""

import json
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional

from see_runtime.bootstrap import get_base_dir
from see_runtime.constants import APP_RUNTIME_DIR, CRASH_LOG_FILE_NAME, SHARED_SECRET_FILE_NAME, VERSION_FILE_NAME
from see_runtime.protocol_utils import coerce_int


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
    # Not `Path(__file__)`: this module no longer lives next to SuperEagleEye.py,
    # and sys.argv[0] is what was actually invoked (`python SuperEagleEye.py`),
    # so it still resolves to the entry script's directory.
    return Path(sys.argv[0]).resolve().parent / "logs"


def build_runtime_log_path(instance_id: str, grpc_port: int) -> Path:
    safe_instance_id = normalize_instance_id(instance_id)
    return get_runtime_log_dir() / f"SuperEagleEye_{safe_instance_id}_grpc{grpc_port}.log"


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
