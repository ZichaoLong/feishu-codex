import json
import os
import queue
import tempfile
import threading
import time
import unittest
from websockets.exceptions import ConnectionClosedOK
from websockets.sync.client import connect
from websockets.sync.server import serve
from unittest.mock import Mock, patch
from io import StringIO
from pathlib import Path

from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary, ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcClient
from bot.fcodex import _default_data_dir, _launch_local_cwd_proxy, main as fcodex_main
from bot.fcodex_proxy import _relay_messages, _rewrite_thread_start_cwd, run_proxy
from bot.profile_resolution import resolve_local_default_profile
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.session_resolution import (
    format_thread_match,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)


class _FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        payload = params or {}
        self.calls.append((method, payload))
        if method == "model/list":
            return {
                "data": [
                    {"model": "gpt-5.3-codex", "isDefault": True, "hidden": False},
                    {"model": "gpt-5.4", "isDefault": False, "hidden": False},
                ]
            }
        if method == "config/read":
            return {
                "config": {
                    "profile": "provider1",
                    "modelProvider": "provider1_api",
                    "profiles": {
                        "provider1": {"modelProvider": "provider1_api"},
                        "provider2": {"modelProvider": "provider2_api"},
                    },
                }
            }
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {
                    "id": "thread-1",
                    "cwd": "/tmp/project",
                    "name": "demo",
                    "preview": "hello",
                    "createdAt": 0,
                    "updatedAt": 0,
                    "source": "cli",
                    "status": {"type": "idle", "activeFlags": []},
                }
            }
        return {"ok": True}


class CodexAppServerAdapterTests(unittest.TestCase):
    def test_create_thread_can_attach_profile_override(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project", profile="provider2")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/start",
                {
                    "cwd": "/tmp/project",
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "personality": "pragmatic",
                    "serviceName": "feishu-codex",
                    "config": {"profile": "provider2"},
                },
            ),
        )

    def test_create_thread_allows_permission_overrides(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(
            cwd="/tmp/project",
            approval_policy="never",
            sandbox="danger-full-access",
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/start",
                {
                    "cwd": "/tmp/project",
                    "sandbox": "danger-full-access",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "personality": "pragmatic",
                    "serviceName": "feishu-codex",
                },
            ),
        )

    def test_resume_thread_can_attach_profile_override(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.resume_thread("thread-1", profile="provider2")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/resume",
                {
                    "threadId": "thread-1",
                    "config": {"profile": "provider2"},
                },
            ),
        )

    def test_start_turn_default_mode_sends_explicit_collaboration_mode(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp")

        self.assertEqual(
            fake_rpc.calls,
            [
                ("model/list", {}),
                (
                    "turn/start",
                    {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "hello"}],
                        "cwd": "/tmp",
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sandboxPolicy": {
                            "type": "workspaceWrite",
                            "writableRoots": [],
                            "readOnlyAccess": {"type": "fullAccess"},
                            "networkAccess": False,
                            "excludeTmpdirEnvVar": False,
                            "excludeSlashTmp": False,
                        },
                        "personality": "pragmatic",
                        "collaborationMode": {
                            "mode": "default",
                            "settings": {
                                "model": "gpt-5.3-codex",
                                "reasoning_effort": None,
                                "developer_instructions": None,
                            },
                        },
                    },
                )
            ],
        )

    def test_start_turn_plan_mode_uses_configured_model(self) -> None:
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(model="gpt-5.4", reasoning_effort="high", collaboration_mode="plan")
        )
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp")

        self.assertEqual(len(fake_rpc.calls), 1)
        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["collaborationMode"]["mode"], "plan")
        self.assertEqual(params["collaborationMode"]["settings"]["model"], "gpt-5.4")
        self.assertEqual(params["collaborationMode"]["settings"]["reasoning_effort"], "high")

    def test_start_turn_plan_mode_resolves_default_model_once(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig(collaboration_mode="plan"))
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp")
        adapter.start_turn(thread_id="thread-2", text="again", cwd="/tmp")

        self.assertEqual(fake_rpc.calls[0][0], "model/list")
        self.assertEqual(fake_rpc.calls[1][0], "turn/start")
        self.assertEqual(fake_rpc.calls[2][0], "turn/start")
        self.assertEqual(
            fake_rpc.calls[1][1]["collaborationMode"]["settings"]["model"],
            "gpt-5.3-codex",
        )
        self.assertEqual(
            fake_rpc.calls[2][1]["collaborationMode"]["settings"]["model"],
            "gpt-5.3-codex",
        )

    def test_start_turn_allows_per_turn_collaboration_mode_override(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig(collaboration_mode="plan"))
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            text="hello",
            cwd="/tmp",
            collaboration_mode="default",
        )

        self.assertEqual(len(fake_rpc.calls), 2)
        self.assertEqual(fake_rpc.calls[0], ("model/list", {}))
        method, params = fake_rpc.calls[1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["collaborationMode"]["mode"], "default")
        self.assertEqual(params["collaborationMode"]["settings"]["model"], "gpt-5.3-codex")

    def test_start_turn_can_attach_profile_override(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp", profile="provider2")

        self.assertEqual(fake_rpc.calls[0], ("model/list", {}))
        self.assertEqual(fake_rpc.calls[1][0], "turn/start")
        self.assertEqual(fake_rpc.calls[1][1]["config"], {"profile": "provider2"})

    def test_start_turn_can_override_sandbox_policy(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            text="hello",
            cwd="/tmp",
            sandbox="danger-full-access",
        )

        self.assertEqual(fake_rpc.calls[0], ("model/list", {}))
        self.assertEqual(fake_rpc.calls[1][0], "turn/start")
        self.assertEqual(
            fake_rpc.calls[1][1]["sandboxPolicy"],
            {"type": "dangerFullAccess"},
        )

    def test_list_threads_can_explicitly_disable_provider_filter(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.list_threads(cwd="/tmp/project", limit=5, model_providers=[])

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/list",
                {
                    "cwd": "/tmp/project",
                    "limit": 5,
                    "sourceKinds": ["cli", "vscode", "exec", "appServer"],
                    "modelProviders": [],
                },
            ),
        )

    def test_read_runtime_config_parses_profiles(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        runtime = adapter.read_runtime_config()

        self.assertEqual(runtime.current_profile, "provider1")
        self.assertEqual(runtime.current_model_provider, "provider1_api")
        self.assertEqual(
            [(item.name, item.model_provider) for item in runtime.profiles],
            [("provider1", "provider1_api"), ("provider2", "provider2_api")],
        )

    def test_set_active_profile_uses_config_batch_write_and_reload(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        runtime = adapter.set_active_profile("provider2")

        self.assertEqual(fake_rpc.calls[0][0], "config/batchWrite")
        self.assertEqual(
            fake_rpc.calls[0][1],
            {
                "edits": [
                    {
                        "keyPath": "profile",
                        "value": "provider2",
                        "mergeStrategy": "replace",
                    }
                ],
                "reloadUserConfig": True,
            },
        )
        self.assertEqual(fake_rpc.calls[1][0], "config/read")
        self.assertEqual(runtime.current_profile, "provider1")

    def test_archive_thread_calls_public_archive_api(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.archive_thread("thread-1")

        self.assertEqual(fake_rpc.calls[0], ("thread/archive", {"threadId": "thread-1"}))

    def test_config_rejects_invalid_collaboration_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAppServerConfig.from_dict({"collaboration_mode": "broken"})

    def test_config_rejects_invalid_app_server_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAppServerConfig.from_dict({"app_server_mode": "broken"})


class AppServerRuntimeStoreTests(unittest.TestCase):
    def test_resolve_effective_app_server_url_uses_runtime_state_for_default_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_managed_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                app_server_pid=os.getpid(),
            )

            self.assertEqual(
                resolve_effective_app_server_url("ws://127.0.0.1:8765", data_dir=data_dir),
                "ws://127.0.0.1:43210",
            )

    def test_resolve_effective_app_server_url_ignores_stale_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            store = AppServerRuntimeStore(data_dir)
            store.save_managed_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=999999,
                app_server_pid=999999,
            )

            with patch("bot.stores.app_server_runtime_store._process_exists", return_value=False):
                self.assertEqual(
                    resolve_effective_app_server_url("ws://127.0.0.1:8765", data_dir=data_dir),
                    "ws://127.0.0.1:8765",
                )


class CodexRpcClientTests(unittest.TestCase):
    def test_start_initializes_with_experimental_api(self) -> None:
        client = CodexRpcClient()
        captured: list[tuple[str, dict, float | None]] = []

        def fake_start_locked() -> None:
            client._ws = object()
            client._process = object()

        def fake_request(method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
            captured.append((method, params or {}, timeout))
            return {}

        with patch.object(client, "_start_locked", fake_start_locked):
            with patch.object(client, "request", fake_request):
                client.start()

        self.assertEqual(
            captured,
            [
                (
                    "initialize",
                    {
                        "clientInfo": {"name": "feishu-codex", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                    client._connect_timeout_seconds,
                )
            ],
        )

    def test_connect_ws_disables_default_frame_limit(self) -> None:
        client = CodexRpcClient(connect_timeout_seconds=0.1)
        client._app_server_url = "ws://127.0.0.1:12345"

        class _Proc:
            def poll(self):
                return None

        client._process = _Proc()

        with patch("bot.codex_protocol.client.connect", return_value="ws-obj") as mock_connect:
            client._connect_ws_locked()

        self.assertEqual(client._ws, "ws-obj")
        _, kwargs = mock_connect.call_args
        self.assertEqual(kwargs["open_timeout"], client._connect_timeout_seconds)
        self.assertIsNone(kwargs["max_size"])

    def test_start_locked_reuses_existing_managed_process(self) -> None:
        client = CodexRpcClient()

        class _Proc:
            def poll(self):
                return None

        class _ThreadStub:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def start(self) -> None:
                return None

        client._process = _Proc()

        with patch.object(client, "_connect_ws_locked", lambda: setattr(client, "_ws", object())):
            with patch("bot.codex_protocol.client.subprocess.Popen") as mock_popen:
                with patch("bot.codex_protocol.client.threading.Thread", _ThreadStub):
                    client._start_locked()

        mock_popen.assert_not_called()
        self.assertIsNotNone(client._ws)

    def test_start_locked_falls_back_to_free_port_when_default_is_busy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback_url = "ws://127.0.0.1:43210"
            store = AppServerRuntimeStore(Path(tmpdir))
            client = CodexRpcClient(app_server_runtime_store=store)

            class _Proc:
                pid = os.getpid()
                stdout = StringIO("")
                stderr = StringIO("")

                def poll(self):
                    return None

            class _ThreadStub:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def start(self) -> None:
                    return None

            with patch.object(client, "_can_bind_listen_url", return_value=False):
                with patch.object(client, "_allocate_free_listen_url", return_value=fallback_url):
                    with patch.object(client, "_connect_ws_locked", lambda: setattr(client, "_ws", object())):
                        with patch("bot.codex_protocol.client.subprocess.Popen", return_value=_Proc()) as mock_popen:
                            with patch("bot.codex_protocol.client.threading.Thread", _ThreadStub):
                                client._start_locked()

            self.assertEqual(client.current_app_server_url(), fallback_url)
            self.assertEqual(mock_popen.call_args[0][0][-1], fallback_url)
            self.assertEqual(
                resolve_effective_app_server_url("ws://127.0.0.1:8765", data_dir=Path(tmpdir)),
                fallback_url,
            )


class FCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch(
            "bot.fcodex.resolve_effective_app_server_url",
            side_effect=lambda configured_url, *, data_dir: configured_url,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_data_dir_falls_back_to_install_path_when_not_in_dev_layout(self) -> None:
        with patch.dict("bot.fcodex.os.environ", {}, clear=True):
            with patch("bot.fcodex._looks_like_dev_layout", return_value=False):
                with patch("bot.fcodex.pathlib.Path.home", return_value=Path("/home/tester")):
                    self.assertEqual(
                        _default_data_dir(),
                        Path("/home/tester/.local/share/feishu-codex"),
                    )

    def test_fcodex_injects_remote_url(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = ""
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())) as mock_proxy:
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                                fcodex_main()

        mock_proxy.assert_called_once_with("ws://127.0.0.1:8765", os.getcwd())
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_uses_runtime_resolved_backend_url(self) -> None:
        fallback_url = "ws://127.0.0.1:43210"
        with patch("bot.fcodex.resolve_effective_app_server_url", return_value=fallback_url):
            with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
                with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                    with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_profile_resolve:
                        mock_profile_resolve.return_value.effective_profile = ""
                        mock_profile_resolve.return_value.stale_profile = ""
                        with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())) as mock_proxy:
                            with patch("bot.fcodex.os.execvpe") as mock_exec:
                                with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                                    fcodex_main()

        self.assertEqual(mock_profile_resolve.call_args.kwargs["app_server_url"], fallback_url)
        mock_proxy.assert_called_once_with(fallback_url, os.getcwd())
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_injects_default_profile_when_not_explicit(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider2"):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = "provider2"
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())):
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                                fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--cd",
                os.getcwd(),
                "--profile",
                "provider2",
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )

    def test_fcodex_explicit_profile_wins(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider2"):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = "provider2"
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())):
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "-p", "provider1", "resume", "thread-1"]):
                                fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "-p", "provider1", "resume", "thread-1"],
        )

    def test_fcodex_explicit_remote_skips_shared_resolution(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_profile_resolve:
                    mock_profile_resolve.return_value.effective_profile = ""
                    mock_profile_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex.resolve_resume_name_via_remote_backend") as mock_resolve:
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "--remote", "ws://127.0.0.1:9900", "resume", "demo"]):
                                fcodex_main()

        mock_resolve.assert_not_called()
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--cd", os.getcwd(), "--remote", "ws://127.0.0.1:9900", "resume", "demo"],
        )

    def test_fcodex_respects_explicit_remote_arg(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_profile_resolve:
                    mock_profile_resolve.return_value.effective_profile = ""
                    mock_profile_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "--remote", "ws://127.0.0.1:9900", "resume"]):
                            fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--cd", os.getcwd(), "--remote", "ws://127.0.0.1:9900", "resume"],
        )

    def test_fcodex_clears_stale_local_default_profile(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider9"):
                with patch("bot.fcodex.ProfileStateStore.save_default_profile") as mock_save:
                    with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                        mock_resolve.return_value.effective_profile = ""
                        mock_resolve.return_value.stale_profile = "provider9"
                        with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())):
                            with patch("bot.fcodex.os.execvpe") as mock_exec:
                                with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                                    fcodex_main()

        mock_save.assert_called_once_with("")
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_slash_session_uses_shared_current_dir_listing(self) -> None:
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="hello",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
            model_provider="provider2",
        )
        stdout = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.list_current_dir_threads", return_value=[thread]) as mock_list:
                with patch("bot.fcodex.CodexAppServerAdapter"):
                    with patch("bot.fcodex.sys.stdout", stdout):
                        with patch("sys.argv", ["fcodex", "/session"]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_list.call_args.kwargs["cwd"], os.getcwd())
        self.assertIn("thread-1", stdout.getvalue())
        self.assertIn("provider2", stdout.getvalue())
        self.assertIn("hello", stdout.getvalue())

    def test_fcodex_slash_session_global_uses_shared_global_listing(self) -> None:
        thread = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="hi",
            preview="hi",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
            model_provider="provider1",
        )
        stdout = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.list_global_threads", return_value=[thread]) as mock_list:
                with patch("bot.fcodex.CodexAppServerAdapter"):
                    with patch("bot.fcodex.sys.stdout", stdout):
                        with patch("sys.argv", ["fcodex", "/session", "global"]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_list.call_args.kwargs["limit"], 100)
        self.assertIn("thread-2", stdout.getvalue())
        self.assertIn("provider1", stdout.getvalue())

    def test_fcodex_slash_help_explains_upstream_picker_boundary(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.sys.stdout", stdout):
                with patch("sys.argv", ["fcodex", "/help"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("fcodex /help", stdout.getvalue())
        self.assertIn("fcodex /profile [name]", stdout.getvalue())
        self.assertIn("fcodex /rm <id|name>", stdout.getvalue())
        self.assertIn("fcodex /session", stdout.getvalue())
        self.assertIn("fcodex /resume <thread_id|thread_name>", stdout.getvalue())
        self.assertIn("进入 TUI 后，`/help`、`/resume` 等命令恢复 upstream 原样", stdout.getvalue())
        self.assertIn("`fcodex`、`fcodex <prompt>`、`fcodex resume <id>` 仍是 upstream Codex CLI", stdout.getvalue())

    def test_fcodex_non_slash_text_is_passthrough_prompt(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = ""
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())):
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "session"]):
                                fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "session"],
        )

    def test_fcodex_rejects_wrapper_command_mixed_with_prefix_flags(self) -> None:
        stderr = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "--cd", "/tmp/project", "/session"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("wrapper 自命令必须单独使用", stderr.getvalue())
        self.assertIn("fcodex /session [cwd|global]", stderr.getvalue())

    def test_fcodex_rejects_wrapper_command_with_extra_args(self) -> None:
        stderr = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/resume", "demo", "--model", "gpt-5"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("用法：fcodex /resume <thread_id|thread_name>", stderr.getvalue())

    def test_fcodex_rejects_unknown_slash_command_in_shell_wrapper(self) -> None:
        stderr = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/cd", "/tmp/project"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("未知 fcodex 自命令：/cd", stderr.getvalue())
        self.assertIn("shell 层只支持 `/help`、`/profile`、`/rm`、`/session`、`/resume`", stderr.getvalue())

    def test_fcodex_slash_profile_shows_runtime_summary(self) -> None:
        stdout = StringIO()
        runtime = RuntimeConfigSummary(
            current_profile="provider1",
            current_model_provider="provider1_api",
            profiles=[
                RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
            ],
        )
        mock_adapter = Mock()
        mock_adapter.read_runtime_config.return_value = runtime
        mock_adapter.stop.return_value = None
        resolution = Mock()
        resolution.effective_profile = "provider2"
        resolution.stale_profile = ""
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.CodexAppServerAdapter", return_value=mock_adapter):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend", return_value=resolution):
                    with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider2"):
                        with patch("bot.fcodex.sys.stdout", stdout):
                            with patch("sys.argv", ["fcodex", "/profile"]):
                                with self.assertRaises(SystemExit) as exc:
                                    fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("feishu-codex / fcodex 默认 profile：`provider2`", stdout.getvalue())
        self.assertIn("当前运行时 provider：`provider1_api`", stdout.getvalue())
        self.assertIn("`provider2` -> `provider2_api` <- 默认", stdout.getvalue())

    def test_fcodex_slash_profile_switches_local_default_profile(self) -> None:
        stdout = StringIO()
        runtime = RuntimeConfigSummary(
            current_profile="provider1",
            current_model_provider="provider1_api",
            profiles=[
                RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
            ],
        )
        mock_adapter = Mock()
        mock_adapter.read_runtime_config.return_value = runtime
        mock_adapter.stop.return_value = None
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.CodexAppServerAdapter", return_value=mock_adapter):
                with patch("bot.fcodex.ProfileStateStore.save_default_profile") as mock_save:
                    with patch("bot.fcodex.sys.stdout", stdout):
                        with patch("sys.argv", ["fcodex", "/profile", "provider2"]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        mock_save.assert_called_once_with("provider2")
        self.assertIn("默认 profile 已切换为：`provider2`", stdout.getvalue())
        self.assertIn("对应 provider：`provider2_api`", stdout.getvalue())

    def test_fcodex_slash_rm_archives_thread(self) -> None:
        stdout = StringIO()
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        thread = ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        mock_adapter = Mock()
        mock_adapter.read_thread.return_value.summary = thread
        mock_adapter.stop.return_value = None
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.CodexAppServerAdapter", return_value=mock_adapter):
                with patch("bot.fcodex.sys.stdout", stdout):
                    with patch("sys.argv", ["fcodex", "/rm", thread_id]):
                        with self.assertRaises(SystemExit) as exc:
                            fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        mock_adapter.archive_thread.assert_called_once_with(thread_id)
        self.assertIn("已归档线程：`019d2e94…` demo", stdout.getvalue())
        self.assertIn("不是硬删除", stdout.getvalue())

    def test_fcodex_slash_resume_resolves_name(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_profile_resolve:
                    mock_profile_resolve.return_value.effective_profile = ""
                    mock_profile_resolve.return_value.stale_profile = ""
                with patch("bot.fcodex.resolve_resume_name_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value = ThreadSummary(
                        thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
                        cwd="/tmp/project",
                        name="demo",
                        preview="hello",
                        created_at=0,
                        updated_at=0,
                        source="cli",
                        status="notLoaded",
                    )
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9100", Mock())):
                        with patch("bot.fcodex.sys.stdout", stdout):
                            with patch("bot.fcodex.os.execvpe") as mock_exec:
                                with patch("sys.argv", ["fcodex", "/resume", "demo"]):
                                    fcodex_main()

        self.assertEqual(mock_resolve.call_args.kwargs["target"], "demo")
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9100", "--cd", os.getcwd(), "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_explicit_cd_is_forwarded_to_proxy(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = ""
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex._launch_local_cwd_proxy", return_value=("ws://127.0.0.1:9101", Mock())) as mock_proxy:
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "--cd", "/home/tester/project"]):
                                fcodex_main()

        mock_proxy.assert_called_once_with("ws://127.0.0.1:8765", "/home/tester/project")
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9101", "--cd", "/home/tester/project"],
        )

    def test_launch_local_cwd_proxy_passes_parent_pid(self) -> None:
        process = Mock()
        process.stdout.readline.return_value = "ws://127.0.0.1:9100\n"
        process.poll.return_value = None
        with patch("bot.fcodex.os.getpid", return_value=4321):
            with patch("bot.fcodex.subprocess.Popen", return_value=process) as mock_popen:
                proxy_url, _ = _launch_local_cwd_proxy(
                    "ws://127.0.0.1:8765",
                    "/tmp/project",
                )

        self.assertEqual(proxy_url, "ws://127.0.0.1:9100")
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--parent-pid", cmd)
        self.assertIn("4321", cmd)

    def test_thread_start_proxy_rewrites_only_missing_cwd(self) -> None:
        rewritten = _rewrite_thread_start_cwd(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "thread/start",
                    "params": {"approvalPolicy": "on-request"},
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
                "params": {"approvalPolicy": "on-request", "cwd": "/tmp/project"},
            },
        )

    def test_thread_start_proxy_keeps_existing_cwd_and_other_methods(self) -> None:
        original_start = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "thread/start",
                "params": {"cwd": "/srv/already-set"},
            }
        )
        original_resume = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "thread/resume",
                "params": {},
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

    def test_proxy_stays_alive_across_resume_style_reconnect(self) -> None:
        backend_url_queue: queue.Queue[str] = queue.Queue()
        backend_server_ref: dict[str, object] = {}

        def _backend_handler(ws) -> None:
            for message in ws:
                ws.send(message)

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
                "idle_timeout_seconds": 0.3,
                "on_listen": proxy_url_queue.put,
            },
            daemon=True,
        )
        proxy_thread.start()
        proxy_url = proxy_url_queue.get(timeout=1)

        try:
            with connect(proxy_url, open_timeout=1, max_size=None) as ws:
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
                echoed = json.loads(ws.recv())
                self.assertEqual(echoed["params"]["cwd"], "/tmp/project")

            time.sleep(0.1)

            with connect(proxy_url, open_timeout=1, max_size=None) as ws:
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
                echoed = json.loads(ws.recv())
                self.assertEqual(echoed["method"], "thread/resume")
                self.assertNotIn("cwd", echoed["params"])

            proxy_thread.join(timeout=1)
            self.assertFalse(proxy_thread.is_alive())
        finally:
            backend_server = backend_server_ref.get("server")
            if backend_server is not None:
                backend_server.shutdown()
            backend_thread.join(timeout=1)


class SessionResolutionTests(unittest.TestCase):
    class _Adapter:
        def __init__(self, threads: list[ThreadSummary]) -> None:
            self.threads = threads

        def list_threads_all(self, **kwargs):
            self.kwargs = kwargs
            return list(self.threads)

    def test_looks_like_thread_id(self) -> None:
        self.assertTrue(looks_like_thread_id("019d2e94-a475-7bc1-b2f7-a3ce37628ede"))
        self.assertFalse(looks_like_thread_id("demo"))

    def test_format_thread_match(self) -> None:
        thread = ThreadSummary(
            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            model_provider="provider2_api",
        )
        self.assertEqual(format_thread_match(thread), "`019d2e94…`@`provider2_api`")

    def test_resolve_resume_target_by_name_uses_cross_provider_listing(self) -> None:
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            model_provider="provider2_api",
        )
        adapter = self._Adapter([thread])

        resolved = resolve_resume_target_by_name(adapter, name="demo", limit=100)

        self.assertEqual(resolved.thread_id, "thread-1")
        self.assertEqual(adapter.kwargs["model_providers"], [])

    def test_resolve_resume_target_by_name_rejects_multiple_matches(self) -> None:
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        adapter = self._Adapter([thread_1, thread_2])

        with self.assertRaisesRegex(ValueError, "匹配到多个同名线程"):
            resolve_resume_target_by_name(adapter, name="demo", limit=100)


class ProfileResolutionTests(unittest.TestCase):
    def test_resolve_local_default_profile_keeps_existing_profile(self) -> None:
        runtime = _FakeRpc().request("config/read")["config"]
        from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary

        resolution = resolve_local_default_profile(
            "provider2",
            RuntimeConfigSummary(
                current_profile="provider1",
                current_model_provider="provider1_api",
                profiles=[
                    RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                    RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
                ],
            ),
        )
        self.assertEqual(resolution.effective_profile, "provider2")
        self.assertEqual(resolution.stale_profile, "")

    def test_resolve_local_default_profile_marks_missing_profile_as_stale(self) -> None:
        from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary

        resolution = resolve_local_default_profile(
            "provider9",
            RuntimeConfigSummary(
                current_profile="provider1",
                current_model_provider="provider1_api",
                profiles=[
                    RuntimeProfileSummary(name="provider1", model_provider="provider1_api"),
                    RuntimeProfileSummary(name="provider2", model_provider="provider2_api"),
                ],
            ),
        )
        self.assertEqual(resolution.effective_profile, "")
        self.assertEqual(resolution.stale_profile, "provider9")


if __name__ == "__main__":
    unittest.main()
