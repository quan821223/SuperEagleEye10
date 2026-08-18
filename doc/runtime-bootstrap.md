# runtime-bootstrap

## Responsibility

負責 runtime startup、CLI options、version loading、shared secret、single instance lock、main lifecycle。

## Source

- `SuperEagleEye.py`：`basic_options()`、`data_options()`、`main()`、`LEGACY_SECRET_PATH`
- `see_runtime/bootstrap.py`：`get_base_dir()`、`BASE_DIR`、`ensure_proto_generated()`、cv2/grpc/pb2 的首次載入
- `see_runtime/runtime_paths.py`：`normalize_instance_id()`、`parse_device_indexes()`、`load_runtime_version()`

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
- （v1.4.0 模組拆分後）`get_base_dir()` 與 `get_runtime_log_dir()` 的 non-frozen 分支改用 `sys.argv[0]` 而不是 `__file__`：因為這兩個函式現在定義在 `see_runtime/bootstrap.py` / `see_runtime/runtime_paths.py`，若沿用 `__file__` 會變成解析到 `see_runtime/` 這個子目錄，而不是進入點腳本所在的目錄，行為就跑掉了。`sys.argv[0]`（也就是 `python SuperEagleEye.py` 被呼叫的那個路徑）在任何模組裡取值都一樣，所以是安全的替代方案，且不影響 frozen 模式（frozen 分支本來就是用 `sys.executable`，跟 `__file__` 無關）。
- `LEGACY_SECRET_PATH` 刻意保留在 `SuperEagleEye.py`（不搬進 `see_runtime/`）：它是唯一一處沒有 frozen 分支、直接用 `Path(__file__)` 的地方，只有留在進入點腳本裡才能保證跟拆分前完全相同的解析結果。

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
