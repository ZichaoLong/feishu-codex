import ast
import pathlib
import threading
import time
import unittest
from unittest.mock import patch

from bot.codex_protocol.stop_barrier import (
    CodexRpcStopBarrier,
    CodexRpcStopError,
    RpcStopAttempt,
    RpcStopResourceTransfer,
)


class _BarrierHarness:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.barrier = CodexRpcStopBarrier(
            identity_lock=self.lock,
            condition=self.condition,
        )

    def begin(
        self,
        transfer: RpcStopResourceTransfer | None = None,
    ) -> RpcStopAttempt:
        self.barrier.request_stop()
        with self.condition:
            return self.barrier.begin_locked(
                transfer or RpcStopResourceTransfer()
            )


class CodexRpcStopBarrierTests(unittest.TestCase):
    def test_drain_closes_websocket_before_joining_reader(self) -> None:
        release_reader = threading.Event()

        class _Websocket:
            def close(self) -> None:
                release_reader.set()

        reader = threading.Thread(
            target=lambda: release_reader.wait(timeout=2.0),
            name="test-reader",
            daemon=True,
        )
        reader.start()
        harness = _BarrierHarness()
        attempt = harness.begin(
            RpcStopResourceTransfer(
                websocket=_Websocket(),
                reader_threads=(reader,),
            )
        )

        harness.barrier.drain_attempt(
            attempt,
            deadline_monotonic=time.monotonic() + 1.0,
        )

        self.assertFalse(reader.is_alive())
        self.assertTrue(harness.barrier.is_clear)

    def test_interrupted_drain_retains_exact_resources_for_retry(self) -> None:
        class _Websocket:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        websocket = _Websocket()
        harness = _BarrierHarness()
        attempt = harness.begin(
            RpcStopResourceTransfer(websocket=websocket)
        )

        with patch.object(
            harness.barrier,
            "_drain_resources",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                harness.barrier.drain_attempt(
                    attempt,
                    deadline_monotonic=time.monotonic() + 1.0,
                )

        self.assertTrue(harness.barrier.stop_requested)
        self.assertTrue(harness.barrier.has_retained_resources)
        self.assertIsInstance(harness.barrier.last_error, CodexRpcStopError)
        with harness.condition:
            with self.assertRaises(CodexRpcStopError):
                harness.barrier.wait_until_startable_locked()

        retry = harness.begin()
        harness.barrier.drain_attempt(
            retry,
            deadline_monotonic=time.monotonic() + 1.0,
        )

        self.assertEqual(websocket.close_calls, 1)
        self.assertTrue(harness.barrier.is_clear)

    def test_websocket_close_base_exception_is_retryable(self) -> None:
        class _Websocket:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise SystemExit("interrupted close")

        websocket = _Websocket()
        harness = _BarrierHarness()
        attempt = harness.begin(
            RpcStopResourceTransfer(websocket=websocket)
        )

        with self.assertRaises(CodexRpcStopError) as caught:
            harness.barrier.drain_attempt(
                attempt,
                deadline_monotonic=time.monotonic() + 1.0,
            )

        self.assertIn("websocket", caught.exception.pending_resources)
        self.assertTrue(harness.barrier.has_retained_resources)

        retry = harness.begin()
        harness.barrier.drain_attempt(
            retry,
            deadline_monotonic=time.monotonic() + 1.0,
        )

        self.assertEqual(websocket.close_calls, 2)
        self.assertTrue(harness.barrier.is_clear)

    def test_websocket_close_timeout_reuses_in_flight_operation(self) -> None:
        close_entered = threading.Event()
        release_close = threading.Event()
        self.addCleanup(release_close.set)

        class _Websocket:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1
                close_entered.set()
                release_close.wait(timeout=2.0)

        websocket = _Websocket()
        harness = _BarrierHarness()
        attempt = harness.begin(
            RpcStopResourceTransfer(websocket=websocket)
        )

        with self.assertRaises(CodexRpcStopError) as caught:
            harness.barrier.drain_attempt(
                attempt,
                deadline_monotonic=time.monotonic() + 0.02,
            )

        self.assertTrue(close_entered.is_set())
        self.assertIn("websocket", caught.exception.pending_resources)
        self.assertEqual(websocket.close_calls, 1)

        release_close.set()
        retry = harness.begin()
        harness.barrier.drain_attempt(
            retry,
            deadline_monotonic=time.monotonic() + 1.0,
        )

        self.assertEqual(websocket.close_calls, 1)
        self.assertTrue(harness.barrier.is_clear)

    def test_concurrent_joiner_observes_single_drain_outcome(self) -> None:
        close_entered = threading.Event()
        release_close = threading.Event()
        joiner_started = threading.Event()
        self.addCleanup(release_close.set)

        class _Websocket:
            def __init__(self) -> None:
                self.close_calls = 0

            def close(self) -> None:
                self.close_calls += 1
                close_entered.set()
                release_close.wait(timeout=2.0)

        websocket = _Websocket()
        harness = _BarrierHarness()
        attempt = harness.begin(
            RpcStopResourceTransfer(websocket=websocket)
        )
        failures: list[BaseException] = []

        def drain() -> None:
            try:
                harness.barrier.drain_attempt(
                    attempt,
                    deadline_monotonic=time.monotonic() + 1.0,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def join() -> None:
            joiner_started.set()
            try:
                with harness.condition:
                    harness.barrier.join_active_locked(
                        deadline_monotonic=time.monotonic() + 1.0,
                    )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        drainer = threading.Thread(target=drain, name="test-stop-owner")
        joiner = threading.Thread(target=join, name="test-stop-joiner")
        drainer.start()
        self.assertTrue(close_entered.wait(timeout=1.0))
        joiner.start()
        self.assertTrue(joiner_started.wait(timeout=1.0))

        release_close.set()
        drainer.join(timeout=1.0)
        joiner.join(timeout=1.0)

        self.assertFalse(drainer.is_alive())
        self.assertFalse(joiner.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(websocket.close_calls, 1)
        self.assertTrue(harness.barrier.is_clear)

    def test_client_contains_no_stop_resource_or_drain_implementation(self) -> None:
        root = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "codex_protocol"
        )
        facade_module = ast.parse(
            (root / "client.py").read_text(encoding="utf-8")
        )
        connection_module = ast.parse(
            (root / "connection.py").read_text(encoding="utf-8")
        )
        connection = next(
            node
            for node in connection_module.body
            if isinstance(node, ast.ClassDef)
            and node.name == "CodexRpcConnection"
        )
        methods = {
            node.name
            for node in connection.body
            if isinstance(node, ast.FunctionDef)
        }
        self_attributes = {
            node.attr
            for node in ast.walk(connection)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        module_classes = {
            node.name
            for node in connection_module.body
            if isinstance(node, ast.ClassDef)
        }
        facade_classes = {
            node.name
            for node in facade_module.body
            if isinstance(node, ast.ClassDef)
        }

        self.assertIn("_stop_barrier", self_attributes)
        self.assertFalse(
            self_attributes
            & {
                "_active_stop_operations",
                "_stop_requested",
                "_stop_attempt",
                "_stop_resources",
                "_last_stop_outcome",
                "_current_stop_attempt",
            }
        )
        self.assertFalse(
            methods
            & {
                "_drain_stop_resources",
                "_drain_websocket_close",
                "_join_owned_threads",
                "_pending_stop_resource_names",
                "_current_thread_is_owned_by_stop_locked",
                "_bounded_stop_identity_lock",
            }
        )
        self.assertFalse(
            module_classes
            & {"_WebsocketCloseOperation", "_StopResources", "_StopAttempt"}
        )
        self.assertFalse(
            facade_classes
            & {"_WebsocketCloseOperation", "_StopResources", "_StopAttempt"}
        )

    def test_stop_owner_has_no_client_or_websocket_dependency(self) -> None:
        path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "codex_protocol"
            / "stop_barrier.py"
        )
        module = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            imported
            for node in module.body
            for imported in self._imported_modules(node)
        }

        self.assertNotIn("bot.codex_protocol.client", imported_modules)
        self.assertFalse(
            any(
                imported == "websockets" or imported.startswith("websockets.")
                for imported in imported_modules
            )
        )

    @staticmethod
    def _imported_modules(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Import):
            return tuple(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            return (node.module,)
        return ()


if __name__ == "__main__":
    unittest.main()
