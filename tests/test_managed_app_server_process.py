import ast
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bot.codex_command_resolver import DEFAULT_CODEX_COMMAND
from bot.codex_protocol.managed_process import ManagedAppServerProcess
from bot.local_websocket_auth import AppServerWebsocketAuthTokenStore
from bot.stores.app_server_runtime_store import (
    AppServerRuntimeStore,
    OrphanedOwnedAppServerError,
)


class _ThreadStub:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def start(self) -> None:
        return None


class ManagedAppServerProcessTests(unittest.TestCase):
    @staticmethod
    def _owner(
        *,
        data_dir: Path | None = None,
        runtime_store: AppServerRuntimeStore | None = None,
    ) -> ManagedAppServerProcess:
        return ManagedAppServerProcess(
            codex_command=DEFAULT_CODEX_COMMAND,
            configured_url="ws://127.0.0.1:8765",
            runtime_store=runtime_store,
            startup_lock_path=None,
            websocket_auth_store=(
                AppServerWebsocketAuthTokenStore(data_dir)
                if data_dir is not None
                else None
            ),
        )

    @staticmethod
    def _set_process(owner: ManagedAppServerProcess, process: object) -> None:
        owner._process = process  # type: ignore[assignment]
        owner._process_reaped = False

    def test_launch_wraps_resolved_codex_command_in_guardian(self) -> None:
        stable_command = (
            "/home/bot/.nvm/versions/node/v24.15.0/bin/node "
            "/home/bot/.nvm/versions/node/v24.15.0/lib/"
            "node_modules/@openai/codex/bin/codex.js"
        )

        class _Process:
            stdout = StringIO("")
            stderr = StringIO("")
            stdin = StringIO()

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            owner = self._owner(data_dir=data_dir)
            with (
                patch(
                    "bot.codex_protocol.managed_process."
                    "resolve_managed_codex_command",
                    return_value=stable_command,
                ),
                patch(
                    "bot.codex_protocol.managed_process.subprocess.Popen",
                    return_value=_Process(),
                ) as popen,
                patch(
                    "bot.codex_protocol.managed_process.threading.Thread",
                    _ThreadStub,
                ),
            ):
                owner.launch("ws://127.0.0.1:8765")

            launched = popen.call_args.args[0]
            self.assertEqual(
                launched,
                [
                    sys.executable,
                    "-m",
                    "bot.owned_app_server_guard",
                    "--",
                    "/home/bot/.nvm/versions/node/v24.15.0/bin/node",
                    "/home/bot/.nvm/versions/node/v24.15.0/lib/"
                    "node_modules/@openai/codex/bin/codex.js",
                    "app-server",
                    "--listen",
                    "ws://127.0.0.1:8765",
                    "--ws-auth",
                    "capability-token",
                    "--ws-token-file",
                    str(AppServerWebsocketAuthTokenStore(data_dir).path),
                ],
            )
            self.assertIs(popen.call_args.kwargs["stdin"], subprocess.PIPE)
            self.assertTrue(AppServerWebsocketAuthTokenStore(data_dir).path.exists())

    def test_launch_encodes_leading_hyphen_cleanup_token(self) -> None:
        class _Process:
            pid = os.getpid()
            stdout = StringIO("")
            stderr = StringIO("")
            stdin = StringIO()

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            runtime_store = AppServerRuntimeStore(data_dir)
            owner = self._owner(data_dir=data_dir, runtime_store=runtime_store)
            with (
                patch.object(
                    runtime_store,
                    "begin_guardian_generation",
                    return_value="-leading-hyphen-token",
                ),
                patch(
                    "bot.codex_protocol.managed_process.subprocess.Popen",
                    return_value=_Process(),
                ) as popen,
                patch(
                    "bot.codex_protocol.managed_process.threading.Thread",
                    _ThreadStub,
                ),
            ):
                owner.launch("ws://127.0.0.1:8765")

            launched = popen.call_args.args[0]
            self.assertIn("--cleanup-token=-leading-hyphen-token", launched)
            self.assertNotIn("--cleanup-token", launched)
            self.assertIn(
                "--cleanup-receipt-path="
                + str(
                    runtime_store.cleanup_receipt_path(
                        "-leading-hyphen-token"
                    )
                ),
                launched,
            )

    def test_dead_guardian_requires_matching_cleanup_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            cleanup_token = "unproved-generation"
            runtime_store = AppServerRuntimeStore(data_dir)
            runtime_store.save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:8765",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token=cleanup_token,
            )
            owner = self._owner(
                data_dir=data_dir,
                runtime_store=runtime_store,
            )

            class _DeadGuardian:
                def poll(self):
                    return -9

            self._set_process(owner, _DeadGuardian())
            owner._cleanup_token = cleanup_token
            owner._runtime_state_cleared = False

            with self.assertRaisesRegex(
                OrphanedOwnedAppServerError,
                "without a matching durable cleanup receipt",
            ):
                owner.prepare_for_start()

            self.assertTrue((data_dir / "app_server_runtime.json").exists())

    def test_terminate_timeout_is_followed_by_kill_and_wait(self) -> None:
        events: list[str] = []

        class _Process:
            killed = False

            def poll(self):
                return None

            def terminate(self) -> None:
                events.append("terminate")

            def kill(self) -> None:
                events.append("kill")
                self.killed = True

            def wait(self, *, timeout: float):
                events.append("wait")
                if not self.killed:
                    raise subprocess.TimeoutExpired("codex", timeout)
                return -9

        owner = self._owner()
        self._set_process(owner, _Process())

        self.assertEqual(owner.request_stop(), ())
        result = owner.drain_stop(deadline_monotonic=time.monotonic() + 1.0)

        self.assertTrue(result.complete)
        self.assertEqual(events, ["terminate", "wait", "kill", "wait"])

    def test_guardian_shutdown_closes_parent_pipe_without_kill(self) -> None:
        events: list[str] = []

        class _GuardianInput:
            def close(self) -> None:
                events.append("close-guardian-input")

        class _GuardianProcess:
            stdin = _GuardianInput()

            def poll(self):
                return None

            def terminate(self) -> None:
                events.append("unexpected-terminate")

            def kill(self) -> None:
                events.append("unexpected-kill")

            def wait(self, *, timeout: float):
                del timeout
                events.append("wait-guardian")
                return 0

        owner = self._owner()
        self._set_process(owner, _GuardianProcess())

        self.assertEqual(owner.request_stop(), ())
        result = owner.drain_stop(deadline_monotonic=time.monotonic() + 1.0)

        self.assertTrue(result.complete)
        self.assertEqual(events, ["close-guardian-input", "wait-guardian"])

    def test_guardian_receives_full_remaining_stop_budget(self) -> None:
        observed_timeouts: list[float] = []

        class _GuardianInput:
            def close(self) -> None:
                return None

        class _GuardianProcess:
            stdin = _GuardianInput()

            def poll(self):
                return None

            def wait(self, *, timeout: float):
                observed_timeouts.append(timeout)
                if timeout < 0.75:
                    raise subprocess.TimeoutExpired("guardian", timeout)
                return 0

        owner = self._owner()
        self._set_process(owner, _GuardianProcess())

        self.assertEqual(owner.request_stop(), ())
        result = owner.drain_stop(deadline_monotonic=time.monotonic() + 1.0)

        self.assertTrue(result.complete)
        self.assertEqual(len(observed_timeouts), 1)
        self.assertGreaterEqual(observed_timeouts[0], 0.75)

    def test_guardian_timeout_retains_generation_for_retry(self) -> None:
        class _GuardianInput:
            close_calls = 0

            def close(self) -> None:
                self.close_calls += 1

        class _GuardianProcess:
            stdin = _GuardianInput()
            wait_calls = 0
            kill_calls = 0

            def poll(self):
                return None

            def kill(self) -> None:
                self.kill_calls += 1

            def wait(self, *, timeout: float):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("guardian", timeout)
                return 0

        process = _GuardianProcess()
        owner = self._owner()
        self._set_process(owner, process)

        self.assertEqual(owner.request_stop(), ())
        first = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 0.1
        )

        self.assertIn(
            "owned process guardian shutdown timed out",
            first.failures,
        )
        self.assertIn("managed process", first.pending_resources)
        self.assertEqual(process.stdin.close_calls, 1)
        self.assertEqual(process.kill_calls, 0)

        self.assertEqual(owner.request_stop(), ())
        second = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 1.0
        )

        self.assertTrue(second.complete)
        self.assertEqual(process.stdin.close_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(process.wait_calls, 2)

    def test_wait_after_kill_timeout_retains_generation_for_retry(self) -> None:
        class _Process:
            killed = False
            wait_after_kill_calls = 0
            kill_calls = 0

            def poll(self):
                return None

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                self.killed = True
                self.kill_calls += 1

            def wait(self, *, timeout: float):
                if not self.killed:
                    raise subprocess.TimeoutExpired("codex", timeout)
                self.wait_after_kill_calls += 1
                if self.wait_after_kill_calls == 1:
                    raise subprocess.TimeoutExpired("codex", timeout)
                return -9

        process = _Process()
        owner = self._owner()
        self._set_process(owner, process)

        self.assertEqual(owner.request_stop(), ())
        first = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 0.1
        )

        self.assertIn("managed process", first.pending_resources)
        self.assertIn("managed process wait after kill timed out", first.failures)

        self.assertEqual(owner.request_stop(), ())
        second = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 1.0
        )

        self.assertTrue(second.complete)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_after_kill_calls, 2)

    def test_stream_thread_timeout_retains_generation_for_retry(self) -> None:
        release_stream = threading.Event()
        self.addCleanup(release_stream.set)

        class _ExitedProcess:
            def poll(self):
                return 0

        stream_thread = threading.Thread(
            target=lambda: release_stream.wait(timeout=2.0),
            name="test-managed-stdout",
            daemon=True,
        )
        stream_thread.start()
        owner = self._owner()
        self._set_process(owner, _ExitedProcess())
        owner._stream_threads.add(stream_thread)

        self.assertEqual(owner.request_stop(), ())
        first = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 0.02
        )

        self.assertIn(
            "managed stream thread",
            " ".join(first.pending_resources),
        )
        self.assertIn("join timed out", " ".join(first.failures))

        release_stream.set()
        self.assertEqual(owner.request_stop(), ())
        second = owner.drain_stop(
            deadline_monotonic=time.monotonic() + 1.0
        )

        self.assertTrue(second.complete)
        self.assertFalse(stream_thread.is_alive())

    def test_client_cannot_reclaim_managed_process_facts(self) -> None:
        bot_root = pathlib.Path(__file__).parents[1] / "bot" / "codex_protocol"
        facade_module = ast.parse(
            (bot_root / "client.py").read_text(encoding="utf-8")
        )
        connection_module = ast.parse(
            (bot_root / "connection.py").read_text(encoding="utf-8")
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
        stop_module = ast.parse(
            (bot_root / "stop_barrier.py").read_text(encoding="utf-8")
        )
        stop_resources = next(
            node
            for node in stop_module.body
            if isinstance(node, ast.ClassDef) and node.name == "_StopResources"
        )
        stop_fields = {
            node.target.id
            for node in stop_resources.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
        }

        self.assertFalse(
            methods
            & {
                "_launch_managed_process_locked",
                "_verify_managed_process_alive_locked",
                "_record_owned_runtime_state",
                "_clear_owned_runtime_state",
            }
        )
        self.assertFalse(
            self_attributes
            & {
                "_process",
                "_managed_stream_threads",
                "_owned_cleanup_token",
                "_managed_endpoint_allocator",
                "_managed_startup_lock_path",
                "_app_server_runtime_store",
            }
        )
        self.assertNotIn(
            "_StopResources",
            {
                node.name
                for node in facade_module.body
                if isinstance(node, ast.ClassDef)
            },
        )
        self.assertIn("managed_process", stop_fields)
        self.assertFalse(
            stop_fields
            & {
                "process",
                "stream_threads",
                "process_terminate_sent",
                "process_kill_sent",
                "process_reaped",
                "runtime_state_cleared",
            }
        )

    def test_managed_process_owner_has_no_websocket_or_client_dependency(self) -> None:
        path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "codex_protocol"
            / "managed_process.py"
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
