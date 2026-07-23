import unittest
from types import SimpleNamespace

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.codex_threads_ui_domain import CodexThreadsUiDomain, ThreadsUiPorts, ThreadsUiRuntimePorts


class _PortsStub:
    def __init__(self) -> None:
        self.archive_calls: list[tuple[str, ThreadSummary | None]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.reply_calls: list[tuple[str, str, str]] = []
        self.resolve_calls: list[str] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.patches: list[tuple[str, str]] = []
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

    def _get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = ""):
        del sender_id, chat_id, message_id
        return SimpleNamespace(
            running=False,
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
        self.reply_calls.append((chat_id, text, message_id))

    def _resolve_resume_target(self, arg: str) -> ThreadSummary:
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

    def _get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None:
        del thread_id
        return self.goal

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

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return True

    def is_thread_not_loaded_error(self, exc: Exception) -> bool:
        del exc
        return False


class CodexThreadsUiDomainTests(unittest.TestCase):
    def test_handle_resume_command_dispatches_via_runtime_port(self) -> None:
        ports_stub = _PortsStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        resume_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=ports_stub._get_thread_goal,
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append((fn, args, kwargs)),
                resume_thread_on_runtime=lambda *args, **kwargs: resume_calls.append((args, kwargs)),
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
        self.assertEqual(resume_calls, [])

    def test_resume_target_on_runtime_calls_resume_port_directly(self) -> None:
        ports_stub = _PortsStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        resume_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=ports_stub._get_thread_goal,
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append((fn, args, kwargs)),
                resume_thread_on_runtime=lambda *args, **kwargs: resume_calls.append((args, kwargs)),
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
        self.assertEqual(len(resume_calls), 1)
        args, kwargs = resume_calls[0]
        self.assertEqual(args, ("ou_user", "chat-a", "thread-1"))
        self.assertEqual(kwargs["original_arg"], "thread-1")
        self.assertEqual(kwargs["summary"], ports_stub.thread)
        self.assertEqual(kwargs["message_id"], "msg-1")
        self.assertEqual(kwargs["refresh_threads_message_id"], "msg-session")

    def test_handle_archive_thread_action_uses_control_path(self) -> None:
        ports_stub = _PortsStub()
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=ports_stub._get_thread_goal,
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: None,
                resume_thread_on_runtime=lambda *args, **kwargs: None,
            ),
        )

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

    def test_archive_command_reports_unknown_without_claiming_success(self) -> None:
        ports_stub = _PortsStub()
        ports_stub.archive_result = {
            "thread_id": "thread-1",
            "upstream_outcome": "unknown",
            "outcome_detail": "request timed out",
            "focus_cleanup": "skipped",
        }
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=ports_stub._get_thread_goal,
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: None,
                resume_thread_on_runtime=lambda *args, **kwargs: None,
            ),
        )

        result = domain.handle_archive_command("ou_user", "chat-a", "thread-1")

        self.assertIn("结果未知", result.text)
        self.assertIn("不要直接重试", result.text)

    def test_archive_command_reports_focus_safety_scope_after_success(self) -> None:
        ports_stub = _PortsStub()
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=ports_stub._get_thread_goal,
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: None,
                resume_thread_on_runtime=lambda *args, **kwargs: None,
            ),
        )

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
        domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=ports_stub._get_runtime_view,
                is_group_chat=ports_stub._is_group_chat,
                is_group_admin_actor=ports_stub._is_group_admin_actor,
                rename_bound_thread_title=ports_stub._rename_bound_thread_title,
                reply_text=ports_stub._reply_text,
                resolve_resume_target=ports_stub._resolve_resume_target,
                list_visible_current_dir_threads=ports_stub._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=ports_stub._read_thread_summary_authoritatively,
                get_thread_goal=lambda _thread_id: (_ for _ in ()).throw(
                    CodexRpcError("thread/goal/get", {"code": -32602, "message": "goals feature is disabled"})
                ),
                archive_thread_for_control=ports_stub._archive_thread_for_control,
                rename_thread=ports_stub.rename_thread,
                patch_message=ports_stub.patch_message,
                is_thread_not_loaded_error=ports_stub.is_thread_not_loaded_error,
                threads_initial_limit=5,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append((fn, args, kwargs)),
                resume_thread_on_runtime=lambda *args, **kwargs: None,
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
