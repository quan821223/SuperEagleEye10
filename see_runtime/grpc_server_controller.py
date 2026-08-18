"""Owns the local gRPC server lifecycle (start/stop/rebind port). See
`doc/grpc-service.md`.
"""

import logging
import threading
import time
from typing import Dict

from see_runtime.bootstrap import futures, grpc, pb2_grpc
from see_runtime.command_router import CommandRouter
from see_runtime.errors import CommandError
from see_runtime.grpc_service import See10Service

LOGGER = logging.getLogger("SuperEagleEye")


class GrpcServerController:
    def __init__(self, router: CommandRouter, initial_port: int):
        self.router = router
        self.port = initial_port
        self.server = None
        self.lock = threading.RLock()

    def start(self) -> None:
        with self.lock:
            if self.server is not None:
                return
            self.server = self._create_server(self.port)
            LOGGER.info("grpc_server_listening port=%s", self.port, extra={"console": True})

    def stop(self) -> None:
        with self.lock:
            if self.server is None:
                return
            self.server.stop(0)
            self.server = None

    def set_port(self, port: int, defer_restart: bool = False) -> Dict[str, object]:
        with self.lock:
            previous_port = self.port
            if previous_port == port:
                return {
                    "grpc_port": self.port,
                    "previous_grpc_port": previous_port,
                    "restarted": False,
                    "message": f"gRPC port is already {self.port}",
                }
            self.port = port

        if defer_restart:
            threading.Thread(target=self._restart_after_reply, daemon=True).start()
            return {
                "grpc_port": port,
                "previous_grpc_port": previous_port,
                "restarted": False,
                "restart_deferred": True,
                "message": f"gRPC listener will restart on port {port}",
            }

        self.restart()
        return {
            "grpc_port": port,
            "previous_grpc_port": previous_port,
            "restarted": True,
            "message": f"gRPC listener restarted on port {port}",
        }

    def restart(self) -> None:
        with self.lock:
            if self.server is not None:
                self.server.stop(0)
                self.server = None
            self.server = self._create_server(self.port)
            LOGGER.info("grpc_server_listening port=%s", self.port, extra={"console": True})

    def _restart_after_reply(self) -> None:
        time.sleep(0.2)
        self.restart()

    def _create_server(self, port: int):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        pb2_grpc.add_SC_communication_gRPCServicer_to_server(See10Service(self.router), server)
        bound_port = server.add_insecure_port(f"127.0.0.1:{port}")
        if bound_port == 0:
            raise CommandError("INTERNAL_ERROR", f"Failed to bind gRPC port {port}")
        server.start()
        return server
