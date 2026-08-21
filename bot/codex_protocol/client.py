"""Typed public façade for the Codex RPC connection owner."""

from __future__ import annotations

import pathlib
from typing import Any, Callable

from bot.codex_protocol.connection import (
    AppServerEndpointMode,
    CodexRpcConnection,
    CodexRpcConnectionGenerationMismatchError,
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.codex_protocol.outbound_transport import (
    OutboundTransportGuard as _OutboundTransportGuard,
)
from bot.codex_protocol.server_request_authority import (
    ServerRequestAuthorityRotationReceipt,
)
from bot.codex_protocol.stop_barrier import (
    DEFAULT_STOP_TIMEOUT_SECONDS as _DEFAULT_STOP_TIMEOUT_SECONDS,
    CodexRpcStopError,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore

__all__ = [
    "AppServerEndpointMode",
    "CodexRpcClient",
    "CodexRpcConnectionGenerationMismatchError",
    "CodexRpcError",
    "CodexRpcPreSendError",
    "CodexRpcProtocolError",
    "CodexRpcStopError",
    "CodexRpcTransportError",
]


class CodexRpcClient:
    """Expose typed RPC commands without owning connection facts."""

    def __init__(
        self,
        *,
        codex_command: str = "codex",
        endpoint_mode: AppServerEndpointMode = AppServerEndpointMode.OWNED_PROCESS,
        app_server_url: str = "ws://127.0.0.1:8765",
        connect_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 30.0,
        on_notification: Callable[[int, str, dict[str, Any]], None] | None = None,
        on_request: Callable[[int, int | str, str, dict[str, Any]], None]
        | None = None,
        on_disconnect_ingress: Callable[[int], bool] | None = None,
        on_disconnect: Callable[[int], None] | None = None,
        on_initialized: Callable[[int, dict[str, Any]], None] | None = None,
        app_server_runtime_store: AppServerRuntimeStore | None = None,
        managed_startup_lock_path: pathlib.Path | str | None = None,
        app_server_data_dir: pathlib.Path | str | None = None,
    ) -> None:
        self._connection = CodexRpcConnection(
            codex_command=codex_command,
            endpoint_mode=endpoint_mode,
            app_server_url=app_server_url,
            connect_timeout_seconds=connect_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            on_notification=on_notification,
            on_request=on_request,
            on_disconnect_ingress=on_disconnect_ingress,
            on_disconnect=on_disconnect,
            on_initialized=on_initialized,
            app_server_runtime_store=app_server_runtime_store,
            managed_startup_lock_path=managed_startup_lock_path,
            app_server_data_dir=app_server_data_dir,
        )

    def require_owned_backend_lifecycle(self) -> None:
        self._connection.require_owned_backend_lifecycle()

    def start(
        self,
        *,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
        outbound_guard_method: str = "connection/start",
    ) -> None:
        self._connection.start(
            outbound_transport_guard=outbound_transport_guard,
            outbound_guard_method=outbound_guard_method,
        )

    def stop(self, *, timeout: float = _DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        self._connection.stop(timeout=timeout)

    def rotate_server_request_authority_after_backend_stop(
        self,
    ) -> ServerRequestAuthorityRotationReceipt:
        return self._connection.rotate_server_request_authority_after_backend_stop()

    def current_app_server_url(self) -> str:
        return self._connection.current_app_server_url()

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        return self._connection.connection_generation(
            timeout=timeout,
            require_existing_connection=require_existing_connection,
        )

    def fence_backend_reset_generation(
        self,
        *,
        expected_connection_generation: int,
        fence_ingress: Callable[[], None],
        timeout: float | None = None,
    ) -> None:
        self._connection.fence_backend_reset_generation(
            expected_connection_generation=expected_connection_generation,
            fence_ingress=fence_ingress,
            timeout=timeout,
        )

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
    ) -> Any:
        return self._connection.request(
            method,
            params,
            timeout=timeout,
            require_existing_connection=require_existing_connection,
            expected_connection_generation=expected_connection_generation,
            outbound_transport_guard=outbound_transport_guard,
        )

    def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
    ) -> None:
        self._connection.notify(
            method,
            params,
            timeout=timeout,
            outbound_transport_guard=outbound_transport_guard,
        )

    def respond(
        self,
        request_id: int | str,
        *,
        result: dict | None = None,
        error: dict | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
    ) -> None:
        self._connection.respond(
            request_id,
            result=result,
            error=error,
            timeout=timeout,
            require_existing_connection=require_existing_connection,
            expected_connection_generation=expected_connection_generation,
            outbound_transport_guard=outbound_transport_guard,
        )
