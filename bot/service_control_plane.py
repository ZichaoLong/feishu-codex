"""Local control plane for managing the running feishu-codex service."""

from __future__ import annotations

import json
import os
import pathlib
import socket
import socketserver
import threading
from typing import Any, Callable

_SOCKET_NAME = "service-control.sock"
_MAX_MESSAGE_BYTES = 1024 * 1024


class ServiceControlError(RuntimeError):
    """Raised when a control-plane request fails."""


def control_socket_path(data_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(data_dir) / _SOCKET_NAME


class _ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
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


class _ServiceControlServer(_ThreadingUnixStreamServer):
    def __init__(self, socket_path: str, dispatch: Callable[[str, dict[str, Any]], Any]) -> None:
        self.dispatch = dispatch
        super().__init__(socket_path, _ServiceControlRequestHandler)


class ServiceControlPlane:
    def __init__(
        self,
        *,
        data_dir: pathlib.Path,
        dispatch: Callable[[str, dict[str, Any]], Any],
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._server: _ServiceControlServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def socket_path(self) -> pathlib.Path:
        return control_socket_path(self._data_dir)

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            path = self.socket_path
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            server = _ServiceControlServer(str(path), self._dispatch)
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass
            thread = threading.Thread(
                target=server.serve_forever,
                name="service-control-plane",
                daemon=True,
            )
            self._server = server
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def control_request(
    data_dir: pathlib.Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 3.0,
) -> Any:
    socket_path = control_socket_path(pathlib.Path(data_dir))
    if not socket_path.exists():
        raise ServiceControlError(f"控制面未启动：{socket_path}")
    payload = json.dumps(
        {
            "method": str(method or "").strip(),
            "params": dict(params or {}),
        },
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.connect(str(socket_path))
            sock.sendall(payload)
            response = _recv_line(sock)
    except FileNotFoundError as exc:
        raise ServiceControlError(f"控制面未启动：{socket_path}") from exc
    except ConnectionRefusedError as exc:
        raise ServiceControlError(f"控制面连接失败：{socket_path}") from exc
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
