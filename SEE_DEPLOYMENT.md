# SEE Deployment

This document describes how `SEE` is deployed with `SuperCarter`.

## 1. Standard Publish Deployment

This is the default deployment path for release builds.

Flow:
1. Run `build_SuperEagleEye.ps1` in the repository root.
2. The script packages `SuperEagleEye.py` with PyInstaller into a temporary `SuperEagleEye.exe`.
3. The script renames the executable to `SuperEagleEye_v{version}.exe` and copies the packaged runtime into `dist\SuperEagleEye_v{version}_dist`.
4. The script creates `dist\SuperEagleEye_v{version}_dist.7z` with a versioned top-level folder and no unversioned executable copy.

Relevant files:
- `build_SuperEagleEye.ps1`
- `SuperEagleEye.py`
- `SuperCarter.csproj`

## 2. Local Override Deployment

This is the preferred path when testing a newer SEE runtime without republishing the full `SuperCarter` package.

Place the runtime under:

```text
%LOCALAPPDATA%\SEE\runtime\SuperEagleEye\
```

At startup, `SuperCarter` prefers this external runtime over the bundled publish copy.

## 3. Development Run

For local development, run SEE directly from Python:

```bash
python SuperEagleEye.py --frame_width 640 --frame_height 480 --frame_rate 20 --recording_duration 60 --max_foldersize 10 --save_path ./videos
```

This is for development and debugging, not the standard release deployment path.

## Runtime Resolution Order

`SuperCarter` resolves SEE in this order:

1. `%LOCALAPPDATA%\SEE\runtime\SuperEagleEye\SuperEagleEye_v{version}.exe`
2. `<SuperCarter publish>\config\SuperEagleEye\SuperEagleEye_v{version}.exe`

## Shared Secret

SEE shared secret location:

```text
%LOCALAPPDATA%\SEE\runtime\SuperEagleEye.secret
```

## Version Compatibility

SEE compatibility is defined by `version.json`.

Current fields:
- `runtime_name`
- `version`
- `min_see_version`

## Recommended Usage

- Use publish deployment for normal release delivery.
- Use local override deployment for testing a new SEE runtime quickly.
- Use direct Python execution only for development and debugging.

