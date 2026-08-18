# camera-session

## Responsibility

負責單一 logical camera 的 preview thread、OpenCV capture、snapshot、recording、camera properties 邏輯（brightness/focus）。**不負責 UI**——面板視窗由 `CameraControlsUI`（見 `doc/camera-controls-ui.md`）負責，`CameraSession` 只暴露 thread-safe 的資料/方法供其讀寫。

## Source

`see_runtime/camera_session.py`（原生 DirectShow 控制邏輯在 `see_runtime/dshow_camera_control.py`）

## Public Surface

- `CameraSession.start()`
- `CameraSession.stop()`
- `CameraSession.force_reopen()`
- `CameraSession.update_descriptor()`
- `CameraSession.snapshot()`
- `CameraSession.start_recording()`
- `CameraSession.stop_recording()`
- `CameraSession.apply_config()`
- `CameraSession.open_controls_panel()`
- `CameraSession.close_controls_panel()`
- `CameraSession.reset_camera_properties()`
- `CameraSession.request_property_value()`
- `CameraSession.status()`
- 唯讀資料：`camera_prop_cache`、`camera_prop_reported`、`camera_prop_ranges`（`CameraControlsUI` 直接讀取用來畫面板，不需要額外方法）

## Dependencies

- OpenCV `VideoCapture`
- `CameraDescriptor`
- `CameraConfig`
- `RecordingSession`
- `comtypes`（選用；用於原生 DirectShow `IAMVideoProcAmp`/`IAMCameraControl` 控制，沒裝就 fallback 回 OpenCV 路徑）

## Design Notes

- 每個 session 對應一個 preview thread。
- Camera open 時只設定 frame width、height、fps。
- Image properties 預設不套用，避免破壞 driver default tone。
- 只有 `brightness` 與 `focus` 兩個 properties 可調整，其他不提供、不記錄、不回套。
- Preview backend 優先使用 `CAP_MSMF`，目標是讓色調更接近 Windows Camera app。
- `controls_enabled` 只有在明確 command（`open_controls_panel()`）開啟後才會是 True；`_sync_controls_with_camera()` 每個 preview frame 檢查一次，False 就直接跳過，不做任何 property 相關工作。
- `close_controls_panel()`/使用者在面板按 Close/直接關視窗，最終都會讓 `controls_enabled` 變 False——這三種路徑對 `CameraSession` 來說是同一件事，沒有分別處理。
- `reset_camera_properties()` 是同步、thread-safe 的方法（`acquired_lock` 保護），CLI/gRPC/`CameraControlsUI` 的 Reset 按鈕都直接呼叫它，不需要先打開 controls panel，也不需要經過 preview thread 的輪詢 flag。
- Reopen 時只回套 `_camera_prop_modified_names` 內的 properties。
- **`CameraSession` 完全不知道、不 import 任何 GUI 套件**：使用者要調整 property，一律透過 `request_property_value(name, raw_value)`（thread-safe，`_panel_value_lock` 保護一個小 dict）通知；`_sync_controls_with_camera()` 每個 preview frame 把這個 dict 清空取出來，跟舊版「trackbar 值變了」走完全相同的 commit 流程。是誰在呼叫 `request_property_value()`（Tk 面板、或未來換別的 UI）跟這裡的邏輯無關。
- **Property 變更一律透過 release + 重開套用，不對正在 streaming 的 live capture 呼叫 `capture.set()`**：slider 變動後 debounce（`PROP_COMMIT_DEBOUNCE_SEC`＝0.4 秒）等使用者停止拖動，才呼叫 `_request_capture_reopen()` 交給 preview thread 自己 release/重開；因為 live `capture.set()` 在 MSMF/DSHOW backend 上很不可靠，且容易被 auto 模式覆蓋。
- `focus` spec 帶 `auto_disable_prop=CAP_PROP_AUTOFOCUS`：套用手動 focus 前會先關閉 autofocus，避免 driver 用自動對焦覆蓋手動值；`reset_defaults` 重開後不特別處理，driver 會用出廠預設（通常 autofocus 開啟）。
- `_apply_camera_properties()` 會把 `capture.get()` 回讀到的實際值存進 `camera_prop_reported`，並在 `status()`、`open_controls_panel()`、`reset_camera_properties()` 的回傳 payload 加上 `properties_reported` / `camera_properties_reported`，讓呼叫端能比對「要求值 vs 實際套用值」。
- `apply_config()`（`set` 指令，含解析度/fps）改成只更新 `self.config` 並呼叫 `_request_capture_reopen()`，不再從 CommandRouter 執行緒直接對 `self.capture` 呼叫 `set()`——capture 的 release/reopen 全程只由 preview thread 自己執行，避免跨執行緒同時操作同一個 `cv2.VideoCapture` 造成假死。
- `force_reopen()`（hot-plug 強制重開）維持原本從外部執行緒直接 `release()` 的做法，未變動。
- **優先走原生 DirectShow 控制（`IAMCameraControl`），OpenCV release+reopen 只當 fallback**：`_ensure_dshow_control()` 在 preview thread 第一次需要套用 property 時，透過手動宣告的 COM 介面（`comtypes`，不用 `comtypes.client` 的 typelib 動態產生，避免打包後在唯讀目錄寫 cache 的風險）列舉 DirectShow video-input devices，以 `friendly_name` 比對（找不到才退回用 `device_index` 定位，因為 MSMF 和 DirectShow 的列舉順序理論上可能不同）綁定到同一台實體相機的 `IBaseFilter`，直接呼叫 `IAMCameraControl::Set`（帶 Manual flag）。已在實機（Microsoft LifeCam Cinema）驗證：即使 `cv2.VideoCapture(0, CAP_MSMF)` 同時開著在讀取畫面，原生控制照樣可以即時生效，不需要 release/重開相機、也沒有 flicker。
  - **`brightness` 對應的其實是 `IAMCameraControl::Exposure`（property id 4），不是 `IAMVideoProcAmp::Brightness`**：實機量測發現這台相機的 `IAMVideoProcAmp::Brightness` 的 `Set()`/`Get()` 在 COM 層可以正常 round-trip（寫什麼讀回什麼），但完全不影響實際畫面（拍到的 frame 平均像素值在整個 30-255 範圍內幾乎不變，`GetRange()` 回傳的 capsFlags 也是 0，代表 driver 沒有真的支援 Auto/Manual），是一個沒接到成像管線的假屬性；換成 `IAMCameraControl::Exposure` 之後同一台相機的 frame 平均像素值可以從 ~9（最暗）掃到 ~229（最亮），是真的有效果。這是不少 UVC webcam driver 常見的行為（`VideoProcAmp::Brightness` 是沒接線的舊介面），不是這台相機獨有，所以直接把 `brightness` 這個名稱固定對應到 Exposure，`_DSHOW_PROPERTY_MAP` 裡兩個 property（brightness/focus）現在都走 `IAMCameraControl`，共用同一個 QueryInterface 快取。
  - 只要 `comtypes` 沒安裝、找不到對應裝置、或任何 COM call 失敗，就整個 fallback 回原本的 OpenCV release+reopen debounce 機制（見上，`brightness` fallback 時仍然對應 `CAP_PROP_BRIGHTNESS`，不是 exposure）——原生控制是 best-effort 加分項，不是必要路徑。
  - **`_apply_camera_property_native()` 絕對不能套用 `spec["scale"]`/`spec["offset"]`**：這兩個欄位是給 `_apply_camera_property_opencv()` 把 raw slider 值換算成 OpenCV `CAP_PROP_*` 期望的正規化值用的（例如 brightness 原本 `scale=100.0` 是要把 0-100 換成 0.0-1.0）。原生路徑的 raw 值本來就已經是 `camera_prop_ranges`（`GetRange()`）給的硬體原生單位，不需要也不能再換算——踩過一次坑：exposure 的合法範圍是 -11~1，套用 `raw/100.0` 後任何非 0 的小整數四捨五入都會變成 0，導致不管 slider 怎麼拖，實際送進 `IAMCameraControl::Set()` 的永遠是 0，畫面卡死不動，`camera_prop_reported` 也跟著卡死。原生路徑一律把 `raw` 原封不動傳給 `DirectShowPropertyController.set_manual()`。
  - 走原生路徑時，`_sync_controls_with_camera()` 不再 debounce，slider 變動立即即時套用（COM call 很輕量，不像 release+reopen 那樣會讓畫面閃一下）。
  - `_dshow_available`/`_dshow_control` 綁定的是「當下這台實體裝置」，裝置真的換了（`update_descriptor()` 的 `device_changed`、或 `force_reopen()`）才會重置，讓下次重新判斷/綁定。
  - `_ensure_dshow_control()` 成功時，會把每個 property 的 `GetRange()` 回傳的 `(min, max)` 存進 `camera_prop_ranges`，供 `CameraControlsUI` 畫 slider 時使用真實硬體範圍（沒有原生控制時，UI 端退回用 `CAMERA_PROP_SPECS` 裡的靜態 `max`）。`brightness` 對應 Exposure 之後範圍通常很小（例如 -11 到 1，log scale），slider 會照實際回傳的範圍顯示，不是 0-100。`_sync_controls_with_camera()` 只要 `controls_enabled` 就會主動探測一次（不用等到有值變動），讓面板一開就盡快拿到真實範圍。
  - `GetRange()` 同時也回傳 driver 真正的 default 值，存進 `camera_prop_native_defaults`；只要該 property 還沒被使用者改過（不在 `_camera_prop_modified_names` 裡），就順便把 `camera_prop_cache` 也更新成這個真實 default，`_reset_controls_to_driver_defaults()` 重置時也優先用這個值。**沒有這一步的話，面板會先用 `CAMERA_PROP_SPECS` 的靜態 `default`（例如 brightness=50，這是給舊版 0-100 範圍設計的，換成 exposure 之後完全對不上，通常會被夾到範圍邊界，slider 一開起來就停在最亮或最暗，而不是真正的 driver 預設值）**。因為這個更新只在 `_ensure_dshow_control()` 第一次成功探測時做一次，且只在還沒被改過的情況下才覆蓋，之後使用者真的拖動 slider 不會被這個機制覆蓋掉。

## Independent Test Strategy

- 開啟 camera 後確認不呼叫 property apply。
- 呼叫 `open_controls_panel()` 後確認 controls enabled。
- 呼叫 `close_controls_panel()` 後確認 controls disabled、`_panel_pending_values` 被清空。
- 修改 brightness 或 focus 後拔插/force reopen，確認只回套被修改的項目。
- 呼叫 `request_property_value()` 模擬面板變動，觸發 `reset_camera_properties()`，確認畫面回到 driver default 且後續 reopen 不再回套舊值。
- 直接呼叫 `reset_camera_properties()`，確認未開 panel 時也能清除 property 記錄並 reopen。
- 連續呼叫 `request_property_value()` 模擬拖動，若原生控制不可用，確認畫面不會每次都重開，debounce 到期後只重開一次並套用最終值；若原生控制可用，確認每次呼叫都即時套用、不觸發 capture reopen。
- 呼叫 `apply_config()` 改變 width/height/fps，確認 preview thread 自己 release + 重開，畫面能恢復顯示；只改 `recording_duration`/`max_folder_size_gb` 不觸發重開。
- 移除/不安裝 `comtypes` 時，確認 property 控制自動 fallback 回 OpenCV release+reopen，不會整個功能壞掉。
- `controls_enabled=False` 時呼叫 `request_property_value()`，確認被忽略（不會寫進 pending dict）。
- 有原生控制可用時，把 `brightness` 設到範圍極端值，量測 `capture.read()` 回來的 frame 平均像素值有明顯變化（不能只看 COM `Get()` 回讀值，因為那個對這類「假屬性」永遠會回報你剛剛寫入的值，不代表畫面真的變了）。

## Minimal Tasks

- [x] 建立 preview thread
- [x] 支援 snapshot
- [x] 支援 recording
- [x] 支援 force reopen
- [x] 支援 opt-in controls panel
- [x] 關閉 controls panel 後停止同步
- [x] 只回套使用者修改過的 properties
- [x] 支援 controls panel reset defaults
- [x] 支援 command-driven camera property reset
- [x] Property 變更改走 debounce + release/重開，取代 live capture.set()（fallback 路徑）
- [x] `apply_config()` 改走 preview thread 自己 reopen，避免跨執行緒操作 capture
- [x] 優先走原生 DirectShow `IAMVideoProcAmp`/`IAMCameraControl` 控制，OpenCV 路徑降級為 fallback（已在 Microsoft LifeCam Cinema 上驗證）
- [x] 移除 cv2 HighGUI trackbar 面板，`CameraSession` 改成完全 UI-agnostic，只透過 `request_property_value()`/`camera_prop_cache`/`camera_prop_reported`/`camera_prop_ranges` 跟外部 UI（`CameraControlsUI`）溝通
- [x] 修正原生路徑誤套用 OpenCV `scale`/`offset` 換算，導致 raw 值被四捨五入成 0、slider 怎麼拖都沒反應的 bug
- [x] 新增 `camera_prop_native_defaults`，面板/reset 改用真實 driver 預設值，slider 一開就停在正確位置，不會停在靜態預設換算後的邊界值
- [ ] 實機驗證更多不同廠牌 camera driver 對 DirectShow 原生控制的支援程度
