# SuperEagleEye 詳細設計文件

## 設計原則

- 以目前 `SuperEagleEye.py` 的實際責任邊界切分模組。
- 模組間以清楚的 Python object / method contract 溝通。
- 每個模組都應能以最小任務獨立實作、測試、回歸。
- 相機硬體狀態不可因單次查詢失敗而被立即重置。
- OpenCV property 操作必須是 opt-in，避免破壞 driver default image tone。

## 模組總覽

- [runtime-bootstrap](runtime-bootstrap.md): startup options、runtime path、version、single instance、main lifecycle。
- [logging-runtime-state](logging-runtime-state.md): log file、stdout/stderr redirect、crash log。
- [camera-model-config](camera-model-config.md): CameraConfig、CameraDescriptor、camera_map。
- [recording-session](recording-session.md): segmented video writer。
- [camera-session](camera-session.md): single camera preview、snapshot、recording、property 邏輯（不含 UI）。
- [camera-controls-ui](camera-controls-ui.md): 屬性面板 UI（Tk），跟 camera-session 分離。
- [camera-manager](camera-manager.md): discovery、logical slot、hot-plug、session ownership。
- [command-router](command-router.md): command/query dispatch、auth、runtime info。
- [grpc-service](grpc-service.md): SEE gRPC service adapter。
- [cli-interface](cli-interface.md): interactive terminal command parsing。
- [packaging-deployment](packaging-deployment.md): PyInstaller build and dist layout。

## Runtime Flow

1. `main()` parses CLI options and initializes logging.
2. `main()` resolves shared secret, version, output directory, allowed device indexes.
3. `CameraManager` discovers cameras, initializes logical slots, opens default cameras, and starts hot-plug monitor.
4. `CommandRouter` owns auth validation, command dispatch, query dispatch, connection state, and runtime info.
5. `GrpcServerController` starts local gRPC service.
6. `run_until_shutdown()` runs CLI loop when stdin is interactive.
7. On shutdown, router stops camera manager and gRPC server.

## Camera Control Panel Design

- UI and property logic are split: `CameraControlsUI` owns the panel window (Tk, its own dedicated thread); `CameraSession` owns brightness/focus logic and never imports a GUI toolkit.
- `CameraSession.controls_enabled` controls whether the panel is considered open; `CameraManager.open_camera_panel()`/`close_camera_panel()` toggle it and also tell `CameraControlsUI` to show/destroy the window.
- The controls panel only exposes `brightness` and `focus`.
- The UI reports slider changes through `CameraSession.request_property_value()` (thread-safe). `_sync_controls_with_camera()` runs in the preview loop, drains those pending values, and returns immediately if controls are disabled.
- Applying a value prefers native DirectShow control (`IAMVideoProcAmp`/`IAMCameraControl`, applied immediately); it falls back to OpenCV `capture.set()` via a debounced release+reopen only if native control is unavailable.
- Opening a camera does not apply any property unless the user has modified that property before.
- `_camera_prop_modified_names` records the exact properties changed by the user.
- Reopen applies only modified brightness / focus properties, not every cached default.

## Runtime Info Design

Runtime info is exposed through:

- CLI query: `info` / `runtime_info`
- gRPC query: `GET_RUNTIME_INFO`
- gRPC command: `SHOW_RUNTIME_INFO`

Payload includes:

- runtime version metadata
- instance id and gRPC port
- camera state
- device discovery state
- output/log/config paths
- Python/OpenCV/platform environment

## Hot-Plug Stability Design

Windows device discovery is treated as a diagnostic input, not the sole truth.

- Successful query updates `_last_windows_devices`.
- Timeout or query failure returns last successful device list.
- Hot-plug monitor debounces transient query failure.
- `descriptor_count_mismatch` only participates when the latest query is healthy.

## Vibe Coding Task Model

Each module document contains:

- module responsibility
- public surface
- dependencies
- independent test strategy
- minimal tasks expressed as checklist items

The project-level status is tracked in [progress.md](progress.md).
