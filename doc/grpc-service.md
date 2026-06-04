# grpc-service

## Responsibility

負責將 `SC_communication_gRPC.proto` 的 RPC request 轉接到 `CommandRouter`。

## Public Surface

- `See10Service.Heartbeat()`
- `See10Service.ExecuteCameraCommand()`
- `See10Service.QueryCameraState()`
- `GrpcServerController.start()`
- `GrpcServerController.stop()`
- `GrpcServerController.set_port()`

## Dependencies

- `SC_communication_gRPC_pb2`
- `SC_communication_gRPC_pb2_grpc`
- `grpc`
- `CommandRouter`

## Design Notes

- Proto 不需為每個功能新增 RPC；command/query name 由 payload 指定。
- `ExecuteCameraCommand` 處理 command。
- `QueryCameraState` 處理 query。
- Auth token 在 service 入口驗證。
- Response payload 使用 JSON 字串。

## Independent Test Strategy

- 呼叫 `Heartbeat` 驗證 connected response。
- 呼叫 `ExecuteCameraCommand` 的 `OPEN_CAMERA_PANEL`。
- 呼叫 `QueryCameraState` 的 `GET_RUNTIME_INFO`。
- 驗證 auth failed 時 gRPC status 為 unauthenticated。

## Minimal Tasks

- [x] 建立 gRPC adapter
- [x] 支援 command RPC
- [x] 支援 query RPC
- [x] 支援 port rebind
- [ ] 增加 gRPC integration smoke test
