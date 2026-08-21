"""Local control plane for managing the running FOCUS service."""

from __future__ import annotations

import json
import pathlib
import socket
import socketserver
import threading
import time
from typing import Any, Callable

_MAX_MESSAGE_BYTES = 1024 * 1024
_LISTEN_HOST = "127.0.0.1"
_REQUEST_IO_TIMEOUT_SECONDS = 5.0


class ServiceControlError(RuntimeError):
    """Raised when a control-plane request fails."""


class ServiceControlOutcomeUnknownError(ServiceControlError):
    """Raised after a request may have reached the service without a usable response."""


class ServiceControlKnownNotCommittedError(ServiceControlError):
    """Raised when no control connection was established, so nothing was sent."""


class ServiceControlResponseTimeoutError(ServiceControlOutcomeUnknownError):
    """Raised when a request was sent but the response did not arrive in time."""


class ServiceControlShutdownError(ServiceControlError):
    """The control plane could not prove that every local thread exited."""


def format_control_endpoint(host: str, port: int) -> str:
    return f"tcp://{host}:{int(port)}"


def parse_control_endpoint(endpoint: str) -> tuple[str, int]:
    normalized = str(endpoint or "").strip()
    if not normalized.startswith("tcp://"):
        raise ServiceControlError(f"不支持的 control endpoint: {normalized or '<empty>'}")
    host_port = normalized[len("tcp://") :]
    host, sep, port_text = host_port.rpartition(":")
    if not sep or not host:
        raise ServiceControlError(f"无效的 control endpoint: {normalized}")
    try:
        return host, int(port_text)
    except ValueError as exc:
        raise ServiceControlError(f"无效的 control endpoint: {normalized}") from exc


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # Request lifetime is proved explicitly by ``_ServiceControlServer``.
    # ``server_close()`` must therefore never hide an unbounded private join.
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class _ServiceControlRequestHandler(socketserver.StreamRequestHandler):
    server: "_ServiceControlServer"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_REQUEST_IO_TIMEOUT_SECONDS)

    def handle(self) -> None:
        try:
            if not self.server.begin_request():
                return
            raw = self.rfile.readline(_MAX_MESSAGE_BYTES)
            if not raw:
                return
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ServiceControlError("control request must be an object")
                auth_token = str(request.get("auth_token", "") or "").strip()
                if auth_token != self.server.auth_token():
                    raise ServiceControlError("control request authentication failed")
                method = str(request.get("method", "") or "").strip()
                params = request.get("params") or {}
                if not method:
                    raise ServiceControlError("control request missing method")
                if not isinstance(params, dict):
                    raise ServiceControlError("control request params must be an object")
                result = self.server.dispatch(method, params)
                response = {"ok": True, "result": result}
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": {
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                }
            self.wfile.write(
                (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
            )
        finally:
            self.server.finish_admitted_request()


class _ServiceControlServer(_ThreadingTcpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        dispatch: Callable[[str, dict[str, Any]], Any],
        auth_token: Callable[[], str],
    ) -> None:
        self.dispatch = dispatch
        self.auth_token = auth_token
        self._request_condition = threading.Condition()
        self._accepting_requests = True
        self._active_request_threads: set[int] = set()
        super().__init__(server_address, _ServiceControlRequestHandler)

    def begin_request(self) -> bool:
        thread_id = threading.get_ident()
        with self._request_condition:
            if not self._accepting_requests:
                return False
            self._active_request_threads.add(thread_id)
            return True

    def finish_admitted_request(self) -> None:
        thread_id = threading.get_ident()
        with self._request_condition:
            self._active_request_threads.discard(thread_id)
            self._request_condition.notify_all()

    def close_request_admission(self) -> None:
        with self._request_condition:
            self._accepting_requests = False
            self._request_condition.notify_all()

    def wait_for_requests(self, *, timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        current_thread_id = threading.get_ident()
        with self._request_condition:
            if current_thread_id in self._active_request_threads:
                raise ServiceControlShutdownError(
                    "控制面 request thread 不能证明自身已退出。"
                )
            while self._active_request_threads:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise ServiceControlShutdownError(
                        "控制面仍有 request thread 未退出。"
                    )
                self._request_condition.wait(timeout=remaining)


class ServiceControlPlane:
    def __init__(
        self,
        *,
        data_dir: pathlib.Path,
        dispatch: Callable[[str, dict[str, Any]], Any],
        owns_current_lease: Callable[[], bool] | None = None,
        auth_token: Callable[[], str] | None = None,
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._dispatch = dispatch
        self._owns_current_lease = owns_current_lease
        self._auth_token = auth_token or (lambda: "")
        self._lock = threading.Lock()
        self._transition_lock = threading.Lock()
        self._server: _ServiceControlServer | None = None
        self._thread: threading.Thread | None = None
        self._control_endpoint = ""

    @property
    def control_endpoint(self) -> str:
        return self._control_endpoint

    def start(self) -> str:
        with self._transition_lock:
            with self._lock:
                if self._server is not None:
                    return self._control_endpoint
                if self._owns_current_lease is not None and not self._owns_current_lease():
                    raise ServiceControlError("当前进程不是此控制面的合法 owner。")
                server = _ServiceControlServer(
                    (_LISTEN_HOST, 0),
                    self._dispatch,
                    self._auth_token,
                )
                thread = threading.Thread(
                    target=server.serve_forever,
                    name="service-control-plane",
                    daemon=True,
                )
                host, port = server.server_address
                self._server = server
                self._thread = thread
                self._control_endpoint = format_control_endpoint(host, port)
                thread.start()
                return self._control_endpoint

    def stop(self, *, timeout: float | None = 5.0) -> None:
        """Close admission and prove listener plus request-thread exit.

        A failed proof deliberately retains component references and the
        endpoint so a service-level shutdown transaction can retry. It must
        not release machine authority while an admitted dispatch can still
        reach RuntimeLoop.
        """

        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)

        def remaining() -> float | None:
            if deadline is None:
                return None
            return max(deadline - time.monotonic(), 0.0)

        with self._transition_lock:
            with self._lock:
                server = self._server
                thread = self._thread
            if server is None:
                return
            server.close_request_admission()
            if thread is not None and threading.current_thread() is thread:
                raise ServiceControlShutdownError(
                    "控制面 listener thread 不能证明自身已退出。"
                )
            if thread is not None and thread.is_alive():
                server.shutdown()
                thread.join(timeout=remaining())
                if thread.is_alive():
                    raise ServiceControlShutdownError(
                        "控制面 listener thread 未在期限内退出。"
                    )
            server.wait_for_requests(timeout=remaining())
            server.server_close()
            with self._lock:
                if self._server is server:
                    self._server = None
                    self._thread = None
                    self._control_endpoint = ""


def control_request(
    data_dir: pathlib.Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 3.0,
) -> Any:
    from bot.stores.service_instance_lease import ServiceInstanceLease

    metadata = ServiceInstanceLease(pathlib.Path(data_dir)).load_metadata()
    if metadata is None:
        raise ServiceControlKnownNotCommittedError(
            f"控制面未启动：{pathlib.Path(data_dir)}"
        )
    if not metadata.control_endpoint:
        raise ServiceControlKnownNotCommittedError("控制面尚未发布 endpoint。")
    payload = json.dumps(
        {
            "auth_token": metadata.owner_token,
            "method": str(method or "").strip(),
            "params": dict(params or {}),
        },
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    try:
        host, port = parse_control_endpoint(metadata.control_endpoint)
    except ServiceControlError as exc:
        raise ServiceControlKnownNotCommittedError(str(exc)) from exc
    try:
        sock = socket.create_connection((host, port), timeout=timeout_seconds)
    except ConnectionRefusedError as exc:
        raise ServiceControlKnownNotCommittedError(
            f"控制面连接失败：{metadata.control_endpoint}"
        ) from exc
    except TimeoutError as exc:
        raise ServiceControlKnownNotCommittedError(
            f"控制面连接超时：{metadata.control_endpoint}"
        ) from exc
    except OSError as exc:
        raise ServiceControlKnownNotCommittedError(
            f"控制面连接失败：{metadata.control_endpoint}: {exc}"
        ) from exc
    with sock:
        sock.settimeout(timeout_seconds)
        try:
            sock.sendall(payload)
        except Exception as exc:
            raise ServiceControlOutcomeUnknownError(
                f"控制面请求发送结果未知：{metadata.control_endpoint}: {exc}"
            ) from exc
        try:
            response = _recv_line(sock)
        except TimeoutError as exc:
            raise ServiceControlResponseTimeoutError(
                f"控制面请求已发送，但等待响应超时：{metadata.control_endpoint}"
            ) from exc
        except Exception as exc:
            raise ServiceControlOutcomeUnknownError(
                f"控制面请求已发送，但响应不可用：{metadata.control_endpoint}: {exc}"
            ) from exc
    if not isinstance(response, dict):
        raise ServiceControlOutcomeUnknownError("控制面请求已发送，但返回了无效响应")
    if response.get("ok") is True:
        return response.get("result")
    if response.get("ok") is not False:
        raise ServiceControlOutcomeUnknownError("控制面请求已发送，但响应缺少明确结果")
    error = response.get("error")
    if isinstance(error, dict):
        message = str(error.get("message", "控制面请求失败") or "控制面请求失败")
    elif isinstance(error, str):
        message = error.strip() or "控制面请求失败"
    else:
        message = "控制面请求失败"
    raise ServiceControlError(message)


def _recv_line(sock: socket.socket) -> Any:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_MESSAGE_BYTES:
            raise ServiceControlError("控制面响应过大")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ServiceControlError("控制面没有返回数据")
    return json.loads(raw.decode("utf-8"))
