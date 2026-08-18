"""Tk-based camera controls panel UI, split out from `camera_session.py` so
that module never has to import a GUI toolkit. See `doc/camera-controls-ui.md`.
"""

import queue
import threading
from typing import Dict, Optional, Tuple


class CameraControlsUI:
    """Tk-based controls panel, replacing the old cv2 trackbar window.

    Owns a single process-wide Tk root/mainloop on its own daemon thread.
    Tkinter widgets may only be touched from that thread, so every other
    thread talks to it exclusively through `_queue` (never calls Tk APIs
    directly), and the Tk thread talks back to `CameraSession` only through
    methods that are already documented as thread-safe on their own
    (`request_property_value()`, `reset_camera_properties()`,
    `close_controls_panel()`).
    """

    REFRESH_INTERVAL_MS = 200
    QUEUE_POLL_INTERVAL_MS = 50

    def __init__(self) -> None:
        self._queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._root = None
        self._panels: Dict[str, object] = {}
        self._ready_event = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="camera-controls-ui", daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=5)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.put(("shutdown", None))
        self._thread.join(timeout=3)

    def open_panel(self, session: "CameraSession") -> None:
        self._queue.put(("open", session))

    def close_panel(self, camera_id: str) -> None:
        self._queue.put(("close", camera_id))

    # --- Tk thread only below this point -----------------------------

    def _run(self) -> None:
        import tkinter as tk

        self._tk = tk
        root = tk.Tk()
        root.withdraw()
        self._root = root
        self._ready_event.set()
        root.after(self.QUEUE_POLL_INTERVAL_MS, self._drain_queue)
        root.mainloop()

    def _drain_queue(self) -> None:
        try:
            while True:
                action, payload = self._queue.get_nowait()
                if action == "open":
                    self._open_panel_on_tk_thread(payload)
                elif action == "close":
                    self._close_panel_on_tk_thread(payload)
                elif action == "shutdown":
                    for camera_id in list(self._panels):
                        self._close_panel_on_tk_thread(camera_id)
                    self._root.quit()
                    return
        except queue.Empty:
            pass
        self._root.after(self.QUEUE_POLL_INTERVAL_MS, self._drain_queue)

    def _open_panel_on_tk_thread(self, session: "CameraSession") -> None:
        tk = self._tk
        camera_id = session.camera_id
        existing = self._panels.get(camera_id)
        if existing is not None:
            existing["toplevel"].lift()
            return

        toplevel = tk.Toplevel(self._root)
        toplevel.title(f"{camera_id} controls")

        def handle_close() -> None:
            session.close_controls_panel()
            self._close_panel_on_tk_thread(camera_id)

        toplevel.protocol("WM_DELETE_WINDOW", handle_close)

        sliders: Dict[str, object] = {}
        labels: Dict[str, object] = {}
        for spec in session.CAMERA_PROP_SPECS:
            name = spec["name"]
            range_min, range_max = session.camera_prop_ranges.get(name, (0, int(spec["max"])))
            row = tk.Frame(toplevel)
            row.pack(fill="x", padx=8, pady=4)
            tk.Label(row, text=name, width=12, anchor="w").pack(side="left")
            label = tk.Label(row, text="", width=16, anchor="w")
            label.pack(side="right")

            def on_change(value, _name=name):
                session.request_property_value(_name, int(float(value)))

            # Create without `command` so the initial `.set()` below (and any
            # later silent range/clamp correction) can never masquerade as a
            # user-driven request; `command` is attached only afterward, so
            # only real user drags reach `request_property_value()`.
            scale = tk.Scale(
                toplevel,
                from_=range_min,
                to=range_max,
                orient="horizontal",
                length=260,
            )
            scale.set(int(session.camera_prop_cache.get(name, spec["default"])))
            scale.config(command=on_change)
            scale.pack(fill="x", padx=8)
            sliders[name] = scale
            labels[name] = label

        def handle_reset() -> None:
            session.reset_camera_properties()
            for spec in session.CAMERA_PROP_SPECS:
                name = spec["name"]
                scale = sliders[name]
                cmd = scale["command"]
                scale["command"] = ""
                scale.set(int(spec["default"]))
                scale["command"] = cmd

        button_row = tk.Frame(toplevel)
        button_row.pack(fill="x", padx=8, pady=8)
        tk.Button(button_row, text="Reset to driver defaults", command=handle_reset).pack(side="left")
        tk.Button(button_row, text="Close", command=handle_close).pack(side="right")

        panel = {"toplevel": toplevel, "sliders": sliders, "labels": labels}
        self._panels[camera_id] = panel
        self._refresh_panel(session, camera_id)

    def _refresh_panel(self, session: "CameraSession", camera_id: str) -> None:
        panel = self._panels.get(camera_id)
        if panel is None:
            return
        for spec in session.CAMERA_PROP_SPECS:
            name = spec["name"]
            range_min, range_max = session.camera_prop_ranges.get(name, (0, int(spec["max"])))
            requested_raw = int(session.camera_prop_cache.get(name, spec["default"]))
            requested_display = min(range_max, max(range_min, requested_raw))
            reported = session.camera_prop_reported.get(name)
            reported_text = f"{reported:.1f}" if isinstance(reported, (int, float)) else "-"
            panel["labels"][name].config(text=f"req={requested_display}  actual={reported_text}")
            scale = panel["sliders"][name]
            if (float(scale.cget("from")), float(scale.cget("to"))) != (float(range_min), float(range_max)):
                # Reconfiguring `from_`/`to` on a Tk Scale silently clamps an
                # out-of-range current value AND fires `command` as a side
                # effect - detach it first so a range correction (e.g. the
                # static 0-100 fallback getting replaced by the real
                # hardware range once probed) never gets applied to the
                # camera as if the user had dragged the slider.
                cmd = scale["command"]
                scale["command"] = ""
                scale.config(from_=range_min, to=range_max)
                scale.set(requested_display)
                scale["command"] = cmd
        self._root.after(self.REFRESH_INTERVAL_MS, self._refresh_panel, session, camera_id)

    def _close_panel_on_tk_thread(self, camera_id: str) -> None:
        panel = self._panels.pop(camera_id, None)
        if panel is not None:
            try:
                panel["toplevel"].destroy()
            except Exception:
                pass

