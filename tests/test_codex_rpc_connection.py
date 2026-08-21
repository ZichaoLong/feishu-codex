import json
import os
import tempfile
import threading
import time
import unittest
from websockets.exceptions import ConnectionClosedOK
from unittest.mock import Mock, patch
from io import StringIO
from pathlib import Path

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.codex_protocol.connection import (
    _CONNECTION_DISCONNECTED,
    _CONNECTION_HANDSHAKING,
    _CONNECTION_READY,
    AppServerEndpointMode,
    CodexRpcConnection as RpcConnection,
    CodexRpcConnectionGenerationMismatchError as GenerationMismatchError,
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.local_websocket_auth import (
    AppServerWebsocketAuthTokenStore,
    MissingAppServerWebsocketAuthTokenError,
)
from bot.stores.app_server_runtime_store import (
    AppServerRuntimeStore,
)
from bot.version import __version__


class CodexRpcConnectionTests(unittest.TestCase):
    @staticmethod
    def _mark_ready(client: RpcConnection, ws: object, *, generation: int = 1) -> None:
        client._ws = ws
        if client._endpoint_mode is AppServerEndpointMode.OWNED_PROCESS:
            process = Mock()
            process.poll.return_value = None
            assert client._managed_process is not None
            client._managed_process._process = process
            client._managed_process._process_reaped = False
        client._connection_generation = generation
        client._connection_state = _CONNECTION_READY
        client._closing = False

    def test_request_omits_params_for_parameterless_method_on_wire(self) -> None:
        sent_payloads: list[dict] = []
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        client._connection_generation = 1

        class _Ws:
            def send(self, payload: str) -> None:
                request = json.loads(payload)
                sent_payloads.append(request)
                client._dispatch_payload(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {"requirements": None},
                    },
                    connection_generation=1,
                )

        self._mark_ready(client, _Ws())
        client.start = lambda: None  # type: ignore[method-assign]

        result = client.request("configRequirements/read", None)

        self.assertEqual(result, {"requirements": None})
        self.assertEqual(
            sent_payloads,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "configRequirements/read",
                }
            ],
        )

    def test_endpoint_mode_rejects_public_deployment_vocabulary(self) -> None:
        with self.assertRaises(ValueError):
            RpcConnection(endpoint_mode="remote")  # type: ignore[arg-type]

    def test_start_initializes_with_experimental_api(self) -> None:
        client = RpcConnection()
        captured: list[tuple[str, dict, float | None]] = []
        sent_notifications: list[dict] = []

        class _Ws:
            def send(self, payload: str) -> None:
                sent_notifications.append(json.loads(payload))

        class _Proc:
            def poll(self) -> None:
                return None

        def fake_start_locked() -> None:
            client._ws = _Ws()
            assert client._managed_process is not None
            client._managed_process._process = _Proc()
            client._managed_process._process_reaped = False
            client._connection_generation = 1

        def fake_request(
            method: str, params: dict | None = None, *, timeout: float | None = None
        ) -> dict:
            captured.append((method, params or {}, timeout))
            return {
                "userAgent": "codex_cli_rs/0.146.0",
                "codexHome": "/tmp/codex-home",
                "platformFamily": "unix",
                "platformOs": "linux",
            }

        with patch.object(client, "_start_locked", fake_start_locked):
            with patch.object(client, "request", fake_request):
                client.start()

        self.assertEqual(
            captured,
            [
                (
                    "initialize",
                    {
                        "clientInfo": {"name": "focus", "version": __version__},
                        "capabilities": {"experimentalApi": True},
                    },
                    client._connect_timeout_seconds,
                )
            ],
        )
        self.assertEqual(
            sent_notifications,
            [{"jsonrpc": "2.0", "method": "initialized"}],
        )

    def test_connect_ws_disables_default_frame_limit(self) -> None:
        client = RpcConnection(connect_timeout_seconds=0.1)
        assert client._managed_process is not None
        client._managed_process._active_url = "ws://127.0.0.1:12345"

        class _Proc:
            def poll(self):
                return None

        assert client._managed_process is not None
        client._managed_process._process = _Proc()
        client._managed_process._process_reaped = False

        with patch(
            "bot.codex_protocol.connection.connect", return_value="ws-obj"
        ) as mock_connect:
            client._connect_ws_locked()

        self.assertEqual(client._ws, "ws-obj")
        _, kwargs = mock_connect.call_args
        self.assertEqual(kwargs["open_timeout"], client._connect_timeout_seconds)
        self.assertIsNone(kwargs["max_size"])
        self.assertIsNone(kwargs["proxy"])

    def test_connect_ws_uses_bearer_auth_for_attached_endpoint_when_token_file_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            token = AppServerWebsocketAuthTokenStore(data_dir).ensure()
            client = RpcConnection(
                endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
                app_server_url="ws://127.0.0.1:12345",
                app_server_data_dir=data_dir,
                connect_timeout_seconds=0.1,
            )

            with patch(
                "bot.codex_protocol.connection.connect", return_value="ws-obj"
            ) as mock_connect:
                client._connect_ws_locked()

        self.assertEqual(client._ws, "ws-obj")
        _, kwargs = mock_connect.call_args
        self.assertEqual(
            kwargs["additional_headers"], {"Authorization": f"Bearer {token}"}
        )

    def test_connect_ws_fails_immediately_for_attached_endpoint_when_token_file_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            client = RpcConnection(
                endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
                app_server_url="ws://127.0.0.1:12345",
                app_server_data_dir=data_dir,
                connect_timeout_seconds=5.0,
            )

            with patch("bot.codex_protocol.connection.time.sleep") as mock_sleep:
                with patch(
                    "bot.codex_protocol.connection.connect", return_value="ws-obj"
                ) as mock_connect:
                    with self.assertRaisesRegex(
                        MissingAppServerWebsocketAuthTokenError,
                        "backend websocket auth token 不存在",
                    ):
                        client._connect_ws_locked()

        mock_connect.assert_not_called()
        mock_sleep.assert_not_called()

    def test_start_locked_reuses_existing_managed_process(self) -> None:
        client = RpcConnection()

        class _Proc:
            def poll(self):
                return None

        class _ThreadStub:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                return None

        assert client._managed_process is not None
        client._managed_process._process = _Proc()
        client._managed_process._process_reaped = False

        with patch.object(
            client, "_connect_ws_locked", lambda: setattr(client, "_ws", object())
        ):
            with patch.object(client._managed_process, "launch") as mock_launch:
                with patch(
                    "bot.codex_protocol.connection.threading.Thread", _ThreadStub
                ):
                    client._start_locked()

        mock_launch.assert_not_called()
        self.assertIsNotNone(client._ws)

    def test_start_locked_falls_back_to_free_port_when_default_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_url = "ws://127.0.0.1:43210"
            store = AppServerRuntimeStore(Path(tmpdir))
            client = RpcConnection(
                app_server_runtime_store=store,
                managed_startup_lock_path=Path(tmpdir) / "startup.lock",
                app_server_data_dir=Path(tmpdir),
            )

            class _Proc:
                pid = os.getpid()
                stdout = StringIO("")
                stderr = StringIO("")
                stdin = StringIO()

                def poll(self):
                    return None

            class _ThreadStub:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def start(self) -> None:
                    return None

            assert client._managed_process is not None
            with patch.object(
                client._managed_process,
                "select_endpoint",
                return_value=fallback_url,
            ):
                with patch.object(
                    client,
                    "_connect_ws_locked",
                    lambda: setattr(client, "_ws", object()),
                ):
                    with patch(
                        "bot.codex_protocol.managed_process.subprocess.Popen",
                        return_value=_Proc(),
                    ) as mock_popen:
                        with patch(
                            "bot.codex_protocol.managed_process.threading.Thread",
                            _ThreadStub,
                        ):
                            client._start_locked()

            self.assertEqual(client._managed_process.active_url, fallback_url)
            launched = mock_popen.call_args.args[0]
            self.assertEqual(launched[launched.index("--listen") + 1], fallback_url)

    def test_start_locked_retries_default_url_when_child_exits_after_connect(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            default_url = "ws://127.0.0.1:8765"
            fallback_url = "ws://127.0.0.1:43210"
            store = AppServerRuntimeStore(Path(tmpdir))
            client = RpcConnection(
                app_server_runtime_store=store,
                managed_startup_lock_path=Path(tmpdir) / "startup.lock",
                app_server_data_dir=Path(tmpdir),
            )

            class _ProcDead:
                pid = os.getpid()
                stdout = StringIO("")
                stderr = StringIO("")
                stdin = StringIO()

                def poll(self):
                    return 1

            class _ProcLive:
                pid = os.getpid()
                stdout = StringIO("")
                stderr = StringIO("")
                stdin = StringIO()

                def poll(self):
                    return None

            class _ThreadStub:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def start(self) -> None:
                    return None

            def _fake_connect() -> None:
                client._ws = Mock()

            assert client._managed_process is not None
            with patch.object(
                client._managed_process,
                "select_endpoint",
                return_value=default_url,
            ):
                with patch.object(
                    client._managed_process,
                    "allocate_retry_endpoint",
                    return_value=fallback_url,
                ):
                    with patch.object(client, "_connect_ws_locked", _fake_connect):
                        with patch(
                            "bot.codex_protocol.managed_process._MANAGED_APP_SERVER_VERIFY_GRACE_SECONDS",
                            0.0,
                        ):
                            with patch.object(
                                store,
                                "_cleanup_receipt_matches",
                                return_value=True,
                            ):
                                with patch(
                                    "bot.codex_protocol.managed_process.subprocess.Popen",
                                    side_effect=[_ProcDead(), _ProcLive()],
                                ) as mock_popen:
                                    with patch(
                                        "bot.codex_protocol.managed_process.threading.Thread",
                                        _ThreadStub,
                                    ):
                                        client._start_locked()

            first_launch = mock_popen.call_args_list[0].args[0]
            second_launch = mock_popen.call_args_list[1].args[0]
            self.assertEqual(
                first_launch[first_launch.index("--listen") + 1], default_url
            )
            self.assertEqual(
                second_launch[second_launch.index("--listen") + 1], fallback_url
            )
            self.assertEqual(client._managed_process.active_url, fallback_url)

    def test_reader_loop_notifies_disconnect_once_for_unexpected_close(self) -> None:
        disconnects: list[int] = []
        client = RpcConnection(on_disconnect=disconnects.append)

        class _Ws:
            def recv(self):
                raise ConnectionClosedOK(None, None)

        client._ws = _Ws()
        client._connection_generation = 7

        client._reader_loop(client._ws, 7)

        self.assertEqual(disconnects, [7])
        self.assertIsNone(client._ws)

    def test_server_request_callback_keeps_receiving_connection_generation(
        self,
    ) -> None:
        received: list[tuple[int, int | str, str, dict]] = []
        received_event = threading.Event()

        def on_request(
            connection_generation: int,
            request_id: int | str,
            method: str,
            params: dict,
        ) -> None:
            received.append((connection_generation, request_id, method, params))
            received_event.set()

        client = RpcConnection(on_request=on_request)
        self._mark_ready(client, object(), generation=7)

        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-1"},
            },
            connection_generation=7,
        )

        self.assertTrue(received_event.wait(timeout=1.0))
        self.assertEqual(
            received,
            [(7, "request-1", "item/tool/requestUserInput", {"threadId": "thread-1"})],
        )

    def test_regular_server_request_runs_inline_in_reader_order(self) -> None:
        events: list[str] = []

        def on_request(
            connection_generation: int,
            request_id: int | str,
            method: str,
            params: dict,
        ) -> None:
            self.assertEqual(
                (connection_generation, request_id, method),
                (7, "request-1", "item/tool/requestUserInput"),
            )
            self.assertEqual(params, {"threadId": "thread-1"})
            events.append("request")

        client = RpcConnection(
            on_request=on_request,
        )
        self._mark_ready(client, object(), generation=7)
        with patch.object(client, "_spawn_detached_callback") as spawn:
            client._dispatch_payload(
                {
                    "jsonrpc": "2.0",
                    "id": "request-1",
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "thread-1"},
                },
                connection_generation=7,
            )

        self.assertEqual(events, ["request"])
        spawn.assert_not_called()

    def test_server_request_is_enqueued_before_following_lifecycle(self) -> None:
        queued: list[tuple[str, str]] = []
        client = RpcConnection(
            on_request=lambda _generation, request_id, _method, _params: (
                queued.append(("request", str(request_id)))
            ),
            on_notification=lambda _generation, method, _params: queued.append(
                ("notification", method)
            ),
        )
        self._mark_ready(client, object(), generation=7)

        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            },
            connection_generation=7,
        )
        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turnId": "turn-1"},
            },
            connection_generation=7,
        )

        self.assertEqual(
            queued,
            [
                ("request", "request-1"),
                ("notification", "turn/completed"),
            ],
        )

    def test_current_time_request_is_answered_without_interaction_routing(self) -> None:
        sent: list[dict] = []
        sent_event = threading.Event()
        requests: list[tuple] = []

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(json.loads(payload))
                sent_event.set()

        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            on_request=lambda *args: requests.append(args),
        )
        self._mark_ready(client, _Ws(), generation=7)

        with patch("bot.interaction_contract.time.time", return_value=1_781_717_655.9):
            client._dispatch_payload(
                {
                    "jsonrpc": "2.0",
                    "id": "clock-1",
                    "method": "currentTime/read",
                    "params": {"threadId": "thread-1"},
                },
                connection_generation=7,
            )

        self.assertTrue(sent_event.wait(timeout=1.0))
        self.assertEqual(
            sent,
            [
                {
                    "jsonrpc": "2.0",
                    "id": "clock-1",
                    "result": {"currentTimeAt": 1_781_717_655},
                }
            ],
        )
        self.assertEqual(requests, [])
        self.assertEqual(
            client._server_request_authority.remembered_request_count(),
            0,
        )
        with self.assertRaises(CodexRpcPreSendError):
            client.respond(
                "clock-1",
                expected_connection_generation=7,
                require_existing_connection=True,
            )

    def test_malformed_current_time_request_returns_invalid_params_without_fence(
        self,
    ) -> None:
        sent: list[dict] = []
        sent_event = threading.Event()

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(json.loads(payload))
                sent_event.set()

        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
        )
        self._mark_ready(client, _Ws(), generation=7)
        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "clock-bad",
                "method": "currentTime/read",
                "params": [],
            },
            connection_generation=7,
        )

        self.assertTrue(sent_event.wait(timeout=1.0))
        self.assertEqual(sent[0]["id"], "clock-bad")
        self.assertEqual(sent[0]["error"]["code"], -32602)

    def test_malformed_server_request_params_are_not_dispatched(self) -> None:
        requests: list[tuple] = []
        client = RpcConnection(
            on_request=lambda *args: requests.append(args),
        )
        self._mark_ready(client, object(), generation=7)

        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "request-malformed",
                "method": "item/tool/requestUserInput",
                "params": [],
            },
            connection_generation=7,
        )

        self.assertEqual(requests, [])

    def test_server_callbacks_are_dropped_after_their_connection_disconnects(
        self,
    ) -> None:
        notifications: list[tuple[int, str, dict]] = []
        requests: list[tuple[int, int | str, str, dict]] = []
        client = RpcConnection(
            on_notification=lambda generation, method, params: notifications.append(
                (generation, method, params)
            ),
            on_request=lambda generation, request_id, method, params: requests.append(
                (generation, request_id, method, params)
            ),
        )
        client._connection_generation = 7
        client._ws = object()

        # Simulate a request thread that begins after reader_loop has already
        # detached this websocket and enqueued its disconnect callback.
        client._ws = None
        client._safe_on_notification(7, "turn/started", {"threadId": "thread-1"})
        client._safe_on_request(
            7,
            "request-old",
            "item/tool/requestUserInput",
            {"threadId": "thread-1"},
        )

        self.assertEqual(notifications, [])
        self.assertEqual(requests, [])

    def test_request_raises_transport_error_when_send_fails(self) -> None:
        client = RpcConnection()

        class _Ws:
            def send(self, payload: str) -> None:
                del payload
                raise BrokenPipeError("closed")

        self._mark_ready(client, _Ws())
        client.start = lambda: None  # type: ignore[method-assign]

        with self.assertRaises(CodexRpcTransportError) as caught:
            client.request("thread/delete", {"threadId": "thread-1"})

        self.assertIsInstance(caught.exception, CodexRpcError)
        self.assertEqual(client._pending, {})

    def test_request_reports_serialization_failure_as_local_error_before_send(
        self,
    ) -> None:
        client = RpcConnection()
        sent_payloads: list[str] = []

        class _Ws:
            def send(self, payload: str) -> None:
                sent_payloads.append(payload)

        self._mark_ready(client, _Ws())
        client.start = lambda: None  # type: ignore[method-assign]

        with self.assertRaises(TypeError):
            client.request("thread/start", {"cwd": object()})

        self.assertEqual(sent_payloads, [])
        self.assertEqual(client._pending, {})

    def test_request_wraps_startup_transport_failure_as_pre_send_error(self) -> None:
        client = RpcConnection()
        client.start = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            CodexRpcTransportError("initialize", {"message": "initialize disconnected"})
        )

        with self.assertRaises(CodexRpcPreSendError) as caught:
            client.request("thread/archive", {"threadId": "thread-1"})

        self.assertIn("initialize disconnected", str(caught.exception))
        self.assertEqual(client._pending, {})

    def test_request_treats_missing_websocket_before_send_as_pre_send_error(
        self,
    ) -> None:
        client = RpcConnection()
        client.start = lambda: None  # type: ignore[method-assign]
        client._ws = None

        with self.assertRaises(CodexRpcPreSendError):
            client.request("thread/archive", {"threadId": "thread-1"})

        self.assertEqual(client._pending, {})

    def test_bounded_request_never_starts_a_missing_connection(self) -> None:
        client = RpcConnection()
        start_calls: list[bool] = []
        client.start = lambda: start_calls.append(True)  # type: ignore[method-assign]

        with self.assertRaises(CodexRpcPreSendError):
            client.request(
                "thread/read",
                {"threadId": "thread-1"},
                timeout=0.01,
                require_existing_connection=True,
            )

        self.assertEqual(start_calls, [])
        self.assertEqual(client._pending, {})

    def test_bounded_request_does_not_wait_for_client_connection_lock(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        client._ws = object()
        entered = threading.Event()
        release = threading.Event()

        def hold_client_lock() -> None:
            with client._lock:
                entered.set()
                release.wait(timeout=1.0)

        holder = threading.Thread(target=hold_client_lock)
        holder.start()
        self.assertTrue(entered.wait(timeout=1.0))
        started_at = time.monotonic()
        try:
            with self.assertRaises(CodexRpcPreSendError):
                client.request(
                    "thread/read",
                    {"threadId": "thread-1"},
                    timeout=0.03,
                    require_existing_connection=True,
                )
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertLess(time.monotonic() - started_at, 0.3)
        self.assertEqual(client._pending, {})

    def test_bounded_request_does_not_wait_for_websocket_send_lock(self) -> None:
        sent: list[str] = []
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(payload)

        self._mark_ready(client, _Ws())
        entered = threading.Event()
        release = threading.Event()

        def hold_send_lock() -> None:
            with client._send_lock:
                entered.set()
                release.wait(timeout=1.0)

        holder = threading.Thread(target=hold_send_lock)
        holder.start()
        self.assertTrue(entered.wait(timeout=1.0))
        started_at = time.monotonic()
        try:
            with self.assertRaises(CodexRpcPreSendError):
                client.request(
                    "thread/read",
                    {"threadId": "thread-1"},
                    timeout=0.03,
                    require_existing_connection=True,
                )
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertLess(time.monotonic() - started_at, 0.3)
        self.assertEqual(sent, [])
        self.assertEqual(client._pending, {})

    def test_bounded_response_does_not_wait_for_websocket_send_lock_or_reconnect(
        self,
    ) -> None:
        sent: list[str] = []
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(payload)

        self._mark_ready(client, _Ws())
        client._server_request_authority.remember("request-1", 1)
        start_calls: list[bool] = []
        client.start = lambda: start_calls.append(True)  # type: ignore[method-assign]
        entered = threading.Event()
        release = threading.Event()

        def hold_send_lock() -> None:
            with client._send_lock:
                entered.set()
                release.wait(timeout=1.0)

        holder = threading.Thread(target=hold_send_lock)
        holder.start()
        self.assertTrue(entered.wait(timeout=1.0))
        started_at = time.monotonic()
        try:
            with self.assertRaises(CodexRpcPreSendError):
                client.respond(
                    "request-1",
                    error={"code": -32000, "message": "cancelled"},
                    timeout=0.03,
                    require_existing_connection=True,
                    expected_connection_generation=1,
                )
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertLess(time.monotonic() - started_at, 0.3)
        self.assertEqual(sent, [])
        self.assertEqual(start_calls, [])

    def test_websocket_send_timeout_after_entering_send_is_transport_unknown(
        self,
    ) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)

        class _Ws:
            def send(self, payload: str) -> None:
                del payload
                raise TimeoutError("socket write timed out")

        self._mark_ready(client, _Ws())
        client._server_request_authority.remember("request-1", 1)

        with self.assertRaises(CodexRpcTransportError):
            client.request(
                "thread/read",
                {"threadId": "thread-1"},
                timeout=0.2,
                require_existing_connection=True,
            )
        with self.assertRaises(CodexRpcTransportError):
            client.respond(
                "request-1",
                error={"code": -32000, "message": "cancelled"},
                timeout=0.2,
                require_existing_connection=True,
                expected_connection_generation=1,
            )
        self.assertEqual(
            client._server_request_authority.remembered_request_count(),
            0,
        )
        with self.assertRaises(CodexRpcPreSendError):
            client.respond(
                "request-1",
                error={"code": -32000, "message": "duplicate"},
                timeout=0.2,
                require_existing_connection=True,
                expected_connection_generation=1,
            )

    def test_bounded_connection_generation_does_not_wait_for_client_lock(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        client._ws = object()
        entered = threading.Event()
        release = threading.Event()

        def hold_client_lock() -> None:
            with client._lock:
                entered.set()
                release.wait(timeout=1.0)

        holder = threading.Thread(target=hold_client_lock)
        holder.start()
        self.assertTrue(entered.wait(timeout=1.0))
        started_at = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                client.connection_generation(
                    timeout=0.03,
                    require_existing_connection=True,
                )
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertLess(time.monotonic() - started_at, 0.3)

    def test_generation_fenced_request_never_sends_on_a_reconnected_websocket(
        self,
    ) -> None:
        """The request checks the generation again at the actual send path."""

        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        old_sent: list[str] = []
        replacement_sent: list[str] = []

        class _OldWs:
            def send(self, payload: str) -> None:
                old_sent.append(payload)

        class _ReplacementWs:
            def send(self, payload: str) -> None:
                replacement_sent.append(payload)

        self._mark_ready(client, _OldWs(), generation=7)
        original_register_pending = client._register_pending

        def register_then_reconnect(*, deadline_monotonic=None):
            registered = original_register_pending(
                deadline_monotonic=deadline_monotonic
            )
            with client._lock:
                client._ws = _ReplacementWs()
                client._connection_generation = 8
            return registered

        client._register_pending = register_then_reconnect  # type: ignore[method-assign]

        with self.assertRaises(GenerationMismatchError) as caught:
            client.request(
                "turn/interrupt",
                {"threadId": "thread-1", "turnId": "turn-1"},
                timeout=0.2,
                require_existing_connection=True,
                expected_connection_generation=7,
            )

        self.assertEqual(caught.exception.expected_generation, 7)
        self.assertEqual(caught.exception.observed_generation, 8)
        self.assertEqual(old_sent, [])
        self.assertEqual(replacement_sent, [])
        self.assertEqual(client._pending, {})

    def test_generation_fenced_send_holds_identity_lock_through_socket_write(
        self,
    ) -> None:
        """A reconnect cannot slip between the checked generation and send()."""

        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        client._connection_generation = 7
        entered_send = threading.Event()
        release_send = threading.Event()
        reconnect_attempted = threading.Event()
        replacement_applied = threading.Event()

        class _BlockingWs:
            def send(self, payload: str) -> None:
                del payload
                entered_send.set()
                release_send.wait(timeout=1.0)

        client._ws = _BlockingWs()

        sender = threading.Thread(
            target=client._send_serialized_json,
            args=('{"jsonrpc":"2.0"}',),
            kwargs={"expected_connection_generation": 7},
        )

        def reconnect() -> None:
            entered_send.wait(timeout=1.0)
            reconnect_attempted.set()
            with client._lock:
                client._connection_generation = 8
                client._ws = object()
            replacement_applied.set()

        replacer = threading.Thread(target=reconnect)
        sender.start()
        replacer.start()
        self.assertTrue(entered_send.wait(timeout=1.0))
        self.assertTrue(reconnect_attempted.wait(timeout=1.0))
        self.assertFalse(replacement_applied.wait(timeout=0.05))
        release_send.set()
        sender.join(timeout=1.0)
        replacer.join(timeout=1.0)

        self.assertFalse(sender.is_alive())
        self.assertFalse(replacer.is_alive())
        self.assertTrue(replacement_applied.is_set())

    def test_stale_epoch_guard_rejects_before_websocket_send(self) -> None:
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        permit = gate.issue_outbound_request("turn/start")
        gate.fence_backend_reset()
        gate.begin_backend_reset()
        gate.admit_backend_replacement(1, publish_replacement=lambda: None)

        sent: list[str] = []

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(payload)

        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        self._mark_ready(client, _Ws(), generation=1)

        with self.assertRaises(CodexRpcPreSendError):
            client.request(
                "turn/start",
                {"threadId": "thread-1", "input": []},
                timeout=0.2,
                require_existing_connection=True,
                outbound_transport_guard=lambda: gate.guard_outbound_send(permit),
            )

        self.assertEqual(sent, [])
        self.assertEqual(client._pending, {})

    def test_physical_disconnect_fences_epoch_before_runtime_callback(self) -> None:
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))
        callbacks: list[int] = []
        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            on_disconnect_ingress=gate.fence_disconnect,
            on_disconnect=callbacks.append,
        )

        class _Ws:
            @staticmethod
            def recv():
                return None

        ws = _Ws()
        self._mark_ready(client, ws, generation=1)

        client._reader_loop(ws, 1)

        self.assertEqual(callbacks, [1])
        self.assertTrue(gate.snapshot().disconnect_cleanup_pending)
        with self.assertRaisesRegex(RuntimeError, "epoch is closed"):
            gate.issue_outbound_request("thread/list")
        self.assertTrue(gate.observe_disconnect(1))

    def test_request_rejects_malformed_json_rpc_error_envelopes(self) -> None:
        malformed_errors = [
            "text",
            [],
            None,
            {"code": "-32000", "message": "bad code"},
            {"code": -32000, "message": ["bad message"]},
        ]

        for malformed_error in malformed_errors:
            with self.subTest(error=malformed_error):
                client = RpcConnection()
                client.start = lambda: None  # type: ignore[method-assign]

                class _Ws:
                    def send(self, payload: str) -> None:
                        request_id = json.loads(payload)["id"]
                        client._dispatch_payload(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "error": malformed_error,
                            },
                            connection_generation=1,
                        )

                self._mark_ready(client, _Ws())

                with self.assertRaises(CodexRpcProtocolError):
                    client.request("thread/delete", {"threadId": "thread-1"})

    def test_request_rejects_ambiguous_json_rpc_response_envelope(self) -> None:
        client = RpcConnection()
        client.start = lambda: None  # type: ignore[method-assign]

        class _Ws:
            def send(self, payload: str) -> None:
                request_id = json.loads(payload)["id"]
                client._dispatch_payload(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {},
                        "error": {"code": -32000, "message": "ambiguous"},
                    },
                    connection_generation=1,
                )

        self._mark_ready(client, _Ws())

        with self.assertRaises(CodexRpcProtocolError):
            client.request("thread/archive", {"threadId": "thread-1"})

    def test_concurrent_request_waits_for_full_initial_handshake(self) -> None:
        requirements_entered = threading.Event()
        release_requirements = threading.Event()
        sent: list[tuple[int, str]] = []
        notifications: list[str] = []
        failures: list[Exception] = []
        request_results: list[dict] = []

        def on_initialized(connection_generation: int, _identity: dict) -> None:
            requirements = client.request(
                "configRequirements/read",
                None,
                timeout=1.0,
            )
            self.assertEqual(requirements, {"requirements": None})
            self.assertEqual(connection_generation, 1)
            requirements_entered.set()
            release_requirements.wait(timeout=1.0)

        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            connect_timeout_seconds=1.0,
            request_timeout_seconds=1.0,
            on_initialized=on_initialized,
            on_notification=lambda _generation, method, _params: notifications.append(
                method
            ),
        )

        class _Ws:
            def send(self, payload: str) -> None:
                envelope = json.loads(payload)
                method = envelope["method"]
                sent.append((1, method))
                if "id" not in envelope:
                    return
                result = {
                    "initialize": {"userAgent": "codex/test"},
                    "configRequirements/read": {"requirements": None},
                    "thread/list": {"data": []},
                }[method]
                client._dispatch_payload(
                    {"jsonrpc": "2.0", "id": envelope["id"], "result": result},
                    connection_generation=1,
                )
                if method == "initialize":
                    client._dispatch_payload(
                        {
                            "jsonrpc": "2.0",
                            "method": "configWarning",
                            "params": {"summary": "test warning"},
                        },
                        connection_generation=1,
                    )

            def close(self) -> None:
                return None

        def fake_start_locked() -> None:
            client._closing = False
            client._ws = _Ws()
            client._connection_generation = 1

        def run_start() -> None:
            try:
                client.start()
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def run_request() -> None:
            try:
                request_results.append(client.request("thread/list", None, timeout=1.0))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with patch.object(client, "_start_locked", fake_start_locked):
            starter = threading.Thread(target=run_start)
            starter.start()
            self.assertTrue(requirements_entered.wait(timeout=1.0))

            requester = threading.Thread(target=run_request)
            requester.start()
            requester.join(timeout=0.05)
            self.assertTrue(requester.is_alive())
            self.assertNotIn((1, "thread/list"), sent)
            self.assertEqual(notifications, [])

            release_requirements.set()
            starter.join(timeout=1.0)
            requester.join(timeout=1.0)

        self.assertFalse(starter.is_alive())
        self.assertFalse(requester.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(request_results, [{"data": []}])
        self.assertEqual(notifications, ["configWarning"])
        self.assertEqual(
            [method for _generation, method in sent],
            ["initialize", "initialized", "configRequirements/read", "thread/list"],
        )

    def test_concurrent_request_waits_for_full_reconnect_handshake(self) -> None:
        second_requirements_entered = threading.Event()
        release_second_requirements = threading.Event()
        sent: list[tuple[int, str]] = []
        failures: list[Exception] = []

        def on_initialized(generation: int, _identity: dict) -> None:
            if generation != 2:
                return
            second_requirements_entered.set()
            release_second_requirements.wait(timeout=1.0)

        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            connect_timeout_seconds=1.0,
            request_timeout_seconds=1.0,
            on_initialized=on_initialized,
        )

        class _Ws:
            def __init__(self, generation: int) -> None:
                self.generation = generation

            def send(self, payload: str) -> None:
                envelope = json.loads(payload)
                method = envelope["method"]
                sent.append((self.generation, method))
                if "id" not in envelope:
                    return
                result = (
                    {"userAgent": f"codex/test-{self.generation}"}
                    if method == "initialize"
                    else {"data": []}
                )
                client._dispatch_payload(
                    {"jsonrpc": "2.0", "id": envelope["id"], "result": result},
                    connection_generation=self.generation,
                )

            def close(self) -> None:
                return None

        def fake_start_locked() -> None:
            client._closing = False
            generation = client._connection_generation + 1
            client._ws = _Ws(generation)
            client._connection_generation = generation

        def capture(callable_) -> None:
            try:
                callable_()
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with patch.object(client, "_start_locked", fake_start_locked):
            client.start()
            client.stop()

            starter = threading.Thread(target=lambda: capture(client.start))
            starter.start()
            self.assertTrue(second_requirements_entered.wait(timeout=1.0))

            requester = threading.Thread(
                target=lambda: capture(
                    lambda: client.request("thread/list", None, timeout=1.0)
                )
            )
            requester.start()
            requester.join(timeout=0.05)
            self.assertTrue(requester.is_alive())
            self.assertNotIn((2, "thread/list"), sent)

            release_second_requirements.set()
            starter.join(timeout=1.0)
            requester.join(timeout=1.0)

        self.assertFalse(starter.is_alive())
        self.assertFalse(requester.is_alive())
        self.assertEqual(failures, [])
        self.assertIn((2, "thread/list"), sent)

    def test_stop_during_handshake_wakes_owner_and_waiter_without_ordinary_send(
        self,
    ) -> None:
        initialize_sent = threading.Event()
        sent_methods: list[str] = []
        failures: list[Exception] = []
        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            connect_timeout_seconds=1.0,
            request_timeout_seconds=1.0,
        )

        class _Ws:
            def send(self, payload: str) -> None:
                method = json.loads(payload)["method"]
                sent_methods.append(method)
                if method == "initialize":
                    initialize_sent.set()

            def close(self) -> None:
                return None

        def fake_start_locked() -> None:
            client._closing = False
            client._ws = _Ws()
            client._connection_generation = 1

        def capture(callable_) -> None:
            try:
                callable_()
            except Exception as exc:
                failures.append(exc)

        with patch.object(client, "_start_locked", fake_start_locked):
            owner = threading.Thread(target=lambda: capture(client.start))
            owner.start()
            self.assertTrue(initialize_sent.wait(timeout=1.0))
            waiter = threading.Thread(
                target=lambda: capture(
                    lambda: client.request("thread/list", None, timeout=1.0)
                )
            )
            waiter.start()
            time.sleep(0.02)
            client.stop()
            owner.join(timeout=1.0)
            waiter.join(timeout=1.0)

        self.assertFalse(owner.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(len(failures), 2)
        self.assertTrue(
            all(
                isinstance(exc, (CodexRpcPreSendError, CodexRpcTransportError))
                for exc in failures
            )
        )
        self.assertNotIn("thread/list", sent_methods)

    def test_server_request_before_ready_fails_handshake_closed(self) -> None:
        routed_requests: list[tuple] = []
        client = RpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            connect_timeout_seconds=1.0,
            request_timeout_seconds=1.0,
            on_request=lambda *args: routed_requests.append(args),
        )

        class _Ws:
            def __init__(self) -> None:
                self.closed = False

            def send(self, payload: str) -> None:
                envelope = json.loads(payload)
                if envelope["method"] != "initialize":
                    return
                client._dispatch_payload(
                    {
                        "jsonrpc": "2.0",
                        "id": "malicious-request",
                        "method": "item/tool/requestUserInput",
                        "params": {"threadId": "thread-1"},
                    },
                    connection_generation=1,
                )

            def close(self) -> None:
                self.closed = True

        websocket = _Ws()

        def fake_start_locked() -> None:
            client._closing = False
            client._ws = websocket
            client._connection_generation = 1

        with patch.object(client, "_start_locked", fake_start_locked):
            with self.assertRaises(CodexRpcProtocolError):
                client.start()

        self.assertEqual(routed_requests, [])
        self.assertTrue(websocket.closed)
        self.assertIsNone(client._ws)

    def test_partial_startup_failure_closes_created_transport(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        websocket = Mock()

        def fail_after_transport_creation() -> None:
            client._closing = False
            client._ws = websocket
            client._connection_generation = 1
            raise RuntimeError("runtime state recording failed")

        with patch.object(client, "_start_locked", fail_after_transport_creation):
            with self.assertRaisesRegex(RuntimeError, "runtime state recording failed"):
                client.start()

        websocket.close.assert_called_once_with()
        self.assertIsNone(client._ws)
        self.assertEqual(client._connection_state, _CONNECTION_DISCONNECTED)

    def test_stale_handshake_cleanup_cannot_close_newer_transport(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        replacement = Mock()
        client._ws = replacement
        client._connection_generation = 2
        client._connection_state = _CONNECTION_HANDSHAKING
        client._handshake_attempt = 2
        client._handshake_generation = 2
        client._handshake_owner_thread_id = 12345

        client._stop_connection(
            handshake_attempt=1,
            handshake_generation=1,
            handshake_failure=RuntimeError("old handshake failed"),
        )

        self.assertIs(client._ws, replacement)
        self.assertEqual(client._connection_generation, 2)
        self.assertEqual(client._handshake_attempt, 2)
        self.assertEqual(client._connection_state, _CONNECTION_HANDSHAKING)
        replacement.close.assert_not_called()

    def test_ordinary_request_is_not_redirected_to_reconnect_handshake(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        old_sent: list[str] = []
        replacement_sent: list[str] = []

        class _OldWs:
            def send(self, payload: str) -> None:
                old_sent.append(payload)

        class _ReplacementWs:
            def send(self, payload: str) -> None:
                replacement_sent.append(payload)

        self._mark_ready(client, _OldWs(), generation=7)
        original_register_pending = client._register_pending

        def register_then_begin_reconnect(*, deadline_monotonic=None):
            registered = original_register_pending(
                deadline_monotonic=deadline_monotonic
            )
            with client._lock:
                client._ws = _ReplacementWs()
                client._connection_generation = 8
                client._connection_state = _CONNECTION_HANDSHAKING
                client._handshake_attempt = 2
                client._handshake_generation = 8
                client._handshake_owner_thread_id = 12345
            return registered

        client._register_pending = register_then_begin_reconnect  # type: ignore[method-assign]

        with self.assertRaises(GenerationMismatchError):
            client.request("thread/list", None, timeout=0.2)

        self.assertEqual(old_sent, [])
        self.assertEqual(replacement_sent, [])

    def test_response_is_pinned_to_server_request_receiving_generation(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        old_sent: list[dict] = []
        replacement_sent: list[dict] = []

        class _Ws:
            def __init__(self, sent: list[dict]) -> None:
                self.sent = sent

            def send(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

        self._mark_ready(client, _Ws(old_sent), generation=7)
        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-1"},
            },
            connection_generation=7,
        )
        self._mark_ready(client, _Ws(replacement_sent), generation=8)

        with self.assertRaises(GenerationMismatchError):
            client.respond(
                "request-1",
                result={"answers": {}},
                expected_connection_generation=7,
            )

        self.assertEqual(old_sent, [])
        self.assertEqual(replacement_sent, [])

    def test_exact_generation_answers_reused_server_request_id(self) -> None:
        client = RpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        sent: list[str] = []

        class _Ws:
            def send(self, payload: str) -> None:
                sent.append(payload)

        for generation in (7, 8):
            self._mark_ready(client, _Ws(), generation=generation)
            client._dispatch_payload(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "item/tool/requestUserInput",
                    "params": {"threadId": "thread-1"},
                },
                connection_generation=generation,
            )

        client.respond(
            1,
            result={"answers": {}},
            expected_connection_generation=8,
        )

        self.assertEqual(len(sent), 1)
