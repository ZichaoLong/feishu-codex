"""Local control plane for managing the running feishu-codex service."""

from __future__ import annotations

import json
import pathlib
import socket
import socketserver
import threading
from typing import Any, Callable

_MAX_MESSAGE_BYTES = 1024 * 1024
_LISTEN_HOST = "127.0.0.1"


class ServiceControlError(RuntimeError):
    """Raised when a control-plane request fails."""


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
    daemon_threads = True
    allow_reuse_address = True


class _ServiceControlRequestHandler(socketserver.StreamRequestHandler):
    server: "_ServiceControlServer"

    def handle(self) -> None:
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
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class _ServiceControlServer(_ThreadingTcpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        dispatch: Callable[[str, dict[str, Any]], Any],
        auth_token: Callable[[], str],
    ) -> None:
        self.dispatch = dispatch
        self.auth_token = auth_token
        super().__init__(server_address, _ServiceControlRequestHandler)


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
        self._server: _ServiceControlServer | None = None
        self._thread: threading.Thread | None = None
        self._control_endpoint = ""

    @property
    def control_endpoint(self) -> str:
        return self._control_endpoint

    def start(self) -> str:
        with self._lock:
            if self._server is not None:
                return self._control_endpoint
            if self._owns_current_lease is not None and not self._owns_current_lease():
                raise ServiceControlError("当前进程不是此控制面的合法 owner。")
            server = _ServiceControlServer((_LISTEN_HOST, 0), self._dispatch, self._auth_token)
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

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._control_endpoint = ""
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1)


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
        raise ServiceControlError(f"控制面未启动：{pathlib.Path(data_dir)}")
    if not metadata.control_endpoint:
        raise ServiceControlError("控制面尚未发布 endpoint。")
    payload = json.dumps(
        {
            "auth_token": metadata.owner_token,
            "method": str(method or "").strip(),
            "params": dict(params or {}),
        },
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    host, port = parse_control_endpoint(metadata.control_endpoint)
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendall(payload)
            response = _recv_line(sock)
    except ConnectionRefusedError as exc:
        raise ServiceControlError(f"控制面连接失败：{metadata.control_endpoint}") from exc
    except OSError as exc:
        raise ServiceControlError(f"控制面请求失败：{exc}") from exc
    if not isinstance(response, dict):
        raise ServiceControlError("控制面返回了无效响应")
    if response.get("ok") is True:
        return response.get("result")
    error = response.get("error") or {}
    raise ServiceControlError(str(error.get("message", "控制面请求失败")))


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
