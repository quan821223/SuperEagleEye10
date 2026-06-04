# camera-manager

## Responsibility

負責 camera discovery、logical slot assignment、hot-plug monitor、session ownership。

## Public Surface

- `CameraManager.open_camera()`
- `CameraManager.close_camera()`
- `CameraManager.open_camera_panel()`
- `CameraManager.close_camera_panel()`
- `CameraManager.rescan_devices()`
- `CameraManager.refresh_cameras()`
- `CameraManager.swap_cameras()`
- `CameraManager.list_cameras()`
- `CameraManager.list_devices()`
- `CameraManager.shutdown()`

## Dependencies

- `CameraSession`
- Windows PowerShell / CIM device query
- OpenCV probe
- `camera_map.json`

## Design Notes

- Discovery 使用 Windows device metadata 與 OpenCV probe 結合。
- Windows query timeout 不代表 camera disconnected。
- 查詢失敗時回退到最後一次成功 device list。
- Hot-plug monitor 對短暫 query failure 做 debounce。
- Session ownership 由 manager 統一管理，避免同一 device_index 被兩個 session 同時打開。

## Independent Test Strategy

- mock Windows query timeout，確認不立即清空 devices。
- mock descriptor count mismatch，確認只在 query healthy 時觸發 refresh。
- 測試 `swap_cameras()` 交換 logical binding。
- 測試 `open_camera_panel()` 會委派到 session。

## Minimal Tasks

- [x] 建立 logical slot model
- [x] 支援 device discovery
- [x] 支援 manual rescan
- [x] 支援 refresh cameras
- [x] 支援 hot-plug monitor
- [x] 支援 Windows query debounce
- [x] 支援 controls panel delegation
- [ ] 增加 same-model dual camera 實機回歸測試
