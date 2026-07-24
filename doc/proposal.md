# SuperEagleEye 技術需求文件

## 目標

本文件描述 `camera_v2` 目前 `SuperEagleEye.py` 的既有規格，以及本輪需補齊的穩定性與可診斷性需求。文件對象是後續維護工程師，內容以中文為主，保留必要 English technical terms。

## 背景

`SuperEagleEye` 是 SEE / SuperCarter 使用的 camera runtime。它負責啟動本機相機 preview、snapshot、recording、camera hot-plug recovery，並透過 gRPC 與 SuperCarter 溝通。

目前專案過去已有初步功能，但缺少 `doc/` 開發文件，導致後續維護者難以判斷 Python、OpenCV、gRPC 與 runtime lifecycle 的設計意圖。本次需求包含文件補齊與近期屬性面板問題修正。

## 現有功能需求

- Runtime 應可由 `SuperEagleEye.py` 或 PyInstaller packaged executable 啟動。
- Runtime 預設使用 gRPC port `50051`，允許範圍為 `50051-50060`。
- Runtime 應使用 shared secret 驗證 gRPC request。
- Runtime 應提供 logical camera id，例如 `cam0`、`cam1`，避免使用者直接操作 OpenCV `device_index`。
- Runtime 啟動時應自動 discover camera devices，建立 logical slot，並開啟可用相機。
- Runtime 應支援 snapshot、recording、list cameras、list devices、camera config query。
- Runtime 應支援 hot-plug monitor，當 USB camera 拔插時能盡量恢復既有 logical slot。
- Runtime 應提供 CLI commands 供直接執行時診斷與操作。
- Runtime 應提供 gRPC commands / queries 供 SuperCarter 呼叫。

## 本輪新增與修正需求

- 屬性面板不得在打開相機時自動套用亮度、焦距或其他 OpenCV properties。
- 打開相機後應先採用 OpenCV / camera driver 原始預設狀態。
- 屬性面板應由明確指令開啟，不應每次 camera frame loop 自動建立。
- 關閉屬性面板後不得持續產生 OpenCV window / trackbar error log。
- 屬性面板只允許調整 brightness 與 focus；其他 OpenCV properties 使用 driver default。
- 使用者在屬性面板調整過的 brightness / focus，才允許記錄並於 camera reopen 時回套。
- CLI 應支援打開與關閉屬性面板。
- gRPC 應支援打開與關閉屬性面板。
- CLI 與 gRPC 應支援查詢 runtime 基本資訊。
- runtime 基本資訊應包含 version、instance、gRPC port、path、environment、camera list、device list。

## 非目標

- 本輪不建立新的大型 GUI framework。
- 本輪不更換 OpenCV camera backend 架構。
- 本輪不修改 `SC_communication_gRPC.proto`，仍使用既有 generic command/query 欄位承載新 command name。
- 本輪不保證所有 camera driver 都支援每個 OpenCV property；需要透過 log 觀察 `applied` 與 `reported`。

## 驗收標準

- `python` AST parse 檢查可通過。
- `open cam0` 後不應主動套用 brightness、focus 或其他 image properties。
- CLI `panel cam0` 或 `open_panel cam0` 可開啟屬性面板。
- CLI `close_panel cam0` 可關閉屬性面板。
- CLI `reset_panel cam0` 或 `reset_camera_properties cam0` 可不用打開面板就恢復 driver default。
- 使用者手動關閉屬性面板後，不應持續出現 trackbar / window error。
- gRPC `OPEN_CAMERA_PANEL` 可開啟屬性面板。
- gRPC `CLOSE_CAMERA_PANEL` 可關閉屬性面板。
- gRPC `RESET_CAMERA_PROPERTIES` 可恢復 driver default。
- CLI `info` 或 `runtime_info` 可輸出 runtime 基本資訊。
- gRPC query `GET_RUNTIME_INFO` 可輸出 runtime 基本資訊。
- `doc/proposal.md`、`doc/detailed-design.md`、`doc/progress.md` 與每個模組文件存在。

## 風險與限制

- OpenCV property support 依 camera driver 與 backend 而定；目前只暴露 brightness 與 focus。
- `CAP_DSHOW` 與 `CAP_MSMF` 對同一 property 的值域可能不同。
- 屬性面板（`camera-controls-ui.md`）是獨立 Tk 視窗；使用者直接關閉視窗會造成 window state 變化，程式必須將其視為正常事件。
- Windows camera discovery 透過 PowerShell / CIM 查詢可能 timeout，因此不可把單次查詢失敗直接視為 camera disconnected。
