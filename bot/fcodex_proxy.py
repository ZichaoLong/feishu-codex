"""
fcodex 本地 websocket proxy。

Upstream Codex TUI 在 `--remote` 模式下不会给 `thread/start` 带 `cwd`，
shared app-server 会回退到服务进程自己的工作目录。这里补一个很薄的
本地代理，在需要时给 `thread/start` 补回调用方 cwd。

另外，upstream `codex --remote ... resume <id>` 启动时会先连一次 remote
app-server 做 session lookup，再断开后重连进入正式 TUI；因此这里不能在
首条 websocket 连接结束后立即自关，而要保留一个很短的 idle 窗口给下一次连接。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import threading
import time
from collections.abc import Callable
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import serve

from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
)

_CWD_PROXY_METHODS = {"thread/start"}
_DEFAULT_IDLE_TIMEOUT_SECONDS = 5.0
_INTERACTIVE_SERVER_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
}
_OWNER_WRITE_METHODS = {
    "turn/start",
    "turn/interrupt",
}
_NON_ACTIVE_THREAD_STATUS_TYPES = {
    "idle",
    "errored",
    "closed",
    "archived",
    "notLoaded",
}


def _rewrite_thread_start_cwd(message: str | bytes, cwd: str) -> str | bytes:
    raw: str
    if isinstance(message, bytes):
        try:
            raw = message.decode("utf-8")
        except UnicodeDecodeError:
            return message
    else:
        raw = message

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return message
    if not isinstance(payload, dict):
        return message
    if payload.get("method") not in _CWD_PROXY_METHODS:
        return message
    params = payload.get("params")
    if not isinstance(params, dict):
        return message
    if params.get("cwd") not in (None, ""):
        return message

    updated_payload = dict(payload)
    updated_params = dict(params)
    updated_params["cwd"] = cwd
    updated_payload["params"] = updated_params
    encoded = json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))
    if isinstance(message, bytes):
        return encoded.encode("utf-8")
    return encoded


def _parse_jsonrpc_message(message: str | bytes) -> tuple[dict[str, Any], bool] | None:
    raw: str
    is_bytes = isinstance(message, bytes)
    if is_bytes:
        try:
            raw = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        raw = message

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload, is_bytes


def _encode_jsonrpc_payload(payload: dict[str, Any], *, as_bytes: bool) -> str | bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if as_bytes:
        return encoded.encode("utf-8")
    return encoded


def _payload_thread_id(payload: dict[str, Any]) -> str:
    params = payload.get("params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("threadId", "") or "").strip()


def _jsonrpc_id_key(value: Any) -> str:
    return str(value)


def _send_local_error(client_ws: Any, request_id: Any, message: str) -> None:
    if request_id in (None, ""):
        return
    client_ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32002,
                    "message": message,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _thread_became_non_active(payload: dict[str, Any]) -> bool:
    params = payload.get("params")
    if not isinstance(params, dict):
        return False
    status = params.get("status")
    if not isinstance(status, dict):
        return False
    return str(status.get("type", "") or "").strip() in _NON_ACTIVE_THREAD_STATUS_TYPES


class _ProxyInteractionGate:
    def __init__(
        self,
        *,
        cwd: str,
        data_dir: pathlib.Path,
        holder_pid: int,
    ) -> None:
        self._cwd = cwd
        self._holder = make_fcodex_interaction_holder(
            f"fcodex:{holder_pid}",
            owner_pid=holder_pid,
        )
        self._lease_store = InteractionLeaseStore(data_dir)
        self._lock = threading.Lock()
        self._pending_server_request_thread_by_id: dict[str, str] = {}
        self._pending_client_request_by_id: dict[str, tuple[str, str, bool]] = {}
        self._owned_thread_ids: set[str] = set()

    def _remember_owned_thread(self, thread_id: str) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        with self._lock:
            self._owned_thread_ids.add(normalized_thread_id)

    def _forget_owned_thread(self, thread_id: str) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        with self._lock:
            self._owned_thread_ids.discard(normalized_thread_id)

    def close(self) -> None:
        with self._lock:
            owned_thread_ids = set(self._owned_thread_ids)
            owned_thread_ids.update(
                thread_id
                for thread_id in self._pending_server_request_thread_by_id.values()
                if thread_id
            )
            owned_thread_ids.update(
                thread_id
                for _, thread_id, _ in self._pending_client_request_by_id.values()
                if thread_id
            )
            self._owned_thread_ids.clear()
            self._pending_server_request_thread_by_id.clear()
            self._pending_client_request_by_id.clear()
        for thread_id in owned_thread_ids:
            self._lease_store.release(thread_id, self._holder)

    def handle_client_message(self, message: str | bytes, *, client_ws: Any, backend_ws: Any) -> None:
        rewritten = _rewrite_thread_start_cwd(message, self._cwd)
        parsed = _parse_jsonrpc_message(rewritten)
        if parsed is None:
            backend_ws.send(rewritten)
            return
        payload, is_bytes = parsed

        method = payload.get("method")
        if isinstance(method, str):
            thread_id = _payload_thread_id(payload)
            if method in _OWNER_WRITE_METHODS and thread_id:
                request_id = payload.get("id")
                if method == "turn/start":
                    lease = self._lease_store.acquire(thread_id, self._holder)
                    if not lease.granted:
                        _send_local_error(
                            client_ws,
                            request_id,
                            "当前线程正由其他终端执行；请等待当前 turn 结束后再试。",
                        )
                        return
                    self._remember_owned_thread(thread_id)
                    with self._lock:
                        self._pending_client_request_by_id[_jsonrpc_id_key(request_id)] = (
                            method,
                            thread_id,
                            lease.acquired,
                        )
                elif method == "turn/interrupt":
                    lease = self._lease_store.load(thread_id)
                    if lease is None or not lease.holder.same_holder(self._holder):
                        _send_local_error(
                            client_ws,
                            request_id,
                            "当前终端不是该线程的交互 owner，不能取消这次执行。",
                        )
                        return
                    self._remember_owned_thread(thread_id)
            backend_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            return

        response_id = payload.get("id")
        if response_id not in (None, ""):
            request_key = _jsonrpc_id_key(response_id)
            with self._lock:
                thread_id = self._pending_server_request_thread_by_id.pop(request_key, "")
            if thread_id:
                lease = self._lease_store.load(thread_id)
                if lease is not None and not lease.holder.same_holder(self._holder):
                    return
        backend_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))

    def handle_backend_message(self, message: str | bytes, *, client_ws: Any, backend_ws: Any) -> None:
        del backend_ws
        parsed = _parse_jsonrpc_message(message)
        if parsed is None:
            client_ws.send(message)
            return
        payload, is_bytes = parsed

        method = payload.get("method")
        if isinstance(method, str) and "id" in payload:
            thread_id = _payload_thread_id(payload)
            if method in _INTERACTIVE_SERVER_REQUEST_METHODS and thread_id:
                lease = self._lease_store.load(thread_id)
                if lease is not None and not lease.holder.same_holder(self._holder):
                    return
                self._remember_owned_thread(thread_id)
                with self._lock:
                    self._pending_server_request_thread_by_id[_jsonrpc_id_key(payload["id"])] = thread_id
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            return

        if isinstance(method, str):
            params = payload.get("params")
            if method == "serverRequest/resolved" and isinstance(params, dict):
                request_id = params.get("requestId")
                with self._lock:
                    self._pending_server_request_thread_by_id.pop(_jsonrpc_id_key(request_id), None)
            thread_id = _payload_thread_id(payload)
            if thread_id:
                if method in {"turn/completed", "thread/closed"}:
                    self._lease_store.release(thread_id, self._holder)
                    self._forget_owned_thread(thread_id)
                elif method == "thread/status/changed" and _thread_became_non_active(payload):
                    self._lease_store.release(thread_id, self._holder)
                    self._forget_owned_thread(thread_id)
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            return

        response_id = payload.get("id")
        if response_id not in (None, ""):
            with self._lock:
                request_context = self._pending_client_request_by_id.pop(_jsonrpc_id_key(response_id), None)
            if request_context is not None:
                request_method, thread_id, acquired = request_context
                if request_method == "turn/start" and acquired and "error" in payload:
                    self._lease_store.release(thread_id, self._holder)
                    self._forget_owned_thread(thread_id)
        client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))


def _close_quietly(ws: Any) -> None:
    try:
        ws.close()
    except Exception:
        pass


def _relay_messages(
    source_ws: Any,
    target_ws: Any,
    *,
    transform: Callable[[str | bytes], str | bytes] | None = None,
) -> None:
    try:
        for message in source_ws:
            payload = transform(message) if transform is not None else message
            try:
                target_ws.send(payload)
            except ConnectionClosed:
                break
    except ConnectionClosed:
        pass


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_proxy(
    *,
    backend_url: str,
    cwd: str,
    data_dir: str | pathlib.Path | None = None,
    listen_host: str = "127.0.0.1",
    listen_port: int = 0,
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    parent_pid: int | None = None,
    on_listen: Callable[[str], None] | None = None,
) -> None:
    server_ref: dict[str, Any] = {}
    shutdown_once = threading.Event()
    state_lock = threading.Lock()
    active_connections = 0
    idle_deadline = 0.0

    def _shutdown_server() -> None:
        if shutdown_once.is_set():
            return
        shutdown_once.set()
        server = server_ref.get("server")
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _arm_idle_shutdown() -> None:
        nonlocal idle_deadline
        with state_lock:
            idle_deadline = time.monotonic() + max(0.0, idle_timeout_seconds)

    def _cancel_idle_shutdown() -> None:
        nonlocal idle_deadline
        with state_lock:
            idle_deadline = 0.0

    def _wait_until_idle_deadline() -> None:
        while not shutdown_once.is_set():
            with state_lock:
                current_connections = active_connections
                deadline = idle_deadline
            if current_connections > 0 or deadline <= 0.0:
                time.sleep(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.05))
                continue
            with state_lock:
                if active_connections == 0 and idle_deadline == deadline:
                    _shutdown_server()
                    return

    def _wait_until_parent_exit() -> None:
        if parent_pid is None:
            return
        while not shutdown_once.is_set():
            if not _process_exists(parent_pid):
                _shutdown_server()
                return
            time.sleep(0.25)

    def _handler(client_ws: Any) -> None:
        nonlocal active_connections
        with state_lock:
            active_connections += 1
        _cancel_idle_shutdown()
        try:
            with connect(backend_url, max_size=None, proxy=None) as backend_ws:
                holder_pid = parent_pid or os.getpid()
                gate = _ProxyInteractionGate(
                    cwd=cwd,
                    data_dir=pathlib.Path(data_dir or os.environ.get("FC_DATA_DIR") or "."),
                    holder_pid=holder_pid,
                )

                def _backend_to_client() -> None:
                    try:
                        for backend_message in backend_ws:
                            gate.handle_backend_message(
                                backend_message,
                                client_ws=client_ws,
                                backend_ws=backend_ws,
                            )
                    finally:
                        _close_quietly(client_ws)
                        _close_quietly(backend_ws)

                thread = threading.Thread(target=_backend_to_client, daemon=True)
                thread.start()
                try:
                    for client_message in client_ws:
                        gate.handle_client_message(
                            client_message,
                            client_ws=client_ws,
                            backend_ws=backend_ws,
                        )
                finally:
                    _close_quietly(backend_ws)
                    _close_quietly(client_ws)
                    thread.join(timeout=1)
                    gate.close()
        finally:
            with state_lock:
                active_connections = max(0, active_connections - 1)
                should_arm_idle = active_connections == 0
            if should_arm_idle:
                _arm_idle_shutdown()

    with serve(_handler, listen_host, listen_port, max_size=None) as server:
        server_ref["server"] = server
        actual_port = server.socket.getsockname()[1]
        listen_url = f"ws://{listen_host}:{actual_port}"
        if on_listen is not None:
            on_listen(listen_url)
        else:
            print(listen_url, flush=True)
        if parent_pid is None:
            _arm_idle_shutdown()
            threading.Thread(target=_wait_until_idle_deadline, daemon=True).start()
        else:
            threading.Thread(target=_wait_until_parent_exit, daemon=True).start()
        server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fcodex local cwd proxy")
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)
    run_proxy(
        backend_url=args.backend_url,
        cwd=args.cwd,
        data_dir=args.data_dir or None,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        parent_pid=args.parent_pid or None,
    )


if __name__ == "__main__":
    main()
