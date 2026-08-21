from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.client import CodexRpcError
from bot.codex_protocol.connection import (
    CodexRpcConnectionGenerationMismatchError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)


_GENERATION = 7
_THREAD_RESULT = {
    "thread": {
        "id": "thread-1",
        "historyMode": "legacy",
        "cwd": "/tmp/project",
        "createdAt": 0,
        "updatedAt": 0,
        "source": "cli",
        "status": {"type": "idle", "activeFlags": []},
        "turns": [],
    }
}
_RESUME_RESULT = {
    **_THREAD_RESULT,
    "approvalsReviewer": "user",
    "model": "gpt-effective",
    "reasoningEffort": None,
    "approvalPolicy": "never",
    "activePermissionProfile": None,
}


class CodexAppServerGenerationPinnedReadTests(unittest.TestCase):
    def _adapter(self):
        permit = object()
        issue = Mock(return_value=permit)
        guard = Mock(side_effect=lambda _permit: nullcontext())
        confirm = Mock()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=guard,
            confirm_outbound_request=confirm,
        )
        rpc = Mock()
        adapter._rpc = rpc
        return adapter, rpc, issue, guard, confirm, permit

    def _assert_exact_existing_generation(
        self,
        rpc: Mock,
        *,
        method: str = "thread/read",
        call_index: int = -1,
    ) -> None:
        args, kwargs = rpc.request.call_args_list[call_index]
        self.assertEqual(args[0], method)
        self.assertTrue(kwargs["require_existing_connection"])
        self.assertEqual(kwargs["expected_connection_generation"], _GENERATION)
        self.assertTrue(callable(kwargs["outbound_transport_guard"]))

    def test_read_uses_admitted_exact_existing_connection(self) -> None:
        adapter, rpc, issue, guard, confirm, permit = self._adapter()

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            return _THREAD_RESULT

        rpc.request.side_effect = request

        snapshot = adapter.read_thread(
            "thread-1",
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(snapshot.summary.thread_id, "thread-1")
        issue.assert_called_once_with("thread/read")
        guard.assert_called_once_with(permit)
        confirm.assert_called_once_with(permit)
        self._assert_exact_existing_generation(rpc)

    def test_generation_mismatch_is_pre_send_without_confirmation(self) -> None:
        adapter, rpc, issue, guard, confirm, _permit = self._adapter()
        mismatch = CodexRpcConnectionGenerationMismatchError(
            "thread/read",
            expected_generation=_GENERATION,
            observed_generation=_GENERATION + 1,
        )
        rpc.request.side_effect = mismatch

        with self.assertRaises(CodexRpcPreSendError) as raised:
            adapter.read_thread(
                "thread-1",
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception, mismatch)
        issue.assert_called_once_with("thread/read")
        guard.assert_not_called()
        confirm.assert_not_called()
        self._assert_exact_existing_generation(rpc)

    def test_transport_failure_after_send_guard_remains_unknown(self) -> None:
        adapter, rpc, issue, guard, confirm, permit = self._adapter()
        transport_error = CodexRpcTransportError(
            "thread/read",
            {"code": -32000, "message": "connection closed during read"},
        )

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            raise transport_error

        rpc.request.side_effect = request

        with self.assertRaises(CodexRpcTransportError) as raised:
            adapter.read_thread(
                "thread-1",
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception, transport_error)
        issue.assert_called_once_with("thread/read")
        guard.assert_called_once_with(permit)
        confirm.assert_not_called()

    def test_successful_response_requires_exact_epoch_confirmation(self) -> None:
        adapter, rpc, _issue, guard, confirm, permit = self._adapter()
        epoch_lost = RuntimeError("response epoch changed")
        confirm.side_effect = epoch_lost

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            return _THREAD_RESULT

        rpc.request.side_effect = request

        with self.assertRaises(CodexRpcTransportError) as raised:
            adapter.read_thread(
                "thread-1",
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception.__cause__, epoch_lost)
        guard.assert_called_once_with(permit)
        confirm.assert_called_once_with(permit)

    def test_generation_pin_covers_every_thread_read_and_resume_route(self) -> None:
        cases = (
            (
                "thread/list",
                lambda adapter: adapter.list_threads(
                    expected_connection_generation=_GENERATION
                ),
                {"data": [], "nextCursor": None},
            ),
            (
                "thread/loaded/list",
                lambda adapter: adapter.list_loaded_thread_ids(
                    expected_connection_generation=_GENERATION
                ),
                {"data": [], "nextCursor": None},
            ),
            (
                "thread/turns/list",
                lambda adapter: adapter.list_thread_turns(
                    "thread-1",
                    expected_connection_generation=_GENERATION,
                ),
                {"data": [], "nextCursor": None, "backwardsCursor": None},
            ),
            (
                "thread/items/list",
                lambda adapter: adapter.list_thread_items(
                    "thread-1",
                    expected_connection_generation=_GENERATION,
                ),
                {"data": [], "nextCursor": None, "backwardsCursor": None},
            ),
            (
                "thread/searchOccurrences",
                lambda adapter: adapter.search_thread_occurrences(
                    "thread-1",
                    search_term="needle",
                    expected_connection_generation=_GENERATION,
                ),
                {"data": [], "nextCursor": None},
            ),
            (
                "thread/goal/get",
                lambda adapter: adapter.get_thread_goal(
                    "thread-1",
                    expected_connection_generation=_GENERATION,
                ),
                {"goal": None},
            ),
            (
                "thread/unsubscribe",
                lambda adapter: adapter.unsubscribe_thread(
                    "thread-1",
                    expected_connection_generation=_GENERATION,
                ),
                {"status": "unsubscribed"},
            ),
            (
                "thread/resume",
                lambda adapter: adapter.resume_thread(
                    "thread-1",
                    expected_connection_generation=_GENERATION,
                ),
                _RESUME_RESULT,
            ),
            (
                "thread/resume",
                lambda adapter: adapter.resume_thread_page(
                    "thread-1",
                    limit=25,
                    expected_connection_generation=_GENERATION,
                ),
                {
                    **_RESUME_RESULT,
                    "initialTurnsPage": {
                        "data": [],
                        "nextCursor": None,
                        "backwardsCursor": None,
                    },
                },
            ),
        )
        for method, invoke, response in cases:
            with self.subTest(method=method, invoke=invoke.__code__.co_firstlineno):
                adapter, rpc, issue, guard, confirm, permit = self._adapter()

                def request(_method, _params, **kwargs):
                    with kwargs["outbound_transport_guard"]():
                        pass
                    return response

                rpc.request.side_effect = request

                invoke(adapter)

                issue.assert_called_once_with(method)
                guard.assert_called_once_with(permit)
                confirm.assert_called_once_with(permit)
                self._assert_exact_existing_generation(rpc, method=method)

    def test_list_threads_all_pins_the_same_generation_across_pages(self) -> None:
        adapter, rpc, issue, guard, confirm, permit = self._adapter()
        second_thread = {
            **_THREAD_RESULT["thread"],
            "id": "thread-2",
        }
        responses = iter(
            (
                {"data": [_THREAD_RESULT["thread"]], "nextCursor": "next-1"},
                {"data": [second_thread], "nextCursor": None},
            )
        )

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            return next(responses)

        rpc.request.side_effect = request

        summaries = adapter.list_threads_all(
            limit=3,
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(
            [summary.thread_id for summary in summaries],
            ["thread-1", "thread-2"],
        )
        self.assertEqual(issue.call_count, 2)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(confirm.call_count, 2)
        issue.assert_has_calls([unittest.mock.call("thread/list")] * 2)
        guard.assert_has_calls([unittest.mock.call(permit)] * 2)
        confirm.assert_has_calls([unittest.mock.call(permit)] * 2)
        for call_index in range(2):
            self._assert_exact_existing_generation(
                rpc,
                method="thread/list",
                call_index=call_index,
            )

    def test_permissions_fallback_keeps_the_same_generation_pin(self) -> None:
        adapter, rpc, issue, guard, confirm, permit = self._adapter()
        first_error = CodexRpcError(
            "thread/resume",
            {"code": -32602, "message": "unknown field `permissions`"},
        )
        response = {
            **_RESUME_RESULT,
            "activePermissionProfile": {"id": ":workspace"},
        }
        call_count = 0

        def request(_method, _params, **kwargs):
            nonlocal call_count
            call_count += 1
            with kwargs["outbound_transport_guard"]():
                pass
            if call_count == 1:
                raise first_error
            return response

        rpc.request.side_effect = request

        snapshot = adapter.resume_thread(
            "thread-1",
            permissions_profile_id=":workspace",
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(snapshot.effective_permissions_profile_id, ":workspace")
        self.assertEqual(issue.call_count, 2)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(confirm.call_count, 2)
        for call_index in range(2):
            self._assert_exact_existing_generation(
                rpc,
                method="thread/resume",
                call_index=call_index,
            )
        first_params = rpc.request.call_args_list[0].args[1]
        second_params = rpc.request.call_args_list[1].args[1]
        self.assertEqual(first_params["permissions"], ":workspace")
        self.assertNotIn("permissions", second_params)
        self.assertEqual(second_params["sandbox"], "workspace-write")
        issue.assert_has_calls([unittest.mock.call("thread/resume")] * 2)
        guard.assert_has_calls([unittest.mock.call(permit)] * 2)
        confirm.assert_has_calls([unittest.mock.call(permit)] * 2)


if __name__ == "__main__":
    unittest.main()
