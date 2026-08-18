# recording-session

## Responsibility

負責單一 camera recording 的 video writer lifecycle 與分段錄影。

## Source

`see_runtime/camera_models.py`

## Public Surface

- `RecordingSession`
- `write(frame)`
- `stop()`

## Dependencies

- OpenCV `cv2.VideoWriter`
- Output directory
- Frame size / FPS / segment duration

## Design Notes

- Recording 以 segment duration 控制每段 video 長度。
- `write()` 在段落超時時自動 release 舊 writer 並開新檔。
- `stop()` 負責 release writer。

## Independent Test Strategy

- 使用 mock frame 啟動 recording。
- 設定短 duration 驗證 segment rollover。
- 停止 recording 後確認 writer 被釋放。

## Minimal Tasks

- [x] 建立 video writer
- [x] 支援分段錄影
- [x] 支援停止錄影
- [ ] 增加 writer open failure test
