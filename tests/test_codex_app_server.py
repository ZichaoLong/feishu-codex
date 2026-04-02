import unittest
from unittest.mock import patch

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcClient
from bot.fcodex import main as fcodex_main


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
        return {"ok": True}


class CodexAppServerAdapterTests(unittest.TestCase):
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
    def test_fcodex_injects_remote_url(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.os.execvpe") as mock_exec:
                with patch("sys.argv", ["fcodex", "resume", "thread-1"]):
                    fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:8765", "resume", "thread-1"],
        )

    def test_fcodex_respects_explicit_remote_arg(self) -> None:
        with patch("bot.fcodex.load_config_file", return_value={"codex_command": "codex", "app_server_url": "ws://127.0.0.1:8765"}):
            with patch("bot.fcodex.os.execvpe") as mock_exec:
                with patch("sys.argv", ["fcodex", "--remote", "ws://127.0.0.1:9900", "resume"]):
                    fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            ["codex", "--remote", "ws://127.0.0.1:9900", "resume"],
        )


if __name__ == "__main__":
    unittest.main()
