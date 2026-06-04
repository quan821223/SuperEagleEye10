# SuperEagleEye 開發進度

## 模組狀態

- [x] `runtime-bootstrap` 文件建立
- [x] `logging-runtime-state` 文件建立
- [x] `camera-model-config` 文件建立
- [x] `recording-session` 文件建立
- [x] `camera-session` 文件建立
- [x] `camera-manager` 文件建立
- [x] `command-router` 文件建立
- [x] `grpc-service` 文件建立
- [x] `cli-interface` 文件建立
- [x] `packaging-deployment` 文件建立

## 本輪程式任務

- [x] 相機開啟時不主動套用 OpenCV image properties
- [x] 屬性面板改為明確指令開啟
- [x] 屬性面板關閉後停止同步並避免持續錯誤 log
- [x] CLI 新增 `panel` / `open_panel`
- [x] CLI 新增 `close_panel`
- [x] CLI 新增 `info` / `runtime_info`
- [x] gRPC command 新增 `OPEN_CAMERA_PANEL`
- [x] gRPC command 新增 `CLOSE_CAMERA_PANEL`
- [x] gRPC query 新增 `GET_RUNTIME_INFO`
- [x] Windows device query 成功輪詢去重，避免 console 被相同 INFO 洗頻
- [x] 屬性面板 HighGUI 建立/關閉改由 preview thread 執行
- [x] Camera backend 優先順序改為 `CAP_MSMF`，更接近 Windows Camera app 色調
- [x] 屬性面板新增 `reset_defaults`，可清除壞掉的 property 設定並回到 driver default
- [x] 新增 `reset_panel` / `reset_camera_properties` / `RESET_CAMERA_PROPERTIES`
- [x] 屬性面板只保留 brightness 與 focus，其他 properties 維持 driver default
- [x] 語法檢查通過

## 待實機驗證

- [ ] 啟動 packaged `SuperEagleEye.exe`
- [ ] 執行 `open cam0` 後確認畫面色調維持 driver default
- [ ] 執行 `panel cam0` 並確認只有 brightness 與 focus 可調
- [ ] 在 controls panel 觸發 `reset_defaults` 後確認畫面回復 driver default
- [ ] 手動關閉 controls panel 後確認不再刷 OpenCV error
- [ ] USB 拔插後確認只回套使用者修改過的 property
- [ ] 由 SuperCarter / gRPC 呼叫 `OPEN_CAMERA_PANEL`
- [ ] 由 SuperCarter / gRPC 呼叫 `GET_RUNTIME_INFO`
