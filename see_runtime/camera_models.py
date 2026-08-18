"""Camera data models (`CameraConfig`, `CameraDescriptor`), path/timestamp
helpers, and `RecordingSession` (segmented video writer). Corresponds to
`doc/camera-model-config.md` and `doc/recording-session.md`.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from see_runtime.bootstrap import cv2
from see_runtime.constants import DEFAULT_GRPC_PORT, GRPC_PORT_MAX, GRPC_PORT_MIN
from see_runtime.errors import CommandError


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


def current_date_folder_name() -> str:
    return time.strftime("%Y_%m_%d")


def dated_output_dir(output_root: Path) -> Path:
    return output_root / current_date_folder_name()


def dated_output_path(output_path: Path) -> Path:
    return output_path.parent / current_date_folder_name() / output_path.name


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
        output_dir = dated_output_dir(self.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.segment_index += 1
        timestamp = file_timestamp_with_millis()
        file_tag = f"_{self.file_tag}" if self.file_tag else ""
        path = output_dir / f"{timestamp}{file_tag}_{self.file_prefix}_{self.segment_index}.mp4"
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
