import unittest
from types import SimpleNamespace

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.codex_threads_ui_domain import CodexThreadsUiDomain, ThreadsUiPorts
from bot.feishu_continuation_controller import (
    FeishuExplicitResumeFailure,
    FeishuExplicitResumeResult,
    FeishuExplicitResumeSuccess,
)


class _PortsStub:
    def __init__(self) -> None:
        self.archive_calls: list[tuple[str, ThreadSummary | None]] = []
        self.effects: list[str] = []
        self.read_calls: list[tuple[str, str]] = []
        self.reply_card_calls: list[tuple[str, dict, str]] = []
        self.reply_calls: list[tuple[str, str, str]] = []
        self.resolve_calls: list[str] = []
        self.resume_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.mutation_denial_text = ""
        self.mutation_access_checks: list[tuple[str, str, str, str]] = []
        self.archive_result: dict[str, object] = {
            "thread_id": "thread-1",
            "cleared_binding_ids": ["p2p:ou_user:chat-a"],
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
        }
        self.thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        self.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="ship goal support",
            status="paused",
            token_budget=100,
            tokens_used=0,
            time_used_seconds=0,
            created_at=1712476800,
            updated_at=1712476801,
        )
        self.goal_error: Exception | None = None
        self.resume_card_error: Exception | None = None
        self.resume_result: FeishuExplicitResumeResult = (
            FeishuExplicitResumeSuccess(
                snapshot=ThreadSnapshot(summary=self.thread),
                paused_for_cold_sync=False,
            )
        )

    def _resolve_session(self, sender_id: str, chat_id: str, message_id: str = ""):
        del sender_id, chat_id, message_id
        return SimpleNamespace(
            running=False,
            execution=SimpleNamespace(has_execution_anchor=False),
            current_thread_id="",
            current_thread_title="",
            working_dir="/tmp/project",
        )

    def _is_group_chat(self, chat_id: str, message_id: str = "") -> bool:
        del chat_id, message_id
        return False

    def _is_group_admin_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        del chat_id, message_id, operator_open_id
        return True

    def _rename_bound_thread_title(
        self,
        sender_id: str,
        chat_id: str,
        title: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> bool:
        del sender_id, chat_id, title, message_id, thread_id
        return True

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None:
        self.effects.append("reply_text")
        self.reply_calls.append((chat_id, text, message_id))

    def _reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None:
        self.effects.append("reply_card")
        self.reply_card_calls.append((chat_id, card, message_id))

    def resolve_resume_target(self, arg: str) -> ThreadSummary:
        self.resolve_calls.append(arg)
        return self.thread

    def _list_visible_current_dir_threads(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> list[ThreadSummary]:
        del sender_id, chat_id, message_id
        return [self.thread]

    def _read_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
    ) -> ThreadSummary:
        self.read_calls.append((thread_id, original_arg))
        return self.thread

    def get_thread_goal_for_resume(self, thread_id: str) -> ThreadGoalSummary | None:
        del thread_id
        if self.goal_error is not None:
            raise self.goal_error
        return self.goal

    def resume_thread(
        self,
        *args: object,
        **kwargs: object,
    ) -> FeishuExplicitResumeResult:
        self.resume_calls.append((args, kwargs))
        return self.resume_result

    def build_explicit_resume_card(
        self,
        _result: FeishuExplicitResumeSuccess,
    ) -> dict:
        self.effects.append("build_card")
        if self.resume_card_error is not None:
            raise self.resume_card_error
        return {"kind": "resume"}

    def _archive_thread_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, object]:
        self.archive_calls.append((thread_id, summary))
        return dict(self.archive_result)

    def rename_thread(self, thread_id: str, name: str) -> None:
        self.rename_calls.append((thread_id, name))

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> bool:
        del chat_id
        self.effects.append("refresh")
        self.patches.append((message_id, content))
        return True

    def is_thread_not_loaded_error(self, exc: Exception) -> bool:
        del exc
        return False

    def prompt_write_denial_text(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> str:
        self.mutation_access_checks.append((sender_id, chat_id, thread_id, message_id))
        return self.mutation_denial_text


def _make_domain(
    ports_stub: _PortsStub,
    *,
    submit_to_runtime=lambda _fn, *_args, **_kwargs: None,
) -> CodexThreadsUiDomain:
    return CodexThreadsUiDomain(
        continuation=ports_stub,  # type: ignore[arg-type]
        ports=ThreadsUiPorts(
            submit_to_runtime=submit_to_runtime,
            resolve_session=ports_stub._resolve_session,
            is_group_chat=ports_stub._is_group_chat,
            is_group_admin_actor=ports_stub._is_group_admin_actor,
            rename_bound_thread_title=ports_stub._rename_bound_thread_title,
            reply_text=ports_stub._reply_text,
            reply_card=ports_stub._reply_card,
            list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
            read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
            archive_thread_for_control=ports_stub._archive_thread_for_control,
            rename_thread=ports_stub.rename_thread,
            patch_message=ports_stub.patch_message,
            is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
            threads_initial_limit=5,
        ),
    )


class CodexThreadsUiDomainTests(unittest.TestCase):
    def test_handle_resume_command_dispatches_via_runtime_port(self) -> None:
        ports_stub = _PortsStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        domain = _make_domain(
            ports_stub,
            submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append(
                (fn, args, kwargs)
            ),
        )

        result = domain.handle_resume_command("ou_user", "chat-a", "thread-1", message_id="msg-1")

        assert result is not None
        assert result.after_dispatch is not None
        result.after_dispatch()

        self.assertEqual(len(submit_calls), 1)
        fn, args, kwargs = submit_calls[0]
        self.assertEqual(getattr(fn, "__name__", ""), "_resume_target_on_runtime")
        self.assertEqual(args, ("ou_user", "chat-a", "thread-1"))
        self.assertEqual(
            kwargs,
            {
                "original_arg": "thread-1",
                "summary": ports_stub.thread,
                "message_id": "msg-1",
            },
        )
        self.assertEqual(ports_stub.resume_calls, [])

    def test_resume_target_on_runtime_calls_resume_port_directly(self) -> None:
        ports_stub = _PortsStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        domain = _make_domain(
            ports_stub,
            submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append(
                (fn, args, kwargs)
            ),
        )

        domain._resume_target_on_runtime(
            "ou_user",
            "chat-a",
            "thread-1",
            message_id="msg-1",
            refresh_threads_message_id="msg-session",
        )

        self.assertEqual(ports_stub.resolve_calls, [])
        self.assertEqual(ports_stub.read_calls, [("thread-1", "thread-1")])
        self.assertEqual(submit_calls, [])
        self.assertEqual(len(ports_stub.resume_calls), 1)
        args, kwargs = ports_stub.resume_calls[0]
        self.assertEqual(args, ("ou_user", "chat-a", "thread-1"))
        self.assertEqual(kwargs["original_arg"], "thread-1")
        self.assertEqual(kwargs["message_id"], "msg-1")
        self.assertEqual(ports_stub.patches[0][0], "msg-session")
        self.assertEqual(
            ports_stub.reply_card_calls,
            [("chat-a", {"kind": "resume"}, "msg-1")],
        )
        self.assertEqual(
            ports_stub.effects,
            ["refresh", "build_card", "reply_card"],
        )

    def test_resume_target_failure_replies_before_refreshing_threads_card(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.resume_result = FeishuExplicitResumeFailure(
            text="恢复线程失败：boom"
        )
        domain = _make_domain(ports_stub)

        domain._resume_target_on_runtime(
            "ou_user",
            "chat-a",
            "thread-1",
            summary=ports_stub.thread,
            message_id="msg-1",
            refresh_threads_message_id="msg-session",
        )

        self.assertEqual(
            ports_stub.reply_calls,
            [("chat-a", "恢复线程失败：boom", "msg-1")],
        )
        self.assertEqual(ports_stub.effects, ["reply_text", "refresh"])

    def test_resume_projection_failure_happens_after_threads_refresh(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.resume_card_error = AttributeError(
            "malformed nested history item"
        )
        domain = _make_domain(ports_stub)

        with self.assertRaises(AttributeError):
            domain._resume_target_on_runtime(
                "ou_user",
                "chat-a",
                "thread-1",
                summary=ports_stub.thread,
                message_id="msg-1",
                refresh_threads_message_id="msg-session",
            )

        self.assertEqual(ports_stub.effects, ["refresh", "build_card"])
        self.assertEqual(ports_stub.reply_card_calls, [])

    def test_handle_archive_thread_action_uses_control_path(self) -> None:
        ports_stub = _PortsStub()
        domain = _make_domain(ports_stub)

        result = domain.handle_archive_thread_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {"thread_id": "thread-1"},
        )

        self.assertEqual(ports_stub.archive_calls, [("thread-1", ports_stub.thread)])
        self.assertIsNotNone(result.toast)
        self.assertEqual(result.toast.content, "已归档线程：thread-1…")
        self.assertEqual(result.toast.type, "success")

    def test_thread_management_mutations_respect_live_writer_denial(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.mutation_denial_text = "当前线程正由另一终端执行；本会话可继续查看，但暂时不能写入。"
        ports_stub._resolve_session = lambda _sender_id, _chat_id, _message_id="": SimpleNamespace(
            running=False,
            execution=SimpleNamespace(has_execution_anchor=False),
            current_thread_id="thread-1",
            current_thread_title="demo",
            working_dir="/tmp/project",
        )
        domain = _make_domain(ports_stub)

        rename = domain.handle_rename_command("ou_user", "chat-a", "new title", message_id="msg-1")
        archive = domain.handle_archive_command("ou_user", "chat-a", "thread-1", message_id="msg-1")
        rename_card = domain.handle_rename_submit_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {"thread_id": "thread-1", "_form_value": {"rename_title": "new title"}},
        )
        archive_card = domain.handle_archive_thread_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {"thread_id": "thread-1"},
        )

        self.assertIn("另一终端", rename.text)
        self.assertIn("另一终端", archive.text)
        self.assertEqual(ports_stub.rename_calls, [])
        self.assertEqual(ports_stub.archive_calls, [])
        self.assertEqual(rename_card.toast.type, "warning")
        self.assertIn("另一终端", rename_card.toast.content)
        self.assertEqual(archive_card.toast.type, "warning")
        self.assertIn("另一终端", archive_card.toast.content)
        self.assertEqual(
            ports_stub.mutation_access_checks,
            [
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
            ],
        )

    def test_archive_command_reports_unknown_without_claiming_success(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.archive_result = {
            "thread_id": "thread-1",
            "upstream_outcome": "unknown",
            "outcome_detail": "request timed out",
            "focus_cleanup": "skipped",
        }
        domain = _make_domain(ports_stub)

        result = domain.handle_archive_command("ou_user", "chat-a", "thread-1")

        self.assertIn("结果未知", result.text)
        self.assertIn("不要直接重试", result.text)

    def test_archive_command_reports_focus_safety_scope_after_success(self) -> None:
        ports_stub = _PortsStub()
        domain = _make_domain(ports_stub)

        result = domain.handle_archive_command("ou_user", "chat-a", "thread-1")

        self.assertIn("只协调了本机已知 Focus/fcodex runtime", result.text)

    def test_handle_resume_thread_action_ignores_goals_disabled_and_still_submits_resume(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        ports_stub.goal_error = CodexRpcError(
            "thread/goal/get",
            {"code": -32602, "message": "goals feature is disabled"},
        )
        domain = _make_domain(
            ports_stub,
            submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append(
                (fn, args, kwargs)
            ),
        )

        response = domain.handle_resume_thread_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {"thread_id": "thread-1", "thread_title": "demo"},
        )

        self.assertIsNotNone(response.card)
        self.assertIsNone(response.toast)
        self.assertEqual(len(submit_calls), 1)


if __name__ == "__main__":
    unittest.main()
