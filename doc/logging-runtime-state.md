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

## Independent Test Strategy

- 在 source mode 啟動並確認 log file 建立。
- 模擬 exception 並確認 crash log 內容包含 stack trace。
- 確認 stdout/stderr print 會進入 log。

## Minimal Tasks

- [x] 建立 runtime log path
- [x] 設定 rotating file handler
- [x] redirect stdout/stderr
- [x] fatal exception 寫 crash log
- [ ] 增加 log path smoke test
