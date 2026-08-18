"""SuperEagleEye entry point: argument parsing and `main()`.

Everything else (camera handling, gRPC service, CLI loop, logging setup,
etc.) lives in the `see_runtime` package — see `doc/detailed-design.md`
for the module map.
"""

import argparse
import logging
import os
import traceback
from pathlib import Path

from see_runtime.bootstrap import BASE_DIR
from see_runtime.camera_controls_ui import CameraControlsUI
from see_runtime.camera_manager import CameraManager
from see_runtime.camera_models import CameraConfig, normalize_grpc_port
from see_runtime.command_router import CommandRouter
from see_runtime.constants import CAMERA_MAP_FILE_NAME, DEFAULT_GRPC_PORT, GRPC_PORT_MAX, GRPC_PORT_MIN, SHARED_SECRET_ENV_VAR, SHARED_SECRET_FILE_NAME
from see_runtime.cli import create_default_camera_map, run_until_shutdown
from see_runtime.grpc_server_controller import GrpcServerController
from see_runtime.logging_setup import acquire_single_instance_lock, configure_runtime_logging, resolve_shared_secret
from see_runtime.runtime_paths import format_runtime_file_tag, format_runtime_title, load_runtime_version, normalize_instance_id, parse_device_indexes, pause_on_fatal_error, write_crash_log

LOGGER = logging.getLogger("SuperEagleEye")

# Kept here (not in see_runtime/) so `__file__` keeps referring to this entry
# script's own directory in both dev and frozen (PyInstaller) runs, matching
# this path's historical, pre-APP_RUNTIME_DIR meaning as a fallback location
# for an exe-adjacent secret file.
LEGACY_SECRET_PATH = Path(__file__).resolve().parent / SHARED_SECRET_FILE_NAME


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
    auth_token = resolve_shared_secret(opt.auth_token, LEGACY_SECRET_PATH)
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
