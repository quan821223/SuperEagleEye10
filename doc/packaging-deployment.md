# packaging-deployment

## Responsibility

描述 PyInstaller packaging、dist layout、SuperCarter publish integration。

## Public Surface

- `build_SuperEagleEye.ps1`
- `build_SuperEagleEye.bat`
- `SuperEagleEye.spec`
- `dist\SuperEagleEye_dist`

## Dependencies

- PyInstaller
- Python virtual environment
- Generated gRPC Python files
- `camera_map.json`
- `version.json`

## Design Notes

- Packaging output 會被複製到 SuperCarter runtime 使用的位置。
- Frozen runtime 的 `BASE_DIR` 以 executable folder 為準。
- Logs 在 frozen executable 旁的 `logs` folder。
- Runtime 啟動時仍會讀取 `camera_map.json` 與 `version.json`。

## Independent Test Strategy

- 執行 build script。
- 確認 packaged output 包含 executable、proto generated files、camera map、version。
- 從 packaged folder 啟動並確認 log path。
- 透過 SuperCarter publish folder 啟動並確認 gRPC port。

## Minimal Tasks

- [x] 建立 PowerShell build script
- [x] 建立 batch wrapper
- [x] 複製 dist 到 runtime folder
- [ ] 增加 packaging smoke checklist
