# camera-model-config

## Responsibility

描述 camera runtime 的資料模型與設定模型。

## Public Surface

- `CameraConfig`
- `CameraDescriptor`
- `camera_map.json`
- `create_default_camera_map()`

## Dependencies

- Python `dataclasses`
- JSON camera map
- Windows device metadata

## Design Notes

- `CameraConfig` 保存 frame width、height、fps、recording duration、folder size limit。
- `CameraDescriptor` 保存 OpenCV `device_index` 與 Windows metadata。
- Logical slot 使用 `cam0` 到 `cam9`。
- `camera_map.json` 保存 logical camera alias 與 binding metadata。

## Independent Test Strategy

- 載入空白或不存在的 camera map 時自動建立 default map。
- 驗證 `cam0` 到 `cam9` 都存在。
- 驗證 descriptor signature 在 device metadata 改變時可比較差異。

## Minimal Tasks

- [x] 定義 camera config
- [x] 定義 camera descriptor
- [x] 建立 default camera map
- [x] 支援 logical slots `cam0..cam9`
- [ ] 增加 camera_map schema check
