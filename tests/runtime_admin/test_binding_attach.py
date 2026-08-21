import unittest

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.reason_codes import (
    PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    ReasonedCheck,
)
from bot.thread_lifecycle_service import (
    ThreadLifecyclePolicyError,
)
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeAdminControllerBindingAttachTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_active_attach_uses_observer_resume_without_writer_admission(
        self,
    ) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-1",
        )
        with lock:
            state["feishu_runtime_state"] = "detached"
        summary = self._direct_root_summary("thread-1")
        summary.status = "active"
        summaries["thread-1"] = summary
        controller._binding_application._prompt_write_denial_check = (
            lambda *_args, **_kwargs: self.fail(
                "active observer attach must not request writer admission"
            )
        )
        calls: list[tuple[tuple[str, str], str, bool]] = []

        def attach(target_binding, thread_id, *, active_observer=False):
            calls.append((target_binding, thread_id, active_observer))
            return summary

        controller._binding_application._attach_binding = attach

        result = controller.attach_binding(binding, writer_binding=binding)

        self.assertTrue(result["active_observer"])
        self.assertEqual(calls, [(binding, "thread-1", True)])

    def test_active_attach_rejects_group_all_exclusivity_before_resume(
        self,
    ) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "chat-b")
        state = self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-1",
        )
        with lock:
            state["feishu_runtime_state"] = "detached"
        summary = self._direct_root_summary("thread-1")
        summary.status = "active"
        summaries["thread-1"] = summary
        controller._binding_application._all_mode_thread_exclusivity_check = (
            lambda chat_id, thread_id: ReasonedCheck.deny(
                "group_all_thread_exclusive",
                f"群聊 `{chat_id}` 的 all 模式不能共享 `{thread_id}`。",
            )
        )
        controller._binding_application._detached_runtime_attach_check = (
            lambda _thread_id: self.fail(
                "group-all rejection must precede runtime attach admission"
            )
        )
        resume_calls: list[tuple[tuple[str, str], str, bool]] = []

        def resume(target_binding, thread_id, *, active_observer=False):
            resume_calls.append((target_binding, thread_id, active_observer))
            return summary

        controller._binding_application._attach_binding = resume

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "all 模式"):
            controller.attach_binding(binding, writer_binding=binding)

        self.assertEqual(resume_calls, [])
        with lock:
            current = binding_runtime.binding_runtime_snapshot_locked(binding)
            assert current is not None
            self.assertEqual(current.feishu_runtime_state, "detached")

    def test_trusted_active_attach_preserves_existing_cli_admission(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "chat-b")
        state = self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-1",
        )
        with lock:
            state["feishu_runtime_state"] = "detached"
        summary = self._direct_root_summary("thread-1")
        summary.status = "active"
        summaries["thread-1"] = summary
        controller._binding_application._all_mode_thread_exclusivity_check = (
            lambda *_args, **_kwargs: self.fail(
                "trusted control attach must retain its existing admission path"
            )
        )
        calls: list[tuple[tuple[str, str], str, bool]] = []

        def attach(target_binding, thread_id, *, active_observer=False):
            calls.append((target_binding, thread_id, active_observer))
            return summary

        controller._binding_application._attach_binding = attach

        result = controller.attach_binding(binding, writer_binding=None)

        self.assertTrue(result["active_observer"])
        self.assertEqual(calls, [(binding, "thread-1", True)])

    def test_active_attach_raced_to_idle_does_not_claim_observer_mirror(
        self,
    ) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-1",
        )
        with lock:
            state["feishu_runtime_state"] = "detached"
        active = self._direct_root_summary("thread-1")
        active.status = "active"
        summaries["thread-1"] = active
        idle = self._direct_root_summary("thread-1")
        idle.status = "idle"
        controller._binding_application._attach_binding = (
            lambda _binding, _thread_id, *, active_observer=False: idle
        )

        result = controller.attach_binding(binding, writer_binding=binding)

        self.assertTrue(result["changed"])
        self.assertFalse(result["active_observer"])

    def test_attach_service_is_partial_success_by_thread(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding_one = ("ou_user", "c1")
        binding_two = ("ou_user", "c2")
        state_one = self._bind_thread(lock, binding_runtime, binding_one, thread_id="thread-1")
        state_two = self._bind_thread(lock, binding_runtime, binding_two, thread_id="thread-2")
        with lock:
            state_one["feishu_runtime_state"] = "detached"
            state_two["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo-1",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="demo-2",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        controller._binding_application._detached_runtime_attach_check = lambda thread_id: (
            ReasonedCheck.allow()
            if thread_id == "thread-1"
            else ReasonedCheck.deny(
                PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
                "当前 thread 仍由运行中的实例 `explorer` 保持为 loaded (`idle`)；当前按 fail-close 拒绝跨实例继续。",
            )
        )

        result = controller._binding_application.attach_service()

        self.assertEqual(result["attached_thread_ids"], ["thread-1"])
        self.assertEqual(result["attached_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(len(result["blocked_threads"]), 1)
        self.assertEqual(result["blocked_threads"][0]["thread_id"], "thread-2")
        self.assertEqual(result["blocked_threads"][0]["binding_ids"], ["p2p:ou_user:c2"])
        self.assertIn("拒绝跨实例继续", result["blocked_threads"][0]["reason"])

    def test_attach_binding_requires_its_feishu_writer_admission_before_resume(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        with lock:
            state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        admissions: list[tuple[tuple[str, str], str]] = []
        resumed: list[tuple[tuple[str, str], str]] = []
        controller._binding_application._prompt_write_denial_check = (
            lambda writer_binding, _chat_id, thread_id, *, message_id="": (
                admissions.append((writer_binding, thread_id))
                or ReasonedCheck.deny(
                    PROMPT_DENIED_BY_INTERACTION_OWNER,
                    "当前 root operation 正由另一前端执行；本会话可继续查看，但不能恢复订阅。",
                )
            )
        )
        controller._binding_application._attach_binding = lambda target_binding, thread_id: (
            resumed.append((target_binding, thread_id)) or summaries[thread_id]
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一前端"):
            controller.attach_binding(binding, writer_binding=binding)

        self.assertEqual(admissions, [(binding, "thread-1")])
        self.assertEqual(resumed, [])
        with lock:
            self.assertEqual(
                binding_runtime.binding_runtime_snapshot_locked(binding).feishu_runtime_state,  # type: ignore[union-attr]
                "detached",
            )

    def test_identityless_attach_control_cannot_impersonate_a_feishu_binding(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        with lock:
            state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        admissions: list[tuple[str, object]] = []
        resumed: list[tuple[tuple[str, str], str]] = []
        controller._binding_application._external_control_write_denial_check = (
            lambda thread_id, writer_holder=None: (
                admissions.append((thread_id, writer_holder))
                or ReasonedCheck.deny(
                    PROMPT_DENIED_BY_INTERACTION_OWNER,
                    "当前 root operation 正由另一前端执行；本机控制面可继续查看，但不能恢复订阅。",
                )
            )
        )
        controller._binding_application._attach_binding = lambda target_binding, thread_id: (
            resumed.append((target_binding, thread_id)) or summaries[thread_id]
        )

        for method, params in (
            ("binding/attach", {"binding_id": "p2p:ou_user:c1"}),
            ("thread/attach", {"thread_id": "thread-1"}),
            ("service/attach", {}),
        ):
            with self.subTest(method=method):
                admissions.clear()
                if method == "service/attach":
                    result = controller.handle_service_control_request(method, params)
                    self.assertEqual(result["attached_thread_ids"], [])
                    self.assertEqual(len(result["blocked_threads"]), 1)
                    self.assertIn("另一前端", result["blocked_threads"][0]["reason"])
                else:
                    with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一前端"):
                        controller.handle_service_control_request(method, params)
                self.assertEqual(admissions, [("thread-1", None)])
                self.assertEqual(resumed, [])
                with lock:
                    self.assertEqual(
                        binding_runtime.binding_runtime_snapshot_locked(binding).feishu_runtime_state,  # type: ignore[union-attr]
                        "detached",
                    )

    def test_identityless_attach_control_fails_closed_for_active_or_unreadable_goal(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        with lock:
            state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        resumed: list[tuple[tuple[str, str], str]] = []
        controller._binding_application._attach_binding = lambda target_binding, thread_id: (
            resumed.append((target_binding, thread_id)) or summaries[thread_id]
        )

        controller._thread_goals["thread-1"] = ThreadGoalSummary(  # type: ignore[attr-defined]
            thread_id="thread-1",
            objective="continue without a writer",
            status="active",
        )
        for method, params in (
            ("binding/attach", {"binding_id": "p2p:ou_user:c1"}),
            ("thread/attach", {"thread_id": "thread-1"}),
            ("service/attach", {}),
        ):
            with self.subTest(goal="active", method=method):
                if method == "service/attach":
                    result = controller.handle_service_control_request(method, params)
                    self.assertEqual(result["attached_thread_ids"], [])
                    self.assertEqual(len(result["blocked_threads"]), 1)
                    self.assertIn("persisted goal 为 active", result["blocked_threads"][0]["reason"])
                else:
                    with self.assertRaisesRegex(ThreadLifecyclePolicyError, "persisted goal 为 active"):
                        controller.handle_service_control_request(method, params)
                self.assertEqual(resumed, [])
                with lock:
                    self.assertEqual(
                        binding_runtime.binding_runtime_snapshot_locked(binding).feishu_runtime_state,  # type: ignore[union-attr]
                        "detached",
                    )

        def _unreadable_goal(_thread_id: str) -> ThreadGoalSummary | None:
            raise RuntimeError("app-server goal read failed")

        controller._binding_application._get_thread_goal = _unreadable_goal
        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "无法确认.*persisted goal"):
            controller.handle_service_control_request(
                "binding/attach",
                {"binding_id": "p2p:ou_user:c1"},
            )
        self.assertEqual(resumed, [])

        controller._binding_application._get_thread_goal = lambda _thread_id: ThreadGoalSummary(
            thread_id="thread-1",
            objective="a newer app-server status",
            status="futureStatus",
        )
        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "无法确认.*是否会继续执行"):
            controller.handle_service_control_request(
                "binding/attach",
                {"binding_id": "p2p:ou_user:c1"},
            )
        self.assertEqual(resumed, [])

    def test_goals_feature_disabled_is_safe_for_identityless_attach(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        with lock:
            state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        resumed: list[tuple[tuple[str, str], str]] = []
        controller._binding_application._attach_binding = lambda target_binding, thread_id: (
            resumed.append((target_binding, thread_id)) or summaries[thread_id]
        )
        controller._binding_application._get_thread_goal = lambda _thread_id: (_ for _ in ()).throw(
            CodexRpcError(
                "thread/goal/get",
                {"code": -32602, "message": "goals feature is disabled"},
            )
        )

        result = controller.handle_service_control_request(
            "binding/attach",
            {"binding_id": "p2p:ou_user:c1"},
        )

        self.assertTrue(result["changed"])
        self.assertEqual(resumed, [(binding, "thread-1")])

    def test_identityless_attach_control_allows_known_inactive_goal(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        with lock:
            state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        resumed: list[tuple[tuple[str, str], str]] = []
        controller._binding_application._attach_binding = lambda target_binding, thread_id: (
            resumed.append((target_binding, thread_id)) or summaries[thread_id]
        )

        for status in (None, "paused", "blocked", "usageLimited", "budgetLimited", "complete"):
            with self.subTest(status=status or "no goal"):
                resumed.clear()
                if status is None:
                    controller._thread_goals.pop("thread-1", None)  # type: ignore[attr-defined]
                else:
                    controller._thread_goals["thread-1"] = ThreadGoalSummary(  # type: ignore[attr-defined]
                        thread_id="thread-1",
                        objective="bounded maintenance",
                        status=status,
                    )

                result = controller.handle_service_control_request(
                    "binding/attach",
                    {"binding_id": "p2p:ou_user:c1"},
                )

                self.assertTrue(result["changed"])
                self.assertEqual(resumed, [(binding, "thread-1")])

    def test_handle_preflight_command_blocks_detached_binding_when_live_runtime_owner_blocks_attach(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        state["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        controller._binding_application._detached_runtime_attach_check = lambda thread_id: ReasonedCheck.deny(
            PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            "当前线程正由实例 `default` 的本地 `fcodex` 持有 live runtime；当前不支持跨实例继续。",
        )

        result = controller.handle_preflight_command(binding, "")

        assert result.card is not None
        content = result.card["elements"][0]["content"]
        self.assertIn("下一条普通消息：`blocked` (`prompt_denied_by_live_runtime_owner`)", content)
        self.assertIn("本地 `fcodex` 持有 live runtime", content)


if __name__ == "__main__":
    unittest.main()
