# packaging-deployment

## Responsibility

描述 PyInstaller packaging、dist layout、SuperCarter publish integration。

## Public Surface

- `pyproject.toml`
- `uv.lock`
- `build_SuperEagleEye.ps1`
- `build_SuperEagleEye.bat`
- `SuperEagleEye.spec`
- `dist\SuperEagleEye_v{version}_dist`

## Dependencies

- uv
- PyInstaller
- Python 3.12 (`pyproject.toml` requires `>=3.12,<3.13`)
- Python virtual environment managed by uv
- Generated gRPC Python files
- `camera_map.json`
- `version.json`

## Design Notes

- Python dependencies are declared in `pyproject.toml`.
- `uv.lock` is committed so packaging uses the same resolved dependency versions on each machine.
- Direct runtime/build dependencies are pinned to the versions that were already installed in the working Python 3.12 environment when uv was introduced:
  - `pyinstaller==6.19.0`
  - `opencv-python==4.13.0.92`
  - `grpcio==1.78.0`
  - `grpcio-tools==1.78.0`
  - `protobuf==6.33.6`
  - `comtypes==1.4.16`
  - `numpy==2.4.3`
  - `py7zr==1.1.3`
- `protobuf` and `grpcio-tools` are locked intentionally because generated files such as `SC_communication_gRPC_pb2.py` are sensitive to protobuf runtime/codegen compatibility.
- `build_SuperEagleEye.ps1` runs `py -3 -m uv sync --frozen` before packaging and then invokes PyInstaller through `py -3 -m uv run pyinstaller`.
- If `UV_CACHE_DIR` is not already set, the build script points uv cache to `.uv-cache` under the project root. This avoids permission issues on locked-down Windows accounts and the folder is ignored by git.
- `build_SuperEagleEye.bat` remains the stable Windows entry point and continues to call the PowerShell script.
- Packaging output 會被複製到版本化 runtime folder，例如 `dist\SuperEagleEye_v1.3.2_dist`。
- Frozen runtime 的 `BASE_DIR` 以 executable folder 為準。
- Logs 在 frozen executable 旁的 `logs` folder。
- Runtime 啟動時仍會讀取 `camera_map.json` 與 `version.json`。
- Packaging renames the PyInstaller executable from `SuperEagleEye.exe` to `SuperEagleEye_v{version}.exe`, where `{version}` is read from `version.json`. The unversioned executable is not kept in the final runtime folder or archive.
- Packaging also creates a versioned 7z archive at `dist\SuperEagleEye_v{version}_dist.7z` through `py7zr` and removes the legacy unversioned `dist\SuperEagleEye_dist.7z` archive if it exists. The archive contains a versioned top-level folder named `SuperEagleEye_v{version}_dist`.

## Build Commands

First-time setup on a machine without uv:

```powershell
py -3 -m pip install uv
```

Package the runtime:

```powershell
.\build_SuperEagleEye.bat
```

Expected executable outputs under `dist\SuperEagleEye_v1.3.2_dist`:

```text
SuperEagleEye_v1.3.2.exe
```

Expected archive output under `dist`:

```text
SuperEagleEye_v1.3.2_dist.7z
```

Expected top-level folder inside the archive:

```text
SuperEagleEye_v1.3.2_dist\
```

The PowerShell script performs the locked environment sync and PyInstaller build:

```powershell
py -3 -m uv sync --frozen
py -3 -m uv run pyinstaller ...
```

When dependency versions intentionally change, update `pyproject.toml`, run `py -3 -m uv lock`, and commit both `pyproject.toml` and `uv.lock`.

## Build Workflow

`build_SuperEagleEye.bat` is the Windows entry point for manual builds:

1. Resolve the repository folder from `%~dp0`.
2. Verify `build_SuperEagleEye.ps1` exists beside the batch file.
3. Run the PowerShell script with `powershell -ExecutionPolicy Bypass -File`.
4. Stop with an error if the PowerShell build fails.
5. Read `version.json` after a successful build.
6. Print the versioned dist folder, versioned executable path, executable list, and archive list.

`build_SuperEagleEye.ps1` performs the package build:

1. Read `version.json` and sanitize the version for file and folder names.
2. Set `.uv-cache` under the project root as `UV_CACHE_DIR` when no cache directory is already configured.
3. Verify Python 3 and uv are available.
4. Run `py -3 -m uv sync --frozen` so the build uses the locked dependency set from `uv.lock`.
5. Remove old build output, the current versioned dist folder, and legacy unversioned publish folders.
6. Remove the generated PyInstaller spec file when it exists.
7. Run PyInstaller through `py -3 -m uv run pyinstaller` into `build\__dist\SuperEagleEye`.
8. Rename `build\__dist\SuperEagleEye\SuperEagleEye.exe` to `SuperEagleEye_v{version}.exe`.
9. Copy `version.json` into the temporary runtime folder.
10. Copy the temporary runtime folder into `dist\SuperEagleEye_v{version}_dist`.
11. Remove the legacy unversioned archive if it exists.
12. Copy the temporary runtime folder into `build\__package\SuperEagleEye_v{version}_dist`.
13. Create `dist\SuperEagleEye_v{version}_dist.7z` from that versioned package folder.

## Independent Test Strategy

- 執行 `py -3 -m uv sync --frozen`。
- 執行 build script。
- 確認 packaged output 只包含 `SuperEagleEye_v{version}.exe`，並包含 proto generated files、camera map、version。
- 從 packaged folder 啟動並確認 log path。
- 透過 SuperCarter publish folder 啟動並確認 gRPC port。

## Minimal Tasks

- [x] 建立 `pyproject.toml`
- [x] 建立並提交 `uv.lock`
- [x] 改用 uv 管理 build environment
- [x] 建立 PowerShell build script
- [x] 建立 batch wrapper
- [x] 複製 dist 到 runtime folder
- [x] 產生版本化 dist folder 與 7z archive
- [x] 最終 package 只保留版本化 executable
- [ ] 增加 packaging smoke checklist


