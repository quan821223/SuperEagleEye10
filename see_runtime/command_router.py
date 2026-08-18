"""Command/query dispatch, auth validation, connection state, and the
runtime info payload — the single point both the CLI and the gRPC service
route through. See `doc/command-router.md`.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from see_runtime.bootstrap import BASE_DIR, cv2
from see_runtime.camera_manager import CameraManager
from see_runtime.camera_models import dated_output_dir, normalize_grpc_port
from see_runtime.constants import ACK_BYTES, CAMERA_MAP_FILE_NAME, GRPC_PORT_MAX, GRPC_PORT_MIN, HEARTBEAT_TIMEOUT_SEC, VERSION_FILE_NAME
from see_runtime.errors import CommandError
from see_runtime.protocol_utils import bytes_to_hex, coerce_int
from see_runtime.runtime_paths import get_runtime_log_dir

LOGGER = logging.getLogger("SuperEagleEye")


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
            output_dir = dated_output_dir(self.camera_manager.output_dir.resolve())
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
                "current_output_dir": str(dated_output_dir(self.camera_manager.output_dir.resolve())),
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
