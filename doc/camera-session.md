# camera-session

## Responsibility

負責單一 logical camera 的 preview thread、OpenCV capture、snapshot、recording、camera properties panel。面板只暴露 brightness 與 focus。

## Public Surface

- `CameraSession.start()`
- `CameraSession.stop()`
- `CameraSession.force_reopen()`
- `CameraSession.update_descriptor()`
- `CameraSession.snapshot()`
- `CameraSession.start_recording()`
- `CameraSession.stop_recording()`
- `CameraSession.apply_config()`
- `CameraSession.open_controls_panel()`
- `CameraSession.close_controls_panel()`
- `CameraSession.reset_camera_properties()`
- `CameraSession.status()`

## Dependencies

- OpenCV `VideoCapture`
- OpenCV HighGUI window / trackbar
- `CameraDescriptor`
- `CameraConfig`
- `RecordingSession`

## Design Notes

- 每個 session 對應一個 preview thread。
- Camera open 時只設定 frame width、height、fps。
- Image properties 預設不套用，避免破壞 driver default tone。
- Controls panel 只建立 `brightness` 與 `focus` 兩個 slider，其他 properties 不提供調整、不記錄、不回套。
- Preview backend 優先使用 `CAP_MSMF`，目標是讓色調更接近 Windows Camera app。
- Controls panel 只有在明確 command 開啟後才會建立。
- 使用者關閉 controls panel 是正常事件，程式會停用 controls sync。
- Controls panel 提供 `reset_defaults` 控制；觸發後會清除已修改 property 清單並重新開啟 camera capture，讓 driver default 重新生效。
- CLI / gRPC 可直接呼叫 reset，不需要先打開 controls panel。
- Reopen 時只回套 `_camera_prop_modified_names` 內的 properties。

## Independent Test Strategy

- 開啟 camera 後確認不呼叫 property apply。
- 呼叫 `open_controls_panel()` 後確認 controls enabled。
- 手動關閉 panel 後確認 controls disabled 且不刷 error。
- 修改 brightness 或 focus 後拔插/force reopen，確認只回套被修改的項目。
- 修改 slider 後觸發 `reset_defaults`，確認畫面回到 driver default 且後續 reopen 不再回套舊值。
- 直接呼叫 `reset_camera_properties()`，確認未開 panel 時也能清除 property 記錄並 reopen。

## Minimal Tasks

- [x] 建立 preview thread
- [x] 支援 snapshot
- [x] 支援 recording
- [x] 支援 force reopen
- [x] 支援 opt-in controls panel
- [x] 關閉 controls panel 後停止同步
- [x] 只回套使用者修改過的 properties
- [x] 支援 controls panel reset defaults
- [x] 支援 command-driven camera property reset
- [ ] 實機驗證不同 camera driver 的 property support
