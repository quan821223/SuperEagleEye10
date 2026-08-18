# SuperEagleEye 詳細設計文件

## 設計原則

- 以下方模組總覽的責任邊界切分模組；自 v1.4.0 起，這個切分不再只是文件上的概念，`see_runtime/` 套件底下每個模組都對應到實際的 `.py` 檔案（見下表的「原始碼」欄）。
- 模組間以清楚的 Python object / method contract 溝通。
- 每個模組都應能以最小任務獨立實作、測試、回歸。
- 相機硬體狀態不可因單次查詢失敗而被立即重置。
- OpenCV property 操作必須是 opt-in，避免破壞 driver default image tone。

## 模組總覽

| 模組 | 原始碼 | 說明 |
|---|---|---|
| [runtime-bootstrap](runtime-bootstrap.md) | `SuperEagleEye.py`、`see_runtime/bootstrap.py`、`see_runtime/runtime_paths.py` | startup options、runtime path、version、single instance、main lifecycle |
| [logging-runtime-state](logging-runtime-state.md) | `see_runtime/logging_setup.py`、`see_runtime/runtime_paths.py` | log file、stdout/stderr redirect、crash log |
| [camera-model-config](camera-model-config.md) | `see_runtime/camera_models.py` | CameraConfig、CameraDescriptor、camera_map |
| [recording-session](recording-session.md) | `see_runtime/camera_models.py` | segmented video writer |
| [camera-session](camera-session.md) | `see_runtime/camera_session.py`、`see_runtime/dshow_camera_control.py` | single camera preview、snapshot、recording、property 邏輯（不含 UI） |
| [camera-controls-ui](camera-controls-ui.md) | `see_runtime/camera_controls_ui.py` | 屬性面板 UI（Tk），跟 camera-session 分離 |
| [camera-manager](camera-manager.md) | `see_runtime/camera_manager.py` | discovery、logical slot、hot-plug、session ownership |
| [command-router](command-router.md) | `see_runtime/command_router.py` | command/query dispatch、auth、runtime info |
| [grpc-service](grpc-service.md) | `see_runtime/grpc_service.py`、`see_runtime/grpc_server_controller.py` | SEE gRPC service adapter |
| [cli-interface](cli-interface.md) | `see_runtime/cli.py` | interactive terminal command parsing |
| [packaging-deployment](packaging-deployment.md) | `SuperEagleEye.spec`、`build_SuperEagleEye.ps1` | PyInstaller build and dist layout |

其餘沒有獨立文件、屬於共用底層的模組：`see_runtime/constants.py`（常數）、`see_runtime/errors.py`（`CommandError`、`acquired_lock`）、`see_runtime/protocol_utils.py`（CLI 與 gRPC 共用的 wire-format helper）、`see_runtime/shell_utils.py`（subprocess 輸出解碼）。

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
