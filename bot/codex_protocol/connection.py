"""Codex app-server websocket connection and JSON-RPC state owner."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from bot.codex_protocol.deadline import (
    held_lock_before_deadline,
    remaining_before_deadline,
)
from bot.codex_protocol.managed_process import (
    ManagedAppServerProcess,
)
from bot.codex_protocol.outbound_transport import (
    OutboundTransportGuard as _OutboundTransportGuard,
    OutboundTransportGuardRejectedError as _CodexOutboundTransportGuardRejectedError,
    held_outbound_transport_guard,
)
from bot.codex_protocol.server_request_authority import (
    ServerRequestAuthorityError,
    ServerRequestAuthorityRotationReceipt,
    ServerRequestAuthorityRegistry,
)
from bot.codex_protocol.stop_barrier import (
    DEFAULT_STOP_TIMEOUT_SECONDS as _DEFAULT_STOP_TIMEOUT_SECONDS,
    CodexRpcStopBarrier,
    CodexRpcStopError,
    RpcStopResourceTransfer,
)
from bot.interaction_contract import automatic_server_request_response
from bot.local_websocket_auth import (
    AppServerWebsocketAuthTokenStore,
    MissingAppServerWebsocketAuthTokenError,
    build_bearer_authorization_headers,
)
from bot.network_contract import (
    parse_app_server_endpoint,
    parse_owned_app_server_listen_endpoint,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.version import __version__

logger = logging.getLogger(__name__)
_CONNECTION_DISCONNECTED = "disconnected"
_CONNECTION_HANDSHAKING = "handshaking"
_CONNECTION_READY = "ready"
_HANDSHAKE_REQUEST_METHODS = frozenset({"initialize", "configRequirements/read"})
_HANDSHAKE_NOTIFICATION_METHODS = frozenset({"initialized"})
_MAX_HANDSHAKE_BUFFERED_NOTIFICATIONS = 128


class AppServerEndpointMode(StrEnum):
    """How this client obtains the app-server endpoint it talks to.

    ``OWNED_PROCESS`` means the client starts, verifies, and stops the
    app-server child process. ``ATTACHED_ENDPOINT`` means another owner has
    already published the endpoint, so this client owns only its websocket.

    This is intentionally separate from the public deployment-mode vocabulary
    and from upstream Codex's ``--remote`` CLI flag.
    """

    OWNED_PROCESS = "owned_process"
    ATTACHED_ENDPOINT = "attached_endpoint"


class CodexRpcError(RuntimeError):
    """Codex JSON-RPC 请求失败。"""

    def __init__(self, method: str, error: dict[str, Any]):
        self.method = method
        self.error = error
        message = error.get("message") or f"{method} failed"
        super().__init__(message)


class CodexRpcTransportError(CodexRpcError):
    """Codex request outcome is unknown because the transport was lost."""


class CodexRpcProtocolError(RuntimeError):
    """Codex returned a response that does not satisfy the expected contract."""

    def __init__(self, method: str, message: str):
        self.method = method
        super().__init__(message)


class CodexRpcPreSendError(RuntimeError):
    """The target Codex request was not sent because a local precondition failed."""

    def __init__(self, method: str, cause: Exception):
        self.method = method
        self.cause = cause
        super().__init__(f"Codex request was not sent because a local pre-send condition failed: {cause}")


class CodexRpcConnectionGenerationMismatchError(CodexRpcPreSendError):
    """A fenced request was never sent on a different websocket generation.

    A generation belongs to one established websocket, not merely to the
    configured app-server URL.  This is deliberately a pre-send outcome: a
    caller that read authority from generation ``N`` must not let a reconnect
    turn that stale evidence into a mutation on generation ``N + 1``.
    """

    def __init__(
        self,
        method: str,
        *,
        expected_generation: int,
        observed_generation: int | None,
    ) -> None:
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        super().__init__(
            method,
            RuntimeError(
                "Codex websocket generation changed before request dispatch "
                f"(expected={expected_generation}, observed={observed_generation})"
            ),
        )


class _CodexWebsocketNotConnectedError(RuntimeError):
    pass


class _CodexRpcPreSendTimeoutError(TimeoutError):
    """A bounded request expired before its websocket write began."""


class _CodexConnectionGenerationMismatchError(RuntimeError):
    """Private send-path signal converted to a method-aware public error."""

    def __init__(self, *, expected_generation: int, observed_generation: int | None) -> None:
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        super().__init__(
            "Codex websocket generation changed before request dispatch "
            f"(expected={expected_generation}, observed={observed_generation})"
        )


@dataclass
class _PendingResponse:
    event: threading.Event
    result: Any = None
    error: dict[str, Any] | None = None
    protocol_error: str | None = None
    transport_error: bool = False


class CodexRpcConnection:
    """Own one Codex app-server websocket connection state machine."""

    def __init__(
        self,
        *,
        codex_command: str = "codex",
        endpoint_mode: AppServerEndpointMode = AppServerEndpointMode.OWNED_PROCESS,
        app_server_url: str = "ws://127.0.0.1:8765",
        connect_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 30.0,
        on_notification: Callable[[int, str, dict[str, Any]], None] | None = None,
        on_request: Callable[[int, int | str, str, dict[str, Any]], None] | None = None,
        on_disconnect_ingress: Callable[[int], bool] | None = None,
        on_disconnect: Callable[[int], None] | None = None,
        on_initialized: Callable[[int, dict[str, Any]], None] | None = None,
        app_server_runtime_store: AppServerRuntimeStore | None = None,
        managed_startup_lock_path: pathlib.Path | str | None = None,
        app_server_data_dir: pathlib.Path | str | None = None,
    ) -> None:
        self._codex_command = codex_command
        self._endpoint_mode = AppServerEndpointMode(endpoint_mode)
        if self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS:
            endpoint = parse_owned_app_server_listen_endpoint(app_server_url)
        else:
            endpoint = parse_app_server_endpoint(app_server_url)
        self._configured_app_server_url = endpoint.url
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._on_notification = on_notification or (lambda _connection_generation, _method, _params: None)
        # Focus's callback only enqueues work on RuntimeLoop. Calling it in
        # reader order preserves request/lifecycle ordering without blocking
        # on surface I/O or inventing a durable write-ahead transaction.
        self._on_request = on_request or (
            lambda _connection_generation, _request_id, _method, _params: None
        )
        # Synchronous hook: fence only gate-owned state; cleanup runs later.
        self._on_disconnect_ingress = on_disconnect_ingress or (
            lambda _connection_generation: True
        )
        self._on_disconnect = on_disconnect or (lambda _connection_generation: None)
        self._on_initialized = on_initialized or (
            lambda _connection_generation, _result: None
        )
        app_server_data_dir_path = (
            pathlib.Path(app_server_data_dir)
            if app_server_data_dir is not None
            else None
        )
        self._app_server_ws_auth_store = (
            AppServerWebsocketAuthTokenStore(app_server_data_dir_path)
            if app_server_data_dir_path is not None
            else None
        )
        self._managed_process = (
            ManagedAppServerProcess(
                codex_command=codex_command,
                configured_url=endpoint.url,
                runtime_store=app_server_runtime_store,
                startup_lock_path=managed_startup_lock_path,
                websocket_auth_store=self._app_server_ws_auth_store,
            )
            if self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS
            else None
        )

        self._lock = threading.RLock()
        self._connection_condition = threading.Condition(self._lock)
        self._send_lock = threading.Lock()
        self._stop_barrier = CodexRpcStopBarrier(
            identity_lock=self._lock,
            condition=self._connection_condition,
        )
        self._pending: dict[int, _PendingResponse] = {}
        self._next_id = 1
        self._server_request_authority = ServerRequestAuthorityRegistry()

        self._ws = None
        self._reader_thread: threading.Thread | None = None
        # Keep every producer handle until a stop barrier has observed it as
        # terminated.  A single "current" pointer is insufficient after an
        # unexpected disconnect because reconnect may replace that pointer
        # while the old reader is still running its disconnect callback.
        self._reader_threads: set[threading.Thread] = set()
        self._callback_threads: set[threading.Thread] = set()
        self._closing = False
        self._initialize_result: dict[str, Any] | None = None
        # Every established websocket gets a monotonically increasing identity.
        # Server requests are dispatched on a separate thread, so the callback
        # must retain the identity of the websocket that received it rather than
        # infer it from whichever websocket happens to be current later.
        self._connection_generation = 0
        # Ordinary callers require the exact connection to finish its owned
        # initialize/requirements handshake and become READY.
        self._connection_state = _CONNECTION_DISCONNECTED
        self._handshake_owner_thread_id: int | None = None
        self._handshake_generation = 0
        self._handshake_outbound_transport_guard: _OutboundTransportGuard | None = None
        self._handshake_attempt = 0
        self._last_handshake_failure: tuple[int, Exception] | None = None
        self._handshake_ingress_failure: CodexRpcProtocolError | None = None
        self._handshake_notifications: list[tuple[int, dict[str, Any]]] = []

    def require_owned_backend_lifecycle(self) -> None:
        """Reject reset when this client owns only its websocket."""

        if self._endpoint_mode is not AppServerEndpointMode.OWNED_PROCESS:
            raise RuntimeError(
                "backend reset requires an owned app-server lifecycle; "
                "an attached endpoint owns only its client connection"
            )

    def start(
        self,
        *,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
        outbound_guard_method: str = "connection/start",
    ) -> None:
        """Connect and return only after the complete protocol gate is READY."""

        self._stop_barrier.raise_if_stop_requested()
        caller_thread_id = threading.get_ident()
        with self._connection_condition:
            self._stop_barrier.wait_until_startable_locked()
            if self._is_ready_locked():
                return
            if self._connection_state == _CONNECTION_HANDSHAKING:
                if self._handshake_owner_thread_id == caller_thread_id:
                    raise RuntimeError(
                        "Codex handshake may only re-enter through its reviewed internal methods"
                    )
                observed_attempt = self._handshake_attempt
                while (
                    self._connection_state == _CONNECTION_HANDSHAKING
                    and self._handshake_attempt == observed_attempt
                    and not (
                        self._last_handshake_failure is not None
                        and self._last_handshake_failure[0] == observed_attempt
                    )
                ):
                    self._connection_condition.wait()
                if self._is_ready_locked():
                    return
                failure = self._last_handshake_failure
                if failure is not None and failure[0] == observed_attempt:
                    raise failure[1]
                raise _CodexWebsocketNotConnectedError(
                    "Codex websocket stopped before its handshake became ready"
                )

            self._handshake_attempt += 1
            handshake_attempt = self._handshake_attempt
            self._connection_state = _CONNECTION_HANDSHAKING
            self._handshake_owner_thread_id = caller_thread_id
            self._handshake_generation = 0
            self._handshake_outbound_transport_guard = outbound_transport_guard
            self._last_handshake_failure = None
            self._handshake_ingress_failure = None
            self._handshake_notifications.clear()
            startup_failure: Exception | None = None
            try:
                with held_outbound_transport_guard(
                    outbound_transport_guard,
                ):
                    self._start_locked()
            except _CodexOutboundTransportGuardRejectedError as exc:
                converted = CodexRpcPreSendError(
                    outbound_guard_method,
                    exc.cause,
                )
                self._handshake_generation = (
                    self._connection_generation if self._ws is not None else 0
                )
                self._last_handshake_failure = (handshake_attempt, converted)
                self._connection_condition.notify_all()
                startup_failure = converted
            except Exception as exc:
                # Preserve ownership until the transport/process created by a
                # partially completed _start_locked() has been detached and
                # closed outside the identity lock.
                self._handshake_generation = (
                    self._connection_generation if self._ws is not None else 0
                )
                self._last_handshake_failure = (handshake_attempt, exc)
                self._connection_condition.notify_all()
                startup_failure = exc
            handshake_generation = self._handshake_generation
            if startup_failure is None:
                handshake_generation = self._connection_generation
                self._handshake_generation = handshake_generation

        if startup_failure is not None:
            self._stop_connection(
                handshake_attempt=handshake_attempt,
                handshake_generation=handshake_generation,
                handshake_failure=startup_failure,
            )
            raise startup_failure

        try:
            initialize_kwargs: dict[str, Any] = {
                "timeout": self._connect_timeout_seconds,
            }
            if outbound_transport_guard is not None:
                initialize_kwargs["outbound_transport_guard"] = (
                    outbound_transport_guard
                )
            initialize_result = self.request(
                "initialize",
                {
                    "clientInfo": {"name": "focus", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
                **initialize_kwargs,
            )
            if not isinstance(initialize_result, dict):
                raise CodexRpcProtocolError(
                    "initialize",
                    "Codex initialize response must be an object",
                )
            # app-server's connection handshake is two-phase.  The response
            # alone does not replace the required client notification, even
            # though older servers happened to accept subsequent requests.
            notification_kwargs: dict[str, Any] = {
                "timeout": self._connect_timeout_seconds,
            }
            if outbound_transport_guard is not None:
                notification_kwargs["outbound_transport_guard"] = (
                    outbound_transport_guard
                )
            self.notify("initialized", **notification_kwargs)
            # Adapter-owned requirements validation is the final handshake
            # phase.  Its exact configRequirements/read call receives the same
            # narrow owner-thread bypass as initialize; no other method does.
            self._on_initialized(
                handshake_generation,
                dict(initialize_result),
            )
            buffered_notifications: list[tuple[int, dict[str, Any]]] = []
            with self._connection_condition:
                if (
                    self._connection_state != _CONNECTION_HANDSHAKING
                    or self._handshake_owner_thread_id != caller_thread_id
                    or self._handshake_attempt != handshake_attempt
                    or self._handshake_generation != handshake_generation
                    or self._handshake_ingress_failure is not None
                    or not self._is_connected_locked()
                ):
                    raise _CodexWebsocketNotConnectedError(
                        "Codex websocket changed before its handshake became ready"
                    )
                self._initialize_result = dict(initialize_result)
                self._connection_state = _CONNECTION_READY
                self._handshake_owner_thread_id = None
                self._handshake_generation = 0
                self._handshake_outbound_transport_guard = None
                buffered_notifications = list(self._handshake_notifications)
                self._handshake_notifications.clear()
                self._connection_condition.notify_all()
            for notification_generation, notification in buffered_notifications:
                self._dispatch_payload(
                    notification,
                    connection_generation=notification_generation,
                )
        except Exception as exc:
            self._stop_connection(
                handshake_attempt=handshake_attempt,
                handshake_generation=handshake_generation,
                handshake_failure=exc,
            )
            raise

    def stop(self, *, timeout: float = _DEFAULT_STOP_TIMEOUT_SECONDS) -> None:
        """Close every owned producer; failed cleanup remains retryable."""

        self._stop_connection(timeout=timeout)

    def rotate_server_request_authority_after_backend_stop(
        self,
    ) -> ServerRequestAuthorityRotationReceipt:
        """Retire response claims only behind a completed stop barrier."""

        with self._lock:
            stopped = bool(
                self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS
                and self._ws is None
                and self._managed_process is not None
                and not self._managed_process.has_active_resources()
                and self._stop_barrier.is_clear
                and self._connection_state == _CONNECTION_DISCONNECTED
            )
        if not stopped:
            raise RuntimeError("server-request authority rotation requires stopped backend")
        return self._server_request_authority.rotate_after_backend_stop()

    def _stop_connection(
        self,
        *,
        handshake_attempt: int | None = None,
        handshake_generation: int | None = None,
        handshake_failure: Exception | None = None,
        timeout: float = _DEFAULT_STOP_TIMEOUT_SECONDS,
    ) -> None:
        """Close transport state and establish a callback/child barrier."""

        deadline_monotonic = self._stop_barrier.deadline(timeout)
        if handshake_attempt is None:
            # This fence does not need the transport identity lock.  In
            # particular, a stuck generation-fenced websocket send may hold
            # that lock; start must still fail closed until stop can retry.
            self._stop_barrier.request_stop()

        with self._stop_barrier.identity_lock(
            deadline_monotonic=deadline_monotonic,
        ):
            active_attempt = self._handshake_attempt
            was_handshaking = self._connection_state == _CONNECTION_HANDSHAKING
            if handshake_attempt is not None and (
                not was_handshaking
                or handshake_attempt != active_attempt
                or handshake_generation != self._handshake_generation
            ):
                # A stopped handshake owner can finish after another caller
                # has already established a replacement connection.  Its
                # cleanup authority belongs only to the attempt and websocket
                # generation that it created; never close a successor here.
                return

            self._stop_barrier.request_stop()

            if self._stop_barrier.active:
                if handshake_attempt is not None:
                    # The external stop owner deliberately leaves a live
                    # handshake attempt owned until its starter acknowledges
                    # the failed attempt.  That acknowledgement can race the
                    # resource drain and join the same single-flight stop.  It
                    # must settle the handshake *before* waiting: returning
                    # directly from the shared attempt would otherwise leave
                    # HANDSHAKING and its owner thread id behind forever after
                    # the starter unwinds.
                    self._settle_handshake_stop_locked(
                        handshake_attempt=handshake_attempt,
                        handshake_failure=handshake_failure,
                    )
                self._stop_barrier.join_active_locked(
                    deadline_monotonic=deadline_monotonic,
                )
                return

            self._closing = True
            if self._stop_barrier.has_retained_resources:
                stop_attempt = self._stop_barrier.begin_locked(
                    RpcStopResourceTransfer()
                )
            else:
                reader_threads = set(self._reader_threads)
                if self._reader_thread is not None:
                    reader_threads.add(self._reader_thread)
                stop_attempt = self._stop_barrier.begin_locked(
                    RpcStopResourceTransfer(
                        websocket=self._ws,
                        reader_threads=tuple(reader_threads),
                        callback_threads=tuple(self._callback_threads),
                        managed_process=(
                            self._managed_process
                            if self._managed_process is not None
                            and self._managed_process.has_active_resources()
                            else None
                        ),
                    )
                )
                self._ws = None
                self._reader_thread = None
                self._reader_threads.clear()
                self._callback_threads.clear()
            self._initialize_result = None
            self._handshake_ingress_failure = None
            self._handshake_notifications.clear()
            if (
                handshake_attempt is not None
                and handshake_attempt == active_attempt
                and handshake_failure is not None
            ):
                self._last_handshake_failure = (
                    handshake_attempt,
                    handshake_failure,
                )
            elif was_handshaking:
                self._last_handshake_failure = (
                    active_attempt,
                    _CodexWebsocketNotConnectedError(
                        "Codex websocket stopped during protocol handshake"
                    ),
                )
            if was_handshaking and handshake_attempt is None:
                # Wake current waiters immediately, but keep the failed
                # attempt owned until its owner unwinds.  Opening a new
                # attempt here would let the stale owner close that successor
                # when it handles its pending initialize/config error.
                self._closing = True
            else:
                self._connection_state = _CONNECTION_DISCONNECTED
                self._handshake_owner_thread_id = None
                self._handshake_generation = 0
                self._handshake_outbound_transport_guard = None
            pending_to_fail = self._take_pending_locked()
            self._connection_condition.notify_all()

        error = {"code": -32000, "message": "Codex app-server closed"}
        for pending in pending_to_fail:
            pending.error = error
            pending.transport_error = True
            pending.event.set()

        self._stop_barrier.drain_attempt(
            stop_attempt,
            deadline_monotonic=deadline_monotonic,
        )

    def _settle_handshake_stop_locked(
        self,
        *,
        handshake_attempt: int,
        handshake_failure: Exception | None,
    ) -> None:
        """Release one stopped handshake owner without touching its resources.

        The caller has already validated the attempt and generation while
        holding ``_lock``.  A concurrent stop owns the detached websocket,
        process, and producer handles; this method only closes the handshake
        state machine so the stale starter can never block or close a later
        generation.
        """

        if handshake_failure is not None:
            self._last_handshake_failure = (
                handshake_attempt,
                handshake_failure,
            )
        self._connection_state = _CONNECTION_DISCONNECTED
        self._handshake_owner_thread_id = None
        self._handshake_generation = 0
        self._handshake_outbound_transport_guard = None
        self._connection_condition.notify_all()

    def current_app_server_url(self) -> str:
        with self._lock:
            return (
                self._connection_target_url()
                if not self._closing and self._is_ready_locked()
                else ""
            )

    def _connection_target_url(self) -> str:
        if self._managed_process is not None:
            return self._managed_process.active_url
        return self._configured_app_server_url

    @staticmethod
    def _deadline_from_timeout(timeout: float | None) -> float | None:
        """Make one monotonic deadline for a caller-supplied total budget."""

        if timeout is None:
            return None
        return time.monotonic() + max(float(timeout), 0.0)

    def _assert_existing_connection(
        self,
        method: str,
        *,
        deadline_monotonic: float | None,
        expected_connection_generation: int | None = None,
        internal_handshake_generation: int | None = None,
    ) -> int:
        try:
            with held_lock_before_deadline(
                self._lock,
                deadline_monotonic=deadline_monotonic,
                operation=f"{method} connection check",
            ):
                observed_generation = self._connection_generation
                if internal_handshake_generation is None:
                    connected = self._is_ready_locked()
                else:
                    connected = bool(
                        self._connection_state == _CONNECTION_HANDSHAKING
                        and self._handshake_owner_thread_id == threading.get_ident()
                        and self._handshake_generation == internal_handshake_generation
                        and observed_generation == internal_handshake_generation
                        and self._is_connected_locked()
                    )
        except TimeoutError as exc:
            raise CodexRpcPreSendError(method, exc) from exc
        if not connected:
            raise CodexRpcPreSendError(
                method,
                _CodexWebsocketNotConnectedError("Codex websocket is not connected"),
            )
        if (
            expected_connection_generation is not None
            and observed_generation != expected_connection_generation
        ):
            raise CodexRpcConnectionGenerationMismatchError(
                method,
                expected_generation=expected_connection_generation,
                observed_generation=observed_generation,
            )
        return observed_generation

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        """Return the identity of the current websocket connection."""

        deadline = self._deadline_from_timeout(timeout)
        try:
            with held_lock_before_deadline(
                self._lock,
                deadline_monotonic=deadline,
                operation="connection generation read",
            ):
                if require_existing_connection and not self._is_connected_locked():
                    raise CodexRpcPreSendError(
                        "connection/generation",
                        _CodexWebsocketNotConnectedError("Codex websocket is not connected"),
                    )
                if require_existing_connection and not self._is_ready_locked():
                    raise CodexRpcPreSendError(
                        "connection/generation",
                        _CodexWebsocketNotConnectedError(
                            "Codex websocket handshake is not ready"
                        ),
                    )
                return self._connection_generation
        except TimeoutError:
            # A generation learned after its caller deadline would be unsafe
            # evidence for a relation capability cache.  Preserve timeout as
            # an unknown upstream state rather than silently using it.
            raise

    def fence_backend_reset_generation(
        self,
        *,
        expected_connection_generation: int,
        fence_ingress: Callable[[], None],
        timeout: float | None = None,
    ) -> None:
        """Compare physical identity, then fence adapter ingress in lock order.

        Reset recovery does not require the socket to remain READY: an already
        disconnected websocket still has a useful generation identity. The
        callback must only acquire the adapter-ingress gate, preserving the
        sole connection-identity -> ingress-gate order.
        """

        if (
            type(expected_connection_generation) is not int
            or expected_connection_generation <= 0
        ):
            raise ValueError(
                "backend reset expected connection generation must be a positive integer"
            )
        if not callable(fence_ingress):
            raise TypeError("backend reset ingress fence must be callable")
        deadline = self._deadline_from_timeout(timeout)
        callback_started = False
        try:
            with held_lock_before_deadline(
                self._lock,
                deadline_monotonic=deadline,
                operation="backend reset generation fence",
            ):
                observed_generation = self._connection_generation
                if observed_generation != expected_connection_generation:
                    raise CodexRpcConnectionGenerationMismatchError(
                        "backend/reset-fence",
                        expected_generation=expected_connection_generation,
                        observed_generation=observed_generation,
                    )
                callback_started = True
                fence_ingress()
        except TimeoutError as exc:
            if callback_started:
                raise
            raise CodexRpcPreSendError("backend/reset-fence", exc) from exc

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
        """Send a JSON-RPC request, optionally pinned to an existing socket."""

        deadline = self._deadline_from_timeout(timeout)
        try:
            handshake_generation, inherited_transport_guard = self._handshake_access(
                method,
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            raise CodexRpcPreSendError(method, exc) from exc
        if handshake_generation is not None:
            require_existing_connection = True
            expected_connection_generation = handshake_generation
            if outbound_transport_guard is None:
                outbound_transport_guard = inherited_transport_guard
        if expected_connection_generation is not None:
            if (
                isinstance(expected_connection_generation, bool)
                or not isinstance(expected_connection_generation, int)
                or expected_connection_generation <= 0
            ):
                raise ValueError("expected_connection_generation must be a positive integer")
            if not require_existing_connection:
                raise ValueError(
                    "expected_connection_generation requires require_existing_connection=True"
                )

        if require_existing_connection:
            send_generation = self._assert_existing_connection(
                method,
                deadline_monotonic=deadline,
                expected_connection_generation=expected_connection_generation,
                internal_handshake_generation=handshake_generation,
            )
        else:
            try:
                if outbound_transport_guard is None:
                    self.start()
                else:
                    self.start(
                        outbound_transport_guard=outbound_transport_guard,
                        outbound_guard_method=method,
                    )
            except CodexRpcPreSendError:
                raise
            except Exception as exc:
                raise CodexRpcPreSendError(method, exc) from exc
            # Pin every ordinary request to the READY websocket admitted by
            # start().  Without this second check, stop/reconnect could replace
            # it with a still-HANDSHAKING socket before the actual send.
            send_generation = self._assert_existing_connection(
                method,
                deadline_monotonic=deadline,
            )
        try:
            request_id, pending = self._register_pending(
                deadline_monotonic=deadline,
            )
        except TimeoutError as exc:
            # The id was not registered and therefore no JSON-RPC write could
            # have happened. The caller may safely decide whether to retry.
            raise CodexRpcPreSendError(method, exc) from exc
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        # JSON-RPC permits params to be omitted.  Preserve that distinction:
        # app-server methods whose protocol type is ``Option<()>`` reject an
        # empty object even though ordinary object-parameter methods accept it.
        if params is not None:
            payload["params"] = params
        try:
            serialized_payload = json.dumps(payload, ensure_ascii=False)
        except Exception:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise
        if method in ("thread/start", "turn/start", "thread/resume"):
            logger.debug("rpc request: %s payload=%s", method, serialized_payload)
        try:
            self._send_serialized_json(
                serialized_payload,
                deadline_monotonic=deadline,
                expected_connection_generation=send_generation,
                outbound_transport_guard=outbound_transport_guard,
            )
        except _CodexOutboundTransportGuardRejectedError as exc:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise CodexRpcPreSendError(method, exc.cause) from exc
        except _CodexConnectionGenerationMismatchError as exc:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise CodexRpcConnectionGenerationMismatchError(
                method,
                expected_generation=exc.expected_generation,
                observed_generation=exc.observed_generation,
            ) from exc
        except _CodexWebsocketNotConnectedError as exc:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise CodexRpcPreSendError(method, exc) from exc
        except _CodexRpcPreSendTimeoutError as exc:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise CodexRpcPreSendError(method, exc) from exc
        except Exception as exc:
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise CodexRpcTransportError(
                method,
                {
                    "code": -32000,
                    "message": f"Codex websocket send failed: {exc}",
                },
            ) from exc

        try:
            wait_seconds = (
                remaining_before_deadline(
                    deadline,
                    operation=f"{method} response wait",
                )
                if deadline is not None
                else self._request_timeout_seconds
            )
        except TimeoutError:
            # The request was already written.  Do not turn a local cleanup
            # race into an unbounded wait; retaining an occasional stale
            # pending entry is safer than pretending that delivery failed.
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise
        if not pending.event.wait(wait_seconds):
            self._discard_pending_best_effort(
                request_id,
                deadline_monotonic=deadline,
            )
            raise TimeoutError(f"Codex request timed out: {method}")
        if pending.protocol_error is not None:
            raise CodexRpcProtocolError(method, pending.protocol_error)
        if pending.error is not None:
            error_type = CodexRpcTransportError if pending.transport_error else CodexRpcError
            raise error_type(method, pending.error)
        if method in ("thread/start", "turn/start", "thread/resume"):
            logger.debug("rpc result: %s keys=%s", method, sorted((pending.result or {}).keys()))
        return pending.result

    def notify(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
    ) -> None:
        """Send one client JSON-RPC notification on the established socket."""

        normalized_method = str(method or "").strip()
        if not normalized_method:
            raise ValueError("JSON-RPC notification method must not be empty")
        deadline = self._deadline_from_timeout(timeout)
        handshake_generation, inherited_transport_guard = self._handshake_access(
            normalized_method,
            notification=True,
            deadline_monotonic=deadline,
        )
        if handshake_generation is not None and outbound_transport_guard is None:
            outbound_transport_guard = inherited_transport_guard
        send_generation = self._assert_existing_connection(
            normalized_method,
            deadline_monotonic=deadline,
            expected_connection_generation=handshake_generation,
            internal_handshake_generation=handshake_generation,
        )
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": normalized_method,
        }
        if params is not None:
            payload["params"] = params
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        try:
            self._send_serialized_json(
                serialized_payload,
                deadline_monotonic=deadline,
                expected_connection_generation=send_generation,
                outbound_transport_guard=outbound_transport_guard,
            )
        except _CodexOutboundTransportGuardRejectedError as exc:
            raise CodexRpcPreSendError(normalized_method, exc.cause) from exc
        except _CodexWebsocketNotConnectedError as exc:
            raise CodexRpcPreSendError(normalized_method, exc) from exc
        except _CodexRpcPreSendTimeoutError as exc:
            raise CodexRpcPreSendError(normalized_method, exc) from exc
        except Exception as exc:
            raise CodexRpcTransportError(
                normalized_method,
                {
                    "code": -32000,
                    "message": f"Codex websocket notification send failed: {exc}",
                },
            ) from exc

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
        """响应服务端发来的 JSON-RPC request。"""
        deadline = self._deadline_from_timeout(timeout)
        try:
            claimed_authority = self._server_request_authority.claim(
                request_id,
                connection_generation=expected_connection_generation,
                deadline_monotonic=deadline,
            )
        except (TimeoutError, ServerRequestAuthorityError) as exc:
            raise CodexRpcPreSendError("serverRequest/response", exc) from exc
        expected_connection_generation = claimed_authority[1]
        try:
            send_generation = self._assert_existing_connection(
                "serverRequest/response",
                deadline_monotonic=deadline,
                expected_connection_generation=expected_connection_generation,
            )
        except Exception:
            self._server_request_authority.release(claimed_authority)
            raise
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        try:
            serialized_payload = json.dumps(payload, ensure_ascii=False)
        except Exception:
            self._server_request_authority.release(claimed_authority)
            raise
        try:
            self._send_serialized_json(
                serialized_payload,
                deadline_monotonic=deadline,
                expected_connection_generation=send_generation,
                outbound_transport_guard=outbound_transport_guard,
            )
        except _CodexOutboundTransportGuardRejectedError as exc:
            self._server_request_authority.release(claimed_authority)
            raise CodexRpcPreSendError(
                "serverRequest/response",
                exc.cause,
            ) from exc
        except _CodexConnectionGenerationMismatchError as exc:
            self._server_request_authority.release(claimed_authority)
            raise CodexRpcConnectionGenerationMismatchError(
                "serverRequest/response",
                expected_generation=exc.expected_generation,
                observed_generation=exc.observed_generation,
            ) from exc
        except _CodexWebsocketNotConnectedError as exc:
            self._server_request_authority.release(claimed_authority)
            raise CodexRpcPreSendError("serverRequest/response", exc) from exc
        except _CodexRpcPreSendTimeoutError as exc:
            self._server_request_authority.release(claimed_authority)
            raise CodexRpcPreSendError("serverRequest/response", exc) from exc
        except Exception as exc:
            self._server_request_authority.retire(claimed_authority)
            raise CodexRpcTransportError(
                "serverRequest/response",
                {
                    "code": -32000,
                    "message": f"Codex websocket response send failed: {exc}",
                },
            ) from exc
        self._server_request_authority.retire(claimed_authority)

    def _start_locked(self) -> None:
        self._closing = False
        if self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS:
            managed_process = self._managed_process
            if managed_process is None:
                raise RuntimeError("owned app-server process owner is unavailable")
            if not managed_process.prepare_for_start():
                self._start_managed_process_locked()
            else:
                logger.info(
                    "复用已运行的 Codex app-server: %s",
                    managed_process.active_url,
                )
                self._connect_ws_locked()
                managed_process.verify_alive()
                managed_process.publish_runtime()
        else:
            self._connect_ws_locked()
        ws = self._ws
        if ws is None:
            raise RuntimeError("Codex websocket connected without a websocket instance")
        self._connection_generation += 1
        connection_generation = self._connection_generation
        reader_thread = threading.Thread(
            target=self._reader_loop,
            args=(ws, connection_generation),
            name=f"focus-codex-reader-{connection_generation}",
            daemon=True,
        )
        self._reader_thread = reader_thread
        self._reader_threads = {
            thread
            for thread in self._reader_threads
            if self._thread_is_alive(thread)
        }
        self._reader_threads.add(reader_thread)
        try:
            reader_thread.start()
        except Exception:
            self._reader_threads.discard(reader_thread)
            self._reader_thread = None
            raise

    def _start_managed_process_locked(self) -> None:
        managed_process = self._managed_process
        if managed_process is None:
            raise RuntimeError("owned app-server process owner is unavailable")
        max_attempts = managed_process.max_start_attempts()
        attempt = 0
        listen_url = managed_process.select_endpoint()
        with managed_process.startup_lock():
            while True:
                attempt += 1
                try:
                    managed_process.launch(listen_url)
                    self._connect_ws_locked()
                    managed_process.verify_alive()
                    managed_process.publish_runtime()
                    return
                except Exception as exc:
                    self._cleanup_failed_managed_start_locked()
                    if attempt >= max_attempts:
                        raise
                    listen_url = managed_process.allocate_retry_endpoint()
                    logger.warning(
                        "Codex app-server 启动失败（%s），默认地址改用备用端口重试：%s",
                        exc,
                        listen_url,
                    )

    def _cleanup_failed_managed_start_locked(self) -> None:
        ws = self._ws
        failures: list[str] = []
        websocket_closed = ws is None
        if ws is not None:
            try:
                ws.close()
            except Exception as exc:
                failures.append(f"failed-start websocket close failed: {exc}")
            else:
                websocket_closed = True
        managed_process = self._managed_process
        if managed_process is None:
            raise RuntimeError("owned app-server process owner is unavailable")
        deadline_monotonic = time.monotonic() + _DEFAULT_STOP_TIMEOUT_SECONDS
        failures.extend(managed_process.request_stop())
        managed_result = managed_process.drain_stop(
            deadline_monotonic=deadline_monotonic,
        )
        failures.extend(managed_result.failures)
        pending_resources = managed_result.pending_resources
        if not websocket_closed:
            pending_resources = ("websocket", *pending_resources)
        if failures or pending_resources:
            raise CodexRpcStopError(
                pending_resources=pending_resources,
                failures=tuple(failures),
            )
        self._ws = None

    def _connect_ws_locked(self) -> None:
        deadline = time.time() + self._connect_timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            if (
                self._managed_process is not None
                and self._managed_process.has_exited()
            ):
                raise RuntimeError("codex app-server exited before websocket connected")
            try:
                # Codex can return multi-megabyte frames for thread/read(thread.turns)
                # and thread/resume. The default websocket 1 MiB limit breaks valid
                # resume flows for longer sessions, so disable the per-frame cap here.
                connect_kwargs: dict[str, Any] = {
                    "open_timeout": self._connect_timeout_seconds,
                    "max_size": None,
                    "proxy": None,
                }
                auth_headers = self._websocket_auth_headers_for_connect()
                if auth_headers:
                    connect_kwargs["additional_headers"] = auth_headers
                self._ws = connect(
                    self._connection_target_url(),
                    **connect_kwargs,
                )
                return
            except MissingAppServerWebsocketAuthTokenError:
                raise
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"failed to connect Codex websocket: {last_error}")

    def _is_connected_locked(self) -> bool:
        if self._ws is None:
            return False
        if self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS:
            return bool(
                self._managed_process is not None
                and self._managed_process.is_running()
            )
        return True

    def _is_ready_locked(self) -> bool:
        return (
            not self._stop_barrier.stop_requested
            and self._connection_state == _CONNECTION_READY
            and self._is_connected_locked()
        )

    @staticmethod
    def _thread_is_alive(thread: threading.Thread) -> bool:
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive):
            return False
        return bool(is_alive())

    def _handshake_access(
        self,
        method: str,
        *,
        notification: bool = False,
        deadline_monotonic: float | None = None,
    ) -> tuple[int | None, _OutboundTransportGuard | None]:
        with held_lock_before_deadline(
            self._lock,
            deadline_monotonic=deadline_monotonic,
            operation=f"{method} handshake access check",
        ):
            generation = self._handshake_access_generation_locked(
                method,
                notification=notification,
            )
            inherited_guard = (
                self._handshake_outbound_transport_guard
                if generation is not None
                else None
            )
            return generation, inherited_guard

    def _handshake_access_generation_locked(
        self,
        method: str,
        *,
        notification: bool = False,
    ) -> int | None:
        allowed_methods = (
            _HANDSHAKE_NOTIFICATION_METHODS
            if notification
            else _HANDSHAKE_REQUEST_METHODS
        )
        if (
            self._connection_state == _CONNECTION_HANDSHAKING
            and self._handshake_owner_thread_id == threading.get_ident()
            and str(method or "").strip() in allowed_methods
            and self._handshake_generation > 0
            and self._is_connected_locked()
        ):
            return self._handshake_generation
        return None

    def _register_pending(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, _PendingResponse]:
        with held_lock_before_deadline(
            self._lock,
            deadline_monotonic=deadline_monotonic,
            operation="request registration",
        ):
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingResponse(event=threading.Event())
            self._pending[request_id] = pending
            return request_id, pending

    def _discard_pending_best_effort(
        self,
        request_id: int,
        *,
        deadline_monotonic: float | None,
    ) -> None:
        """Forget a timed-out request without extending that request's budget.

        A response id is never reused, so leaving an entry for the reader
        loop to consume is harmless. Do not extend a bounded caller deadline
        merely to clean up process-local bookkeeping.
        """

        if deadline_monotonic is None:
            with self._lock:
                self._pending.pop(request_id, None)
            return

        remaining = deadline_monotonic - time.monotonic()
        if remaining > 0:
            acquired = self._lock.acquire(timeout=remaining)
        else:
            acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            self._pending.pop(request_id, None)
        finally:
            self._lock.release()

    def _send_serialized_json(
        self,
        payload: str,
        *,
        deadline_monotonic: float | None = None,
        expected_connection_generation: int | None = None,
        outbound_transport_guard: _OutboundTransportGuard | None = None,
    ) -> None:
        """Write one envelope while respecting a bounded caller deadline.

        The synchronous websocket library owns the actual socket write and
        cannot be pre-empted safely here.  Focus nevertheless bounds every
        *local* lock before that write begins.  A socket-level exception after
        entering ``send`` remains a transport-unknown outcome, while a local
        lock/deadline failure before ``send`` is a retryable pre-send result.

        A caller may additionally pin a request to an already-established
        websocket generation.  In that narrow path the client keeps ``_lock``
        through ``ws.send``.  This prevents ``start()``/``stop()`` or the
        reader from replacing ``_ws`` between the generation check and the
        synchronous write.  The write itself can still have an unknown
        transport outcome; the guarantee is only that it is never redirected
        to a newer websocket after a stale authority scan.
        """

        entered_websocket_send = False
        try:
            with held_lock_before_deadline(
                self._send_lock,
                deadline_monotonic=deadline_monotonic,
                operation="websocket send",
            ):
                try:
                    with held_lock_before_deadline(
                        self._lock,
                        deadline_monotonic=deadline_monotonic,
                        operation="websocket selection",
                    ):
                        if self._stop_barrier.stop_requested:
                            raise _CodexWebsocketNotConnectedError(
                                "Codex websocket stop has been requested"
                            )
                        ws = self._ws
                        if expected_connection_generation is not None:
                            if not self._is_connected_locked():
                                raise _CodexWebsocketNotConnectedError(
                                    "Codex websocket is not connected"
                                )
                            observed_generation = self._connection_generation
                            if observed_generation != expected_connection_generation:
                                raise _CodexConnectionGenerationMismatchError(
                                    expected_generation=expected_connection_generation,
                                    observed_generation=observed_generation,
                                )
                            try:
                                remaining_before_deadline(
                                    deadline_monotonic,
                                    operation="websocket send",
                                )
                            except TimeoutError as exc:
                                raise _CodexRpcPreSendTimeoutError(str(exc)) from exc
                            # Keep the client identity lock until this specific
                            # websocket write enters and returns.  See the
                            # method docstring for why this is intentionally
                            # narrower than the ordinary request path.
                            assert ws is not None
                            with held_outbound_transport_guard(
                                outbound_transport_guard,
                            ):
                                entered_websocket_send = True
                                ws.send(payload)
                            return
                except TimeoutError as exc:
                    # A fenced ``ws.send`` is intentionally inside ``_lock``
                    # so a generation replacement cannot race it.  Preserve
                    # the same post-send classification as the ordinary path.
                    if entered_websocket_send:
                        raise
                    raise _CodexRpcPreSendTimeoutError(str(exc)) from exc
                if ws is None:
                    raise _CodexWebsocketNotConnectedError("Codex websocket is not connected")
                try:
                    remaining_before_deadline(
                        deadline_monotonic,
                        operation="websocket send",
                    )
                except TimeoutError as exc:
                    raise _CodexRpcPreSendTimeoutError(str(exc)) from exc
                with held_outbound_transport_guard(
                    outbound_transport_guard,
                ):
                    entered_websocket_send = True
                    ws.send(payload)
        except TimeoutError as exc:
            # Do not reclassify a timeout raised by ``ws.send`` itself: once
            # the library entered that call, the remote outcome is unknown.
            if entered_websocket_send or isinstance(exc, _CodexRpcPreSendTimeoutError):
                raise
            raise _CodexRpcPreSendTimeoutError(str(exc)) from exc

    def _reader_loop(self, ws: Any, connection_generation: int) -> None:
        disconnected = False
        while True:
            with self._lock:
                if (
                    self._closing
                    or self._stop_barrier.stop_requested
                    or self._ws is not ws
                    or self._connection_generation != connection_generation
                ):
                    return
            try:
                message = ws.recv()
            except ConnectionClosed:
                disconnected = True
                break
            except Exception as exc:
                logger.warning("Codex websocket recv failed: %s", exc)
                disconnected = True
                break
            if message is None:
                disconnected = True
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            with self._lock:
                if (
                    self._closing
                    or self._stop_barrier.stop_requested
                    or self._ws is not ws
                    or self._connection_generation != connection_generation
                ):
                    return
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的 Codex 消息: %r", message[:200])
                self._fail_pending_protocol("Codex websocket returned invalid JSON")
                continue
            if not isinstance(payload, dict):
                logger.warning("忽略非对象 Codex 消息: %r", payload)
                self._fail_pending_protocol("Codex websocket returned a non-object JSON-RPC envelope")
                continue
            with self._lock:
                if (
                    self._closing
                    or self._stop_barrier.stop_requested
                    or self._ws is not ws
                    or self._connection_generation != connection_generation
                ):
                    return
            self._dispatch_payload(payload, connection_generation=connection_generation)

        pending_to_fail: list[_PendingResponse] = []
        disconnect_cleanup_admitted = False
        with self._connection_condition:
            current_connection_disconnected = (
                disconnected
                and not self._closing
                and not self._stop_barrier.stop_requested
                and self._ws is ws
                and self._connection_generation == connection_generation
            )
            if current_connection_disconnected:
                self._ws = None
                self._initialize_result = None
                # Fence reconnect while this socket identity is still locked;
                # the synchronous hook may mutate only gate-owned state.
                try:
                    disconnect_cleanup_admitted = (
                        self._on_disconnect_ingress(connection_generation) is True
                    )
                except Exception:
                    # A broken injected hook must prevent reconnect while the
                    # canonical cleanup callback still runs.
                    logger.exception(
                        "Fencing Codex websocket disconnect ingress failed"
                    )
                    self._stop_barrier.request_stop()
                    disconnect_cleanup_admitted = True
                if self._connection_state == _CONNECTION_HANDSHAKING:
                    self._last_handshake_failure = (
                        self._handshake_attempt,
                        _CodexWebsocketNotConnectedError(
                            "Codex websocket disconnected during protocol handshake"
                        ),
                    )
                else:
                    self._connection_state = _CONNECTION_DISCONNECTED
                    self._handshake_owner_thread_id = None
                    self._handshake_generation = 0
                    self._handshake_outbound_transport_guard = None
                self._connection_condition.notify_all()
                # Clear pending responses while the connection replacement is
                # still excluded by _lock.  Otherwise a new connection could
                # register a request between `_ws = None` and `_fail_pending`,
                # and be failed as if it belonged to the old websocket.
                pending_to_fail = self._take_pending_locked()
        if current_connection_disconnected:
            self._server_request_authority.retire_connection_generation(
                connection_generation
            )
            error = {"code": -32000, "message": "Codex websocket disconnected"}
            for pending in pending_to_fail:
                pending.error = error
                pending.transport_error = True
                pending.event.set()
            if disconnect_cleanup_admitted:
                self._safe_on_disconnect(connection_generation)

    def _dispatch_payload(self, payload: dict[str, Any], *, connection_generation: int) -> None:
        if "method" in payload and not self._connection_generation_is_ready(
            connection_generation
        ):
            if self._buffer_handshake_notification(
                payload,
                connection_generation=connection_generation,
            ):
                return
            method = payload.get("method")
            logger.warning(
                "Rejecting Codex server message before protocol handshake is READY: "
                "generation=%s method=%r",
                connection_generation,
                method,
            )
            self._fail_pending_protocol(
                "Codex server sent a notification or request before the protocol "
                "handshake became ready"
            )
            return
        if "method" in payload and "id" in payload:
            if not isinstance(payload.get("method"), str):
                self._fail_pending_protocol("Codex server request method is not a string")
                return
            request_id = payload.get("id")
            if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                self._fail_pending_protocol("Codex server request has an invalid id")
                return
            self._server_request_authority.remember(
                request_id,
                connection_generation,
            )
            raw_params = payload.get("params")
            automatic_response = automatic_server_request_response(
                payload["method"],
                {} if raw_params is None else raw_params,
            )
            if automatic_response is not None:
                result, error = automatic_response
                self._spawn_detached_callback(
                    target=self._safe_automatic_server_request_response,
                    args=(
                        connection_generation,
                        request_id,
                        payload["method"],
                        result,
                        error,
                    ),
                    connection_generation=connection_generation,
                    name=f"focus-codex-automatic-response-{connection_generation}",
                )
                return
            if raw_params is None:
                request_params: Any = {}
            elif isinstance(raw_params, dict):
                request_params = raw_params
            else:
                logger.warning(
                    "Dropping malformed Codex server request params: generation=%s method=%s",
                    connection_generation,
                    payload["method"],
                )
                return
            self._safe_on_request(
                connection_generation,
                payload["id"],
                payload["method"],
                request_params,
            )
            return
        if "method" in payload:
            if not isinstance(payload.get("method"), str):
                self._fail_pending_protocol("Codex notification method is not a string")
                return
            notification_params = payload.get("params") or {}
            if (
                payload["method"] == "serverRequest/resolved"
                and isinstance(notification_params, dict)
            ):
                resolved_request_id = notification_params.get("requestId")
                if resolved_request_id not in (None, ""):
                    try:
                        self._server_request_authority.retire_request_generation(
                            resolved_request_id,
                            connection_generation,
                        )
                    except ValueError:
                        logger.warning(
                            "Ignoring malformed serverRequest/resolved request id: %r",
                            resolved_request_id,
                        )
            self._safe_on_notification(
                connection_generation,
                payload["method"],
                notification_params,
            )
            return
        if "id" in payload:
            self._resolve_response(payload)
            return
        self._fail_pending_protocol("Codex JSON-RPC envelope has neither method nor id")

    def _spawn_detached_callback(
        self,
        *,
        target: Callable[..., None],
        args: tuple[Any, ...],
        connection_generation: int,
        name: str,
    ) -> bool:
        """Register a detached callback before it can begin executing.

        The same identity lock which closes ingress also snapshots this set
        for ``stop()``.  Therefore a callback is either rejected after close
        or is a concrete member of the stop barrier; it cannot land between
        those states.
        """

        with self._connection_condition:
            if (
                self._closing
                or not self._is_ready_locked()
                or self._connection_generation != connection_generation
            ):
                return False
            self._callback_threads = {
                thread
                for thread in self._callback_threads
                if self._thread_is_alive(thread)
            }
            callback_thread = threading.Thread(
                target=target,
                args=args,
                name=name,
                daemon=True,
            )
            self._callback_threads.add(callback_thread)
            try:
                callback_thread.start()
            except Exception:
                self._callback_threads.discard(callback_thread)
                raise
            return True

    def _buffer_handshake_notification(
        self,
        payload: dict[str, Any],
        *,
        connection_generation: int,
    ) -> bool:
        """Delay upstream initialize notifications until requirements admission.

        Current app-server deliberately emits config warnings and remote-control
        status after its initialize response but before ordinary outbound
        delivery is enabled. A shared backend can also begin broadcasting
        lifecycle notifications in the small interval before Focus finishes
        configRequirements/read. Buffer notifications in order, but never a
        server request, and fail the handshake on bounded-queue overflow.
        """

        with self._lock:
            if (
                self._connection_state != _CONNECTION_HANDSHAKING
                or self._connection_generation != connection_generation
                or self._ws is None
                or "id" in payload
                or not isinstance(payload.get("method"), str)
                or len(self._handshake_notifications)
                >= _MAX_HANDSHAKE_BUFFERED_NOTIFICATIONS
            ):
                return False
            self._handshake_notifications.append(
                (connection_generation, dict(payload))
            )
            return True

    def _resolve_response(self, payload: dict[str, Any]) -> None:
        response_id = payload.get("id")
        if isinstance(response_id, bool) or not isinstance(response_id, (int, str)):
            self._fail_pending_protocol("Codex JSON-RPC response has an invalid id")
            return
        with self._lock:
            pending = self._pending.pop(response_id, None)
        if pending is None:
            return
        has_error = "error" in payload
        has_result = "result" in payload
        if has_error == has_result:
            pending.protocol_error = "Codex JSON-RPC response must contain exactly one of result or error"
        elif has_error:
            error = payload.get("error")
            if not isinstance(error, dict):
                pending.protocol_error = "Codex JSON-RPC error must be an object"
            elif isinstance(error.get("code"), bool) or not isinstance(error.get("code"), int):
                pending.protocol_error = "Codex JSON-RPC error code must be an integer"
            elif not isinstance(error.get("message"), str):
                pending.protocol_error = "Codex JSON-RPC error message must be a string"
            else:
                pending.error = error
        else:
            pending.result = payload.get("result")
        pending.event.set()

    def _fail_pending(self, error: dict[str, Any], *, transport_error: bool = False) -> None:
        with self._lock:
            pending_items = self._take_pending_locked()
        for pending in pending_items:
            pending.error = error
            pending.transport_error = bool(transport_error)
            pending.event.set()

    def _fail_pending_protocol(self, message: str) -> None:
        with self._connection_condition:
            if self._connection_state == _CONNECTION_HANDSHAKING:
                failure = CodexRpcProtocolError("connection/handshake", message)
                self._handshake_ingress_failure = failure
                self._last_handshake_failure = (
                    self._handshake_attempt,
                    failure,
                )
                self._connection_condition.notify_all()
            pending_items = self._take_pending_locked()
        for pending in pending_items:
            pending.protocol_error = str(message or "Codex JSON-RPC protocol error")
            pending.event.set()

    def _take_pending_locked(self) -> list[_PendingResponse]:
        pending_items = list(self._pending.values())
        self._pending.clear()
        return pending_items

    def _safe_on_notification(
        self,
        connection_generation: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if not self._connection_generation_is_live(connection_generation):
            logger.debug(
                "Dropping Codex notification from disconnected websocket generation: generation=%s method=%s",
                connection_generation,
                method,
            )
            return
        try:
            self._on_notification(connection_generation, method, params)
        except Exception:
            logger.exception("处理 Codex notification 失败: method=%s", method)

    def _safe_on_request(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if not self._connection_generation_is_live(connection_generation):
            logger.debug(
                "Dropping Codex server request from disconnected websocket generation: generation=%s method=%s",
                connection_generation,
                method,
            )
            return
        try:
            self._on_request(connection_generation, request_id, method, params)
        except Exception:
            logger.exception("处理 Codex server request 失败: method=%s", method)

    def _safe_automatic_server_request_response(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> None:
        """Answer a stateless protocol utility on its originating socket."""

        try:
            self.respond(
                request_id,
                result=result,
                error=error,
                timeout=self._request_timeout_seconds,
                require_existing_connection=True,
                expected_connection_generation=connection_generation,
            )
        except CodexRpcConnectionGenerationMismatchError:
            # Never replay a server-request id on a replacement connection.
            logger.debug(
                "Dropping automatic Codex server-request response after reconnect: "
                "generation=%s method=%s",
                connection_generation,
                method,
            )
        except Exception:
            # Upstream defines a failed current-time response as a turn stop;
            # there is no safe retry after an unknown websocket write.
            logger.exception(
                "响应 Codex automatic server request 失败: method=%s",
                method,
            )

    def _safe_on_disconnect(self, connection_generation: int) -> None:
        try:
            self._on_disconnect(connection_generation)
        except Exception:
            logger.exception("处理 Codex websocket disconnect 失败")

    def _connection_generation_is_live(self, connection_generation: int) -> bool:
        return self._connection_generation_is_ready(connection_generation)

    def _connection_generation_is_ready(self, connection_generation: int) -> bool:
        with self._lock:
            return bool(
                not self._closing
                and self._is_ready_locked()
                and self._connection_generation == connection_generation
            )

    def _websocket_auth_headers_for_connect(self) -> dict[str, str]:
        if self._app_server_ws_auth_store is None:
            return {}
        if self._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS:
            token = self._app_server_ws_auth_store.ensure()
        else:
            token = self._app_server_ws_auth_store.require()
        return build_bearer_authorization_headers(token)
