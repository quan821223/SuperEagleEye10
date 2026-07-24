# logging-runtime-state

## Responsibility

負責 runtime log、console stream redirect、crash log、diagnostic trace。

## Public Surface

- `configure_runtime_logging()`
- `build_runtime_log_path()`
- `get_runtime_log_dir()`
- `_LoggerStream`
- `write_crash_log()`
- `pause_on_fatal_error()`

## Dependencies

- Python `logging`
- `TimedRotatingFileHandler`
- runtime `logs` directory
- `%LOCALAPPDATA%\SEE\runtime`

## Design Notes

- Frozen executable 將 log 寫在 executable 旁的 `logs`。
- Source mode 將 log 寫在 source folder 的 `logs`。
- stdout/stderr 會被導到 logger，避免 packaged console 錯誤遺失。
- Fatal exception 會寫入 crash log，方便非 Python 使用者回報。
- File handler 不過濾，收所有 INFO 以上訊息，維持完整除錯資訊。
- Console handler 改成白名單制（`_ConsoleVisibilityFilter`）：預設不印，只有明確帶 `extra={"console": True}` 的 `LOGGER.*` 呼叫才會上小黑窗。
- `_LoggerStream`（stdout/stderr redirect）一律自動帶 `console=True`，所以所有原本靠 `print()` 呈現的內容（CLI banner、`help`、指令結果、崩潰提示）不受影響。
- 4 個 grpc heartbeat / connection-state 訊息前綴維持舊有「整個 runtime 只印一次」行為，不受白名單規則影響。
- 白名單只涵蓋一次性 runtime 生命週期訊息、相機健康狀態的轉折點（斷線/恢復各一次，不含每次重試細節）、真正的裝置清單變化、例外/嚴重錯誤；例行的 command/query/heartbeat/snapshot 流量維持 file-only。
- **OpenCV 自己的 native (C++) logger 完全繞過 Python `logging`／`sys.stderr` redirect**，直接寫到 OS 層的 stderr，上面整套白名單機制對它無效。在某些機器上，如果設定的 `device_index` 對應不到真的存在的相機，`cv2.VideoCapture` 每次嘗試開啟（包含我們自己的 reconnect 重試迴圈）都會直接印出 `[ WARN:...] VIDEOIO(...): backend is generally available but can't be used to capture by index` / obsensor 的 `Camera index out of range` 這類原始訊息，洗版且無法用 Python 端過濾。修法：在 `import cv2` 之前設定 `OPENCV_LOG_LEVEL=SILENT` 環境變數，並在 import 後呼叫 `cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)`（用 `try/except AttributeError` 包起來，相容比較舊的 OpenCV 版本）。我們自己的 `LOGGER` 已經有記 `camera_open_failed`/`camera_open_retry` 等同樣的事件，不會因此少掉除錯資訊。

## Independent Test Strategy

- 在 source mode 啟動並確認 log file 建立。
- 模擬 exception 並確認 crash log 內容包含 stack trace。
- 確認 stdout/stderr print 會進入 log。
- 相機拔插時，確認 console 只出現一次「不可用」與一次「恢復」，log 檔案仍保留完整重試細節。
- 用一個不存在的 `device_index` 開相機（或在沒有相機的機器上啟動），確認 console 不會出現 OpenCV 原生的 `[ WARN:...] VIDEOIO` / obsensor `Camera index out of range` 洗版訊息。

## Minimal Tasks

- [x] 建立 runtime log path
- [x] 設定 rotating file handler
- [x] redirect stdout/stderr
- [x] fatal exception 寫 crash log
- [x] console 訊息改為白名單制，減少小黑窗洗版
- [x] 壓下 OpenCV native logger 的 VIDEOIO/obsensor 洗版訊息
- [ ] 增加 log path smoke test
