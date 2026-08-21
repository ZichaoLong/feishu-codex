import os
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from tests.execution_page_test_support import set_execution_page_state as _set_pages
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
    _DISPLAY_CD_COMMAND,
    _DISPLAY_DEBUG_CONTACT_COMMAND,
    _DISPLAY_INIT_COMMAND,
    _DISPLAY_LOCAL_RESUME_COMMAND,
    _DISPLAY_RENAME_COMMAND,
    _admit_adapter_connection,
)


class CodexHandlerHelpActionTests(CodexHandlerHarness):
    def test_help_chat_page_mentions_status_preflight_and_cd(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help chat")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：连接状态")
        content = card["elements"][0]["content"]
        self.assertIn("查看当前状态、发送前检查", content)
        self.assertIn("附着当前实例", content)
        self.assertIn("切换线程或目录，请到“开始”", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["当前状态", "发送前检查"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["暂停推送", "附着当前实例"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["更多附着方式"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_chat_page_switches_toggle_to_attach_when_binding_detached(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        handler.handle_message("ou_user", "c1", "/help chat")

        _, card = bot.cards[-1]
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["恢复当前会话", "附着当前实例"],
        )
        self.assertEqual(action_elements[1]["actions"][0]["value"], {"action": "attach_runtime"})
        self.assertEqual(
            action_elements[1]["actions"][1]["value"],
            {"action": "attach_runtime", "scope": "service"},
        )

    def test_help_thread_page_mentions_resume_scope_and_local_resume(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help thread")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：开始")
        content = card["elements"][0]["content"]
        self.assertIn("同一线程允许多端订阅观察", content)
        self.assertIn("同一 live turn 只有一个交互 owner", content)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", content)
        self.assertIn("`focusctl thread list --scope cwd`", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["新建线程", "恢复线程"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["浏览线程", "切换目录"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["返回首页"],
        )

    def test_help_thread_settings_page_exposes_goal_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help thread-settings")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：线程设置")
        content = card["elements"][0]["content"]
        self.assertIn("当前 goal 可通过 `/goal` 查看", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["查看 Goal", "压缩上下文"],
        )

    def test_help_runtime_mentions_permissions_as_recommended_entry(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help runtime")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：本轮设置")
        content = card["elements"][0]["content"]
        self.assertIn("`/steer 〈text〉`", content)
        self.assertIn("不会排队、创建下一轮", content)
        self.assertIn("推荐先用“权限基线”", content)
        self.assertIn("`/last text`", content)
        self.assertIn("回退到最近执行卡", content)
        self.assertIn("实例级 backend reset 在“更多 -> 高级操作”", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["权限基线", "模型"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["推理强度", "审批策略"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["最近文本"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )
        steer_form = next(
            element
            for element in card["elements"]
            if element.get("tag") == "form"
        )
        self.assertEqual(steer_form["name"], "help_steer_form")

    def test_help_group_card_has_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：群聊设置")
        self.assertIn("未启用群里，非管理员不能使用机器人", card["elements"][0]["content"])
        self.assertIn("`all` 风险最高", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["群聊启用状态", "启用本群"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["停用本群", "群工作模式"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["返回首页"],
        )

    def test_help_identity_page_has_bootstrap_shortcuts(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help identity")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 工作台：更多")
        content = card["elements"][0]["content"]
        self.assertIn("`/whoami`", content)
        self.assertIn(f"`{_DISPLAY_INIT_COMMAND}`", content)
        self.assertNotIn("/debug-contact", content)
        action_elements = self._action_elements(card)
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["身份信息", "机器人状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["初始化", "命令索引"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["高级操作"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_page_action_returns_runtime_card(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "runtime"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：本轮设置")
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["权限基线", "模型"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["推理强度", "审批策略"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[2]["actions"]],
            ["最近文本"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[3]["actions"]],
            ["返回首页"],
        )

    def test_reset_backend_command_returns_preview_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/reset-backend")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("作用对象：当前实例 backend", card["elements"][0]["content"])

    def test_reset_backend_card_action_is_group_admin_only(self) -> None:
        handler, _ = self._make_handler()
        handler._feishu_platform.bot.message_contexts["msg-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group",
            {"action": "reset_backend", "force": True, "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "仅管理员可操作群共享会话或群设置。")

    def test_reset_backend_clears_connection_facts_and_fences_old_generation(self) -> None:
        handler, _ = self._make_handler()
        thread_id = "thread-before-backend-reset"
        old_generation = handler._adapter.connection_generation_value
        original_start = handler._adapter.start

        def start_replacement() -> None:
            original_start()
            handler._adapter.connection_generation_value = old_generation + 1

        handler._adapter.start = start_replacement
        handler._effective_settings.record_start_or_resume(
            thread_id,
            model="gpt-5.5",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            source="thread_resume",
        )

        with (
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ),
            patch.object(
                handler._thread_runtime_authority,
                "confirm_backend_reset",
                wraps=handler._thread_runtime_authority.confirm_backend_reset,
            ) as confirm_backend_reset,
        ):
            result = self._reset_backend(handler, force=False)

        confirm_backend_reset.assert_called_once_with()
        self.assertEqual(handler._adapter.start_calls, 1)
        self.assertEqual(result["app_server_url"], handler._adapter.config.app_server_url)
        self.assertIsNone(handler._effective_settings.resolve_model_for_request(thread_id))

        # A callback detached before the intentional stop may arrive after the
        # reset.  The reset boundary must reject that old websocket generation
        # instead of letting it rebuild a capability fact.
        self._dispatch_adapter_notification_for_connection(
            handler,
            old_generation,
            "thread/settings/updated",
            {
                "threadId": thread_id,
                "threadSettings": {
                    "model": "gpt-5.5",
                    "effort": "high",
                    "approvalPolicy": "never",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        self.assertIsNone(handler._effective_settings.resolve_model_for_request(thread_id))

        self._dispatch_adapter_notification_for_connection(
            handler,
            old_generation + 1,
            "thread/settings/updated",
            {
                "threadId": thread_id,
                "threadSettings": {
                    "model": "gpt-5.5",
                    "effort": "high",
                    "approvalPolicy": "never",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        self.assertEqual(
            handler._effective_settings.resolve_model_for_request(thread_id),
            "gpt-5.5",
        )

    def test_reset_backend_generation_read_failure_keeps_all_ingress_blocked(self) -> None:
        handler, _ = self._make_handler()
        thread_id = "thread-before-failed-backend-reset"
        old_generation = handler._adapter.connection_generation_value
        handler._effective_settings.record_start_or_resume(
            thread_id,
            model="gpt-5.5",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            source="thread_resume",
        )

        def generation_unavailable(**_kwargs) -> int:
            raise RuntimeError("generation unavailable")

        handler._adapter.connection_generation = generation_unavailable

        with self.assertRaisesRegex(RuntimeError, "generation unavailable"):
            self._reset_backend(handler, force=False)

        self.assertTrue(handler._adapter_ingress_gate.snapshot().backend_reset_blocked)
        self.assertTrue(handler._adapter.current_app_server_url())
        self.assertEqual(
            handler._handle_service_control_request(
                "service/status",
                {},
            )["app_server_url"],
            "",
        )
        self._dispatch_adapter_notification_for_connection(
            handler,
            old_generation,
            "thread/settings/updated",
            {
                "threadId": thread_id,
                "threadSettings": {
                    "model": "gpt-5.5",
                    "effort": "high",
                    "approvalPolicy": "never",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        self.assertIsNone(handler._effective_settings.resolve_model_for_request(thread_id))

    def test_explicit_backend_reset_recovers_transient_disconnect_cleanup_failure(self) -> None:
        handler, _ = self._make_handler()
        old_generation = handler._adapter.connection_generation_value
        original_start = handler._adapter.start

        def start_replacement() -> None:
            original_start()
            handler._adapter.connection_generation_value = old_generation + 1

        handler._adapter.start = start_replacement
        self.assertTrue(_admit_adapter_connection(handler, old_generation))
        handler._interaction_auto_resolution.schedule("old-request", enabled=True)

        with patch.object(
            handler._thread_runtime_authority,
            "invalidate_connection",
            side_effect=[RuntimeError("cleanup failed once"), None],
        ) as invalidate_connection:
            with self.assertRaisesRegex(RuntimeError, "cleanup failed once"):
                handler._runtime_call(
                    handler._adapter_events.handle_disconnect_for_connection,
                    old_generation,
                )

            blocked_status = handler._operational_status_snapshot()
            self.assertEqual(blocked_status["status"], "degraded")
            self.assertTrue(blocked_status["adapter_ingress"]["cleanup_required"])
            self.assertEqual(handler._interaction_auto_resolution._timers, {})
            self.assertEqual(handler._server_request_registry.connection_generation, 0)

            with patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ):
                self._reset_backend(handler, force=False)

        self.assertEqual(invalidate_connection.call_count, 2)
        recovered = handler._adapter_ingress_gate.snapshot()
        self.assertFalse(recovered.backend_reset_blocked)
        self.assertFalse(recovered.cleanup_required)
        self.assertEqual(handler._operational_status_snapshot()["status"], "ok")

    def test_help_page_action_returns_overview_dashboard(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "overview"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")
        action_elements = self._action_elements(response["card"])
        self.assertEqual(action_elements[0]["layout"], "bisected")
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["开始", "线程设置"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["本轮设置", "连接状态"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["群聊设置", "更多"],
        )

    def test_help_navigation_actions_are_not_group_admin_only(self) -> None:
        handler, _ = self._make_handler()
        handler._feishu_platform.bot.message_contexts["msg-help-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-help-group",
            {"action": "show_help_page", "page": "overview", "_operator_open_id": "ou_user"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")

    def test_help_show_page_action_can_open_current_thread_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-current"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：线程设置")
        self.assertIn("“开始”", response["card"]["elements"][0]["content"])
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["查看 Goal", "压缩上下文"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[1]["actions"]],
            ["重命名", "归档当前"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[2]["actions"]],
            ["按目标归档"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[3]["actions"]],
            ["返回首页"],
        )

    def test_help_show_page_action_can_open_thread_resume_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-resume-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：恢复线程")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertEqual(self._action_elements(response["card"])[0]["actions"][0]["text"]["content"], "返回上一页")

    def test_help_show_page_action_can_open_chat_cd_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "chat-cd-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：切换目录")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_CD_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_thread_rename_current_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "thread-rename-current-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：重命名")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_RENAME_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_identity_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "identity"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：更多")
        self.assertNotIn("/debug-contact", response["card"]["elements"][0]["content"])
        action_elements = self._action_elements(response["card"])
        self.assertEqual(
            [item["text"]["content"] for item in action_elements[0]["actions"]],
            ["身份信息", "机器人状态"],
        )

    def test_help_show_page_action_can_open_identity_init_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "identity-init-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：初始化")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_INIT_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_can_open_attach_more_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "connection-status-attach-more"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：更多附着方式")
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["附着当前线程", "附着当前会话"],
        )
        self.assertEqual(
            self._action_elements(response["card"])[0]["actions"][0]["value"],
            {"action": "attach_runtime", "scope": "thread"},
        )
        self.assertEqual(
            self._action_elements(response["card"])[0]["actions"][1]["value"],
            {"action": "attach_runtime"},
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["返回上一页"],
        )

    def test_help_show_page_action_can_open_more_advanced_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "more-advanced"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：高级操作")
        self.assertIn("恢复或排障时重置当前实例 backend", response["card"]["elements"][0]["content"])
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[0]["actions"]],
            ["重置 backend", "联系人排障"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in self._action_elements(response["card"])[1]["actions"]],
            ["返回上一页"],
        )

    def test_help_show_page_action_can_open_debug_contact_form(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "more-debug-contact-form"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台：联系人排障")
        self.assertTrue(any(element.get("tag") == "form" for element in response["card"]["elements"]))
        self.assertIn(_DISPLAY_DEBUG_CONTACT_COMMAND, response["card"]["elements"][0]["content"])

    def test_help_show_page_action_returns_warning_for_unknown_page(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "missing-page"},
        ))

        self.assertEqual(response["toast"], "未知帮助页面。")
        self.assertEqual(response["toast_type"], "warning")

    def test_help_unknown_topic_returns_warning_text(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/help nonsense")

        self.assertIn("帮助主题支持", bot.replies[-1][1])

    def test_unknown_command_mentions_help_and_commands(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/missing")

        self.assertIn("发送 `/help` 或 `/commands` 查看可用命令。", bot.replies[-1][1])

    def test_help_execute_command_action_reuses_status_command(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "help_execute_command", "command": "/status", "title": "Codex 当前状态"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前状态")
        self.assertIn("当前线程：`thread-1", response["card"]["elements"][0]["content"])

    def test_help_execute_group_command_uses_sender_id_fallback_for_group_admin(self) -> None:
        handler, _ = self._make_handler()
        handler._feishu_platform.bot.chat_types["chat-group"] = "group"

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_admin",
            "chat-group",
            "msg-help-card",
            {"action": "help_execute_command", "command": "/threads", "title": "Codex Threads"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")

    def test_help_execute_whoami_action_uses_operator_identity_context(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-p2p",
            "msg-help",
            {
                "action": "help_execute_command",
                "command": "/whoami",
                "title": "Codex 身份信息",
                "_operator_open_id": "ou_user",
                "_operator_user_id": "u2",
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 身份信息")
        content = response["card"]["elements"][0]["content"]
        self.assertIn("user_id: `u2`", content)
        self.assertIn("open_id: `ou_user`", content)

    def test_help_submit_resume_command_reuses_resume_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {
                "action": "help_submit_command",
                "command": "/resume",
                "field_name": "resume_target",
                "title": "Codex 恢复线程",
                "_form_value": {"resume_target": "demo"},
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)
        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")

    def test_help_submit_init_command_uses_operator_identity_context(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "open_id": "ou_bot_new",
            "source": "auto-discovered",
            "configured_open_id": "",
            "discovered_open_id": "ou_bot_new",
            "trigger_open_ids": "",
        }

        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"), patch(
            "bot.codex_settings_domain.load_system_config_raw",
            return_value={
                "app_id": "cli_test_app",
                "app_secret": "secret",
                "admin_open_ids": ["ou_admin"],
            },
        ), patch("bot.codex_settings_domain.save_system_config") as save_config:
            response = self._unpack_card_response(handler.handle_card_action(
                "ou_user2",
                "chat-p2p",
                "msg-help-init",
                {
                    "action": "help_submit_command",
                    "command": "/init",
                    "field_name": "init_token",
                    "title": "Codex 初始化结果",
                    "_form_value": {"init_token": "secret-1"},
                    "_operator_open_id": "ou_user2",
                    "_operator_user_id": "u2",
                },
            ))

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertEqual(saved["bot_open_id"], "ou_bot_new")
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 初始化结果")
        content = response["card"]["elements"][0]["content"]
        self.assertIn("已加入 `Alice`", content)
        self.assertIn("`ou_bot_new`", content)

    def test_help_submit_cd_command_reuses_cd_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {
                "action": "help_submit_command",
                "command": "/cd",
                "field_name": "cd_path",
                "title": "Codex 目录切换结果",
                "_form_value": {"cd_path": "/tmp"},
            },
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 目录已切换")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")

    def test_help_submit_init_command_preserves_scope_guard(self) -> None:
        handler, _ = self._make_handler()
        handler._feishu_platform.bot.message_contexts["msg-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "msg-group",
            {
                "action": "help_submit_command",
                "command": "/init",
                "field_name": "init_token",
                "title": "Codex 初始化结果",
                "_form_value": {"init_token": "demo"},
            },
        ))

        self.assertEqual(response["toast"], f"请私聊机器人执行 `{_DISPLAY_INIT_COMMAND}`。")
        self.assertEqual(response["toast_type"], "warning")

    def test_new_command_reply_focuses_on_next_step(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/new")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 线程已新建")
        content = card["elements"][0]["content"]
        self.assertIn("线程：`", content)
        self.assertIn("目录：`", content)
        self.assertIn("直接发送普通文本开始第一轮对话。", content)

    def test_cd_command_success_uses_card_and_clears_binding(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        handler.handle_message("ou_user", "c1", "/cd /tmp")

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录已切换")
        self.assertIn("当前线程绑定已清空。", card["elements"][0]["content"])

    def test_cd_command_expands_home_directory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        home = pathlib.Path(tempdir.name) / "home"
        project = home / "project"
        project.mkdir(parents=True)
        handler, bot = self._make_handler({"default_working_dir": str(home)})
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd=str(home),
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        with patch.dict(os.environ, {"HOME": str(home)}):
            handler.handle_message("ou_user", "c1", "/cd ~/project")

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["working_dir"], str(project))
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")
        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录已切换")
        self.assertIn("目录：`~/project`", card["elements"][0]["content"])

    def test_cd_command_invalidates_pending_attachments_in_current_scope(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace-1"
        workspace_2 = pathlib.Path(tempdir.name) / "workspace-2"
        workspace.mkdir()
        workspace_2.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"spec-content",
            file_name="spec.pdf",
            content_type="application/pdf",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "spec.pdf")
        staged_file = next((workspace / "_feishu_attachments").iterdir())

        handler.handle_message("ou_user", "c1", f"/cd {workspace_2}")

        self.assertEqual(handler._pending_attachment_store.list_all(), ())
        _, card = bot.cards[-1]
        self.assertIn("已使 1 个待消费附件失效。", card["elements"][0]["content"])

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "请处理附件")
        self.assertNotIn(str(staged_file), handler._adapter.start_turn_calls[-1]["text"])

    def test_bind_thread_to_new_thread_clears_previous_execution_anchor(self) -> None:
        handler, _ = self._make_handler()
        old_thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        new_thread = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-2",
            name="new",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", old_thread)
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(
                state,
                current_message_id="card-live",
                last_message_id="card-old",
            )
            state["current_turn_id"] = "turn-1"
            state["current_prompt_message_id"] = "prompt-1"
            state["execution_transcript"].set_reply_text("stale")

        _bind_authoritative_thread(handler, "ou_user", "c1", new_thread)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_thread_id"], "thread-2")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "")
        self.assertEqual(state["current_prompt_message_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        with patch.object(handler._terminal_execution, "refresh_ingress") as refresh:
            ok, message = handler._runtime_call(
                handler._feishu_surface.cancel_current_turn,
                "ou_user",
                "c1",
            )
        self.assertFalse(ok)
        self.assertEqual(message, "当前没有正在执行的 turn。")
        refresh.assert_not_called()

    def test_replacing_bound_thread_cancels_old_feishu_fifo(self) -> None:
        """Queued work for one root cannot cross an explicit binding switch."""

        handler, _ = self._make_handler()
        binding = ("ou_user", "c1")
        old_thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-1",
            name="old",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        new_thread = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-2",
            name="new",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, old_thread)
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=old_thread.thread_id,
            message_id="queued-old-root",
            text="must not move roots",
            input_items=({"type": "text", "text": "must not move roots"},),
        )

        _bind_authoritative_thread(handler, *binding, new_thread)
        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(_runtime_state(handler, *binding)["current_thread_id"], "thread-2")
        self.assertFalse(
            self._queue_snapshot(handler, binding).has_pending_or_draining
        )

    def test_clear_thread_binding_clears_previous_execution_anchor(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(
                state,
                current_message_id="card-live",
                last_message_id="card-old",
            )
            state["current_turn_id"] = "turn-1"
            state["current_prompt_message_id"] = "prompt-1"
            state["execution_transcript"].set_reply_text("stale")

        handler._runtime_call(
            handler._binding_runtime_coordinator.clear_thread_binding,
            "ou_user",
            "c1",
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "")
        self.assertEqual(state["current_prompt_message_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        with patch.object(handler._terminal_execution, "refresh_ingress") as refresh:
            ok, message = handler._runtime_call(
                handler._feishu_surface.cancel_current_turn,
                "ou_user",
                "c1",
            )
        self.assertFalse(ok)
        self.assertEqual(message, "当前没有正在执行的 turn。")
        refresh.assert_not_called()

    def test_clearing_bound_thread_cancels_old_feishu_fifo(self) -> None:
        """A `/cd`-style clear cannot replay its old queue in a new session."""

        handler, _ = self._make_handler()
        binding = ("ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, thread)
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=thread.thread_id,
            message_id="queued-old-root",
            text="must not create a new thread",
            input_items=(
                {"type": "text", "text": "must not create a new thread"},
            ),
        )

        handler._runtime_call(handler._binding_runtime_coordinator.clear_thread_binding, *binding)
        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(_runtime_state(handler, *binding)["current_thread_id"], "")
        self.assertFalse(
            self._queue_snapshot(handler, binding).has_pending_or_draining
        )

    def test_cd_command_failure_uses_warning_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/cd /definitely-not-exists")

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 目录未切换")
        self.assertIn("目录不存在", card["elements"][0]["content"])

    def test_resume_success_merges_switch_summary_into_history_preview_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._adapter.resume_thread = lambda thread_id, **kwargs: ThreadSnapshot(
            summary=thread,
            turns=[
                {
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "hello"}]},
                        {"type": "agentMessage", "text": "world"},
                    ]
                }
            ],
            effective_model="gpt-5.5",
            effective_reasoning_effort="high",
            effective_approval_policy="never",
            effective_permissions_profile_id=":danger-full-access",
        )

        handler.handle_message("ou_user", "c1", "/resume demo")

        self.assertEqual(bot.cards[0][1]["header"]["title"]["content"], "Codex 正在恢复线程")
        handler._runtime_call(lambda: None)

        _, card = bot.cards[-1]
        self.assertEqual(card["header"]["title"]["content"], "线程 thread-1… 最近对话")
        content = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("已切换到线程", content)
        self.assertIn("目录：`/tmp/project`", content)
        self.assertIn("👤 **你**", content)
        self.assertIn("🤖 **Codex**", content)

    def test_resume_card_action_for_not_loaded_thread_resumes_directly(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
            service_name="codex-tui",
        )
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-1",
            {"action": "resume_thread", "thread_id": "thread-1", "thread_title": "demo"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 当前目录线程")
        self.assertIn("正在恢复线程", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

    def test_resume_card_action_failure_refreshes_threads_card(self) -> None:
        handler, bot = self._make_handler({"threads_initial_limit": 1})
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="one",
            preview="",
            created_at=0,
            updated_at=3,
            source="cli",
            status="idle",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "show_more_threads"},
        )

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-session",
            {"action": "resume_thread", "thread_id": "thread-missing", "thread_title": "missing"},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("恢复线程失败", response["toast"])
        self.assertNotIn("card", response)
        self.assertEqual(bot.replies, [])
        self.assertEqual(bot.patches, [])

    def test_attach_binding_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action("ou_user", "c1", "msg-attach", {"action": "attach_runtime"})
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前会话推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_attach_thread_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-attach",
                {"action": "attach_runtime", "scope": "thread"},
            )
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前线程推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")

    def test_attach_service_card_action_returns_ack_card_then_sends_result_card(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        state = _runtime_state(handler, "ou_user", "c1")
        state["feishu_runtime_state"] = "detached"

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-attach",
                {"action": "attach_runtime", "scope": "service"},
            )
        )

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 正在恢复飞书推送")
        self.assertIn("当前实例推送", response["card"]["elements"][0]["content"])
        handler._runtime_call(lambda: None)

        _, final_card = bot.cards[-1]
        self.assertEqual(final_card["header"]["title"]["content"], "Codex 已附着飞书推送")

    def test_show_rename_form_registers_pending_message(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        handler._adapter.list_threads_all = lambda **kwargs: [thread]

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"action": "show_rename_form", "thread_id": "thread-1"},
        ))

        pending = self._pending_rename_form_snapshot(handler, "msg-rename")
        assert pending is not None
        self.assertEqual(pending["thread_id"], "thread-1")
        self.assertEqual(response["card"]["header"]["title"]["content"], "重命名线程")

    def test_form_value_only_callback_submits_rename(self) -> None:
        handler, _ = self._make_handler()
        renamed = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old-title",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="vscode",
            status="notLoaded",
        )
        self._register_pending_rename_form(handler, "msg-rename", thread_id="thread-1")
        handler._adapter.list_threads_all = lambda **kwargs: [thread]
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        def fake_rename_thread(thread_id: str, name: str) -> None:
            renamed["thread_id"] = thread_id
            renamed["name"] = name

        handler._adapter.rename_thread = fake_rename_thread

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        ))

        self.assertEqual(renamed, {"thread_id": "thread-1", "name": "new-title"})
        self.assertIsNone(self._pending_rename_form_snapshot(handler, "msg-rename"))
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已重命名。")

    def test_form_value_only_help_cd_callback_reuses_cd_handler(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"_form_value": {"cd_path": "/tmp"}},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 目录已切换")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["working_dir"], "/tmp")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")

    def test_form_value_only_help_rename_current_callback_reuses_rename_handler(self) -> None:
        handler, _ = self._make_handler()
        renamed = {}
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="old-title",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        def fake_rename_thread(thread_id: str, name: str) -> None:
            renamed["thread_id"] = thread_id
            renamed["name"] = name

        handler._adapter.rename_thread = fake_rename_thread

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"_form_value": {"help_rename_current_title": "new-title"}},
        ))

        self.assertEqual(renamed, {"thread_id": "thread-1", "name": "new-title"})
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 重命名结果")

    def test_form_value_only_callback_without_pending_rename_returns_warning(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-rename",
            {"_form_value": {"rename_title": "new-title"}},
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "重命名表单已失效，请重新打开。")
