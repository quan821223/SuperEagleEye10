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
