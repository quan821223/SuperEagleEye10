# camera-controls-ui

## Responsibility

負責相機屬性面板的 UI 呈現（brightness/focus slider、reset/close 按鈕），取代舊版 cv2 HighGUI trackbar 面板。是 process 內唯一跑 Tk `mainloop()` 的地方。

## Source

`see_runtime/camera_controls_ui.py`

## Public Surface

- `CameraControlsUI.start()`
- `CameraControlsUI.stop()`
- `CameraControlsUI.open_panel(session)`
- `CameraControlsUI.close_panel(camera_id)`

## Dependencies

- `tkinter`（Python 標準庫，非額外依賴；但打包成 PyInstaller exe 時需要確認 tcl/tk 資源有被正確收進去）
- `CameraSession`（只用它已經是 thread-safe 的公開介面：`request_property_value()`、`reset_camera_properties()`、`close_controls_panel()`，以及唯讀 dict `camera_prop_cache`/`camera_prop_reported`/`camera_prop_ranges`）
- `queue.Queue`（跨執行緒指令傳遞）

## Design Notes

- Tkinter widget 只能在建立它們、跑 `mainloop()` 的那個執行緒操作。這個 class 在 `start()` 時起一個獨立 daemon thread，建立一個隱藏的 `tk.Tk()` root（`withdraw()`），之後所有 widget 操作都只在這個執行緒發生。
- 其他執行緒（CommandRouter/CameraManager）一律透過 `queue.Queue` 下指令（`open`/`close`/`shutdown`），Tk thread 用 `root.after(50, ...)` 定時清空 queue 來執行，不直接跨執行緒呼叫任何 Tk API。
- 每台相機一個 `Toplevel`，用 `camera_id` 當 key 存在 `self._panels`；`open_panel()` 對已經開著的相機是 no-op（只把視窗 `lift()` 到前面）。
- Slider 範圍優先用 `session.camera_prop_ranges`（DirectShow `GetRange()` 回讀到的真實硬體範圍），還沒探測到時退回 `CameraSession.CAMERA_PROP_SPECS` 的靜態 `max`。面板剛開啟時原生控制通常還沒探測完成，所以一開始一定是先用靜態 fallback 範圍（例如 brightness 的 0-100），探測完成後才會換成真實範圍（例如 exposure 的 -11 到 1）——這個「範圍中途換掉」的時刻踩過一個嚴重的坑，見下。
- Slider 的 `command` callback只做一件事：呼叫 `session.request_property_value(name, value)`，實際套用邏輯完全在 `CameraSession`/preview thread 那邊，這裡不碰 `capture`、不碰 `_dshow_control`。
- **`tk.Scale.config(from_=, to=)` 改變範圍時，如果目前值超出新範圍，Tk 會自動把值夾到新的邊界，而且這個自動夾值「算作一次值變動」，會觸發 `command` callback**——踩過這個坑：面板剛開時 brightness 用靜態 fallback 範圍 0-100、預設值 50；範圍探測完成換成真實 exposure 範圍 -11~1 後，Tk 自動把 50 夾到新的上限 1（exposure 最大值，也就是最長曝光時間），這個「自動夾值」被當成使用者操作送進 `request_property_value('brightness', 1)`，實際把相機曝光拉到最大，畫面整個死白過曝，而使用者根本沒碰過那顆 slider。修法：任何用程式改 `from_`/`to`/`.set()` 的地方（初始化、範圍校正、Reset），都要先把 `scale["command"]` 暫時清空、操作完再接回去，確保只有真人拖動才會呼叫 `request_property_value()`。
- **沒有「使用者正在拖動」這個週期性強制同步 slider 位置**：原本每次 refresh 都會把 slider 位置強制設回 `camera_prop_cache` 目前值，但 `camera_prop_cache` 是 preview thread 非同步更新的，跟 200ms 的 refresh timer 之間有競速——使用者拖動途中，refresh 讀到還沒更新的舊值就會把 slider 拉回去，感覺像「怎麼拖都沒用」。修法：拿掉這個週期性強制同步，slider 的值只在三個明確時機被程式改動：面板剛建立、Reset 按鈕、範圍校正時的（靜默）夾值；其餘時間完全交給使用者操作決定，refresh 只更新 label 文字跟範圍。
- Reset/Close 按鈕、視窗右上角 X（`WM_DELETE_WINDOW`）都直接呼叫 `session.reset_camera_properties()`/`close_controls_panel()`——這兩個方法本來就是 `acquired_lock` 保護的 thread-safe 方法，今天 CLI/gRPC 也是這樣跨執行緒呼叫，從 Tk thread 呼叫不是新風險。Reset 按鈕額外用「暫時拔掉 command」的方式把 slider 視覺上歸位到 `CAMERA_PROP_SPECS` 的靜態預設值，不會另外觸發 `request_property_value()`。
- `stop()` 送 `shutdown` 指令給 Tk thread，由它自己銷毀所有 Toplevel 並呼叫 `root.quit()` 結束 mainloop，然後 `join()` 該執行緒。

## Independent Test Strategy

- 用假的 `CameraSession`（只需 `camera_id`/`CAMERA_PROP_SPECS`/`camera_prop_cache`/`camera_prop_reported`/`camera_prop_ranges`/`request_property_value`/`reset_camera_properties`/`close_controls_panel`）測試 `open_panel()`→操作 slider→`close_panel()` 全流程，不需要真的相機。
- 確認同一個 `camera_id` 呼叫兩次 `open_panel()` 不會建立第二個視窗。
- 確認 `close_panel()` 對不存在的 `camera_id` 是安全的 no-op。
- 確認 `stop()` 後 Tk thread 確實結束（`join()` 不逾時）。
- 用假 session：`camera_prop_ranges` 一開始是空的（模擬還沒探測完成），開面板後才把它設成一個跟 `CAMERA_PROP_SPECS` 靜態預設值差很多的窄範圍（模擬 brightness→exposure 那種情況），確認 `request_property_value()` 完全沒被呼叫（範圍校正不能觸發假的使用者請求）。
- 模擬使用者拖動 slider（直接呼叫 `.set()`），確認只送出一次對應的 `request_property_value()`。
- 呼叫 Reset 按鈕，確認 `reset_camera_properties()` 被呼叫、但 `request_property_value()` 沒有被呼叫。

## Minimal Tasks

- [x] 建立獨立 Tk UI thread + queue-based 跨執行緒指令
- [x] 支援開/關單一相機面板
- [x] Slider 範圍讀取 DirectShow 真實硬體範圍，沒有時退回靜態值
- [x] Reset/Close 導到既有 thread-safe `CameraSession` 方法
- [x] 面板定時自我刷新（label、slider 範圍）
- [x] 修正「範圍校正時 Tk 自動夾值觸發假請求」導致 brightness 一開面板就衝到最大過曝的 bug
- [x] 拿掉週期性強制同步 slider 位置，避免跟使用者拖動競速打架
- [ ] 打包後的 exe 實機驗證 tcl/tk 資源正確收錄
- [ ] 多台相機同時開面板的長時間穩定性測試
