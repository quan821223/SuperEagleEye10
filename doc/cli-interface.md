# cli-interface

## Responsibility

負責 interactive terminal command parsing、help text、query frame print。

## Source

`see_runtime/cli.py`

## Public Surface

- `parse_cli_line()`
- `print_cli_help()`
- `print_client_command_help()`
- `maybe_prompt_grpc_port()`
- `cli_loop()`

## Dependencies

- `CommandRouter`
- `build_query_frame()`
- stdin/stdout

## Design Notes

- CLI command 最終都轉成 router command 或 query。
- Query command 會印 JSON payload 與 encoded response frame。
- 新增 `panel cam0` / `open_panel cam0` 開啟 controls panel。
- 新增 `close_panel cam0` 關閉 controls panel。
- 新增 `info` / `runtime_info` 顯示 runtime info。
- （v1.4.0）新增 `help clients` 子指令：印出 SuperCarter / DDS 這類外部控制端會透過 `ExecuteCameraCommand` / `QueryCameraState` 送給這台 runtime 的完整指令與查詢清單（對照 README 的 SuperCarter Command/Query Reference），跟本機終端機自己用的指令表分開陳列，避免混淆兩種不同的「指令」。`help` 輸出結尾會提示這個子指令的存在。

## Independent Test Strategy

- 測試 `parse_cli_line("panel cam0")`。
- 測試 `parse_cli_line("close_panel cam0")`。
- 測試 `parse_cli_line("info")`。
- 測試無效 command 回 `INVALID_COMMAND`。

## Minimal Tasks

- [x] 支援基本 CLI commands
- [x] 支援 controls panel commands
- [x] 支援 runtime info query
- [x] 更新 help text
- [x] 新增 `help clients` 子指令（外部控制端指令參考）
- [ ] 增加 parse_cli_line unit tests
