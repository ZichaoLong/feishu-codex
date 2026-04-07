"""
Codex app-server JSON-RPC 客户端。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from bot.stores.app_server_runtime_store import AppServerRuntimeStore, uses_default_app_server_url

logger = logging.getLogger(__name__)


class CodexRpcError(RuntimeError):
    """Codex JSON-RPC 请求失败。"""

    def __init__(self, method: str, error: dict[str, Any]):
        self.method = method
        self.error = error
        message = error.get("message") or f"{method} failed"
        super().__init__(message)


@dataclass
class _PendingResponse:
    event: threading.Event
    result: Any = None
    error: dict[str, Any] | None = None


class CodexRpcClient:
    """基于 websocket 的 Codex app-server 客户端。"""

    def __init__(
        self,
        *,
        codex_command: str = "codex",
        app_server_mode: str = "managed",
        app_server_url: str = "ws://127.0.0.1:8765",
        connect_timeout_seconds: float = 15.0,
        request_timeout_seconds: float = 30.0,
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
        on_request: Callable[[int | str, str, dict[str, Any]], None] | None = None,
        app_server_runtime_store: AppServerRuntimeStore | None = None,
    ) -> None:
        self._codex_command = codex_command
        self._app_server_mode = app_server_mode
        self._configured_app_server_url = app_server_url
        self._app_server_url = app_server_url
        self._connect_timeout_seconds = connect_timeout_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._on_notification = on_notification or (lambda _method, _params: None)
        self._on_request = on_request or (lambda _request_id, _method, _params: None)
        self._app_server_runtime_store = app_server_runtime_store

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending: dict[int, _PendingResponse] = {}
        self._next_id = 1

        self._process: subprocess.Popen[str] | None = None
        self._ws = None
        self._reader_thread: threading.Thread | None = None
        self._closing = False

    def start(self) -> None:
        """启动或连接 app-server 并建立 websocket 连接。"""
        need_initialize = False
        with self._lock:
            if self._is_connected_locked():
                return
            self._start_locked()
            need_initialize = True
        if need_initialize:
            try:
                self.request(
                    "initialize",
                    {
                        "clientInfo": {"name": "feishu-codex", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                    timeout=self._connect_timeout_seconds,
                )
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        """关闭连接与本地 app-server 子进程。"""
        with self._lock:
            self._closing = True
            ws = self._ws
            process = self._process
            self._ws = None
            self._process = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._clear_managed_runtime_state()
        self._fail_pending({"code": -32000, "message": "Codex app-server closed"})

    def current_app_server_url(self) -> str:
        return self._app_server_url or self._configured_app_server_url

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        self.start()
        request_id, pending = self._register_pending()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self._send_json(payload)

        wait_seconds = timeout or self._request_timeout_seconds
        if not pending.event.wait(wait_seconds):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(f"Codex request timed out: {method}")
        if pending.error is not None:
            raise CodexRpcError(method, pending.error)
        return pending.result

    def respond(self, request_id: int | str, *, result: dict | None = None, error: dict | None = None) -> None:
        """响应服务端发来的 JSON-RPC request。"""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        self._send_json(payload)

    def _start_locked(self) -> None:
        self._closing = False
        if self._app_server_mode == "managed":
            if self._process is not None and self._process.poll() is not None:
                self._process = None
            if self._process is None:
                self._app_server_url = self._select_managed_listen_url()
                cmd = [*shlex.split(self._codex_command), "app-server", "--listen", self._app_server_url]
                logger.info("启动 Codex app-server: %s", cmd)
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=os.environ.copy(),
                )
                assert self._process.stdout is not None
                assert self._process.stderr is not None
                threading.Thread(
                    target=self._log_stream,
                    args=(self._process.stdout, logging.DEBUG, "stdout"),
                    daemon=True,
                ).start()
                threading.Thread(
                    target=self._log_stream,
                    args=(self._process.stderr, logging.INFO, "stderr"),
                    daemon=True,
                ).start()
            else:
                logger.info("复用已运行的 Codex app-server: %s", self._app_server_url)
        else:
            self._app_server_url = self._configured_app_server_url
        self._connect_ws_locked()
        self._record_managed_runtime_state()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _connect_ws_locked(self) -> None:
        deadline = time.time() + self._connect_timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError("codex app-server exited before websocket connected")
            try:
                # Codex can return multi-megabyte frames for thread/read(thread.turns)
                # and thread/resume. The default websocket 1 MiB limit breaks valid
                # resume flows for longer sessions, so disable the per-frame cap here.
                self._ws = connect(
                    self._app_server_url,
                    open_timeout=self._connect_timeout_seconds,
                    max_size=None,
                )
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"failed to connect Codex websocket: {last_error}")

    def _is_connected_locked(self) -> bool:
        if self._ws is None:
            return False
        if self._app_server_mode == "managed":
            return self._process is not None and self._process.poll() is None
        return True

    def _register_pending(self) -> tuple[int, _PendingResponse]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            pending = _PendingResponse(event=threading.Event())
            self._pending[request_id] = pending
            return request_id, pending

    def _send_json(self, payload: dict[str, Any]) -> None:
        with self._send_lock:
            if self._ws is None:
                raise RuntimeError("Codex websocket is not connected")
            self._ws.send(json.dumps(payload, ensure_ascii=False))

    def _reader_loop(self) -> None:
        while True:
            with self._lock:
                if self._closing:
                    return
                ws = self._ws
            if ws is None:
                return
            try:
                message = ws.recv()
            except ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("Codex websocket recv failed: %s", exc)
                break
            if message is None:
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="replace")
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的 Codex 消息: %r", message[:200])
                continue
            self._dispatch_payload(payload)

        self._fail_pending({"code": -32000, "message": "Codex websocket disconnected"})
        with self._lock:
            self._ws = None

    def _dispatch_payload(self, payload: dict[str, Any]) -> None:
        if "method" in payload and "id" in payload:
            threading.Thread(
                target=self._safe_on_request,
                args=(payload["id"], payload["method"], payload.get("params") or {}),
                daemon=True,
            ).start()
            return
        if "method" in payload:
            self._safe_on_notification(payload["method"], payload.get("params") or {})
            return
        if "id" in payload:
            self._resolve_response(payload)

    def _resolve_response(self, payload: dict[str, Any]) -> None:
        response_id = payload.get("id")
        with self._lock:
            pending = self._pending.pop(response_id, None)
        if pending is None:
            return
        if "error" in payload:
            pending.error = payload["error"]
        else:
            pending.result = payload.get("result")
        pending.event.set()

    def _fail_pending(self, error: dict[str, Any]) -> None:
        with self._lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.error = error
            pending.event.set()

    def _safe_on_notification(self, method: str, params: dict[str, Any]) -> None:
        try:
            self._on_notification(method, params)
        except Exception:
            logger.exception("处理 Codex notification 失败: method=%s", method)

    def _safe_on_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        try:
            self._on_request(request_id, method, params)
        except Exception:
            logger.exception("处理 Codex server request 失败: method=%s", method)

    @staticmethod
    def _log_stream(stream, level: int, name: str) -> None:
        for line in iter(stream.readline, ""):
            text = line.rstrip()
            if text:
                logger.log(level, "[codex app-server %s] %s", name, text)

    def _record_managed_runtime_state(self) -> None:
        if self._app_server_mode != "managed" or self._app_server_runtime_store is None:
            return
        app_server_pid = 0
        if self._process is not None and getattr(self._process, "pid", None):
            app_server_pid = int(self._process.pid)
        self._app_server_runtime_store.save_managed_runtime(
            configured_url=self._configured_app_server_url,
            active_url=self._app_server_url,
            owner_pid=os.getpid(),
            app_server_pid=app_server_pid,
        )

    def _clear_managed_runtime_state(self) -> None:
        if self._app_server_mode != "managed" or self._app_server_runtime_store is None:
            return
        self._app_server_runtime_store.clear_managed_runtime(owner_pid=os.getpid())

    def _select_managed_listen_url(self) -> str:
        listen_url = self._configured_app_server_url
        if not uses_default_app_server_url(listen_url):
            return listen_url
        if self._can_bind_listen_url(listen_url):
            return listen_url
        fallback_url = self._allocate_free_listen_url(listen_url)
        logger.warning("Codex app-server 默认地址 %s 不可用，自动切换到 %s", listen_url, fallback_url)
        return fallback_url

    @classmethod
    def _can_bind_listen_url(cls, url: str) -> bool:
        family, address = cls._socket_address_for_url(url)
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(address)
            except OSError:
                return False
        return True

    @classmethod
    def _allocate_free_listen_url(cls, url: str) -> str:
        scheme, host, _port, path = cls._parse_listen_url(url)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        bind_address: tuple[Any, ...]
        if family == socket.AF_INET6:
            bind_address = (host, 0, 0, 0)
        else:
            bind_address = (host, 0)
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind(bind_address)
            actual_port = int(sock.getsockname()[1])
        return cls._format_listen_url(scheme, host, actual_port, path)

    @classmethod
    def _socket_address_for_url(cls, url: str) -> tuple[socket.AddressFamily, tuple[Any, ...]]:
        _scheme, host, port, _path = cls._parse_listen_url(url)
        if ":" in host:
            return socket.AF_INET6, (host, port, 0, 0)
        return socket.AF_INET, (host, port)

    @staticmethod
    def _parse_listen_url(url: str) -> tuple[str, str, int, str]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"不支持的 app-server URL：{url}")
        if parsed.query or parsed.fragment:
            raise ValueError(f"不支持带 query/fragment 的 app-server URL：{url}")
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            raise ValueError(f"app-server URL 缺少 host/port：{url}")
        path = parsed.path if parsed.path not in {"", "/"} else ""
        return parsed.scheme, host, port, path

    @staticmethod
    def _format_listen_url(scheme: str, host: str, port: int, path: str = "") -> str:
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return urlunsplit((scheme, netloc, path, "", ""))
