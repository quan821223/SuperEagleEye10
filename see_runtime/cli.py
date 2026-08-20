"""Interactive terminal: command parsing, help text, and the CLI loop. See
`doc/cli-interface.md`.
"""

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, Tuple

from see_runtime.camera_manager import CameraManager
from see_runtime.command_router import CommandRouter
from see_runtime.constants import DEFAULT_GRPC_PORT, GRPC_PORT_MAX, GRPC_PORT_MIN
from see_runtime.errors import CommandError
from see_runtime.protocol_utils import bytes_to_hex, build_query_frame, coerce_int


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
        (
            "record_start docs",
            "Detailed START_RECORD notes: https://quan821223.github.io/Doc-SuperCarter/see10-supplemental-notes/#start_record",
        ),
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
    print()
    print("Type 'help clients' to see the commands external control clients (SuperCarter / DDS) send to this runtime.")


def print_client_command_help() -> None:
    command_rows = [
        ("PING", "Health check. Returns ack_hex and connection_state."),
        ("OPEN_CAMERA", "Open or reopen a logical camera slot. Requires camera_id."),
        ("CLOSE_CAMERA", "Close one logical camera slot. Requires camera_id."),
        ("OPEN_CAMERA_PANEL", "Open the controls panel for a camera. Requires camera_id."),
        ("CLOSE_CAMERA_PANEL", "Close the controls panel for a camera. Requires camera_id."),
        ("RESET_CAMERA_PROPERTIES", "Clear modified camera properties and reopen with driver defaults. Requires camera_id."),
        ("SHOW_RUNTIME_INFO", "Return runtime version, paths, environment, cameras, and devices."),
        ("SCAN_DEVICES", "Rescan camera devices and refresh logical slot bindings."),
        ("REFRESH_CAMERAS", "Rescan devices, rebuild logical slots, open connected cameras, and reopen existing sessions."),
        ("SWAP_CAMERAS", "Swap hardware bindings behind two logical slots. args_json: source_camera_id, target_camera_id."),
        ("START_RECORD", "Start segmented recording. Requires camera_id. args_json: duration_sec, output_dir, file_prefix."),
        ("STOP_RECORD", "Stop recording. Requires camera_id."),
        ("CAPTURE_SNAPSHOT", "Save one frame to disk. camera_id accepts cam0/cam1/cam2 or 'all'. args_json: output_path."),
        ("SET_CAMERA_CONFIG", "Update capture settings. Requires camera_id. args_json: width, height, fps, recording_duration, max_folder_size_gb."),
        ("SET_OUTPUT_ROOT", "Update the default save folder. args_json: output_dir (or save_path)."),
        ("OPEN_OUTPUT_FOLDER", "Return the currently active output folder."),
        ("SET_GRPC_PORT", "Rebind this runtime's control-channel listener port. args_json: port."),
        ("SHUTDOWN", "Request full runtime shutdown."),
    ]
    query_rows = [
        ("GET_STATUS", "Runtime-level state: connection_state, uptime_sec, camera_count, recording_cameras."),
        ("LIST_CAMERAS", "List opened logical cameras and their status."),
        ("LIST_DEVICES", "List discovered camera device indexes, friendly names, and current bindings."),
        ("GET_CAMERA_CONFIG", "Current config/status for one camera. Requires camera_id."),
        ("GET_RECORD_STATE", "Current recording flag for one camera. Requires camera_id."),
        ("GET_RUNTIME_INFO", "Full runtime info payload: version, paths, environment, cameras, devices."),
    ]
    name_width = max(len(name) for name, _ in command_rows + query_rows) + 2

    print("Commands sent by external control clients (SuperCarter / DDS) to this runtime:")
    print()
    print("Every request carries a command/query name, camera_id (cam0/cam1/cam2, or 'all' where noted),")
    print("an optional args_json object, an auth_token, a request_id, and a source label.")
    print()
    print("Commands:")
    for name, description in command_rows:
        print(f"  {name:<{name_width}}{description}")
    print()
    print("Queries:")
    for name, description in query_rows:
        print(f"  {name:<{name_width}}{description}")


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
        lowered_line = line.lower()
        if lowered_line in {"help", "?", "commands"}:
            print_cli_help()
            continue
        if lowered_line == "help clients":
            print_client_command_help()
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
                if not result["success"]:
                    print(f"error[{result['code']}]: {result['message']}")
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

