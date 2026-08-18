# SuperEagleEye (SEE_1.0)  Developer Guide

*AMSC_MGMDA0 · DQA*

*SEE Software Version: at least 1.2.0*
*Document Version: 0.2*

Developer-oriented documentation for the `camera_v2` runtime used by SEE.

If you are looking for operator instructions, read `README_user.md`.

## Overview

`SuperEagleEye.py` is the camera runtime used by SEE.

Current behavior:
- `SEE_1.0` exposes gRPC on `127.0.0.1:50051` by default
- gRPC port can be set within `50051` to `50060`
- `SuperCarter` connects as gRPC client and sends heartbeat every second
- runtime is resolved from the published `dist` folder under `SuperCarter`
- on startup, discovered cameras are assigned to fixed logical slots such as `cam0`, `cam1`, `cam2`
- operators should work with logical camera ids, not Windows `device_index`
- current runtime flow is one camera session per thread

## Runtime and Packaging

Current packaging flow:
1. Run `build_SuperEagleEye.ps1` or `build_SuperEagleEye.bat`
2. PyInstaller first generates a temporary runtime under `build\__dist\SuperEagleEye\`
3. The build renames `SuperEagleEye.exe` to `SuperEagleEye_v{version}.exe`
4. Packaging refreshes `dist\SuperEagleEye_v{version}_dist\` and creates `dist\SuperEagleEye_v{version}_dist.7z`
5. The versioned runtime folder and archive do not keep the unversioned `SuperEagleEye.exe` copy

Build commands:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_SuperEagleEye.ps1
```

or

```powershell
.\build_SuperEagleEye.bat
```

Important notes:
- `deploy_SuperEagleEye_to_local_runtime.bat` no longer deploys to `%LOCALAPPDATA%`
- runtime execution should come from the local or published `dist` folder only

## Shared Secret

Current shared secret behavior:
- shared secret file remains `%LOCALAPPDATA%\SEE\runtime\SuperEagleEye.secret`
- legacy `config\SuperEagleEye.secret` is still migrated when present
- manual startup can still pass `--auth_token`
- runtime package location moved to the versioned `dist\SuperEagleEye_v{version}_dist` output, but the auth token storage location did not change

## CLI Startup Arguments

- `--frame_width`: output frame width in pixels
- `--frame_height`: output frame height in pixels
- `--frame_rate`: preview and recording FPS target
- `--recording_duration`: maximum seconds per recording segment
- `--max_foldersize`: allowed folder size in GB
- `--grpc_port`: gRPC listener port; valid range is `50051` to `50060`, invalid input falls back to `50051`
- `--instance_id`: runtime instance id; use a different value only for intentional multi-instance runs
- `--device_indexes`: comma-separated OpenCV device indexes this runtime instance may use, for example `0` or `1,2`
- `--save_path`: output folder for snapshots and recordings
- `--auth_token`: shared secret for gRPC control

### Intentional Multi-Instance Startup

Default startup is protected against accidental double launch. If two `SuperEagleEye.exe`
instances are intentionally required, each instance must use a different `--instance_id`,
a different `--grpc_port`, and a non-overlapping `--device_indexes` value so they do not
compete for the same camera hardware.

Example:

```powershell
.\SuperEagleEye.exe --instance_id see10_a --grpc_port 50051 --device_indexes 0 --save_path .\videos_a
.\SuperEagleEye.exe --instance_id see10_b --grpc_port 50052 --device_indexes 1 --save_path .\videos_b
```

Do not start two instances with different `--instance_id` values but without
`--device_indexes`; both runtimes would scan and try to open the same camera devices.

## Logical Camera Model

Current logical camera model:
- `cam0`, `cam1`, `cam2` are logical slots
- startup auto-assigns discovered cameras to those logical slots and opens them
- `open cam1` means reopen logical slot `cam1`
- `close cam1` means close logical slot `cam1`
- `swap cam0 cam1` swaps the hardware assignments behind those logical slots
- `change cam0 cam1` is kept as an alias of `swap`
- operators should not need to manually care about `device_index` during normal use

Diagnostics:
- `list` shows opened logical sessions
- `list_devices` shows discovered devices plus metadata such as `device_id`, `pnp_device_id`, `assigned_camera_id`, and `opened_camera_id`
- hot-plug background refresh is silent during runtime; use `scan_devices` when you want a manual rescan result immediately
- if camera streams look mismatched after hot-plug, use `refresh_cameras` to rescan and force opened sessions to reopen

## Interactive Terminal Commands

These commands are available only when running `SEE_1.0` directly in its console.

### Status and Discovery

```text
status
list
list_devices
scan_devices
refresh_cameras
config cam0
record_state cam0
set_grpc_port
```

- `status`: runtime status and heartbeat/reconnect state
- `list`: opened logical camera sessions
- `list_devices`: discovered devices and current logical-slot bindings
- `scan_devices`: manually rescan devices and refresh logical-slot bindings immediately
- `refresh_cameras`: rescan devices and force all opened camera sessions to reopen
- `config <camera_id>`: query width, height, fps, recording duration, and folder size limits
- `record_state <camera_id>`: query recording state
- `set_grpc_port` / `grpc_port`: interactively rebind the gRPC listener; valid range is `50051` to `50060`, invalid input falls back to `50051`

### Capture and Recording

```text
snapshot cam0
record_start cam0 60
record_stop cam0
open_output_folder
```

- `snapshot <camera_id>`: capture one frame to the current output folder
- `record_start <camera_id> <duration_sec>`: start segmented recording
- `record_stop <camera_id>`: stop recording
- `open_output_folder`: open the active output folder in Windows Explorer

### Camera Lifecycle

```text
open cam1
close cam1
scan_devices
refresh_cameras
swap cam0 cam1
change cam0 cam1
set cam0 width=1280 height=720 fps=20
exit
```

- `open <camera_id>`: reopen an assigned logical camera slot
- `close <camera_id>`: close a logical camera slot
- `scan_devices` / `rescan`: manually rescan camera hardware once and refresh logical slot bindings
- `refresh_cameras` / `refresh`: rescan camera hardware and force opened sessions to reopen their streams
- `swap <camera_id_a> <camera_id_b>`: swap hardware bindings between two logical slots
- `change <camera_id_a> <camera_id_b>`: alias of `swap`
- `set <camera_id> key=value ...`: update runtime config for the selected logical camera
- `exit`: shutdown runtime

Console output behavior:
- command-type instructions print `ack: C3 0D 0A`
- query-type instructions print JSON plus the encoded response frame

## gRPC Contract

Proto file:
- `SC_communication_gRPC.proto`

Used RPCs for `SEE_1.0`:
- `Heartbeat(HeartbeatRequest) returns (HeartbeatResponse)`
- `ExecuteCameraCommand(CommandRequest) returns (CommandReply)`
- `QueryCameraState(QueryRequest) returns (QueryReply)`

Supported command RPC usage in current implementation:
- `PING`
- `OPEN_CAMERA`
- `CLOSE_CAMERA`
- `START_RECORD`
- `STOP_RECORD`
- `SCAN_DEVICES`
- `CAPTURE_SNAPSHOT`
- `SET_CAMERA_CONFIG`
- `SET_OUTPUT_ROOT`
- `SET_GRPC_PORT`
- `OPEN_OUTPUT_FOLDER`
- `SHUTDOWN`
- `SWAP_CAMERAS`

Supported query RPC usage:
- `GET_STATUS`
- `LIST_CAMERAS`
- `LIST_DEVICES`
- `GET_CAMERA_CONFIG`
- `GET_RECORD_STATE`

### SuperCarter Command Reference

The following table describes the full command set that `SuperCarter` can send to `SEE_1.0`
through `ExecuteCameraCommand(CommandRequest)`.

General request fields:
- `command`: command name such as `CAPTURE_SNAPSHOT`
- `camera_id`: logical camera slot such as `cam0`, `cam1`, `cam2`; all camera-targeting commands must fill this field explicitly
- `args_json`: JSON object encoded as UTF-8 text; include it only when extra arguments are needed
- `auth_token`: shared secret from `%LOCALAPPDATA%\SEE\runtime\SuperEagleEye.secret`
- `request_id`: caller-generated id for request tracking
- `source`: caller label, typically `SuperCarter` or `grpc`

Camera targeting rule:
- the command name itself stays generic, for example `CAPTURE_SNAPSHOT`
- the actual target camera is selected by `camera_id`
- this means `SuperCarter` must explicitly fill `camera_id = "cam0"` or `camera_id = "cam1"` for camera-specific actions
- do not assume the runtime will infer the target camera from the command name alone

#### `PING`

Purpose:
- verify that the runtime is alive and responsive
- check current connection state seen by the runtime

Required fields:
- `command = "PING"`
- `camera_id` may be empty or `cam0`

Successful payload:
- `ack_hex`
- `connection_state`

Typical use:
- lightweight health check before sending camera-specific actions

**ex:** `PING`

#### `OPEN_CAMERA`

Purpose:
- open or reopen a logical camera slot such as `cam0`
- bind that logical slot to its currently assigned hardware descriptor

Required fields:
- `command = "OPEN_CAMERA"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector

Successful payload:
- full camera status object from the opened session
- includes `camera_id`, `device_index`, `friendly_name`, `opened`, `recording`, `width`, `height`, `fps`

Failure cases:
- `CAMERA_BUSY` when the logical slot is already open
- `NO_FRAME_YET` when the camera is open but has not produced a first frame yet
- `CAMERA_NOT_FOUND` when the slot has no assignment

Typical use:
- reopen a preview after a manual close
- reopen a slot after swapping camera assignments

**ex:** `OPEN_CAMERA cam0`

#### `CLOSE_CAMERA`

Purpose:
- close one logical camera slot and stop its preview and recording state

Required fields:
- `command = "CLOSE_CAMERA"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector

Successful payload:
- `camera_id`
- `closed = true`

Typical use:
- stop one preview window without shutting down the whole runtime

**ex:** `CLOSE_CAMERA cam1`

#### `SWAP_CAMERAS`

Purpose:
- swap the hardware assignments behind two logical camera slots

String command syntax:

```text
SWAP_CAMERAS [source_camera_id] [target_camera_id]
```

Explanation:
- `source_camera_id`: the logical slot to swap from
- `target_camera_id`: the logical slot to swap with
- this command exchanges the hardware bindings behind those two logical slots

Structured RPC note:
- current RPC implementation still carries these values through `args_json`
- the caller should provide:
  - `source_camera_id`
  - `target_camera_id`

Successful payload:
- updated binding information for the affected logical slots

Typical use:
- when two USB cameras are reversed relative to the expected `cam0` / `cam1` view

**ex:** `SWAP_CAMERAS cam0 cam6`

#### `START_RECORD`

Purpose:
- start segmented recording on one logical camera slot

Required fields:
- `command = "START_RECORD"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector
- `args_json` may include:
  - `duration_sec`
  - `output_dir`
  - `file_prefix`

Example `args_json`:

```json
{
  "duration_sec": 60,
  "file_prefix": "cam0"
}
```

Successful payload:
- `camera_id`
- `recording = true`
- `duration_sec`

Notes:
- recording requires the target camera to already be opened and receiving frames

**ex:** `START_RECORD cam0 {"duration_sec":60}`

#### `STOP_RECORD`

Purpose:
- stop recording on one logical camera slot

Required fields:
- `command = "STOP_RECORD"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector

Successful payload:
- `camera_id`
- `recording = false`

**ex:** `STOP_RECORD cam0`

#### `CAPTURE_SNAPSHOT`

Purpose:
- capture one still frame from an opened logical camera slot

Required fields:
- `command = "CAPTURE_SNAPSHOT"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector
- `camera_id = "all"` is also supported for a broadcast-style snapshot across every opened logical camera
- `args_json` may be empty or may include `output_path`

String command syntax:

```text
CAPTURE_SNAPSHOT [cam0|cam1|cam2|all]
```

Target definition:
- `cam0|cam1|cam2`: capture one still frame from the specified logical camera slot
- `all`: capture one still frame from every currently opened logical camera

Accepted target values:
- `cam0|cam1|cam2|all`
- `0|1|2`

Structured RPC formula:

```json
{
  "command": "CAPTURE_SNAPSHOT",
  "camera_id": "<cam0|cam1|cam2|all>",
  "args_json": "{} | {\"output_path\":\"D:\\\\temp\\\\cam0.jpg\"}"
}
```

Successful payload:
- `camera_id`
- `snapshot_path`

**Note:**
- this is the command `SuperCarter` should send when it wants SEE to perform a screenshot

`ALL` behavior:
- when `camera_id = "all"`, the runtime captures one frame from every currently opened logical camera
- in this mode the payload returns:
  - `camera_id = "all"`
  - `snapshots`: list of `{ camera_id, snapshot_path }`
  - `count`: number of snapshots captured
- `output_path` should not be combined with `all`; let the runtime generate per-camera filenames automatically

Constraint:
- `all` is currently supported for `CAPTURE_SNAPSHOT` only
- single-camera commands such as `OPEN_CAMERA`, `CLOSE_CAMERA`, and `SET_CAMERA_CONFIG` must still use one explicit `camera_id`

**ex:** `CAPTURE_SNAPSHOT cam0`, `CAPTURE_SNAPSHOT all`

#### `SET_CAMERA_CONFIG`

Purpose:
- update runtime capture settings on one logical camera slot

Required fields:
- `command = "SET_CAMERA_CONFIG"`
- `camera_id = "cam0"` or another logical slot; this field is the actual target selector
- `args_json` must include at least one of:
  - `width`
  - `height`
  - `fps`
  - `recording_duration`
  - `max_folder_size_gb`

Successful payload:
- current full status for that logical camera after config changes

**ex:** `SET_CAMERA_CONFIG cam0 {"width":1280,"height":720,"fps":20}`

#### `SET_OUTPUT_ROOT`

Purpose:
- update the default save folder used by snapshots and recordings

Required fields:
- `command = "SET_OUTPUT_ROOT"`
- `camera_id` may be empty or `cam0`
- `args_json` must include either:
  - `output_dir`
  - `save_path`

Successful payload:
- resolved `output_dir`

Typical use:
- `SuperCarter` should send this after startup so SEE writes files into the current operator-selected folder

**ex:** `SET_OUTPUT_ROOT {"output_dir":"D:\\capture_output"}`

Alternative syntax:

```json
{
  "save_path": "D:\\capture_output"
}
```

#### `OPEN_OUTPUT_FOLDER`

Purpose:
- ask the runtime which output folder is currently active

Required fields:
- `command = "OPEN_OUTPUT_FOLDER"`

Successful payload:
- resolved `output_dir`

Note:
- current implementation returns the folder path; the caller decides whether to open Explorer

**ex:** `OPEN_OUTPUT_FOLDER`

#### `SHUTDOWN`

Purpose:
- request full SEE runtime shutdown

Required fields:
- `command = "SHUTDOWN"`

Successful payload:
- `shutdown = true`

Typical use:
- stop button in `SuperCarter`

**ex:** `SHUTDOWN`

### SuperCarter Query Reference

The following query set is sent through `QueryCameraState(QueryRequest)`.

General request fields:
- `query`: query name such as `GET_STATUS`
- `camera_id`: required for camera-specific queries and must be explicitly filled with `cam0`, `cam1`, or `cam2`
- `args_json`: JSON object when the query needs extra arguments

#### `GET_STATUS`

Purpose:
- retrieve runtime-level state

Successful payload:
- `connection_state`
- `uptime_sec`
- `camera_count`
- `default_camera_id`
- `recording_cameras`

**ex:** `GET_STATUS`

#### `LIST_CAMERAS`

Purpose:
- list all currently opened logical sessions

Successful payload:
- `cameras`

Each camera entry includes:
- `camera_id`
- `device_index`
- `friendly_name`
- `opened`
- `recording`
- `width`
- `height`
- `fps`

**ex:** `LIST_CAMERAS`

#### `LIST_DEVICES`

Purpose:
- list discovered physical devices and their logical-slot assignments

Successful payload:
- `devices`

Each device entry may include:
- `camera_index`
- `device_index`
- `friendly_name`
- `device_id`
- `pnp_device_id`
- `location_information`
- `manufacturer`
- `is_external`
- `assigned_camera_id`
- `opened_camera_id`

Typical use:
- diagnose wrong camera mapping when two devices have the same friendly name

**ex:** `LIST_DEVICES`

#### `GET_CAMERA_CONFIG`

Purpose:
- retrieve current config for one logical camera slot

Required fields:
- `query = "GET_CAMERA_CONFIG"`
- `camera_id = "cam0"` or another logical slot

Successful payload:
- same camera status/config fields returned by the runtime session

**ex:** `GET_CAMERA_CONFIG cam0`

#### `GET_RECORD_STATE`

Purpose:
- retrieve recording state for one logical camera slot

Required fields:
- `query = "GET_RECORD_STATE"`
- `camera_id = "cam0"` or another logical slot

Successful payload:
- `camera_id`
- `recording`

**ex:** `GET_RECORD_STATE cam0`

## Current Known Issues

Current known limitations from field testing:
- two cameras of the same model may still expose unstable stream behavior through OpenCV and Windows backend combinations
- `cam1` may fail under `CAP_MSMF`; runtime now rotates backends after read failures
- cameras connected through the same USB hub may interfere with each other
- two same-model cameras can be different physical devices even when `friendly_name` is identical; use `list_devices` and compare `device_id` / `pnp_device_id`
- `close camX` used to be crash-prone; shutdown path was hardened so capture is released before window destruction

## Operator Guidance for Debugging

Recommended debugging order:
1. Start `SEE_1.0`
2. Run `list_devices`
3. Confirm `assigned_camera_id` and `opened_camera_id`
4. If camera views look wrong, test `swap cam0 cam1`
5. If stream instability continues, move cameras to different USB root ports instead of the same hub

## Notes

- snapshot and preview continue working when SEE is offline
- logical-slot workflow is now the primary workflow; avoid the older `device_index` mental model in day-to-day operation

## Important Reminder

> 1. **When two camera USB connections are placed on the same USB-HUB, black screen, stutter, frame drops, or stream-read failures are more likely to occur.**
> 2. **Do not place camera USB connections and the SuperCarrier USB connection on the same USB-HUB.**

