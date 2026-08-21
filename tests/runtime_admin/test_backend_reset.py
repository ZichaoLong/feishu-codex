import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSummary
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
    _backend_reset_result,
)


class RuntimeAdminControllerBackendResetTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_handle_service_control_request_service_status_aggregates_runtime_inventory(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
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
        state["running"] = True
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="active",
        )
        loaded_thread_ids.append("thread-1")

        status = controller.handle_service_control_request("service/status", {})

        self.assertEqual(status["instance_name"], "corp-a")
        self.assertEqual(status["binding_count"], 1)
        self.assertEqual(status["bound_binding_count"], 1)
        self.assertEqual(status["attached_binding_count"], 1)
        self.assertEqual(status["thread_count"], 1)
        self.assertEqual(status["loaded_thread_ids"], ["thread-1"])
        self.assertEqual(status["running_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(status["app_server_url"], "http://127.0.0.1:1234")
        self.assertTrue(status["web_gateway_enabled"])
        self.assertEqual(status["web_gateway_url"], "http://127.0.0.1:8766")
        self.assertEqual(status["backend_reset_status"], "force-only")
        self.assertEqual(status["backend_reset_reason_code"], "backend_reset_force_only_by_running_binding")
        self.assertEqual(status["operator_status"]["status"], "ok")

    def test_handle_service_control_request_reset_backend_forwards_force_flag(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            reset_calls,
            _sent_images,
        ) = self._make_controller()

        result = controller.handle_service_control_request("service/reset-backend", {"force": True})

        self.assertEqual(reset_calls, [True])
        self.assertTrue(result["force"])

    def test_handle_reset_backend_command_renders_available_preview_card(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
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

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        self.assertEqual(result.card["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("作用对象：当前实例 backend", result.card["elements"][0]["content"])
        action = result.card["elements"][2]["actions"][0]
        self.assertEqual(action["text"]["content"], "重置 backend")
        self.assertEqual(action["value"]["force"], False)

    def test_handle_reset_backend_command_renders_force_reset_button_when_force_only(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        pending_requests.append({"request_id": "req-1"})

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        self.assertIn("只能显式确认强制重置", result.card["elements"][0]["content"])
        action = result.card["elements"][2]["actions"][0]
        self.assertEqual(action["text"]["content"], "强制重置 backend")
        self.assertEqual(action["value"]["force"], True)

    def test_backend_reset_preview_exposes_blockers_and_collateral_summary(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        state["running"] = True
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="active",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="other",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        loaded_thread_ids.extend(["thread-1", "thread-2"])
        pending_requests.append({"request_id": "req-1"})

        preview = controller.backend_reset_preview()

        self.assertEqual(preview.status, "force-only")
        self.assertEqual(preview.blocking_pending_request_count, 1)
        self.assertEqual(preview.blocking_active_turn_count, 1)
        self.assertEqual(preview.attached_binding_ids, ("p2p:ou_user:c1",))
        self.assertEqual(preview.loaded_thread_preview, ("thread-1", "thread-2"))
        self.assertEqual(preview.collateral_loaded_thread_count, 2)
        self.assertEqual(preview.collateral_active_loaded_thread_count, 1)
        self.assertIn("hard blocker：待处理审批/输入请求：`1`", preview.diagnostics)
        self.assertIn("collateral impact：attached Feishu bindings：`p2p:ou_user:c1`", preview.diagnostics)
        self.assertIn("collateral impact：当前实例 loaded threads：`2`", preview.diagnostics)

    def test_handle_reset_backend_command_renders_hard_blockers_and_collateral_sections(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        state["running"] = True
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="active",
        )
        loaded_thread_ids.append("thread-1")
        pending_requests.append({"request_id": "req-1"})

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        content = result.card["elements"][0]["content"]
        self.assertIn("**Hard Blockers**", content)
        self.assertIn("待处理审批/输入请求：`1`", content)
        self.assertIn("**Collateral Impact**", content)
        self.assertIn("attached Feishu bindings：`p2p:ou_user:c1`", content)
        self.assertIn("当前实例 loaded threads：`1`", content)

    def test_handle_reset_backend_action_executes_reset_and_returns_result_card(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            reset_calls,
            _sent_images,
        ) = self._make_controller()

        response = controller.handle_reset_backend_action("ou_user", "c1", "m1", {"force": True})

        self.assertEqual(reset_calls, [True])
        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.toast.content, "已重置当前实例 backend。")
        self.assertIsNotNone(response.card)
        assert response.card is not None
        self.assertEqual(response.card.data["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("已重置当前实例 backend。", response.card.data["elements"][0]["content"])
        self.assertIn("如需确认飞书侧继续接收本地", response.card.data["elements"][0]["content"])
        actions = response.card.data["elements"][-1]["actions"]
        self.assertEqual([action["text"]["content"] for action in actions], ["附着当前实例", "保持 detached"])

    def test_handle_reset_backend_action_rejects_non_boolean_force(self) -> None:
        for force in (None, 0, 1, "", "false", [], {}):
            with self.subTest(force=force):
                (
                    _lock,
                    _binding_runtime,
                    controller,
                    _summaries,
                    _loaded_thread_ids,
                    _unsubscribed,
                    _archived,
                    _released_runtime_leases,
                    _pending_by_thread,
                    _pending_by_binding,
                    _pending_requests,
                    reset_calls,
                    _sent_images,
                ) = self._make_controller()
                controller._binding_application.effective_binding_key = (  # type: ignore[method-assign]
                    lambda *_args: self.fail("invalid force read binding state")
                )
                controller.backend_reset_preview = (  # type: ignore[method-assign]
                    lambda: self.fail("invalid force read reset preview")
                )

                response = controller.handle_reset_backend_action(
                    "ou_user",
                    "c1",
                    "m1",
                    {"force": force},
                )

                self.assertEqual(reset_calls, [])
                self.assertEqual(response.toast.type, "warning")
                self.assertIn("JSON boolean", response.toast.content)

    def test_handle_reset_backend_action_defaults_missing_force_to_safe(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            reset_calls,
            _sent_images,
        ) = self._make_controller()

        response = controller.handle_reset_backend_action(
            "ou_user",
            "c1",
            "m1",
            {},
        )

        self.assertEqual(reset_calls, [False])
        self.assertEqual(response.toast.type, "success")

    def test_handle_reset_backend_action_reports_malformed_result_as_unknown(self) -> None:
        malformed_results = (
            {},
            _backend_reset_result(True),
            {**_backend_reset_result(False), "projection_warnings": [1]},
        )
        for raw_result in malformed_results:
            with self.subTest(raw_result=raw_result):
                (
                    _lock,
                    _binding_runtime,
                    controller,
                    _summaries,
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
                controller.backend_reset_preview = (  # type: ignore[method-assign]
                    lambda: self.fail("unknown result must not rebuild reset preview")
                )

                with patch.object(
                    controller,
                    "_reset_current_instance_backend",
                    return_value=raw_result,
                ) as reset_backend:
                    response = controller.handle_reset_backend_action(
                        "ou_user",
                        "c1",
                        "m1",
                        {"force": False},
                    )

                reset_backend.assert_called_once_with(False)
                self.assertEqual(response.toast.type, "warning")
                self.assertEqual(
                    response.toast.content,
                    "backend 可能已重置，但结果未知；请勿立即重复操作。",
                )
                self.assertIsNotNone(response.card)
                assert response.card is not None
                content = response.card.data["elements"][0]["content"]
                self.assertIn("不声明成功或失败", content)
                self.assertIn(
                    "focusctl --instance corp-a service status",
                    content,
                )
                self.assertNotIn("已重置当前实例 backend", content)
                self.assertNotIn(
                    "action",
                    [element["tag"] for element in response.card.data["elements"]],
                )

    def test_handle_reset_backend_action_offers_current_thread_attach_after_reset(self) -> None:
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
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        response = controller.handle_reset_backend_action("ou_user", "c1", "m1", {"force": False})

        self.assertIsNotNone(response.card)
        assert response.card is not None
        actions = response.card.data["elements"][-1]["actions"]
        self.assertEqual(
            [action["text"]["content"] for action in actions],
            ["附着当前线程", "附着当前实例", "保持 detached"],
        )
        self.assertEqual(actions[0]["value"]["thread_id"], "thread-1")

    def test_service_status_reports_runtime_unverified_as_force_only(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
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
        controller._list_loaded_thread_ids = lambda: (_ for _ in ()).throw(RuntimeError("backend down"))

        status = controller.handle_service_control_request("service/status", {})

        self.assertEqual(status["backend_reset_status"], "force-only")
        self.assertEqual(
            status["backend_reset_reason_code"],
            "backend_reset_force_only_by_runtime_unverified",
        )


if __name__ == "__main__":
    unittest.main()
