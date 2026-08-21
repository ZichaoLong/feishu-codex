import unittest
from unittest.mock import Mock, patch

from bot.adapters.base import (
    RuntimeModelSummary,
    RuntimeReasoningEffortOption,
)
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.codex_thread_summary import thread_summary_from_app_server_thread
from bot.codex_protocol.connection import (
    CodexRpcProtocolError,
)


from tests.codex_app_server_test_support import _FakeRpc


class CodexAppServerInventoryTests(unittest.TestCase):
    def test_thread_source_shape_is_preserved_as_authority_evidence(self) -> None:
        base = {
            "id": "thread-source",
            "historyMode": "legacy",
            "status": {"type": "idle", "activeFlags": []},
        }

        valid_root = thread_summary_from_app_server_thread(
            {**base, "source": "cli"}
        )
        self.assertEqual(valid_root.source_status, "known")
        self.assertEqual(valid_root.source, "cli")

        valid_spawn = thread_summary_from_app_server_thread(
            {
                **base,
                "source": {
                    "subAgent": {
                        "thread_spawn": {
                            "parent_thread_id": "root-1",
                            "depth": 1,
                        }
                    }
                },
            }
        )
        self.assertEqual(valid_spawn.source_status, "known")
        self.assertEqual(valid_spawn.subagent_kind, "threadSpawn")

        for source in (
            None,
            "future-source",
            {"subAgent": {"thread_spawn": {}}},
            {"unexpected": "shape"},
        ):
            with self.subTest(source=source):
                summary = thread_summary_from_app_server_thread(
                    {**base, "source": source}
                )
                self.assertNotEqual(summary.source_status, "known")

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

    def test_list_threads_can_request_archived_threads(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.list_threads(
            cwd="/tmp/project", limit=5, model_providers=[], archived=True
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/list",
                {
                    "cwd": "/tmp/project",
                    "limit": 5,
                    "sourceKinds": ["cli", "vscode", "exec", "appServer"],
                    "archived": True,
                    "modelProviders": [],
                },
            ),
        )

    def test_relationship_thread_list_omits_default_source_filter(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.list_threads(limit=5, parent_thread_id="thread-root")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/list",
                {
                    "limit": 5,
                    "parentThreadId": "thread-root",
                },
            ),
        )

    def test_existing_authority_response_forwards_bounded_no_reconnect_options(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        adapter._rpc = fake_rpc

        adapter.respond_with_existing_backend_authority(
            "request-1",
            connection_generation=7,
            error={"code": -32000, "message": "cancelled"},
            timeout=0.25,
        )

        fake_rpc.respond.assert_called_once_with(
            "request-1",
            result=None,
            error={"code": -32000, "message": "cancelled"},
            timeout=0.25,
            require_existing_connection=True,
            expected_connection_generation=7,
        )

    def test_connection_generation_forwards_bounded_no_reconnect_options(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.connection_generation.return_value = 7
        adapter._rpc = fake_rpc

        generation = adapter.connection_generation(
            timeout=0.25,
            require_existing_connection=True,
        )

        self.assertEqual(generation, 7)
        fake_rpc.connection_generation.assert_called_once_with(
            timeout=0.25,
            require_existing_connection=True,
        )

    def test_backend_reset_generation_fence_is_a_narrow_rpc_forwarder(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        adapter._rpc = fake_rpc
        fence = Mock()

        adapter.fence_backend_reset_generation(
            expected_connection_generation=7,
            fence_ingress=fence,
            timeout=0.25,
        )

        fake_rpc.fence_backend_reset_generation.assert_called_once_with(
            expected_connection_generation=7,
            fence_ingress=fence,
            timeout=0.25,
        )

    def test_list_threads_all_rejects_non_advancing_pages(self) -> None:
        thread_1 = {
            "id": "child-1",
            "cwd": "/tmp/project",
            "createdAt": 0,
            "updatedAt": 0,
            "source": "cli",
            "status": {"type": "idle", "activeFlags": []},
        }
        thread_2 = {**thread_1, "id": "child-2"}

        class _PagedRpc(_FakeRpc):
            def __init__(self, responses: list[dict]) -> None:
                super().__init__()
                self.responses = list(responses)

            def request(
                self,
                method: str,
                params: dict | None = None,
                *,
                timeout: float | None = None,
            ) -> dict:
                del timeout
                payload = params or {}
                self.calls.append((method, payload))
                if method == "thread/list":
                    return self.responses.pop(0)
                return {"ok": True}

        cases = (
            [{"data": [], "nextCursor": "next"}],
            [
                {"data": [thread_1], "nextCursor": "next"},
                {"data": [thread_2], "nextCursor": "next"},
            ],
        )
        for responses in cases:
            with self.subTest(responses=responses):
                adapter = CodexAppServerAdapter(CodexAppServerConfig())
                adapter._rpc = _PagedRpc(responses)
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.list_threads_all(limit=5)

    def test_relation_inventory_rejects_duplicate_thread_ids_across_pages(self) -> None:
        thread_1 = {
            "id": "child-1",
            "cwd": "/tmp/project",
            "createdAt": 0,
            "updatedAt": 0,
            "source": "cli",
            "status": {"type": "idle", "activeFlags": []},
        }

        class _RelationPagedRpc(_FakeRpc):
            def __init__(self) -> None:
                super().__init__()
                self.responses = [
                    {"data": [thread_1], "nextCursor": "next"},
                    {"data": [thread_1], "nextCursor": None},
                ]

            def request(
                self,
                method: str,
                params: dict | None = None,
                *,
                timeout: float | None = None,
            ) -> dict:
                del timeout
                payload = params or {}
                self.calls.append((method, payload))
                if method == "thread/list":
                    return self.responses.pop(0)
                return {"ok": True}

        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        adapter._rpc = _RelationPagedRpc()

        with self.assertRaises(CodexRpcProtocolError):
            adapter.list_threads_all(limit=5, parent_thread_id="root-1")

    def test_list_threads_rejects_missing_or_invalid_page_boundary(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        for response in (
            {"nextCursor": None},
            {"data": []},
            {"data": [], "nextCursor": ""},
            {"data": [], "nextCursor": 123},
        ):
            with self.subTest(response=response):
                with patch.object(fake_rpc, "request", return_value=response):
                    with self.assertRaises(CodexRpcProtocolError):
                        adapter.list_threads(parent_thread_id="root-1")

    def test_list_threads_rejects_invalid_inventory_entries(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        valid_thread = {
            "id": "child-1",
            "cwd": "/tmp/project",
            "createdAt": 0,
            "updatedAt": 0,
            "source": "cli",
            "status": {"type": "idle", "activeFlags": []},
        }
        for response in (
            {"data": ["not-an-object"], "nextCursor": None},
            {"data": [{**valid_thread, "id": ""}], "nextCursor": None},
            {"data": [valid_thread, dict(valid_thread)], "nextCursor": None},
        ):
            with self.subTest(response=response):
                with patch.object(fake_rpc, "request", return_value=response):
                    with self.assertRaises(CodexRpcProtocolError):
                        adapter.list_threads(parent_thread_id="root-1")

    def test_loaded_thread_inventory_rejects_incomplete_or_invalid_response(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        for response in (
            {"nextCursor": None},
            {"data": []},
            {"data": [], "nextCursor": "more"},
            {"data": ["child-1", "child-1"], "nextCursor": None},
            {"data": [" child-1 "], "nextCursor": None},
            {"data": [123], "nextCursor": None},
        ):
            with self.subTest(response=response):
                with patch.object(fake_rpc, "request", return_value=response):
                    with self.assertRaises(CodexRpcProtocolError):
                        adapter.list_loaded_thread_ids()

    def test_thread_summary_preserves_spawned_subagent_metadata(self) -> None:
        summary = CodexAppServerAdapter._summary_from_thread(
            {
                "id": "child-1",
                "historyMode": "legacy",
                "cwd": "/tmp/project",
                "createdAt": 1,
                "updatedAt": 2,
                "source": {
                    "subAgent": {
                        "threadSpawn": {
                            "parentThreadId": "thread-root",
                            "depth": 1,
                        }
                    }
                },
                "parentThreadId": "thread-root",
                "canAcceptDirectInput": False,
                "agentNickname": "Explorer",
                "agentRole": "explorer",
                "ephemeral": False,
                "status": {"type": "active", "activeFlags": []},
            }
        )

        self.assertEqual(summary.source, "subAgent")
        self.assertEqual(summary.subagent_kind, "threadSpawn")
        self.assertEqual(summary.parent_thread_id, "thread-root")
        self.assertFalse(summary.can_accept_direct_input)
        self.assertEqual(summary.agent_nickname, "Explorer")

    def test_read_runtime_config_parses_model_provider_and_memory_mode(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        runtime = adapter.read_runtime_config()

        self.assertEqual(runtime.current_model_provider, "provider1_api")
        self.assertEqual(runtime.current_memory_mode, "read")

    def test_list_models_reads_visible_models(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        models = adapter.list_models()

        self.assertEqual(fake_rpc.calls[0], ("model/list", {}))
        self.assertEqual(
            models,
            [
                RuntimeModelSummary(
                    model="gpt-5.3-codex",
                    display_name=None,
                    is_default=True,
                    hidden=False,
                    default_reasoning_effort="medium",
                    supported_reasoning_efforts=[
                        RuntimeReasoningEffortOption(
                            reasoning_effort="low", description="Fast"
                        ),
                        RuntimeReasoningEffortOption(
                            reasoning_effort="medium", description="Balanced"
                        ),
                        RuntimeReasoningEffortOption(
                            reasoning_effort="high", description="Deep"
                        ),
                    ],
                ),
                RuntimeModelSummary(
                    model="gpt-5.4", display_name=None, is_default=False, hidden=False
                ),
            ],
        )

    def test_list_models_reads_every_page(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())

        class _PagedModelsRpc:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []
                self.responses = [
                    {
                        "data": [{"id": "catalog-1", "model": "model-1"}],
                        "nextCursor": "page-2",
                    },
                    {
                        "data": [{"id": "catalog-2", "model": "model-2"}],
                        "nextCursor": None,
                    },
                ]

            def request(
                self,
                method: str,
                params: dict | None = None,
                *,
                timeout: float | None = None,
            ) -> dict:
                self.assert_timeout(timeout)
                self.calls.append((method, params or {}))
                return self.responses.pop(0)

            @staticmethod
            def assert_timeout(timeout: float | None) -> None:
                if timeout is None or not 0 < timeout <= 30:
                    raise AssertionError(
                        f"missing bounded model/list timeout: {timeout!r}"
                    )

        fake_rpc = _PagedModelsRpc()
        adapter._rpc = fake_rpc

        models = adapter.list_models()

        self.assertEqual([model.model for model in models], ["model-1", "model-2"])
        self.assertEqual(
            fake_rpc.calls,
            [
                ("model/list", {}),
                ("model/list", {"cursor": "page-2"}),
            ],
        )

    def test_list_models_rejects_ambiguous_or_non_progressing_pages(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())

        for responses in (
            [
                {
                    "data": [
                        {"id": "catalog-1", "model": "model-1"},
                        {"id": "catalog-2", "model": "model-1"},
                    ],
                    "nextCursor": None,
                },
            ],
            [
                {
                    "data": [
                        {"id": "catalog-1", "model": "model-1"},
                        {"id": "catalog-1", "model": "model-2"},
                    ],
                    "nextCursor": None,
                },
            ],
            [
                {
                    "data": [{"id": "catalog-1", "model": "model-1"}],
                    "nextCursor": "same",
                },
                {
                    "data": [{"id": "catalog-2", "model": "model-2"}],
                    "nextCursor": "same",
                },
            ],
            [
                {
                    "data": [{"id": "catalog-1", "model": "model-1"}],
                    "nextCursor": "more",
                },
                {"data": [{"id": "catalog-1", "model": "model-2"}], "nextCursor": None},
            ],
            [
                {
                    "data": [{"id": "catalog-1", "model": "model-1"}],
                    "nextCursor": "more",
                },
                {"data": [{"id": "catalog-2", "model": "model-1"}], "nextCursor": None},
            ],
            [{"data": [], "nextCursor": "more"}],
        ):
            with self.subTest(responses=responses):
                fake_rpc = Mock()
                fake_rpc.request.side_effect = list(responses)
                adapter._rpc = fake_rpc
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.list_models()

    def test_list_models_rejects_invalid_page_boundary(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        for response in (
            {"data": []},
            {"data": [], "nextCursor": ""},
            {"data": [], "nextCursor": 123},
        ):
            with self.subTest(response=response):
                fake_rpc = Mock()
                fake_rpc.request.return_value = response
                adapter._rpc = fake_rpc
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.list_models()

    def test_list_models_rejects_malformed_catalog_items_instead_of_hiding_them(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        malformed_items = (
            "not-an-object",
            {},
            {"model": 123},
            {"model": "model-1", "isDefault": "false"},
            {"model": "model-1", "hidden": 0},
            {"model": "model-1", "id": 123},
            {"model": "model-1", "description": None},
            {"model": "model-1", "serviceTiers": None},
            {"model": "model-1", "supportedReasoningEfforts": None},
            {"model": "model-1", "supportedReasoningEfforts": "high"},
            {
                "model": "model-1",
                "supportedReasoningEfforts": ["high"],
            },
            {
                "model": "model-1",
                "supportedReasoningEfforts": [{}],
            },
        )
        for item in malformed_items:
            with self.subTest(item=item):
                fake_rpc = Mock()
                fake_rpc.request.return_value = {
                    "data": [item],
                    "nextCursor": None,
                }
                adapter._rpc = fake_rpc
                with self.assertRaises(CodexRpcProtocolError):
                    adapter.list_models()
