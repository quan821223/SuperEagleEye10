"""Adapts `SC_communication_gRPC.proto` RPCs (used by SuperCarter / DDS) to
`CommandRouter`. See `doc/grpc-service.md`.
"""

import json
import logging

from see_runtime.bootstrap import grpc, pb2, pb2_grpc
from see_runtime.command_router import CommandRouter
from see_runtime.constants import ACK_BYTES, RESULT_CODES
from see_runtime.errors import CommandError
from see_runtime.protocol_utils import build_query_frame, current_millis, parse_json_dict

LOGGER = logging.getLogger("SuperEagleEye")


class See10Service(pb2_grpc.SC_communication_gRPCServicer):
    def __init__(self, router: CommandRouter):
        self.router = router
        LOGGER.info("grpc_service_initialized")

    def Heartbeat(self, request, context):
        try:
            LOGGER.info("grpc_heartbeat_request client_id=%s", request.client_id)
            payload = self.router.heartbeat(request.client_id, request.auth_token)
        except CommandError as exc:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(exc.message)
            LOGGER.warning("grpc_heartbeat_failed client_id=%s code=%s message=%s", request.client_id, exc.code, exc.message)
            return pb2.HeartbeatResponse(connected=False, ack=b"", server_time=current_millis(), message=exc.message)
        LOGGER.info("grpc_heartbeat_response client_id=%s connected=%s", request.client_id, True)
        return pb2.HeartbeatResponse(
            connected=True,
            ack=ACK_BYTES,
            server_time=current_millis(),
            message=json.dumps(payload, ensure_ascii=False),
        )

    def ExecuteCameraCommand(self, request, context):
        try:
            self.router.validate_auth(request.auth_token)
            args = parse_json_dict(request.args_json)
            LOGGER.info(
                "grpc_execute_request request_id=%s source=%s command=%s camera_id=%s args_json=%s",
                request.request_id,
                request.source,
                request.command,
                request.camera_id,
                request.args_json,
            )
            result = self.router.execute(request.command, request.camera_id or "cam0", args, source=request.source or "grpc")
        except CommandError as exc:
            if exc.code == "AUTH_FAILED":
                context.set_code(grpc.StatusCode.UNAUTHENTICATED)
                context.set_details(exc.message)
            result = {"success": False, "code": exc.code, "message": exc.message, "payload": {}, "ack": b"", "source": "grpc"}
            LOGGER.warning("grpc_execute_failed request_id=%s code=%s message=%s", request.request_id, exc.code, exc.message)
        payload_json = json.dumps(result["payload"], ensure_ascii=False)
        LOGGER.info(
            "grpc_execute_response request_id=%s success=%s code=%s message=%s",
            request.request_id,
            result["success"],
            result["code"],
            result["message"],
        )
        return pb2.CommandReply(
            success=result["success"],
            request_id=request.request_id,
            ack=result["ack"],
            result_code=RESULT_CODES[result["code"]],
            message=result["message"],
            response_frame=b"",
            payload_json=payload_json,
            server_time=current_millis(),
        )

    def QueryCameraState(self, request, context):
        try:
            self.router.validate_auth(request.auth_token)
            args = parse_json_dict(request.args_json)
            LOGGER.info(
                "grpc_query_request request_id=%s query=%s camera_id=%s args_json=%s",
                request.request_id,
                request.query,
                request.camera_id,
                request.args_json,
            )
            result = self.router.query(request.query, request.camera_id or "cam0", args, source="grpc")
        except CommandError as exc:
            if exc.code == "AUTH_FAILED":
                context.set_code(grpc.StatusCode.UNAUTHENTICATED)
                context.set_details(exc.message)
            result = {"success": False, "code": exc.code, "message": exc.message, "payload": {}, "source": "grpc"}
            LOGGER.warning("grpc_query_failed request_id=%s code=%s message=%s", request.request_id, exc.code, exc.message)
        payload_json = json.dumps(result["payload"], ensure_ascii=False)
        response_frame = build_query_frame(request.request_id, request.query, result["code"], payload_json)
        LOGGER.info(
            "grpc_query_response request_id=%s success=%s code=%s message=%s",
            request.request_id,
            result["success"],
            result["code"],
            result["message"],
        )
        return pb2.QueryReply(
            success=result["success"],
            request_id=request.request_id,
            result_code=RESULT_CODES[result["code"]],
            message=result["message"],
            response_frame=response_frame,
            payload_json=payload_json,
            server_time=current_millis(),
        )

    def SendMessage(self, request, context):
        LOGGER.info("grpc_send_message_request sender=%s message=%s", request.sender, request.message)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Use ExecuteCameraCommand instead")
        return pb2.MessageResponse()

    def SubscribeMessages(self, request, context):
        LOGGER.info("grpc_subscribe_messages_request client_id=%s", request.client_id)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.MessageResponse()

    def SubscribeUpdates(self, request, context):
        LOGGER.info("grpc_subscribe_updates_request client_id=%s", request.client_id)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.MessageResponse()

    def SendImage(self, request, context):
        LOGGER.info("grpc_send_image_request file_name=%s width=%s height=%s", request.file_name, request.width, request.height)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Not implemented in SuperEagleEye")
        yield pb2.ImageResponse()

    def CommandMessage(self, request, context):
        LOGGER.info("grpc_command_message_request sender=%s message=%s", request.sender, request.message)
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Use ExecuteCameraCommand instead")
        yield pb2.MsgResponse()
