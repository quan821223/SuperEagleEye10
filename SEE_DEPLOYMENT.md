# SEE Deployment

This document describes how `SEE` is deployed with `SuperCarter`.

## 1. Standard Publish Deployment

This is the default deployment path for release builds.

Flow:
1. Run `build_SuperEagleEye.ps1` in `Camera\camera_v2`.
2. The script packages `SuperEagleEye.py` into `SuperEagleEye.exe`.
3. The packaged runtime is copied into `tools\SuperEagleEye_dist`.
4. During `SuperCarter` publish, those files are included in `config\SuperEagleEye\...`.

Relevant files:
- `Camera\camera_v2\build_SuperEagleEye.ps1`
- `Camera\camera_v2\SuperEagleEye.py`
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

1. `%LOCALAPPDATA%\SEE\runtime\SuperEagleEye\SuperEagleEye.exe`
2. `<SuperCarter publish>\config\SuperEagleEye\SuperEagleEye.exe`

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
