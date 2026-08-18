"""Wire-format helpers shared by the CLI and the gRPC service: JSON arg
parsing, hex dumps, the binary query-response frame, and integer coercion.
"""

import json
import time
import uuid
from typing import Dict, Optional

from see_runtime.constants import FRAME_ETX, FRAME_STX, FRAME_VERSION, QUERY_MSG_TYPES, RESULT_CODES
from see_runtime.errors import CommandError


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
