# runtime-bootstrap

## Responsibility

負責 runtime startup、CLI options、version loading、shared secret、single instance lock、main lifecycle。

## Public Surface

- `basic_options()`
- `data_options(parser)`
- `main()`
- `normalize_instance_id()`
- `parse_device_indexes()`
- `normalize_grpc_port()`
- `load_runtime_version()`
- `resolve_shared_secret()`
- `acquire_single_instance_lock()`

## Dependencies

- `argparse`
- `version.json`
- `%LOCALAPPDATA%\SEE\runtime\SuperEagleEye.secret`
- `CameraManager`
- `CommandRouter`
- `GrpcServerController`

## Design Notes

- `main()` 是 runtime composition root。
- Runtime 可從 source mode 或 PyInstaller frozen mode 執行。
- `BASE_DIR` 依 frozen 狀態決定，所有相對資源都應以 `BASE_DIR` 為基準。
- Shared secret 優先順序為 CLI value、runtime secret file、legacy secret file、generated secret。

## Independent Test Strategy

- 使用不同 `--grpc_port` 測試 port normalization。
- 使用不同 `--instance_id` 測試 single instance lock key。
- 使用 `--device_indexes` 測試 allowed camera list parsing。
- 模擬缺少 `version.json` 時 fallback version。

## Minimal Tasks

- [x] 定義 startup options
- [x] 載入 runtime version
- [x] 建立 output directory
- [x] 建立 camera manager
- [x] 建立 command router
- [x] 啟動 gRPC server
- [x] shutdown 時停止 router 與 gRPC server
- [ ] 增加 startup option regression tests
