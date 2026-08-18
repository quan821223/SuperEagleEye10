"""Camera discovery, logical slot assignment, hot-plug monitoring, and
`CameraSession` ownership. See `doc/camera-manager.md`.
"""

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from see_runtime.bootstrap import cv2
from see_runtime.camera_models import CameraConfig, CameraDescriptor
from see_runtime.camera_session import CameraSession
from see_runtime.constants import MANAGER_LOCK_TIMEOUT_SEC
from see_runtime.errors import CommandError, acquired_lock
from see_runtime.shell_utils import _decode_shell_output

LOGGER = logging.getLogger("SuperEagleEye")


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


