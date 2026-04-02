import os
import unittest
from unittest.mock import patch
from io import StringIO
from pathlib import Path

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcClient
from bot.fcodex import _default_data_dir, main as fcodex_main
from bot.profile_resolution import resolve_local_default_profile
from bot.session_resolution import (
    format_thread_match,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)
from bot.adapters.base import ThreadSummary


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

    def test_start_turn_default_mode_omits_collaboration_mode(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp")

        self.assertEqual(fake_rpc.calls, [("turn/start", {"threadId": "thread-1", "input": [{"type": "text", "text": "hello"}], "cwd": "/tmp", "approvalPolicy": "on-request", "approvalsReviewer": "user", "personality": "pragmatic"})])

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

        self.assertEqual(len(fake_rpc.calls), 1)
        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertNotIn("collaborationMode", params)

    def test_start_turn_can_attach_profile_override(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(thread_id="thread-1", text="hello", cwd="/tmp", profile="provider2")

        self.assertEqual(fake_rpc.calls[0][0], "turn/start")
        self.assertEqual(fake_rpc.calls[0][1]["config"], {"profile": "provider2"})

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

    def test_config_rejects_invalid_collaboration_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAppServerConfig.from_dict({"collaboration_mode": "broken"})

    def test_config_rejects_invalid_app_server_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAppServerConfig.from_dict({"app_server_mode": "broken"})


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


class FCodexTests(unittest.TestCase):
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
                with patch("bot.fcodex.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                        fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:8765", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_injects_default_profile_when_not_explicit(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider2"):
                with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                    mock_resolve.return_value.effective_profile = "provider2"
                    mock_resolve.return_value.stale_profile = ""
                    with patch("bot.fcodex.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                            fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:8765",
                "--profile",
                "provider2",
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )

    def test_fcodex_explicit_profile_wins(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider2"):
                with patch("bot.fcodex.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "-p", "provider1", "resume", "thread-1"]):
                        fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:8765", "-p", "provider1", "resume", "thread-1"],
        )

    def test_fcodex_resume_name_uses_shared_resolution(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
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
                    with patch("bot.fcodex.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "resume", "demo"]):
                            fcodex_main()

        self.assertEqual(mock_resolve.call_args.kwargs["target"], "demo")
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:8765",
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )

    def test_fcodex_explicit_remote_skips_shared_resolution(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.resolve_resume_name_via_remote_backend") as mock_resolve:
                    with patch("bot.fcodex.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "--remote", "ws://127.0.0.1:9900", "resume", "demo"]):
                            fcodex_main()

        mock_resolve.assert_not_called()
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9900", "resume", "demo"],
        )

    def test_fcodex_respects_explicit_remote_arg(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value=""):
                with patch("bot.fcodex.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "--remote", "ws://127.0.0.1:9900", "resume"]):
                        fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9900", "resume"],
        )

    def test_fcodex_clears_stale_local_default_profile(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.ProfileStateStore.load_default_profile", return_value="provider9"):
                with patch("bot.fcodex.ProfileStateStore.save_default_profile") as mock_save:
                    with patch("bot.fcodex.resolve_local_default_profile_via_remote_backend") as mock_resolve:
                        mock_resolve.return_value.effective_profile = ""
                        mock_resolve.return_value.stale_profile = "provider9"
                        with patch("bot.fcodex.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"]):
                                fcodex_main()

        mock_save.assert_called_once_with("")
        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:8765", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
        )

    def test_fcodex_sessions_uses_shared_current_dir_listing(self) -> None:
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
                        with patch("sys.argv", ["fcodex", "sessions"]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_list.call_args.kwargs["cwd"], os.getcwd())
        self.assertIn("thread-1", stdout.getvalue())
        self.assertIn("provider2", stdout.getvalue())
        self.assertIn("hello", stdout.getvalue())

    def test_fcodex_sessions_global_uses_shared_global_listing(self) -> None:
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
                        with patch("sys.argv", ["fcodex", "sessions", "global"]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_list.call_args.kwargs["limit"], 100)
        self.assertIn("thread-2", stdout.getvalue())
        self.assertIn("provider1", stdout.getvalue())

    def test_fcodex_help_resume_explains_upstream_picker_boundary(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.sys.stdout", stdout):
                with patch("sys.argv", ["fcodex", "help-resume"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 0)
        self.assertIn("fcodex sessions", stdout.getvalue())
        self.assertIn("fcodex resume <thread_name>", stdout.getvalue())
        self.assertIn("TUI 内置 /resume", stdout.getvalue())


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
