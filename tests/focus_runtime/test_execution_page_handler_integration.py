"""Composition regressions for initial execution-page settlement."""

from __future__ import annotations

import unittest

from bot.execution_pages import ExecutionPageStatus
from bot.feishu_outbound import FeishuOutboundOperation
from tests.focus_runtime.codex_handler_fakes import _outbound_rejected, _runtime_state
from tests.focus_runtime.codex_handler_test_harness import CodexHandlerHarness


class ExecutionPageHandlerIntegrationTests(CodexHandlerHarness):

    def test_prompt_initial_page_rejection_does_not_block_upstream_start(self) -> None:
        handler, bot = self._make_handler()
        bot.reply_to_message = (
            lambda chat_id, *_args, **_kwargs: _outbound_rejected(
                FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
            )
        )

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertEqual(state["execution_pages"].pages, ())
        self.assertEqual(bot.replies, [])

    def test_prompt_initial_page_exception_scopes_unknown_to_page_effect(self) -> None:
        handler, bot = self._make_handler()

        def fail_initial_page(*_args, **_kwargs):
            raise RuntimeError("injected initial page disconnect")

        bot.reply_to_message = fail_initial_page

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        state = _runtime_state(handler, "ou_user", "c1")
        page = state["execution_pages"].current_page
        assert page is not None
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertIs(page.status, ExecutionPageStatus.SEND_UNKNOWN)
        self.assertTrue(
            handler._turn_execution.has_active_execution_locked(state)
        )


if __name__ == "__main__":
    unittest.main()
