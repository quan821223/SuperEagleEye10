# Handoff

Last updated: 2026-04-14
Working directory: `D:\yqgithub\Tool-SuperCarter\SuperCarter\SuperCarter\Camera\camera_v2`

## Current Goal

This handoff file was created so the next Codex session can pick up work in `camera_v2` without relying on prior chat history.

Current active work has focused on stabilizing camera hot-plug behavior, logical slot assignment, and manual recovery commands for same-model USB cameras.

## Project Context

- This directory contains the `SuperEagleEye` camera runtime for SEE.
- Main runtime entry point: `SuperEagleEye.py`
- Developer documentation: `README.md`
- Operator documentation: `README_user.md`
- Packaging/build scripts:
  - `build_SuperEagleEye.ps1`
  - `build_SuperEagleEye.bat`
- Protocol files:
  - `SC_communication_gRPC.proto`
  - generated `*_pb2.py` files

## Current Runtime Model

- Runtime exposes gRPC on `127.0.0.1:50051`
- `SuperCarter` acts as gRPC client and sends heartbeat
- Cameras are mapped to logical IDs `cam0` through `cam9`
- Operators are expected to use logical camera IDs instead of Windows device indexes
- Published runtime is expected under `dist\SuperEagleEye\` and later included in `SuperCarter` publish output

Current runtime behavior under active development:

- logical slots are fixed to `cam0`..`cam9`
- `camera_map.json` now contains `cam0`..`cam9` entries and stores binding metadata
- hot-plug refresh can reassign descriptors after device changes
- manual `scan_devices` / `rescan` rescans hardware and refreshes logical bindings
- manual `refresh_cameras` / `refresh` rescans hardware and forces opened sessions to reopen
- camera sessions now force reopen on topology changes
- all camera sessions now share one global backend choice at a time instead of mixing DSHOW/MSMF per camera

## Working Tree Status

Observed from `git status --short` when this handoff was updated:

```text
 M ../../../SuperCarter.Tests.XUnit/bin/Debug/net6.0-windows/.msCoverageSourceRootsMapping_SuperCarter.Tests.XUnit
 M README.md
 M README_user.md
 M SuperEagleEye.py
 M build_SuperEagleEye.bat
 M build_SuperEagleEye.ps1
 M camera_map.json
 M ../../bin/Debug/SuperCarter.dll
 M ../../bin/Debug/SuperCarter.exe
 M ../../bin/Debug/SuperCarter.pdb
 M ../../bin/Debug/config/root/appsettings.json
 M ../../../SuperCarterTests/bin/Debug/net6.0-windows/.msCoverageSourceRootsMapping_SuperCarterTests
 M ../../../SuperCarterTests/bin/Debug/net6.0-windows/CoverletSourceRootsMapping_SuperCarterTests
?? HANDOFF.md
```

Notes:

- The worktree was already dirty before this handoff was created.
- This session added `HANDOFF.md` and modified runtime/build/docs files listed above.
- Treat `bin/Debug` outputs and coverage mapping files as generated artifacts unless the next task proves otherwise.

## Important Files To Inspect First

If continuing development, read these first:

1. `README.md`
2. `README_user.md`
3. `SuperEagleEye.py`
4. `camera_map.json`
5. `SEE_DEPLOYMENT.md`

## Useful Commands

Build/package runtime:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_SuperEagleEye.ps1
```

or

```powershell
.\build_SuperEagleEye.bat
```

Current packaging behavior:

- output is copied to `dist\SuperEagleEye_dist`

Inspect repo state:

```powershell
git status --short
git diff -- README.md README_user.md SuperEagleEye.py
```

Useful runtime recovery commands in SEE console:

```text
list
list_devices
scan_devices
refresh_cameras
```

- `scan_devices` / `rescan`: rescan hardware and refresh logical slot bindings
- `refresh_cameras` / `refresh`: rescan hardware and force all opened sessions to reopen

## What Changed In This Session

- Added hot-plug and rescan work in `SuperEagleEye.py`
- Added manual `refresh_cameras` / `refresh` CLI command
- Updated CLI help and docs to mention the new command
- Expanded `camera_map.json` to fixed `cam0`..`cam9` slots
- Updated build scripts so packaging copies to `dist\SuperEagleEye_dist`
- Added handoff documentation

## Known Issues / Current Risks

- Same-model dual cameras are still not fully stable after unplug/replug sequences.
- A specific unresolved case remains:
  - when both devices are unplugged and then reinserted in sequence, `cam1` can still show the same image as `cam0` even though `list_devices` reports distinct `device_index` and distinct `device_id` values
- The current best hypothesis is backend / driver behavior in OpenCV for same-model cameras, not just logical-slot metadata.
- The latest mitigation already in code:
  - force session reopen on topology changes
  - global shared backend choice for all sessions
  - manual `refresh_cameras` command
- Further debugging should inspect whether both sessions are opening unique streams despite distinct `device_index` values.

## Important Findings

- `SuperCarter` launches `SuperEagleEye.exe` directly from C# using `ProcessStartInfo`; it is not launching the `.py` file directly during normal runtime.
- The console window seen when SuperCarter launches SEE is expected because:
  - the packaged runtime is built with PyInstaller `--console`
  - `See10ProcessService.cs` uses `UseShellExecute = true` and `CreateNoWindow = false`
- `list_devices` has shown two distinct same-model cameras with distinct `device_id` values in at least one tested state:
  - `USB\\VID_045E&PID_0812&MI_00\\8&146C3F30&0&0000`
  - `USB\\VID_045E&PID_0812&MI_00\\8&1B28E8B&0&0000`
- Even with distinct IDs and correct slot assignment in `list_devices`, both sessions can still show the same image. This strongly suggests the remaining bug is at capture/backend behavior rather than only slot metadata.

## Suggested Next Steps

1. Run `git diff -- README.md README_user.md SuperEagleEye.py build_SuperEagleEye.ps1 build_SuperEagleEye.bat camera_map.json` to inspect current in-progress changes.
2. Rebuild/package after code changes with `build_SuperEagleEye.ps1` or `build_SuperEagleEye.bat`.
3. Reproduce the unresolved case:
   unplug all opened cameras, then reinsert both same-model devices in sequence, and compare actual windows with `list` and `list_devices`.
4. If duplicate images still occur, instrument `SuperEagleEye.py` further:
   log backend choice, session reopen timing, and per-session descriptor after every refresh.
5. Confirm whether the modified `bin/Debug` outputs should be ignored, rebuilt, or committed.

## Prompt For Next Codex

Use this as the starting prompt for the next session:

```text
Read HANDOFF.md in camera_v2 first, then inspect git diff for README.md, README_user.md, SuperEagleEye.py, build_SuperEagleEye.ps1, build_SuperEagleEye.bat, and camera_map.json. Assume the repo is already dirty and do not revert unrelated changes. Focus first on the unresolved same-model dual-camera hot-plug bug where cam1 can still show cam0's image after unplug/replug even when list_devices reports distinct device_id values.
```
