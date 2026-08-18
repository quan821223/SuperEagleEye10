"""Runtime-wide constants: protocol bytes, port range, file names, result codes.

Pure values only, no logic, so any module can import this without risking a
circular import.
"""

import os
from pathlib import Path

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
CRASH_LOG_FILE_NAME = "SuperEagleEye.crash.log"
SINGLE_INSTANCE_MUTEX_NAME = "Local\\SuperEagleEye_SEE10_Runtime"

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
