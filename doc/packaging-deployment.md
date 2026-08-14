# packaging-deployment

## Responsibility

描述 PyInstaller packaging、dist layout、SuperCarter publish integration。

## Public Surface

- `pyproject.toml`
- `uv.lock`
- `build_SuperEagleEye.ps1`
- `build_SuperEagleEye.bat`
- `SuperEagleEye.spec`
- `dist\SuperEagleEye_dist`

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
- `protobuf` and `grpcio-tools` are locked intentionally because generated files such as `SC_communication_gRPC_pb2.py` are sensitive to protobuf runtime/codegen compatibility.
- `build_SuperEagleEye.ps1` runs `py -3 -m uv sync --frozen` before packaging and then invokes PyInstaller through `py -3 -m uv run pyinstaller`.
- If `UV_CACHE_DIR` is not already set, the build script points uv cache to `.uv-cache` under the project root. This avoids permission issues on locked-down Windows accounts and the folder is ignored by git.
- `build_SuperEagleEye.bat` remains the stable Windows entry point and continues to call the PowerShell script.
- Packaging output 會被複製到 SuperCarter runtime 使用的位置。
- Frozen runtime 的 `BASE_DIR` 以 executable folder 為準。
- Logs 在 frozen executable 旁的 `logs` folder。
- Runtime 啟動時仍會讀取 `camera_map.json` 與 `version.json`。

## Build Commands

First-time setup on a machine without uv:

```powershell
py -3 -m pip install uv
```

Package the runtime:

```powershell
.\build_SuperEagleEye.bat
```

The PowerShell script performs the locked environment sync and PyInstaller build:

```powershell
py -3 -m uv sync --frozen
py -3 -m uv run pyinstaller ...
```

When dependency versions intentionally change, update `pyproject.toml`, run `py -3 -m uv lock`, and commit both `pyproject.toml` and `uv.lock`.

## Independent Test Strategy

- 執行 `py -3 -m uv sync --frozen`。
- 執行 build script。
- 確認 packaged output 包含 executable、proto generated files、camera map、version。
- 從 packaged folder 啟動並確認 log path。
- 透過 SuperCarter publish folder 啟動並確認 gRPC port。

## Minimal Tasks

- [x] 建立 `pyproject.toml`
- [x] 建立並提交 `uv.lock`
- [x] 改用 uv 管理 build environment
- [x] 建立 PowerShell build script
- [x] 建立 batch wrapper
- [x] 複製 dist 到 runtime folder
- [ ] 增加 packaging smoke checklist
