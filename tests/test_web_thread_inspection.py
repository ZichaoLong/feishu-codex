from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest.mock import Mock

from bot.adapter_ingress_gate import AdapterOutboundRequestEpochLost
from bot.adapters.base import (
    ThreadItemEntry,
    ThreadItemsPage,
    ThreadSearchOccurrence,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
)
from bot.web_runtime.contract import WebRuntimeError
from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.selection_coordinator import WebSelectionCoordinator
from bot.web_runtime.thread_inspection import (
    WebThreadInspectionEffect,
    WebThreadInspectionPorts,
    WebThreadInspectionPreparation,
    WebThreadInspectionService,
)
from bot.web_runtime.thread_read_model import WebThreadReadModel
from tests.web_runtime.harness import WebRuntimeControllerHarness


def _snapshot(
    *,
    history_mode: str | None = "paginated",
    subagent_kind: str | None = None,
) -> ThreadSnapshot:
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id="thread-1",
            cwd="/workspace",
            name="Demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="appServer",
            status="idle",
            subagent_kind=subagent_kind,
            history_mode=history_mode,  # type: ignore[arg-type]
        )
    )


def _command(
    item_id: str = "cmd-1",
    *,
    status: str = "completed",
    command: str = "pytest -q",
) -> dict:
    return {
        "id": item_id,
        "type": "commandExecution",
        "pluginId": None,
        "scriptPath": None,
        "command": command,
        "cwd": "/workspace",
        "processId": None,
        "source": "agent",
        "status": status,
        "commandActions": [],
        "aggregatedOutput": "2 passed",
        "exitCode": 0,
        "durationMs": 1,
    }


def _file_change(
    item_id: str = "patch-1",
    *,
    changes: list[dict] | None = None,
    status: str = "completed",
) -> dict:
    return {
        "id": item_id,
        "type": "fileChange",
        "status": status,
        "changes": changes
        if changes is not None
        else [
            {
                "path": "a.py",
                "kind": {"type": "update", "move_path": None},
                "diff": "@@ -1 +1 @@\n-old\n+new",
            }
        ],
    }


class WebThreadInspectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.in_runtime_context = True

        def require_runtime_context() -> None:
            if not self.in_runtime_context:
                raise AssertionError("inspection external effect entered RuntimeLoop")

        self.runtime_context_guard = Mock(side_effect=require_runtime_context)
        self.documents = WebDocumentRegistry(
            runtime_context_guard=self.runtime_context_guard
        )
        self.profile_store = WebWriterProfileStore(
            pathlib.Path(self.temp_dir.name)
        )
        self.selection = WebSelectionCoordinator(
            profile_store=self.profile_store,
            document_registry=self.documents,
            runtime_interest=WebRuntimeInterestRegistry(),
        )
        self.profile_store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir="/workspace",
        )
        self.documents.materialize_thread("tab-1", "thread-1")
        self.direct_targets = Mock(spec=WebDirectThreadTargetCoordinator)
        self.read_model = WebThreadReadModel()
        self.read_thread = Mock(return_value=_snapshot())
        self.list_items = Mock(
            return_value=ThreadItemsPage(
                items=[
                    ThreadItemEntry(
                        turn_id="turn-1",
                        item=_command(),
                    )
                ]
            )
        )
        self.search = Mock(return_value=ThreadSearchOccurrencesPage())
        self.coordinates = Mock(
            return_value={"runtime_epoch": "epoch-1", "revision": 7}
        )
        self.connection_generation = 17
        self.capture_connection_generation = Mock(
            side_effect=lambda: self.connection_generation
        )
        self.settled_generations: list[int] = []
        self.in_generation_settle = False

        def run_if_connection_generation(generation, callback):
            self.settled_generations.append(generation)
            if generation != self.connection_generation:
                raise AdapterOutboundRequestEpochLost(
                    "backend connection generation changed before inspection settle"
                )
            self.in_generation_settle = True
            try:
                return callback()
            finally:
                self.in_generation_settle = False

        self.run_if_connection_generation = Mock(
            side_effect=run_if_connection_generation
        )
        self.service = WebThreadInspectionService(
            documents=self.documents,
            selection=self.selection,
            direct_targets=self.direct_targets,
            ports=WebThreadInspectionPorts(
                read_thread=self.read_thread,
                list_thread_items=self.list_items,
                search_thread_occurrences=self.search,
                coordinates=self.coordinates,
                capture_observation=self.read_model.capture_observation,
                observation_is_current=self.read_model.observation_is_current,
                capture_connection_generation=self.capture_connection_generation,
                run_if_connection_generation=self.run_if_connection_generation,
            ),
            runtime_context_guard=self.runtime_context_guard,
            monotonic=lambda: 0.0,
        )

    def _execute(
        self,
        service: WebThreadInspectionService,
        prepared: WebThreadInspectionPreparation,
    ) -> WebThreadInspectionEffect:
        self.in_runtime_context = False
        try:
            return service.execute_inspection(prepared)
        finally:
            self.in_runtime_context = True

    def _read_tool_detail(
        self,
        client_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        *,
        view: str = "preview",
        change_index: int | None = None,
        cursor: str | None = None,
        service: WebThreadInspectionService | None = None,
    ) -> dict:
        owner = self.service if service is None else service
        prepared = owner.prepare_tool_detail(
            client_id,
            thread_id,
            turn_id,
            item_id,
            view=view,
            change_index=change_index,
            cursor=cursor,
        )
        return owner.settle_inspection(prepared, self._execute(owner, prepared))

    def _search_conversation(
        self,
        client_id: str,
        thread_id: str,
        *,
        query: str,
        cursor: str | None = None,
    ) -> dict:
        prepared = self.service.prepare_conversation_search(
            client_id,
            thread_id,
            query=query,
            cursor=cursor,
        )
        return self.service.settle_inspection(
            prepared,
            self._execute(self.service, prepared),
        )

    def test_command_detail_uses_exact_selected_proof_and_one_upstream_page(self) -> None:
        self.direct_targets.remember_verified_snapshot.side_effect = (
            lambda _snapshot: self.assertTrue(self.in_generation_settle)
        )
        result = self._read_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
        )

        self.read_thread.assert_called_once_with(
            "thread-1",
            False,
            timeout=2.0,
            expected_connection_generation=17,
        )
        self.list_items.assert_called_once_with(
            "thread-1",
            turn_id="turn-1",
            cursor=None,
            limit=100,
            sort_direction="asc",
            timeout=2.0,
            expected_connection_generation=17,
        )
        self.assertEqual(
            {key: result[key] for key in ("runtime_epoch", "revision", "thread_id")},
            {"runtime_epoch": "epoch-1", "revision": 7, "thread_id": "thread-1"},
        )
        self.assertEqual(result["kind"], "commandExecution")
        self.assertEqual(result["view"], "preview")
        self.assertIsNone(result["change_index"])
        self.assertEqual(result["status"], "found")
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(result["scanned_items"], 1)
        self.assertEqual(
            result["detail"]["tool"]["inspectionLocator"],
            {
                "turn_id": "turn-1",
                "item_id": "cmd-1",
                "kind": "commandExecution",
                "change_index": None,
            },
        )
        self.assertEqual(result["detail"]["tool"]["output"], ["2 passed"])
        self.direct_targets.remember_verified_snapshot.assert_called_once_with(
            self.read_thread.return_value
        )

    def test_tool_scan_returns_a_cursor_page_without_server_side_loop(self) -> None:
        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry(turn_id="turn-1", item=_command("other"))],
            next_cursor="cursor-1",
        )

        result = self._read_tool_detail(
            "tab-1", "thread-1", "turn-1", "cmd-1"
        )

        self.assertEqual(result["status"], "scanning")
        self.assertEqual(result["next_cursor"], "cursor-1")
        self.assertEqual(result["scanned_items"], 1)
        self.list_items.assert_called_once_with(
            "thread-1",
            turn_id="turn-1",
            cursor=None,
            limit=100,
            sort_direction="asc",
            timeout=2.0,
            expected_connection_generation=17,
        )

        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry(turn_id="turn-1", item=_command())],
        )
        result = self._read_tool_detail(
            "tab-1", "thread-1", "turn-1", "cmd-1", cursor="cursor-1"
        )
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["item_id"], "cmd-1")
        self.assertEqual(
            [call.kwargs["cursor"] for call in self.list_items.call_args_list],
            [None, "cursor-1"],
        )

    def test_authoritative_exhaustion_is_not_found(self) -> None:
        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry(turn_id="turn-1", item=_command("other"))]
        )

        result = self._read_tool_detail(
            "tab-1", "thread-1", "turn-1", "missing"
        )
        self.assertEqual(result["status"], "not_found")
        self.assertIsNone(result["detail"])
        self.assertEqual(result["scanned_items"], 1)

    def test_scan_has_no_focus_page_or_item_ceiling(self) -> None:
        result = None
        for index in range(9):
            cursor = None if index == 0 else f"cursor-{index - 1}"
            next_cursor = f"cursor-{index}" if index < 8 else None
            self.list_items.return_value = ThreadItemsPage(
                items=[ThreadItemEntry("turn-1", _command(f"other-{index}"))],
                next_cursor=next_cursor,
            )
            result = self._read_tool_detail(
                "tab-1", "thread-1", "turn-1", "missing", cursor=cursor
            )
        assert result is not None
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.list_items.call_count, 9)

    def test_repeated_or_empty_progress_page_is_protocol_error(self) -> None:
        cases = (
            (ThreadItemsPage(
                items=[ThreadItemEntry("turn-1", _command("other-1"))],
                next_cursor="same",
            ), "same"),
            (ThreadItemsPage(items=[], next_cursor="cursor-1"), None),
        )
        for page, cursor in cases:
            with self.subTest(items=len(page.items)):
                self.list_items.reset_mock(side_effect=True)
                self.list_items.return_value = page
                with self.assertRaises(WebRuntimeError) as caught:
                    self._read_tool_detail(
                        "tab-1", "thread-1", "turn-1", "missing", cursor=cursor
                    )
                self.assertEqual(
                    caught.exception.code,
                    "thread_inspection_protocol_error",
                )
                self.assertEqual(caught.exception.status, 502)

    def test_mismatched_turn_or_oversized_page_is_protocol_error(self) -> None:
        pages = (
            ThreadItemsPage(
                items=[ThreadItemEntry("turn-other", _command())],
            ),
            ThreadItemsPage(
                items=[
                    ThreadItemEntry("turn-1", _command(f"cmd-{index}"))
                    for index in range(101)
                ]
            ),
        )
        for page in pages:
            with self.subTest(items=len(page.items)):
                self.list_items.return_value = page
                with self.assertRaises(WebRuntimeError) as caught:
                    self._read_tool_detail(
                        "tab-1", "thread-1", "turn-1", "cmd-1"
                    )
                self.assertEqual(
                    caught.exception.code,
                    "thread_inspection_protocol_error",
                )

    def test_only_supported_terminal_items_and_exact_change_index_are_admitted(self) -> None:
        cases = (
            (
                {"id": "reason-1", "type": "reasoning", "status": "completed"},
                None,
                "tool_detail_unsupported_item",
            ),
            (_command(status="inProgress"), None, "tool_detail_not_terminal"),
            (_command(), 0, "tool_detail_unsupported_item"),
            (
                {
                    "id": "patch-1",
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [{"path": "a.py", "diff": "+new"}],
                },
                None,
                "tool_detail_unsupported_item",
            ),
        )
        for item, change_index, code in cases:
            with self.subTest(code=code, item_type=item["type"]):
                self.list_items.return_value = ThreadItemsPage(
                    items=[ThreadItemEntry("turn-1", item)]
                )
                with self.assertRaises(WebRuntimeError) as caught:
                    self._read_tool_detail(
                        "tab-1",
                        "thread-1",
                        "turn-1",
                        str(item["id"]),
                        change_index=change_index,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, 409)

    def test_legacy_thread_fails_closed_without_items_fallback(self) -> None:
        self.read_thread.return_value = _snapshot(
            history_mode="legacy"
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self._read_tool_detail(
                "tab-1", "thread-1", "turn-1", "cmd-1"
            )

        self.assertEqual(caught.exception.code, "thread_inspection_unavailable")
        self.list_items.assert_not_called()

    def test_upstream_unsupported_and_malformed_results_are_local_failures(self) -> None:
        cases = (
            (
                CodexRpcError(
                    "thread/items/list",
                    {"code": -32601, "message": "not supported yet"},
                ),
                "thread_inspection_upstream_unsupported",
                503,
            ),
            (
                CodexRpcProtocolError(
                    "thread/items/list",
                    "malformed response",
                ),
                "thread_inspection_protocol_error",
                502,
            ),
        )
        for error, code, status in cases:
            with self.subTest(code=code):
                self.list_items.side_effect = error
                with self.assertRaises(WebRuntimeError) as caught:
                    self._read_tool_detail(
                        "tab-1", "thread-1", "turn-1", "cmd-1"
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, status)
                self.list_items.reset_mock(side_effect=True)

    def test_tool_detail_encoded_response_is_hard_bounded(self) -> None:
        self.list_items.return_value = ThreadItemsPage(
            items=[
                ThreadItemEntry(
                    "turn-1",
                    _command(command="x" * (1024 * 1024)),
                )
            ]
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self._read_tool_detail(
                "tab-1", "thread-1", "turn-1", "cmd-1"
            )

        self.assertEqual(caught.exception.code, "tool_detail_too_large")
        self.assertEqual(caught.exception.status, 413)

    def test_full_command_preserves_persisted_output_without_focus_size_crop(self) -> None:
        persisted_output = "head\n" + ("middle\n" * 200_000) + "tail"
        command = _command()
        command.update(
            {
                "pluginId": "plugin-1",
                "scriptPath": "scripts/check.py",
                "processId": "pty-1",
                "commandActions": [
                    {
                        "type": "read",
                        "command": "cat a.py",
                        "name": "cat",
                        "path": "/workspace/a.py",
                    },
                    {"type": "listFiles", "command": "find", "path": None},
                    {
                        "type": "search",
                        "command": "rg needle",
                        "query": "needle",
                        "path": "src",
                    },
                    {"type": "unknown", "command": "opaque-command"},
                ],
                "aggregatedOutput": persisted_output,
            }
        )
        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry("turn-1", command)]
        )

        result = self._read_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="full",
        )

        self.assertEqual(result["view"], "full")
        self.assertEqual(result["status"], "found")
        self.assertEqual(
            result["detail"],
            {
                "view": "full",
                "source": {
                    "type": "commandExecution",
                    "id": "cmd-1",
                    "pluginId": "plugin-1",
                    "scriptPath": "scripts/check.py",
                    "command": "pytest -q",
                    "cwd": "/workspace",
                    "processId": "pty-1",
                    "source": "agent",
                    "status": "completed",
                    "commandActions": [
                        {
                            "type": "read",
                            "command": "cat a.py",
                            "name": "cat",
                            "path": "/workspace/a.py",
                        },
                        {"type": "listFiles", "command": "find", "path": None},
                        {
                            "type": "search",
                            "command": "rg needle",
                            "query": "needle",
                            "path": "src",
                        },
                        {"type": "unknown", "command": "opaque-command"},
                    ],
                    "aggregatedOutput": persisted_output,
                    "exitCode": 0,
                    "durationMs": 1,
                },
            },
        )

    def test_full_file_change_keeps_every_change_and_uses_index_only_as_focus(self) -> None:
        upstream_changes = [
            {"path": "added.py", "kind": {"type": "add"}, "diff": "+added"},
            {
                "path": "renamed.py",
                "kind": {"type": "update", "move_path": "old.py"},
                "diff": "@@ -1 +1 @@\n-before\n+after",
            },
            {"path": "deleted.py", "kind": {"type": "delete"}, "diff": "-deleted"},
        ]
        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry("turn-1", _file_change(changes=upstream_changes))]
        )

        result = self._read_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "patch-1",
            view="full",
            change_index=1,
        )

        self.assertEqual(result["view"], "full")
        self.assertEqual(result["change_index"], 1)
        self.assertEqual(
            result["detail"]["source"]["changes"],
            [
                {"path": "added.py", "kind": {"type": "add"}, "diff": "+added"},
                {
                    "path": "renamed.py",
                    "kind": {"type": "update", "movePath": "old.py"},
                    "diff": "@@ -1 +1 @@\n-before\n+after",
                },
                {"path": "deleted.py", "kind": {"type": "delete"}, "diff": "-deleted"},
            ],
        )
        self.assertEqual(len(result["detail"]["source"]["changes"]), 3)

    def test_full_file_change_null_move_path_projects_wire_name(self) -> None:
        self.list_items.return_value = ThreadItemsPage(
            items=[
                ThreadItemEntry(
                    "turn-1",
                    _file_change(
                        changes=[
                            {
                                "path": "edited.py",
                                "kind": {"type": "update", "move_path": None},
                                "diff": "@@ -1 +1 @@\n-before\n+after",
                            }
                        ]
                    ),
                )
            ]
        )

        result = self._read_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "patch-1",
            view="full",
            change_index=0,
        )

        self.assertEqual(
            result["detail"]["source"]["changes"],
            [
                {
                    "path": "edited.py",
                    "kind": {"type": "update", "movePath": None},
                    "diff": "@@ -1 +1 @@\n-before\n+after",
                }
            ],
        )

    def test_full_file_change_rejects_guessed_update_shapes(self) -> None:
        for raw_kind in (
            {"type": "update"},
            {"type": "update", "movePath": None},
        ):
            with self.subTest(raw_kind=raw_kind):
                self.list_items.return_value = ThreadItemsPage(
                    items=[
                        ThreadItemEntry(
                            "turn-1",
                            _file_change(
                                changes=[
                                    {
                                        "path": "edited.py",
                                        "kind": raw_kind,
                                        "diff": "@@ -1 +1 @@\n-before\n+after",
                                    }
                                ]
                            ),
                        )
                    ]
                )

                with self.assertRaises(WebRuntimeError) as caught:
                    self._read_tool_detail(
                        "tab-1",
                        "thread-1",
                        "turn-1",
                        "patch-1",
                        view="full",
                        change_index=0,
                    )

                self.assertEqual(caught.exception.code, "thread_inspection_protocol_error")
                self.assertEqual(caught.exception.status, 502)

    def test_full_source_shape_fails_only_the_current_detail_read(self) -> None:
        malformed = _command()
        malformed["commandActions"] = [{"type": "future", "command": "x"}]
        self.list_items.return_value = ThreadItemsPage(
            items=[ThreadItemEntry("turn-1", malformed)]
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self._read_tool_detail(
                "tab-1",
                "thread-1",
                "turn-1",
                "cmd-1",
                view="full",
            )

        self.assertEqual(caught.exception.code, "thread_inspection_protocol_error")
        self.assertEqual(caught.exception.status, 502)

    def test_tool_detail_view_is_closed_before_upstream_reads(self) -> None:
        with self.assertRaises(WebRuntimeError) as caught:
            self.service.prepare_tool_detail(
                "tab-1",
                "thread-1",
                "turn-1",
                "cmd-1",
                view="raw",
            )

        self.assertEqual(caught.exception.code, "invalid_tool_detail_view")
        self.assertEqual(caught.exception.status, 400)
        self.read_thread.assert_not_called()
        self.list_items.assert_not_called()

    def test_search_returns_one_bounded_page_and_echoes_canonical_inputs(self) -> None:
        self.search.return_value = ThreadSearchOccurrencesPage(
            occurrences=[
                ThreadSearchOccurrence(
                    turn_id="turn-2",
                    item_id="agent-2",
                    snippet="prefix needle suffix",
                    snippet_match_range=(7, 13),
                    turn_cursor="turn-cursor-2",
                )
            ],
            next_cursor="next-search",
        )

        result = self._search_conversation(
            "tab-1",
            "thread-1",
            query="  needle  ",
            cursor="search-page-1",
        )

        self.search.assert_called_once_with(
            "thread-1",
            search_term="needle",
            cursor="search-page-1",
            limit=20,
            timeout=2.0,
            expected_connection_generation=17,
        )
        self.read_thread.assert_called_once_with(
            "thread-1",
            False,
            timeout=2.0,
            expected_connection_generation=17,
        )
        self.assertEqual(result["query"], "needle")
        self.assertEqual(result["cursor"], "search-page-1")
        self.assertEqual(result["next_cursor"], "next-search")
        self.assertEqual(
            result["occurrences"],
            [
                {
                    "turn_id": "turn-2",
                    "item_id": "agent-2",
                    "snippet": "prefix needle suffix",
                    "snippet_match_range": {"start": 7, "end": 13},
                    "turn_cursor": "turn-cursor-2",
                }
            ],
        )

    def test_search_rejects_invalid_inputs_before_upstream_reads(self) -> None:
        cases = (
            ("   ", None, "invalid_conversation_search"),
            ("x" * 257, None, "invalid_conversation_search"),
            ("needle", " cursor ", "invalid_inspection_cursor"),
            ("needle", "x" * 4097, "invalid_inspection_cursor"),
        )
        for query, cursor, code in cases:
            with self.subTest(code=code, cursor=cursor is not None):
                with self.assertRaises(WebRuntimeError) as caught:
                    self._search_conversation(
                        "tab-1", "thread-1", query=query, cursor=cursor
                    )
                self.assertEqual(caught.exception.code, code)
        self.read_thread.assert_not_called()
        self.capture_connection_generation.assert_not_called()
        self.search.assert_not_called()

    def test_search_rejects_oversized_upstream_page_snippet_and_response(self) -> None:
        pages = (
            ThreadSearchOccurrencesPage(
                occurrences=[
                    ThreadSearchOccurrence(
                        f"turn-{index}",
                        f"item-{index}",
                        "needle",
                        (0, 6),
                        f"turn-cursor-{index}",
                    )
                    for index in range(21)
                ]
            ),
            ThreadSearchOccurrencesPage(
                occurrences=[
                    ThreadSearchOccurrence(
                        "turn-1",
                        "item-1",
                        "x" * 1025,
                        (0, 1),
                        "turn-cursor",
                    )
                ]
            ),
        )
        for page in pages:
            with self.subTest(occurrences=len(page.occurrences)):
                self.search.return_value = page
                with self.assertRaises(WebRuntimeError) as caught:
                    self._search_conversation(
                        "tab-1", "thread-1", query="needle"
                    )
                self.assertEqual(
                    caught.exception.code,
                    "thread_inspection_protocol_error",
                )

        for field in ("turn", "next"):
            with self.subTest(oversized_cursor=field):
                occurrence = ThreadSearchOccurrence(
                    "turn-1",
                    "item-1",
                    "needle",
                    (0, 6),
                    "x" * (4097 if field == "turn" else 1),
                )
                self.search.return_value = ThreadSearchOccurrencesPage(
                    occurrences=[occurrence],
                    next_cursor="x" * 4097 if field == "next" else None,
                )
                with self.assertRaises(WebRuntimeError) as caught:
                    self._search_conversation(
                        "tab-1", "thread-1", query="needle"
                    )
                self.assertEqual(
                    caught.exception.code,
                    "thread_inspection_protocol_error",
                )

        self.search.return_value = ThreadSearchOccurrencesPage(
            occurrences=[
                ThreadSearchOccurrence(
                    f"turn-{index}-" + ("t" * 1024),
                    f"item-{index}-" + ("i" * 1024),
                    "n" * 1024,
                    (0, 1),
                    f"cursor-{index}-" + ("c" * 4080),
                )
                for index in range(20)
            ]
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self._search_conversation(
                "tab-1", "thread-1", query="needle"
            )
        self.assertEqual(
            caught.exception.code,
            "conversation_search_response_too_large",
        )
        self.assertEqual(caught.exception.status, 413)

    def test_single_total_deadline_includes_direct_thread_proof(self) -> None:
        monotonic = Mock(side_effect=[0.0, 0.1, 2.1])
        service = WebThreadInspectionService(
            documents=self.documents,
            selection=self.selection,
            direct_targets=self.direct_targets,
            ports=WebThreadInspectionPorts(
                read_thread=self.read_thread,
                list_thread_items=self.list_items,
                search_thread_occurrences=self.search,
                coordinates=self.coordinates,
                capture_observation=self.read_model.capture_observation,
                observation_is_current=self.read_model.observation_is_current,
                capture_connection_generation=self.capture_connection_generation,
                run_if_connection_generation=self.run_if_connection_generation,
            ),
            runtime_context_guard=self.runtime_context_guard,
            monotonic=monotonic,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self._read_tool_detail(
                "tab-1",
                "thread-1",
                "turn-1",
                "cmd-1",
                service=service,
            )

        self.assertEqual(caught.exception.code, "thread_inspection_timeout")
        self.assertEqual(caught.exception.status, 504)
        self.list_items.assert_not_called()

    def test_pre_send_deadline_expiry_is_reported_as_timeout(self) -> None:
        self.list_items.side_effect = CodexRpcPreSendError(
            "thread/items/list",
            TimeoutError("connection lock deadline expired"),
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self._read_tool_detail(
                "tab-1", "thread-1", "turn-1", "cmd-1"
            )

        self.assertEqual(caught.exception.code, "thread_inspection_timeout")
        self.assertEqual(caught.exception.status, 504)

    def test_execute_stage_never_requires_runtime_context(self) -> None:
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        guard_calls = self.runtime_context_guard.call_count

        effect = self._execute(self.service, prepared)

        self.assertIsNone(effect.error)
        self.assertEqual(self.runtime_context_guard.call_count, guard_calls)

    def test_newer_same_kind_document_operation_replaces_old_settle(self) -> None:
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        effect = self._execute(self.service, prepared)
        self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_document_read")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(
            str(caught.exception),
            "This browser inspection was replaced before it completed.",
        )
        self.direct_targets.remember_verified_snapshot.assert_not_called()

    def test_document_reissue_rejects_old_settle_before_selection_cleanup(self) -> None:
        prepared = self.service.prepare_conversation_search(
            "tab-1",
            "thread-1",
            query="needle",
        )
        effect = self._execute(self.service, prepared)
        self.documents.mark_document_reissued("tab-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_document_read")
        self.direct_targets.remember_verified_snapshot.assert_not_called()
        self.direct_targets.clear_rejected_direct_thread.assert_not_called()

    def test_selection_change_rejects_old_settle(self) -> None:
        prepared = self.service.prepare_conversation_search(
            "tab-1",
            "thread-1",
            query="needle",
        )
        effect = self._execute(self.service, prepared)
        self.documents.materialize_thread("tab-1", "thread-2")

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "thread_not_selected")
        self.direct_targets.remember_verified_snapshot.assert_not_called()

    def test_backend_generation_change_rejects_settle_without_remembering(self) -> None:
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        effect = self._execute(self.service, prepared)
        self.connection_generation = 18

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            self.service.settle_inspection(prepared, effect)

        self.direct_targets.remember_verified_snapshot.assert_not_called()

    def test_projection_coordinate_change_rejects_settle_without_local_mutation(
        self,
    ) -> None:
        for coordinates in (
            {"runtime_epoch": "epoch-2", "revision": 7},
            {"runtime_epoch": "epoch-1", "revision": 8},
        ):
            with self.subTest(coordinates=coordinates):
                self.coordinates.return_value = {
                    "runtime_epoch": "epoch-1",
                    "revision": 7,
                }
                prepared = self.service.prepare_tool_detail(
                    "tab-1",
                    "thread-1",
                    "turn-1",
                    "cmd-1",
                    view="preview",
                )
                effect = self._execute(self.service, prepared)
                self.coordinates.return_value = coordinates

                with self.assertRaises(WebRuntimeError) as caught:
                    self.service.settle_inspection(prepared, effect)

                self.assertEqual(caught.exception.code, "stale_thread_read")
                self.assertEqual(caught.exception.status, 409)
                self.direct_targets.remember_verified_snapshot.assert_not_called()
                self.direct_targets.clear_rejected_direct_thread.assert_not_called()

    def test_notification_observation_rejects_stale_tool_detail_before_mutation(
        self,
    ) -> None:
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        effect = self._execute(self.service, prepared)
        self.read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_thread_read")
        self.assertEqual(caught.exception.status, 409)
        self.direct_targets.remember_verified_snapshot.assert_not_called()
        self.direct_targets.clear_rejected_direct_thread.assert_not_called()

    def test_notification_observation_rejects_stale_search_before_mutation(
        self,
    ) -> None:
        prepared = self.service.prepare_conversation_search(
            "tab-1",
            "thread-1",
            query="needle",
        )
        effect = self._execute(self.service, prepared)
        self.read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_thread_read")
        self.assertEqual(caught.exception.status, 409)
        self.direct_targets.remember_verified_snapshot.assert_not_called()
        self.direct_targets.clear_rejected_direct_thread.assert_not_called()

    def test_subagent_rejection_cleans_only_after_exact_current_settle(self) -> None:
        self.read_thread.return_value = _snapshot(subagent_kind="threadSpawn")
        self.direct_targets.clear_rejected_direct_thread.side_effect = (
            lambda *_args, **_kwargs: self.assertTrue(
                self.in_generation_settle,
                "subagent cleanup escaped the adapter generation gate",
            )
        )
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        effect = self._execute(self.service, prepared)

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        self.assertEqual(
            str(caught.exception),
            "线程 `thread-1` 是 parent-owned ThreadSpawn subagent；不能直接"
            "inspect tool details。请在 root thread 上继续或管理；已存在的 child "
            "审批/输入由 Focus 按 exact request 路由。",
        )
        self.list_items.assert_not_called()
        self.search.assert_not_called()
        self.direct_targets.clear_rejected_direct_thread.assert_called_once_with(
            "thread-1",
            reason="web_direct_target_selection_cleared",
        )
        self.direct_targets.remember_verified_snapshot.assert_not_called()

    def test_stale_subagent_rejection_does_not_run_cleanup(self) -> None:
        self.read_thread.return_value = _snapshot(subagent_kind="threadSpawn")
        prepared = self.service.prepare_tool_detail(
            "tab-1",
            "thread-1",
            "turn-1",
            "cmd-1",
            view="preview",
        )
        effect = self._execute(self.service, prepared)
        self.documents.begin_operation(
            "tab-1",
            operation="thread_tool_detail",
            target_thread_id="thread-1",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.settle_inspection(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_document_read")
        self.direct_targets.clear_rejected_direct_thread.assert_not_called()


class WebThreadInspectionControllerTests(WebRuntimeControllerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.fake.history_mode = "paginated"
        self.profile_store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir=self.fake.cwd,
        )
        self.document_registry.materialize_thread("tab-1", "thread-1")

    def test_capabilities_enable_closed_browser_inspection_surfaces(self) -> None:
        capabilities = self.controller.meta("tab-1")["capabilities"]

        self.assertTrue(capabilities["tool_detail"])
        self.assertTrue(capabilities["history_search"])

        self.fake.history_mode = "legacy"
        self.profile_store.update("tab-1", selected_thread_id="")
        capabilities_without_inspectable_thread = self.controller.meta("tab-1")[
            "capabilities"
        ]

        self.assertTrue(capabilities_without_inspectable_thread["tool_detail"])
        self.assertTrue(capabilities_without_inspectable_thread["history_search"])

    def test_controller_composes_stateless_search_with_adapter_port(self) -> None:
        prepared = self.controller.prepare_conversation_search(
            "tab-1",
            "thread-1",
            query="needle",
        )
        result = self.controller.run_prepared_thread_read(prepared)

        self.assertEqual(result["occurrences"], [])
        self.assertEqual(len(self.fake.search_pages), 1)
        timeout = self.fake.search_pages[0].pop("timeout")
        self.assertGreater(timeout, 0.0)
        self.assertLessEqual(timeout, 2.0)
        self.assertEqual(
            self.fake.search_pages,
            [
                {
                    "thread_id": "thread-1",
                    "search_term": "needle",
                    "cursor": None,
                    "limit": 20,
                    "expected_connection_generation": 1,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
