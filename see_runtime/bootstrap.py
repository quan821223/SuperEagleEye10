"""First-touch runtime setup: OpenCV log silencing, base_dir resolution,
proto codegen, and the grpc/pb2 imports everything else builds on.

Every other module that needs `cv2`, `grpc`, `pb2`, or `pb2_grpc` should
import them from here (`from see_runtime.bootstrap import cv2`, etc.)
rather than importing them directly, so this module's setup always runs
first regardless of which module happens to be imported first.
"""

import os
import subprocess
import sys
from pathlib import Path

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

from see_runtime.constants import PROTO_FILE_NAME
from see_runtime.shell_utils import _decode_shell_output


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Not `Path(__file__)`: this module no longer lives next to SuperEagleEye.py,
    # and sys.argv[0] is what was actually invoked (`python SuperEagleEye.py`),
    # so it still resolves to the entry script's directory.
    return Path(sys.argv[0]).resolve().parent


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


BASE_DIR = get_base_dir()
ensure_proto_generated(BASE_DIR)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import grpc  # noqa: E402
from concurrent import futures  # noqa: E402
import SC_communication_gRPC_pb2 as pb2  # noqa: E402
import SC_communication_gRPC_pb2_grpc as pb2_grpc  # noqa: E402
