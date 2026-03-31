import unittest
from unittest.mock import patch

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcClient


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

    def test_config_rejects_invalid_collaboration_mode(self) -> None:
        with self.assertRaises(ValueError):
            CodexAppServerConfig.from_dict({"collaboration_mode": "broken"})


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


if __name__ == "__main__":
    unittest.main()
