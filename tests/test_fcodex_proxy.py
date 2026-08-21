import json
import os
import queue
import tempfile
import threading
import time
import unittest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.http11 import Response
from websockets.sync.client import connect
from websockets.sync.server import serve
from unittest.mock import patch
from io import StringIO
from pathlib import Path

from bot.fcodex.proxy import (
    _DEFAULT_IDLE_TIMEOUT_SECONDS,
    _relay_client_messages,
    _relay_messages,
    _rewrite_thread_start_cwd,
    main as fcodex_proxy_main,
    run_proxy,
)
from bot.local_websocket_auth import (
    AppServerWebsocketAuthTokenStore,
)


class FcodexProxyTests(unittest.TestCase):
    @staticmethod
    def _allow_proxy_operation_control(
        _data_dir: Path, method: str, params: dict
    ) -> dict:
        """Minimal service-control double for proxy transport echo tests."""

        if method == "operation/participant-connected":
            return {"connected": True, "state": "connected"}
        if method == "operation/admit":
            return {
                "allowed": True,
                "tracks_response": True,
                "request_token": 1,
            }
        if method == "operation/client-response":
            return {
                "known": True,
                "settled": True,
                "request_token": params.get("request_token"),
            }
        return {"ok": True}

    def test_fcodex_proxy_rejects_service_token_cli_arg(self) -> None:
        stderr = StringIO()
        with patch("bot.fcodex.proxy.sys.stderr", stderr):
            with self.assertRaises(SystemExit) as exc:
                fcodex_proxy_main(
                    [
                        "--backend-url",
                        "ws://127.0.0.1:8765",
                        "--cwd",
                        "/tmp/project",
                        "--service-token",
                        "svc-token",
                    ]
                )

        self.assertEqual(exc.exception.code, 2)
        self.assertIn(
            "unrecognized arguments: --service-token svc-token", stderr.getvalue()
        )

    def test_thread_start_proxy_rewrites_only_missing_cwd_and_preserves_payload(self) -> None:
        params = {
            "model": "gpt-5.4",
            "modelProvider": "openai",
            "serviceTier": "priority",
            "approvalPolicy": "never",
            "approvalsReviewer": "auto_review",
            "sandbox": "danger-full-access",
            "permissions": ":full",
            "config": {"model_reasoning_effort": "high"},
            "baseInstructions": "base override",
            "developerInstructions": "developer override",
            "personality": "pragmatic",
            "futureStartOption": {"enabled": True},
        }
        rewritten = _rewrite_thread_start_cwd(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "thread/start",
                    "params": params,
                }
            ),
            "/tmp/project",
        )

        self.assertEqual(
            json.loads(rewritten),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "thread/start",
                "params": {**params, "cwd": "/tmp/project"},
            },
        )

    def test_thread_start_proxy_keeps_existing_cwd_and_resume_payload_unchanged(self) -> None:
        original_start = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "thread/start",
                "params": {
                    "cwd": "/srv/already-set",
                    "approvalsReviewer": "auto_review",
                    "futureStartOption": True,
                },
            }
        )
        original_resume = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "thread/resume",
                "params": {
                    "threadId": "thread-1",
                    "model": "gpt-5.4",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "auto_review",
                    "baseInstructions": "base override",
                    "developerInstructions": "developer override",
                    "futureResumeOption": {"enabled": True},
                },
            }
        )

        self.assertEqual(
            _rewrite_thread_start_cwd(original_start, "/tmp/project"),
            original_start,
        )
        self.assertEqual(
            _rewrite_thread_start_cwd(original_resume, "/tmp/project"),
            original_resume,
        )

    def test_relay_messages_treats_normal_target_close_as_clean_exit(self) -> None:
        class _Source:
            def __iter__(self):
                return iter(["hello"])

        class _Target:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def send(self, payload: str) -> None:
                self.calls.append(payload)
                raise ConnectionClosedOK(None, None)

        target = _Target()
        _relay_messages(_Source(), target)
        self.assertEqual(target.calls, ["hello"])

    def test_client_relay_treats_peer_close_as_clean_handler_exit(self) -> None:
        class _Source:
            def __iter__(self):
                yield "hello"
                raise ConnectionClosedOK(None, None)

        class _Gate:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object, object]] = []

            def handle_client_message(
                self,
                message: str,
                *,
                client_ws: object,
                backend_ws: object,
            ) -> None:
                self.calls.append((message, client_ws, backend_ws))

        source = _Source()
        backend = object()
        gate = _Gate()
        _relay_client_messages(gate, source, backend)  # type: ignore[arg-type]

        self.assertEqual(gate.calls, [("hello", source, backend)])

    def test_proxy_rejects_unauthorized_websocket_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "instance"
            AppServerWebsocketAuthTokenStore(data_dir).ensure()
            proxy_url_queue: queue.Queue[str] = queue.Queue()
            proxy_thread = threading.Thread(
                target=run_proxy,
                kwargs={
                    "backend_url": "ws://127.0.0.1:8765",
                    "cwd": "/tmp/project",
                    "proxy_auth_token": "proxy-auth-token",
                    "data_dir": data_dir,
                    "idle_timeout_seconds": 0.2,
                    "on_listen": proxy_url_queue.put,
                },
                daemon=True,
            )
            proxy_thread.start()
            proxy_url = proxy_url_queue.get(timeout=1)

            response: Response
            with self.assertRaises(InvalidStatus) as exc:
                connect(proxy_url, open_timeout=1, max_size=None)
            response = exc.exception.args[0]
            self.assertEqual(response.status_code, 401)

            proxy_thread.join(timeout=1)
            self.assertFalse(proxy_thread.is_alive())

    def test_proxy_fails_closed_when_backend_token_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "instance"

            with self.assertRaisesRegex(
                RuntimeError, "backend websocket auth token 不存在"
            ):
                run_proxy(
                    backend_url="ws://127.0.0.1:8765",
                    cwd="/tmp/project",
                    proxy_auth_token="proxy-auth-token",
                    data_dir=data_dir,
                    idle_timeout_seconds=0.1,
                )

    def test_proxy_fails_closed_when_backend_auth_data_dir_is_missing(self) -> None:
        with patch.dict(os.environ, {"FOCUS_DATA_DIR": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "requires instance data dir"):
                run_proxy(
                    backend_url="ws://127.0.0.1:8765",
                    cwd="/tmp/project",
                    proxy_auth_token="proxy-auth-token",
                    data_dir=None,
                    idle_timeout_seconds=0.1,
                )

    def test_proxy_forwards_backend_bearer_auth_from_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "instance"
            backend_token = AppServerWebsocketAuthTokenStore(data_dir).ensure()
            backend_url_queue: queue.Queue[str] = queue.Queue()
            backend_server_ref: dict[str, object] = {}
            backend_auth_headers: queue.Queue[str | None] = queue.Queue()
            backend_requests: queue.Queue[dict] = queue.Queue()

            def _backend_process_request(_connection, request) -> Response | None:
                header = request.headers.get("Authorization")
                backend_auth_headers.put(header)
                if header == f"Bearer {backend_token}":
                    return None
                return Response(
                    401,
                    "Unauthorized",
                    Headers([("Content-Type", "text/plain; charset=utf-8")]),
                    b"missing backend token\n",
                )

            def _backend_handler(ws) -> None:
                for message in ws:
                    request = json.loads(message)
                    backend_requests.put(request)
                    ws.send(
                        json.dumps(
                            {
                                "id": request["id"],
                                "result": {
                                    "approvalsReviewer": "user",
                                    "thread": {
                                        "id": "thread-1",
                                        "status": {"type": "idle"},
                                    },
                                },
                            }
                        )
                    )

            def _backend_main() -> None:
                with serve(
                    _backend_handler,
                    "127.0.0.1",
                    0,
                    max_size=None,
                    process_request=_backend_process_request,
                ) as server:
                    backend_server_ref["server"] = server
                    port = server.socket.getsockname()[1]
                    backend_url_queue.put(f"ws://127.0.0.1:{port}")
                    server.serve_forever()

            backend_thread = threading.Thread(target=_backend_main, daemon=True)
            backend_thread.start()
            backend_url = backend_url_queue.get(timeout=1)

            proxy_url_queue: queue.Queue[str] = queue.Queue()
            proxy_thread = threading.Thread(
                target=run_proxy,
                kwargs={
                    "backend_url": backend_url,
                    "cwd": "/tmp/project",
                    "data_dir": data_dir,
                    "proxy_auth_token": "proxy-auth-token",
                    "idle_timeout_seconds": 0.2,
                    "on_listen": proxy_url_queue.put,
                    "control_request_fn": self._allow_proxy_operation_control,
                },
                daemon=True,
            )
            proxy_thread.start()
            proxy_url = proxy_url_queue.get(timeout=1)

            try:
                with connect(
                    proxy_url,
                    open_timeout=1,
                    max_size=None,
                    additional_headers={"Authorization": "Bearer proxy-auth-token"},
                ) as ws:
                    ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "thread/start",
                                "params": {},
                            }
                        )
                    )
                    response = json.loads(ws.recv())
                    self.assertNotIn("jsonrpc", response)
                    self.assertEqual(response["result"]["thread"]["id"], "thread-1")
                    self.assertEqual(
                        backend_requests.get(timeout=1)["params"]["cwd"], "/tmp/project"
                    )

                self.assertEqual(
                    backend_auth_headers.get(timeout=1),
                    f"Bearer {backend_token}",
                )
                proxy_thread.join(timeout=1)
                self.assertFalse(proxy_thread.is_alive())
            finally:
                backend_server = backend_server_ref.get("server")
                if backend_server is not None:
                    backend_server.shutdown()
                backend_thread.join(timeout=1)

    def test_proxy_stays_alive_across_resume_style_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root_dir = Path(tmpdir)
            data_dir = root_dir / "instance"
            AppServerWebsocketAuthTokenStore(data_dir).ensure()
            global_data_dir = root_dir / "_global"
            backend_url_queue: queue.Queue[str] = queue.Queue()
            backend_server_ref: dict[str, object] = {}
            backend_requests: queue.Queue[dict] = queue.Queue()

            def _backend_handler(ws) -> None:
                for message in ws:
                    request = json.loads(message)
                    backend_requests.put(request)
                    ws.send(
                        json.dumps(
                            {
                                "id": request["id"],
                                "result": {
                                    "approvalsReviewer": "user",
                                    "thread": {
                                        "id": "thread-1",
                                        "status": {"type": "idle"},
                                    },
                                },
                            }
                        )
                    )

            def _backend_main() -> None:
                with serve(_backend_handler, "127.0.0.1", 0, max_size=None) as server:
                    backend_server_ref["server"] = server
                    port = server.socket.getsockname()[1]
                    backend_url_queue.put(f"ws://127.0.0.1:{port}")
                    server.serve_forever()

            backend_thread = threading.Thread(target=_backend_main, daemon=True)
            backend_thread.start()
            backend_url = backend_url_queue.get(timeout=1)

            proxy_url_queue: queue.Queue[str] = queue.Queue()
            proxy_thread = threading.Thread(
                target=run_proxy,
                kwargs={
                    "backend_url": backend_url,
                    "cwd": "/tmp/project",
                    "proxy_auth_token": "proxy-auth-token",
                    "data_dir": data_dir,
                    "global_data_dir": global_data_dir,
                    "idle_timeout_seconds": 0.3,
                    "on_listen": proxy_url_queue.put,
                    "control_request_fn": self._allow_proxy_operation_control,
                },
                daemon=True,
            )
            proxy_thread.start()
            proxy_url = proxy_url_queue.get(timeout=1)

            try:
                with connect(
                    proxy_url,
                    open_timeout=1,
                    max_size=None,
                    additional_headers={"Authorization": "Bearer proxy-auth-token"},
                ) as ws:
                    ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "thread/start",
                                "params": {},
                            }
                        )
                    )
                    response = json.loads(ws.recv())
                    self.assertNotIn("jsonrpc", response)
                    self.assertEqual(response["result"]["thread"]["id"], "thread-1")
                    self.assertEqual(
                        backend_requests.get(timeout=1)["params"]["cwd"], "/tmp/project"
                    )

                time.sleep(0.1)

                with connect(
                    proxy_url,
                    open_timeout=1,
                    max_size=None,
                    additional_headers={"Authorization": "Bearer proxy-auth-token"},
                ) as ws:
                    ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 2,
                                "method": "thread/resume",
                                "params": {"threadId": "thread-1"},
                            }
                        )
                    )
                    response = json.loads(ws.recv())
                    self.assertNotIn("jsonrpc", response)
                    self.assertEqual(response["result"]["thread"]["id"], "thread-1")
                    self.assertNotIn("cwd", backend_requests.get(timeout=1)["params"])

                proxy_thread.join(timeout=1)
                self.assertFalse(proxy_thread.is_alive())
            finally:
                backend_server = backend_server_ref.get("server")
                if backend_server is not None:
                    backend_server.shutdown()
                backend_thread.join(timeout=1)

    def test_proxy_default_idle_timeout_keeps_startup_reconnect_window(self) -> None:
        self.assertGreaterEqual(_DEFAULT_IDLE_TIMEOUT_SECONDS, 30.0)

    def test_proxy_exits_when_parent_process_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "instance"
            AppServerWebsocketAuthTokenStore(data_dir).ensure()
            proxy_url_queue: queue.Queue[str] = queue.Queue()
            with patch(
                "bot.fcodex.proxy.process_exists", return_value=False
            ) as mock_process_exists:
                proxy_thread = threading.Thread(
                    target=run_proxy,
                    kwargs={
                        "backend_url": "ws://127.0.0.1:8765",
                        "cwd": "/tmp/project",
                        "proxy_auth_token": "proxy-auth-token",
                        "data_dir": data_dir,
                        "parent_pid": 4321,
                        "on_listen": proxy_url_queue.put,
                    },
                    daemon=True,
                )
                proxy_thread.start()
                proxy_url = proxy_url_queue.get(timeout=1)
                self.assertTrue(proxy_url.startswith("ws://127.0.0.1:"))
                proxy_thread.join(timeout=1)

            self.assertFalse(proxy_thread.is_alive())
            self.assertEqual(mock_process_exists.call_args_list[0].args, (4321,))

    def test_proxy_parent_pid_mode_still_honors_idle_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "instance"
            AppServerWebsocketAuthTokenStore(data_dir).ensure()
            proxy_url_queue: queue.Queue[str] = queue.Queue()
            with patch("bot.fcodex.proxy.process_exists", return_value=True):
                proxy_thread = threading.Thread(
                    target=run_proxy,
                    kwargs={
                        "backend_url": "ws://127.0.0.1:8765",
                        "cwd": "/tmp/project",
                        "proxy_auth_token": "proxy-auth-token",
                        "data_dir": data_dir,
                        "parent_pid": 4321,
                        "idle_timeout_seconds": 0.1,
                        "on_listen": proxy_url_queue.put,
                    },
                    daemon=True,
                )
                proxy_thread.start()
                proxy_url = proxy_url_queue.get(timeout=1)
                self.assertTrue(proxy_url.startswith("ws://127.0.0.1:"))
                proxy_thread.join(timeout=1)

            self.assertFalse(proxy_thread.is_alive())
