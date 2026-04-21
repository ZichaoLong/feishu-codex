import threading
import unittest
from types import SimpleNamespace

from bot.adapters.base import ThreadSummary
from bot.codex_session_ui_domain import CodexSessionUiDomain, SessionUiRuntimePorts


class _OwnerStub:
    def __init__(self) -> None:
        self.bot = SimpleNamespace(patch_message=lambda message_id, content: None)
        self._adapter = SimpleNamespace()
        self._lock = threading.RLock()
        self._session_recent_limit = 5
        self._thread_list_query_limit = 20
        self.reply_calls: list[tuple[str, str, str]] = []
        self.resolve_calls: list[str] = []
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

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        del sender_id, chat_id, message_id

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
        del thread_id, original_arg
        return self.thread


class CodexSessionUiDomainTests(unittest.TestCase):
    def test_handle_resume_command_dispatches_via_runtime_port(self) -> None:
        owner = _OwnerStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        resume_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        domain = CodexSessionUiDomain(
            owner,
            runtime_ports=SessionUiRuntimePorts(
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
        self.assertEqual(kwargs, {"message_id": "msg-1"})
        self.assertEqual(resume_calls, [])

    def test_resume_target_on_runtime_calls_resume_port_directly(self) -> None:
        owner = _OwnerStub()
        submit_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        resume_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        domain = CodexSessionUiDomain(
            owner,
            runtime_ports=SessionUiRuntimePorts(
                submit_to_runtime=lambda fn, *args, **kwargs: submit_calls.append((fn, args, kwargs)),
                resume_thread_on_runtime=lambda *args, **kwargs: resume_calls.append((args, kwargs)),
            ),
        )

        domain._resume_target_on_runtime(
            "ou_user",
            "chat-a",
            "thread-1",
            message_id="msg-1",
            refresh_session_message_id="msg-session",
        )

        self.assertEqual(owner.resolve_calls, ["thread-1"])
        self.assertEqual(submit_calls, [])
        self.assertEqual(len(resume_calls), 1)
        args, kwargs = resume_calls[0]
        self.assertEqual(args, ("ou_user", "chat-a", "thread-1"))
        self.assertEqual(kwargs["original_arg"], "thread-1")
        self.assertEqual(kwargs["summary"], owner.thread)
        self.assertEqual(kwargs["message_id"], "msg-1")
        self.assertEqual(kwargs["refresh_session_message_id"], "msg-session")


if __name__ == "__main__":
    unittest.main()
