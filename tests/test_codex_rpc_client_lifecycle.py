import json
import threading
import time
import unittest
from unittest.mock import patch

from bot.codex_protocol.connection import (
    _CONNECTION_DISCONNECTED,
    _CONNECTION_READY,
    AppServerEndpointMode,
    CodexRpcConnection,
    CodexRpcStopError,
)


class _ClosableWebsocket:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class CodexRpcConnectionLifecycleTests(unittest.TestCase):
    @staticmethod
    def _set_managed_process(client: CodexRpcConnection, process: object) -> None:
        assert client._managed_process is not None
        client._managed_process._process = process  # type: ignore[assignment]
        client._managed_process._process_reaped = False

    def test_owned_backend_lifecycle_capability_rejects_attached_client(self) -> None:
        owned = CodexRpcConnection(endpoint_mode=AppServerEndpointMode.OWNED_PROCESS)
        attached = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            app_server_url="ws://127.0.0.1:43210",
        )

        owned.require_owned_backend_lifecycle()
        with self.assertRaisesRegex(RuntimeError, "attached endpoint"):
            attached.require_owned_backend_lifecycle()

    @staticmethod
    def _mark_ready(
        client: CodexRpcConnection,
        websocket: object,
        *,
        generation: int = 1,
    ) -> None:
        client._ws = websocket
        client._connection_generation = generation
        client._connection_state = _CONNECTION_READY
        client._closing = False

    def test_current_app_server_url_requires_ready_connection_generation(self) -> None:
        client = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            app_server_url="ws://127.0.0.1:43210",
        )

        self.assertEqual(client.current_app_server_url(), "")

        self._mark_ready(client, object(), generation=7)
        self.assertEqual(client.current_app_server_url(), "ws://127.0.0.1:43210")

        client._closing = True
        self.assertEqual(client.current_app_server_url(), "")

    def test_resolved_notification_retires_exact_transport_authority(self) -> None:
        client = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
        )
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
        self.assertEqual(
            client._server_request_authority.remembered_request_count(),
            1,
        )

        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "method": "serverRequest/resolved",
                "params": {"requestId": "request-1", "threadId": "thread-1"},
            },
            connection_generation=7,
        )

        self.assertEqual(
            client._server_request_authority.remembered_request_count(),
            0,
        )

    def test_current_app_server_url_rejects_exited_owned_guardian(self) -> None:
        class _Guardian:
            def __init__(self) -> None:
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

        client = CodexRpcConnection()
        guardian = _Guardian()
        self._set_managed_process(client, guardian)
        self._mark_ready(client, object(), generation=3)

        self.assertEqual(client.current_app_server_url(), "ws://127.0.0.1:8765")

        guardian.returncode = -9
        self.assertEqual(client.current_app_server_url(), "")

    def test_stop_revokes_published_endpoint_before_close_barrier_completes(self) -> None:
        close_entered = threading.Event()
        release_close = threading.Event()
        stop_errors: list[BaseException] = []
        self.addCleanup(release_close.set)

        class _BlockingCloseWebsocket:
            def close(self) -> None:
                close_entered.set()
                release_close.wait(timeout=2.0)

        client = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            app_server_url="ws://127.0.0.1:43210",
        )
        self._mark_ready(client, _BlockingCloseWebsocket(), generation=9)

        def stop_client() -> None:
            try:
                client.stop(timeout=1.0)
            except BaseException as exc:  # pragma: no cover - asserted below
                stop_errors.append(exc)

        stopper = threading.Thread(target=stop_client)
        stopper.start()
        self.assertTrue(close_entered.wait(timeout=1.0))

        self.assertEqual(client.current_app_server_url(), "")

        release_close.set()
        stopper.join(timeout=1.0)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_errors, [])

    def test_stop_joins_reader_blocked_in_request_callback_before_completing(self) -> None:
        request_entered = threading.Event()
        release_request = threading.Event()
        routed_requests: list[tuple] = []
        self.addCleanup(release_request.set)

        def on_request(*args) -> None:
            routed_requests.append(args)
            request_entered.set()
            release_request.wait(timeout=2.0)

        client = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            on_request=on_request,
        )

        class _ReaderWebsocket(_ClosableWebsocket):
            def __init__(self) -> None:
                super().__init__()
                self._delivered = False

            def recv(self) -> str | None:
                if not self._delivered:
                    self._delivered = True
                    return json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "request-1",
                            "method": "item/tool/requestUserInput",
                            "params": {"threadId": "thread-1"},
                        }
                    )
                self.closed.wait(timeout=2.0)
                return None

        websocket = _ReaderWebsocket()
        self._mark_ready(client, websocket)
        reader = threading.Thread(
            target=client._reader_loop,
            args=(websocket, 1),
            name="test-codex-reader",
            daemon=True,
        )
        client._reader_thread = reader
        client._reader_threads.add(reader)
        reader.start()
        self.assertTrue(request_entered.wait(timeout=1.0))

        with self.assertRaises(CodexRpcStopError) as caught:
            client.stop(timeout=0.02)

        self.assertIn("reader thread", " ".join(caught.exception.pending_resources))
        with self.assertRaises(CodexRpcStopError):
            client.start()

        release_request.set()
        client.stop(timeout=1.0)

        self.assertFalse(reader.is_alive())
        self.assertEqual(len(routed_requests), 1)

    def test_stop_drains_detached_automatic_response(self) -> None:
        callback_entered = threading.Event()
        release_callback = threading.Event()
        self.addCleanup(release_callback.set)

        client = CodexRpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        self._mark_ready(client, _ClosableWebsocket())

        def automatic_response(*_args) -> None:
            callback_entered.set()
            release_callback.wait(timeout=2.0)

        client._safe_automatic_server_request_response = automatic_response  # type: ignore[method-assign]
        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "clock-1",
                "method": "currentTime/read",
                "params": {"threadId": "thread-1"},
            },
            connection_generation=1,
        )
        self.assertTrue(callback_entered.wait(timeout=1.0))

        with self.assertRaises(CodexRpcStopError) as caught:
            client.stop(timeout=0.02)

        self.assertIn(
            "server callback thread",
            " ".join(caught.exception.pending_resources),
        )
        release_callback.set()
        client.stop(timeout=1.0)

    def test_stop_request_fences_start_while_automatic_response_holds_send_lock(self) -> None:
        send_entered = threading.Event()
        release_send = threading.Event()
        self.addCleanup(release_send.set)

        class _BlockingSendWebsocket(_ClosableWebsocket):
            def send(self, _payload: str) -> None:
                send_entered.set()
                release_send.wait(timeout=2.0)

        client = CodexRpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        self._mark_ready(client, _BlockingSendWebsocket())
        client._dispatch_payload(
            {
                "jsonrpc": "2.0",
                "id": "clock-1",
                "method": "currentTime/read",
                "params": {"threadId": "thread-1"},
            },
            connection_generation=1,
        )
        self.assertTrue(send_entered.wait(timeout=1.0))

        with self.assertRaises(CodexRpcStopError) as caught:
            client.stop(timeout=0.02)

        self.assertIn("client identity/send lock", caught.exception.pending_resources)
        with self.assertRaises(CodexRpcStopError):
            client.start()

        release_send.set()
        client.stop(timeout=1.0)

    def test_concurrent_stop_calls_share_one_websocket_close(self) -> None:
        close_entered = threading.Event()
        release_close = threading.Event()
        second_started = threading.Event()
        self.addCleanup(release_close.set)

        class _BlockingCloseWebsocket:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1
                close_entered.set()
                release_close.wait(timeout=2.0)

        websocket = _BlockingCloseWebsocket()
        client = CodexRpcConnection(endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT)
        self._mark_ready(client, websocket)
        failures: list[Exception] = []

        def stop_client(*, mark_started: bool = False) -> None:
            if mark_started:
                second_started.set()
            try:
                client.stop(timeout=1.0)
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        first = threading.Thread(target=stop_client)
        second = threading.Thread(
            target=lambda: stop_client(mark_started=True),
        )
        first.start()
        self.assertTrue(close_entered.wait(timeout=1.0))
        second.start()
        self.assertTrue(second_started.wait(timeout=1.0))
        time.sleep(0.02)
        self.assertEqual(websocket.close_calls, 1)

        release_close.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(websocket.close_calls, 1)

    def test_handshake_cleanup_settles_while_sharing_external_stop(self) -> None:
        initialize_sent = threading.Event()
        close_entered = threading.Event()
        release_close = threading.Event()
        internal_cleanup_entered = threading.Event()
        self.addCleanup(release_close.set)

        class _BlockingCloseWebsocket:
            def send(self, payload: str) -> None:
                if json.loads(payload).get("method") == "initialize":
                    initialize_sent.set()

            def close(self) -> None:
                close_entered.set()
                release_close.wait(timeout=2.0)

        client = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            connect_timeout_seconds=1.0,
            request_timeout_seconds=1.0,
        )
        websocket = _BlockingCloseWebsocket()
        original_stop_connection = client._stop_connection

        def observe_stop_connection(**kwargs) -> None:
            if kwargs.get("handshake_attempt") is not None:
                internal_cleanup_entered.set()
            original_stop_connection(**kwargs)

        client._stop_connection = observe_stop_connection  # type: ignore[method-assign]

        def fake_start_locked() -> None:
            client._closing = False
            client._ws = websocket
            client._connection_generation = 1

        owner_failures: list[BaseException] = []
        stop_failures: list[BaseException] = []

        def start_client() -> None:
            try:
                client.start()
            except BaseException as exc:  # pragma: no branch - asserted below
                owner_failures.append(exc)

        def stop_client() -> None:
            try:
                client.stop(timeout=1.0)
            except BaseException as exc:  # pragma: no cover - asserted below
                stop_failures.append(exc)

        with patch.object(client, "_start_locked", fake_start_locked):
            owner = threading.Thread(target=start_client, name="test-handshake-owner")
            owner.start()
            self.assertTrue(initialize_sent.wait(timeout=1.0))

            stopper = threading.Thread(target=stop_client, name="test-stop-owner")
            stopper.start()
            self.assertTrue(close_entered.wait(timeout=1.0))
            self.assertTrue(internal_cleanup_entered.wait(timeout=1.0))

            deadline = time.monotonic() + 1.0
            while (
                client._connection_state != _CONNECTION_DISCONNECTED
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            settled_while_stop_active = (
                client._connection_state == _CONNECTION_DISCONNECTED
                and client._stop_barrier.active
            )

            release_close.set()
            owner.join(timeout=1.0)
            stopper.join(timeout=1.0)

        self.assertTrue(settled_while_stop_active)
        self.assertFalse(owner.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(stop_failures, [])
        self.assertEqual(len(owner_failures), 1)
        self.assertIsNone(client._handshake_owner_thread_id)
        self.assertEqual(client._connection_state, _CONNECTION_DISCONNECTED)
        self.assertFalse(client._stop_barrier.has_retained_resources)

        replacement = _ClosableWebsocket()

        def fake_replacement_start_locked() -> None:
            client._closing = False
            client._ws = replacement
            client._connection_generation += 1

        with patch.object(client, "_start_locked", fake_replacement_start_locked):
            with patch.object(client, "request", return_value={"userAgent": "codex/test"}):
                with patch.object(client, "notify"):
                    client.start()

        self.assertEqual(client._connection_state, _CONNECTION_READY)
        client.stop(timeout=1.0)

if __name__ == "__main__":
    unittest.main()
