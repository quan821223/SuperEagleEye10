# CHANGELOG - SuperEagleEye
> from AMSC
> 所有顯著變更都會記錄在此檔案中。
> 本專案遵循 [Semantic Versioning](https://semver.org/lang/zh-TW/) 版本規範。

---

## [Unreleased]

**新增**

**修復**

**調整**

---

# [1.4.0] - 2026-08-18

**新增**

* feat(Build): 打包後於 `dist` 第一層產生版本化 runtime 資料夾與 7z，例如 `SuperEagleEye_v1.3.2_dist` / `SuperEagleEye_v1.3.2_dist.7z`，且壓縮檔內最上層資料夾同樣使用版本化名稱
* feat(Build): 新增 `py7zr` 建置依賴，讓 PowerShell build script 可直接產生 7z 封裝檔
* feat(CLI): `help` 新增 `help clients` 子指令，列出 SuperCarter / DDS 等外部控制端透過控制通道（`ExecuteCameraCommand` / `QueryCameraState`）可送給 `SEE_1.0` 的完整指令與查詢參考，跟本機終端機自己的指令表分開陳列

**修復**

* fix(Build): 最終 runtime folder 與 7z 只保留 `SuperEagleEye_v{version}.exe`，移除無版本名的 `SuperEagleEye.exe` 複本

**調整**

* refactor(Runtime): 將 `SuperEagleEye.py`（原本單一檔案 3712 行）拆分為 `see_runtime/` 套件，依職責切成 16 個模組（`camera_session`、`camera_manager`、`command_router`、`grpc_service`、`cli` 等），`SuperEagleEye.py` 保留為約 140 行的進入點；不影響既有功能與對外行為，僅為程式碼結構調整。已實際跑過一次 PyInstaller 完整 build 確認新套件會被自動收錄，不需修改 `SuperEagleEye.spec`
* chore(Security): 移除已追蹤的舊 `dist/` 打包產物與 runtime log，並整理 `.gitignore` 以阻擋 build output、logs、archives、env files 與 local secret files
* docs(Build): 更新 README、部署文件、handoff 與 packaging doc，對齊版本化 dist folder、版本化 exe、7z 封裝與 bat/ps1 工作流程
* docs(Design): 更新 `doc/detailed-design.md` 與各模組設計文件，補上對應的實際原始碼路徑（`see_runtime/*.py`）；更新 `doc/cli-interface.md` 說明新增的 `help clients` 子指令
* version: SEE10 runtime 版本由 `1.3.2` 進版到 `1.4.0`

---

# [1.3.2] - 2026-08-14

**新增**

* feat(Build): 新增 `pyproject.toml`，用 uv 管理 Python 3.12 建置環境與直接依賴版本
* feat(Build): 新增並提交 `uv.lock`，固定 PyInstaller、OpenCV、gRPC、protobuf、comtypes、numpy 與其解析後的相依套件版本

**修復**

**調整**

* refactor(Build): `build_SuperEagleEye.ps1` 改為先執行 `py -3 -m uv sync --frozen`，再透過 `py -3 -m uv run pyinstaller` 打包，不再每次用 pip 安裝最新版套件
* refactor(Build): 保留 `build_SuperEagleEye.bat` 作為既有 Windows 建置入口
* docs(Build): 更新 `doc/packaging-deployment.md`，說明 uv lock、Python 版本限制、依賴鎖定策略與建置指令
* version: SEE10 runtime 版本由 `1.3.1` 進版到 `1.3.2`

---

# [1.3.1] - 2026-08-14

**新增**

* feat(Output): 影像與影片輸出會先建立本機日期資料夾，格式為 `yyyy_MM_dd`，例如 `2026_08_14`
* feat(Output): `snapshot` 預設輸出改為 `--save_path/yyyy_MM_dd/檔名.jpg`
* feat(Output): SuperCarter 傳入 `output_path` 時保留原檔名，並輸出到原父資料夾底下的本機日期資料夾
* feat(Output): 錄影分段每次建立新 mp4 時重新解析本機日期資料夾，跨日後下一個 segment 會自動切到新日期資料夾
* feat(RuntimeInfo): `GET_RUNTIME_INFO` 的 `paths` 增加 `current_output_dir`，方便查詢目前實際輸出資料夾
* feat(CLI): `help` 增加 `record_start docs`，提供 `START_RECORD` 詳細文件連結

**修復**

**調整**

* version: SEE10 runtime 版本由 `1.3.0` 進版到 `1.3.1`

