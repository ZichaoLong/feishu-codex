import unittest
from types import SimpleNamespace

from bot.adapters.base import ThreadGoalSummary
from bot.cards import build_goal_detached_confirm_card
from bot.codex_goal_domain import CodexGoalDomain, GoalDomainPorts


class _PortsStub:
    def __init__(self) -> None:
        self.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="existing goal",
            status="paused",
            token_budget=None,
            tokens_used=0,
            time_used_seconds=0,
            created_at=0,
            updated_at=0,
        )
        self.set_calls: list[dict[str, object]] = []
        self.clear_calls: list[str] = []
        self.denial = "当前线程正由另一终端执行；本会话可继续查看，但暂时不能写入。"
        self.denial_checks: list[tuple[str, str, str, str]] = []
        self.runtime_submissions: list[tuple[object, tuple[object, ...]]] = []
        self.resume_calls: list[tuple[object, ...]] = []
        self.card_replies: list[tuple[str, dict, str]] = []

    @staticmethod
    def resolve_session(_sender_id: str, _chat_id: str, _message_id: str = ""):
        return SimpleNamespace(
            current_thread_id="thread-1",
            current_thread_title="demo",
            thread=SimpleNamespace(feishu_runtime_state="attached"),
        )

    def get_thread_goal(self, _thread_id: str) -> ThreadGoalSummary | None:
        return self.goal

    def mutate_goal(
        self,
        _sender_id: str,
        _chat_id: str,
        thread_id: str,
        **kwargs: object,
    ) -> ThreadGoalSummary:
        self.set_calls.append({"thread_id": thread_id, **kwargs})
        return self.goal

    def clear_goal(
        self,
        _sender_id: str,
        _chat_id: str,
        thread_id: str,
        **_kwargs: object,
    ) -> bool:
        self.clear_calls.append(thread_id)
        return True

    def thread_mutation_denial_text(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> str:
        self.denial_checks.append((sender_id, chat_id, thread_id, message_id))
        return self.denial

    @staticmethod
    def attach_current_binding(_sender_id: str, _chat_id: str, _message_id: str) -> None:
        raise AssertionError("a denied goal mutation must not attach a binding")

    @staticmethod
    def update_runtime_goal_projection(
        _sender_id: str,
        _chat_id: str,
        _message_id: str,
        _goal: ThreadGoalSummary | None,
    ) -> None:
        raise AssertionError("a denied goal mutation must not update the projection")

    def submit_to_runtime(self, fn: object, *args: object) -> None:
        self.runtime_submissions.append((fn, args))

    def resume_goal(self, *args: object) -> dict:
        self.resume_calls.append(args)
        return {"result": "settled"}

    def reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None:
        self.card_replies.append((chat_id, card, message_id))


class CodexGoalDomainTests(unittest.TestCase):
    @staticmethod
    def _make_domain(ports: _PortsStub) -> CodexGoalDomain:
        return CodexGoalDomain(
            ports=GoalDomainPorts(
                resolve_session=ports.resolve_session,
                get_thread_goal=ports.get_thread_goal,
                mutate_goal=ports.mutate_goal,
                clear_goal=ports.clear_goal,
                thread_mutation_denial_text=ports.thread_mutation_denial_text,
                attach_current_binding=ports.attach_current_binding,
                update_runtime_goal_projection=ports.update_runtime_goal_projection,
                submit_to_runtime=ports.submit_to_runtime,
                resume_goal=ports.resume_goal,
                reply_card=ports.reply_card,
            )
        )

    def test_goal_mutations_do_not_bypass_live_writer_denial(self) -> None:
        ports = _PortsStub()
        domain = self._make_domain(ports)

        set_result = domain.handle_goal_command("ou_user", "chat-a", "set new objective", message_id="msg-1")
        pause_result = domain.handle_goal_command("ou_user", "chat-a", "pause", message_id="msg-1")
        clear_result = domain.handle_goal_command("ou_user", "chat-a", "clear", message_id="msg-1")
        resume_result = domain.handle_goal_command("ou_user", "chat-a", "resume", message_id="msg-1")

        for result in (set_result, pause_result, clear_result, resume_result):
            self.assertIsNotNone(result.card)
            self.assertIn("另一终端", result.card["elements"][0]["content"])
        self.assertEqual(ports.set_calls, [])
        self.assertEqual(ports.clear_calls, [])
        self.assertEqual(ports.runtime_submissions, [])
        self.assertEqual(
            ports.denial_checks,
            [
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
                ("ou_user", "chat-a", "thread-1", "msg-1"),
            ],
        )

    def test_active_confirmation_fast_ack_only_submits_exact_thread(self) -> None:
        ports = _PortsStub()

        def forbidden_read(*_args: object) -> object:
            raise AssertionError("fast ACK must not read RuntimeLoop-owned state")

        ports.resolve_session = forbidden_read  # type: ignore[method-assign]
        ports.get_thread_goal = forbidden_read  # type: ignore[method-assign]
        ports.thread_mutation_denial_text = forbidden_read  # type: ignore[method-assign]
        domain = self._make_domain(ports)

        response = domain.handle_goal_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {
                "action": "goal_apply_confirm",
                "thread_id": "thread-1",
                "status": "active",
                "attach_binding": "true",
            },
        )

        self.assertIsNone(response.toast)
        self.assertIsNotNone(response.card)
        self.assertEqual(len(ports.runtime_submissions), 1)
        submitted, args = ports.runtime_submissions[0]
        self.assertEqual(submitted, domain.resume_goal_on_runtime)
        self.assertEqual(
            args,
            ("ou_user", "chat-a", "thread-1", True, "msg-1"),
        )
        self.assertEqual(ports.resume_calls, [])
        submitted(*args)
        self.assertEqual(
            ports.resume_calls,
            [("ou_user", "chat-a", "thread-1", True, "msg-1")],
        )
        self.assertEqual(
            ports.card_replies,
            [("chat-a", {"result": "settled"}, "msg-1")],
        )

    def test_active_confirmation_without_thread_id_fails_closed(self) -> None:
        ports = _PortsStub()
        ports.resolve_session = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("invalid fast ACK must not read runtime state")
        )
        domain = self._make_domain(ports)

        response = domain.handle_goal_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {
                "action": "goal_apply_confirm",
                "status": "active",
                "attach_binding": "true",
            },
        )

        self.assertIsNotNone(response.toast)
        self.assertEqual(response.toast.type, "warning")
        self.assertIn("thread_id", response.toast.content)
        self.assertEqual(ports.runtime_submissions, [])

    def test_resume_presentation_failure_happens_after_settlement(self) -> None:
        ports = _PortsStub()
        events: list[str] = []

        def resume_goal(*_args: object) -> dict:
            events.append("settled")
            return {"result": "settled"}

        def fail_presentation(*_args: object, **_kwargs: object) -> None:
            events.append("presentation")
            raise RuntimeError("presentation unavailable")

        ports.resume_goal = resume_goal  # type: ignore[method-assign]
        ports.reply_card = fail_presentation  # type: ignore[method-assign]
        domain = self._make_domain(ports)

        with self.assertRaisesRegex(RuntimeError, "presentation unavailable"):
            domain.resume_goal_on_runtime(
                "ou_user",
                "chat-a",
                "thread-1",
                True,
                "msg-1",
            )

        self.assertEqual(events, ["settled", "presentation"])

    def test_nonactive_confirmation_rejects_stale_thread_identity(self) -> None:
        ports = _PortsStub()
        domain = self._make_domain(ports)

        response = domain.handle_goal_action(
            "ou_user",
            "chat-a",
            "msg-1",
            {
                "action": "goal_apply_confirm",
                "thread_id": "thread-stale",
                "objective": "new objective",
                "attach_binding": "true",
            },
        )

        self.assertIsNotNone(response.toast)
        self.assertEqual(response.toast.type, "warning")
        self.assertIn("已过期", response.toast.content)
        self.assertEqual(ports.set_calls, [])
        self.assertEqual(ports.runtime_submissions, [])

    def test_detached_confirmation_actions_carry_exact_thread_identity(self) -> None:
        card = build_goal_detached_confirm_card(
            thread_id="thread-1",
            thread_title="demo",
            status="active",
        )

        action_rows = [
            element for element in card["elements"] if element.get("tag") == "action"
        ]
        values = [
            action["value"]
            for row in action_rows
            for action in row.get("actions", [])
        ]
        self.assertEqual(len(values), 2)
        self.assertEqual(
            {value.get("thread_id") for value in values},
            {"thread-1"},
        )

        with self.assertRaisesRegex(ValueError, "thread_id"):
            build_goal_detached_confirm_card(
                thread_id="",
                thread_title="demo",
                status="active",
            )


if __name__ == "__main__":
    unittest.main()
