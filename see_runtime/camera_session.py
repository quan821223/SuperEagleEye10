"""Single logical camera: preview thread, OpenCV capture, snapshot,
recording, and brightness/focus property logic. Does not own any UI —
the controls panel window lives in `camera_controls_ui.py`. See
`doc/camera-session.md`.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from see_runtime.bootstrap import cv2
from see_runtime.camera_models import CameraConfig, CameraDescriptor, RecordingSession, dated_output_dir, dated_output_path, file_timestamp_with_millis
from see_runtime.constants import SESSION_LOCK_TIMEOUT_SEC
from see_runtime.dshow_camera_control import DirectShowPropertyController, _DSHOW_CONTROL_AVAILABLE, comtypes, find_dshow_video_filter
from see_runtime.errors import CommandError, acquired_lock
from see_runtime.protocol_utils import coerce_int

LOGGER = logging.getLogger("SuperEagleEye")


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
        target = dated_output_path(output_path) if output_path else self._default_snapshot_path(command_name)
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
        return dated_output_dir(self.output_root) / f"{timestamp}{file_tag}_{safe_command_name}.jpg"

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


