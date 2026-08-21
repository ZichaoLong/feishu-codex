from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import Mock

from bot.adapters.base import ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcError, CodexRpcProtocolError


_MISSING = object()


def _thread(history_mode: object = "legacy") -> dict[str, Any]:
    thread: dict[str, Any] = {
        "id": "thread-1",
        "cwd": "/tmp/project",
        "name": "demo",
        "preview": "hello",
        "createdAt": 1,
        "updatedAt": 2,
        "source": "appServer",
        "status": {"type": "idle", "activeFlags": []},
    }
    if history_mode is not _MISSING:
        thread["historyMode"] = history_mode
    return thread


def _start_or_resume_result(history_mode: object) -> dict[str, Any]:
    return {
        "thread": _thread(history_mode),
        "approvalsReviewer": "user",
        "model": "gpt-5.4",
        "reasoningEffort": "medium",
        "approvalPolicy": "on-request",
        "activePermissionProfile": {"id": ":workspace"},
    }


class ThreadHistoryModeProjectionTests(unittest.TestCase):
    def test_focus_provisional_summary_keeps_history_mode_unknown(self) -> None:
        summary = ThreadSummary(
            thread_id="thread-1",
            cwd="",
            name="",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="unknown",
        )

        self.assertIsNone(summary.history_mode)

    def test_summary_rejects_non_contract_history_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "legacy, paginated, or None"):
            ThreadSummary(
                thread_id="thread-1",
                cwd="",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
                history_mode="future",  # type: ignore[arg-type]
            )

    def test_all_upstream_thread_projection_paths_accept_exact_modes(self) -> None:
        for mode in ("legacy", "paginated"):
            with self.subTest(mode=mode, route="read"):
                _, snapshot = CodexAppServerAdapter._require_thread_snapshot_result(
                    "thread/read",
                    {"thread": _thread(mode)},
                    expected_thread_id="thread-1",
                )
                self.assertEqual(snapshot.history_mode, mode)
                self.assertEqual(snapshot.summary.history_mode, mode)

            with self.subTest(mode=mode, route="list"):
                summaries, cursor = CodexAppServerAdapter._thread_list_page_from_result(
                    {"data": [_thread(mode)], "nextCursor": None}
                )
                self.assertIsNone(cursor)
                self.assertEqual(summaries[0].history_mode, mode)

            for method in ("thread/start", "thread/resume"):
                with self.subTest(mode=mode, route=method):
                    _, snapshot = CodexAppServerAdapter._require_thread_snapshot_result(
                        method,
                        _start_or_resume_result(mode),
                        expected_thread_id="thread-1",
                    )
                    self.assertEqual(snapshot.history_mode, mode)

    def test_all_upstream_thread_projection_paths_fail_closed_on_bad_mode(self) -> None:
        for bad_mode in (_MISSING, None, "", "future", 1):
            for method in ("thread/read", "thread/start", "thread/resume"):
                result = (
                    {"thread": _thread(bad_mode)}
                    if method == "thread/read"
                    else _start_or_resume_result(bad_mode)
                )
                with self.subTest(mode=bad_mode, route=method):
                    with self.assertRaises(CodexRpcProtocolError):
                        CodexAppServerAdapter._require_thread_snapshot_result(
                            method,
                            result,
                            expected_thread_id="thread-1",
                        )

            with self.subTest(mode=bad_mode, route="thread/list"):
                with self.assertRaises(CodexRpcProtocolError):
                    CodexAppServerAdapter._thread_list_page_from_result(
                        {"data": [_thread(bad_mode)], "nextCursor": None}
                    )

    def test_focus_create_requests_and_requires_paginated_history(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        rpc = Mock()
        rpc.request.return_value = _start_or_resume_result("paginated")
        adapter._rpc = rpc

        snapshot = adapter.create_thread(cwd="/tmp/project")

        self.assertEqual(snapshot.history_mode, "paginated")
        request_params = rpc.request.call_args.args[1]
        self.assertEqual(request_params["historyMode"], "paginated")

    def test_focus_create_rejects_legacy_success_without_retry(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        rpc = Mock(return_value=None)
        rpc.request.return_value = _start_or_resume_result("legacy")
        adapter._rpc = rpc

        with self.assertRaisesRegex(
            CodexRpcProtocolError,
            "did not apply the requested historyMode",
        ):
            adapter.create_thread(cwd="/tmp/project")

        rpc.request.assert_called_once()

    def test_focus_create_does_not_retry_when_history_mode_is_unsupported(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        rpc = Mock()
        rpc.request.side_effect = CodexRpcError(
            "thread/start",
            {"code": -32602, "message": "unknown field `historyMode`"},
        )
        adapter._rpc = rpc

        with self.assertRaises(CodexRpcError):
            adapter.create_thread(cwd="/tmp/project")

        rpc.request.assert_called_once()


class ThreadInspectionAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = CodexAppServerAdapter(CodexAppServerConfig())
        self.rpc = Mock()
        self.adapter._rpc = self.rpc

    def test_list_thread_items_forwards_exact_page_request_and_decodes_items(self) -> None:
        item = {
            "type": "commandExecution",
            "id": "item-1",
            "command": "printf ok",
            "cwd": "/tmp/project",
            "status": "completed",
            "aggregatedOutput": "ok",
            "exitCode": 0,
        }
        self.rpc.request.return_value = {
            "data": [{"turnId": "turn-1", "item": item}],
            "nextCursor": "next-1",
            "backwardsCursor": None,
        }

        page = self.adapter.list_thread_items(
            "thread-1",
            turn_id="turn-1",
            cursor="cursor-1",
            limit=25,
            sort_direction="asc",
            timeout=2.0,
            require_existing_connection=True,
        )

        self.assertEqual(page.items[0].turn_id, "turn-1")
        self.assertEqual(page.items[0].item, item)
        self.assertIsNot(page.items[0].item, item)
        self.assertEqual(page.next_cursor, "next-1")
        self.assertIsNone(page.backwards_cursor)
        self.rpc.request.assert_called_once_with(
            "thread/items/list",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "cursor": "cursor-1",
                "limit": 25,
                "sortDirection": "asc",
            },
            timeout=2.0,
            require_existing_connection=True,
        )

    def test_search_occurrences_preserves_utf16_match_range(self) -> None:
        self.rpc.request.return_value = {
            "data": [
                {
                    "turnId": "turn-1",
                    "itemId": "final-1",
                    "snippet": "😀 Final needle",
                    "snippetMatchRange": {"start": 9, "end": 15},
                    "turnCursor": "turn-cursor-1",
                }
            ],
            "nextCursor": None,
        }

        page = self.adapter.search_thread_occurrences(
            "thread-1",
            search_term="needle",
            cursor="search-cursor-1",
            limit=20,
        )

        occurrence = page.occurrences[0]
        self.assertEqual(occurrence.turn_id, "turn-1")
        self.assertEqual(occurrence.item_id, "final-1")
        self.assertEqual(occurrence.snippet_match_range, (9, 15))
        self.assertEqual(occurrence.turn_cursor, "turn-cursor-1")
        self.assertIsNone(page.next_cursor)
        self.rpc.request.assert_called_once_with(
            "thread/searchOccurrences",
            {
                "threadId": "thread-1",
                "searchTerm": "needle",
                "cursor": "search-cursor-1",
                "limit": 20,
            },
            timeout=30.0,
        )

    def test_request_parameters_reject_values_outside_fixed_contract(self) -> None:
        invalid_item_calls = (
            {"thread_id": ""},
            {"thread_id": " thread-1"},
            {"thread_id": "thread-1", "turn_id": ""},
            {"thread_id": "thread-1", "cursor": " "},
            {"thread_id": "thread-1", "limit": True},
            {"thread_id": "thread-1", "limit": -1},
            {"thread_id": "thread-1", "limit": 1 << 32},
            {"thread_id": "thread-1", "sort_direction": "ASC"},
        )
        for kwargs in invalid_item_calls:
            with self.subTest(items=kwargs):
                with self.assertRaises(ValueError):
                    self.adapter.list_thread_items(**kwargs)

        invalid_search_calls = (
            {"thread_id": "", "search_term": "needle"},
            {"thread_id": "thread-1", "search_term": ""},
            {"thread_id": "thread-1", "search_term": "   "},
            {"thread_id": "thread-1", "search_term": "needle", "cursor": "bad "},
            {"thread_id": "thread-1", "search_term": "needle", "limit": False},
        )
        for kwargs in invalid_search_calls:
            with self.subTest(search=kwargs):
                with self.assertRaises(ValueError):
                    self.adapter.search_thread_occurrences(**kwargs)

        self.rpc.request.assert_not_called()

    def test_item_page_rejects_malformed_response_shapes(self) -> None:
        valid_entry = {
            "turnId": "turn-1",
            "item": {"type": "fileChange", "id": "item-1"},
        }
        invalid_responses = (
            None,
            {},
            {"data": {}, "nextCursor": None, "backwardsCursor": None},
            {"data": [None], "nextCursor": None, "backwardsCursor": None},
            {
                "data": [{**valid_entry, "turnId": " turn-1"}],
                "nextCursor": None,
                "backwardsCursor": None,
            },
            {
                "data": [{"turnId": "turn-1", "item": None}],
                "nextCursor": None,
                "backwardsCursor": None,
            },
            {
                "data": [
                    {"turnId": "turn-1", "item": {"type": "fileChange"}}
                ],
                "nextCursor": None,
                "backwardsCursor": None,
            },
            {"data": [valid_entry], "backwardsCursor": None},
            {"data": [valid_entry], "nextCursor": " ", "backwardsCursor": None},
            {"data": [valid_entry], "nextCursor": None},
        )
        for response in invalid_responses:
            with self.subTest(response=response):
                self.rpc.request.return_value = response
                with self.assertRaises(CodexRpcProtocolError):
                    self.adapter.list_thread_items("thread-1")

    def test_search_page_rejects_invalid_utf16_ranges_and_cursors(self) -> None:
        valid = {
            "turnId": "turn-1",
            "itemId": "item-1",
            "snippet": "😀 needle",
            "snippetMatchRange": {"start": 3, "end": 9},
            "turnCursor": "turn-cursor-1",
        }
        invalid_occurrences = (
            {**valid, "turnId": ""},
            {**valid, "itemId": 1},
            {**valid, "snippet": None},
            {**valid, "snippetMatchRange": None},
            {**valid, "snippetMatchRange": {"start": True, "end": 9}},
            {**valid, "snippetMatchRange": {"start": 3, "end": 3}},
            {**valid, "snippetMatchRange": {"start": 1, "end": 9}},
            {**valid, "snippetMatchRange": {"start": 3, "end": 10}},
            {**valid, "turnCursor": " turn-cursor-1"},
        )
        for occurrence in invalid_occurrences:
            with self.subTest(occurrence=occurrence):
                self.rpc.request.return_value = {
                    "data": [occurrence],
                    "nextCursor": None,
                }
                with self.assertRaises(CodexRpcProtocolError):
                    self.adapter.search_thread_occurrences(
                        "thread-1",
                        search_term="needle",
                    )

        for response in (
            None,
            {},
            {"data": {}, "nextCursor": None},
            {"data": [None], "nextCursor": None},
            {"data": [valid]},
            {"data": [valid], "nextCursor": " "},
        ):
            with self.subTest(response=response):
                self.rpc.request.return_value = response
                with self.assertRaises(CodexRpcProtocolError):
                    self.adapter.search_thread_occurrences(
                        "thread-1",
                        search_term="needle",
                    )


if __name__ == "__main__":
    unittest.main()
