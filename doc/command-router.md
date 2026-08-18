# command-router

## Responsibility

負責 command/query dispatch、auth validation、connection state、runtime info payload。

## Source

`see_runtime/command_router.py`

## Public Surface

- `CommandRouter.execute()`
- `CommandRouter.query()`
- `CommandRouter.validate_auth()`
- `CommandRouter.heartbeat()`
- `CommandRouter.shutdown()`

## Dependencies

- `CameraManager`
- gRPC service adapter
- CLI loop
- Shared secret

## Design Notes

- Command 與 Query 統一回傳 `success/code/message/payload` 結構。
- gRPC 與 CLI 共用同一套 router，避免邏輯分叉。
- `OPEN_CAMERA_PANEL` 與 `CLOSE_CAMERA_PANEL` 是 command。
- `GET_RUNTIME_INFO` 是 query。
- `SHOW_RUNTIME_INFO` 是 command wrapper，可供只支援 command 的 caller 使用。

## Independent Test Strategy

- 測試 invalid auth 會回 `AUTH_FAILED`。
- 測試 unsupported command 會回 `INVALID_COMMAND`。
- 測試 `GET_RUNTIME_INFO` payload 包含 runtime、paths、environment、cameras、devices。

## Minimal Tasks

- [x] 建立 command dispatch
- [x] 建立 query dispatch
- [x] 支援 heartbeat state
- [x] 支援 controls panel commands
- [x] 支援 runtime info query
- [ ] 增加 router unit tests
