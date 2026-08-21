from __future__ import annotations

import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.feishu_continuation_controller import (
    FeishuContinuationController,
    FeishuExplicitResumeSuccess,
)
from bot.feishu_root_operation_contract import FeishuRootOperationToken


def _thread() -> ThreadSummary:
    return ThreadSummary(
        thread_id="thread-1",
        cwd="/workspace",
        name="demo",
        preview="",
        created_at=0,
        updated_at=0,
        source="appServer",
        status="idle",
    )


def _controller(
    *,
    snapshot: ThreadSnapshot | None = None,
    history_preview_rounds: int = 2,
    show_history_preview_on_resume: bool = True,
) -> FeishuContinuationController:
    summary = _thread()
    snapshot = snapshot or ThreadSnapshot(summary=summary)
    adapter = SimpleNamespace(
        list_loaded_thread_ids=lambda: [summary.thread_id],
        get_thread_goal=lambda _thread_id: None,
    )
    binding_runtime = SimpleNamespace(
        resolve_session=lambda _sender_id, _chat_id, _message_id="": SimpleNamespace(
            execution=SimpleNamespace(has_execution_anchor=False),
            approval_policy="",
            permissions_profile_id="",
            model="",
            reasoning_effort="",
        ),
        existing_chat_binding_key_locked=lambda sender_id, chat_id: (
            sender_id,
            chat_id,
        ),
        fresh_chat_binding_key=lambda sender_id, chat_id, _message_id="": (
            sender_id,
            chat_id,
        ),
    )
    access_policy = SimpleNamespace(
        all_mode_thread_exclusivity_violation=lambda *_args, **_kwargs: "",
    )
    root_operations = SimpleNamespace(
        admit=Mock(return_value=FeishuRootOperationToken(1, 1))
    )
    resume_settlement = SimpleNamespace(settle_success=Mock())
    thread_sessions = SimpleNamespace(
        read_direct_thread_summary=lambda *_args, **_kwargs: summary,
        resume_and_commit_feishu_binding=Mock(return_value=snapshot),
    )
    thread_runtime_authority = SimpleNamespace(update_thread_settings=Mock())
    return FeishuContinuationController(
        lock=threading.RLock(),
        adapter=adapter,
        binding_runtime=binding_runtime,
        access_policy=access_policy,
        root_operations=root_operations,
        resume_settlement=resume_settlement,
        thread_sessions=thread_sessions,
        thread_runtime_authority=thread_runtime_authority,
        history_preview_rounds=history_preview_rounds,
        show_history_preview_on_resume=show_history_preview_on_resume,
        thread_list_query_limit=20,
        local_thread_safety_rule="same live turn has one writer",
        logger=logging.getLogger("test.feishu_continuation"),
    )


class FeishuContinuationControllerTests(unittest.TestCase):
    def test_active_observer_attach_does_not_claim_root_operation(self) -> None:
        summary = _thread()
        summary.status = "active"
        snapshot = ThreadSnapshot(
            summary=summary,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        controller = _controller(snapshot=snapshot)
        controller._thread_sessions.read_direct_thread_summary = (
            lambda *_args, **_kwargs: summary
        )

        result = controller.attach_binding_for_control(
            ("ou-user", "chat-a"),
            "thread-1",
            active_observer=True,
        )

        self.assertIs(result, summary)
        controller._root_operations.admit.assert_not_called()
        call = (
            controller._thread_sessions.resume_and_commit_feishu_binding
            .call_args
        )
        self.assertTrue(call.kwargs["active_observer"])
        self.assertEqual(
            call.kwargs["failure_policy"].value,
            "compensate",
        )
        self.assertTrue(call.kwargs["exact_mutation_guard"]())

    def test_history_projection_keeps_last_rounds_and_joins_segments(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_thread(),
            turns=[
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "discarded"}],
                        },
                        {"type": "agentMessage", "text": "discarded reply"},
                    ]
                },
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [
                                {"type": "text", "text": "  first segment  "},
                                {"type": "image", "url": "ignored"},
                                {"type": "text", "text": "second segment"},
                            ],
                        },
                        {"type": "agentMessage", "text": "first reply"},
                        {"type": "agentMessage", "text": "  second reply  "},
                    ]
                },
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "last user"}],
                        },
                        {"type": "agentMessage", "text": "last reply"},
                    ]
                },
            ],
        )

        rounds = _controller()._extract_history_rounds(snapshot)

        self.assertEqual(
            rounds,
            [
                (
                    "first segment\nsecond segment",
                    "first reply\n\nsecond reply",
                ),
                ("last user", "last reply"),
            ],
        )

    def test_history_projection_fills_empty_user_and_assistant_sides(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_thread(),
            turns=[
                {"items": [{"type": "agentMessage", "text": "reply only"}]},
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "user only"}],
                        }
                    ]
                },
            ],
        )

        rounds = _controller()._extract_history_rounds(snapshot)

        self.assertEqual(
            rounds,
            [("（空）", "reply only"), ("user only", "（无回复）")],
        )

    def test_resume_projects_history_only_when_enabled(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_thread(),
            turns=[
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "hello"}],
                        },
                        {"type": "agentMessage", "text": "world"},
                    ]
                }
            ],
        )

        enabled_controller = _controller(
            snapshot=snapshot,
            show_history_preview_on_resume=True,
        )
        disabled_controller = _controller(
            snapshot=snapshot,
            show_history_preview_on_resume=False,
        )
        enabled_result = enabled_controller.resume_thread(
            "ou-user", "chat-a", "thread-1"
        )
        disabled_result = disabled_controller.resume_thread(
            "ou-user", "chat-a", "thread-1"
        )

        assert isinstance(enabled_result, FeishuExplicitResumeSuccess)
        assert isinstance(disabled_result, FeishuExplicitResumeSuccess)
        enabled = enabled_controller.build_explicit_resume_card(enabled_result)
        disabled = disabled_controller.build_explicit_resume_card(
            disabled_result
        )
        self.assertEqual(
            enabled["header"]["title"]["content"],
            "线程 thread-1… 最近对话",
        )
        self.assertEqual(
            disabled["header"]["title"]["content"],
            "Codex 已切换线程",
        )

    def test_malformed_history_projects_only_after_resume_settlement(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_thread(),
            turns=[{"items": [None]}],
        )
        controller = _controller(snapshot=snapshot)

        result = controller.resume_thread(
            "ou-user",
            "chat-a",
            "thread-1",
        )

        assert isinstance(result, FeishuExplicitResumeSuccess)
        controller._resume_settlement.settle_success.assert_called_once()
        with self.assertRaises(AttributeError):
            controller.build_explicit_resume_card(result)


if __name__ == "__main__":
    unittest.main()
