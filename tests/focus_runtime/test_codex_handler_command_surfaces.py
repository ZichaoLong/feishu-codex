import json
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
    _store_canonical_pending_interaction,
)
from tests.focus_runtime.codex_handler_fakes import _flush_execution, _runtime_state, _store_pending_interaction as _store_pending
from bot.cards import build_execution_card
from bot.adapters.base import (
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcTransportError,
)
from bot.execution_transcript import ExecutionReplySegment

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
    _DISPLAY_DEBUG_CONTACT_COMMAND,
    _DISPLAY_INIT_COMMAND,
)


class CodexHandlerCommandSurfaceTests(CodexHandlerHarness):
    def test_execution_card_is_patchable_shared_card(self) -> None:
        card = build_execution_card("", [], running=True)

        self.assertTrue(card["config"]["update_multi"])

    def test_execution_card_renders_native_reply_divider(self) -> None:
        card = build_execution_card(
            "",
            [
                ExecutionReplySegment("assistant", "第一段"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "第二段"),
            ],
            running=False,
        )

        reply_panel = next(
            element
            for element in card["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "回复"
        )
        self.assertEqual(
            [element["tag"] for element in reply_panel["elements"]],
            ["markdown", "hr", "markdown"],
        )

    def test_execution_card_process_panel_defaults_to_collapsed(self) -> None:
        card = build_execution_card(
            "process log",
            [ExecutionReplySegment("assistant", "reply")],
            running=True,
        )

        process_panel = next(
            element
            for element in card["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "执行过程"
        )
        self.assertFalse(process_panel["expanded"])

    def test_agent_message_completed_without_delta_preserves_divider_after_work(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-created", "turnId": "turn-1", "delta": "第一段"},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/started",
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "command": "ls", "cwd": "/tmp/project"},
            },
        )
        self._dispatch_adapter_notification(
            handler,
            "item/completed",
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "status": "completed", "exitCode": 0},
            }
        )
        self._dispatch_adapter_notification(
            handler,
            "item/completed",
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "第二段"},
            }
        )
        _flush_execution(handler, "ou_user", "c1", immediate=True)

        patched = json.loads(bot.patches[-1][1])
        reply_panel = next(
            element
            for element in patched["body"]["elements"]
            if isinstance(element, dict)
            and element.get("tag") == "collapsible_panel"
            and element.get("header", {}).get("title", {}).get("content") == "回复"
        )
        self.assertEqual(
            [element["tag"] for element in reply_panel["elements"]],
            ["markdown", "hr", "markdown"],
        )

    def test_whoami_command_in_p2p_returns_identity_and_admin_config_hint(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_user_id": "u2",
            "sender_open_id": "ou_user",
            "sender_type": "user",
        }

        handler.handle_message("ou_user", "chat-p2p", "/whoami", message_id="m-p2p")

        reply = bot.replies[-1][1]
        self.assertIn("name: `User`", reply)
        self.assertIn("user_id: `u2`", reply)
        self.assertIn("open_id: `ou_user`", reply)
        self.assertIn("admin_open_ids", reply)

    def test_whoami_command_in_group_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user2", "chat-group", "/whoami", message_id="m-group")

        self.assertIn("请私聊机器人执行", bot.replies[-1][1])

    def test_bot_status_command_returns_bot_identity(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "ou_bot",
            "discovered_open_id": "ou_bot",
            "trigger_open_ids": ["ou_alias_1", "ou_alias_2"],
        }

        handler.handle_message("ou_user", "chat-p2p", "/bot-status")

        reply = bot.replies[-1][1]
        self.assertIn("机器人身份信息", reply)
        self.assertIn("app_id: `cli_test_app`", reply)
        self.assertIn("configured bot_open_id: `ou_bot`", reply)
        self.assertIn("discovered open_id: `ou_bot`", reply)
        self.assertIn("runtime mention matching: `enabled`", reply)
        self.assertIn("trigger_open_ids: `ou_alias_1, ou_alias_2`", reply)
        self.assertIn("system.yaml.bot_open_id", reply)

    def test_bot_status_reports_missing_bot_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "configured_open_id": "",
            "discovered_open_id": "",
            "trigger_open_ids": [],
        }

        handler.handle_message("ou_user", "chat-p2p", "/bot-status")

        reply = bot.replies[-1][1]
        self.assertIn("configured bot_open_id: `（空）`", reply)
        self.assertIn("discovered open_id: `（空）`", reply)
        self.assertIn("runtime mention matching: `disabled`", reply)
        self.assertIn("application:application:self_manage", reply)

    def test_debug_contact_command_in_p2p_returns_resolution_diagnostics(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {"chat_type": "p2p", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user", "chat-p2p", "/debug-contact ou_user", message_id="m-p2p")

        reply = bot.replies[-1][1]
        self.assertIn("联系人解析诊断", reply)
        self.assertIn("open_id: `ou_user`", reply)
        self.assertIn("cache: `hit`", reply)
        self.assertIn("resolved_name: `User`", reply)

    def test_debug_contact_command_in_group_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_admin", "chat-group", "/debug-contact ou_user", message_id="m-group")

        self.assertIn(f"请私聊机器人执行 `{_DISPLAY_DEBUG_CONTACT_COMMAND}`", bot.replies[-1][1])

    def test_init_command_requires_p2p(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/init abc", message_id="m-group")

        self.assertIn(f"请私聊机器人执行 `{_DISPLAY_INIT_COMMAND}`", bot.replies[-1][1])

    def test_init_command_with_token_updates_admin_and_bot_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user2",
            "sender_type": "user",
        }
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
            handler.handle_message("ou_user2", "chat-p2p", "/init secret-1", message_id="m-p2p")

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertEqual(saved["bot_open_id"], "ou_bot_new")
        self.assertIn("ou_user2", bot.admin_open_ids)
        self.assertEqual(bot.runtime_bot_open_id, "ou_bot_new")
        reply = bot.replies[-1][1]
        self.assertIn("初始化结果", reply)
        self.assertIn("已加入 `Alice`", reply)
        self.assertIn("`ou_bot_new`", reply)

    def test_init_command_does_not_write_runtime_only_admins_back_to_config(self) -> None:
        handler, bot = self._make_handler()
        bot.admin_open_ids = {"ou_admin", "ou_stale_runtime"}
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user2",
            "sender_type": "user",
        }
        bot.bot_identity = {
            "app_id": "cli_test_app",
            "open_id": "",
            "source": "auto-discovered",
            "configured_open_id": "",
            "discovered_open_id": "",
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
            handler.handle_message("ou_user2", "chat-p2p", "/init secret-1", message_id="m-p2p")

        saved = save_config.call_args.args[0]
        self.assertEqual(saved["admin_open_ids"], ["ou_admin", "ou_user2"])
        self.assertNotIn("ou_stale_runtime", saved["admin_open_ids"])

    def test_init_command_rejects_invalid_token(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-p2p"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user",
            "sender_type": "user",
        }
        with patch("bot.codex_settings_domain.ensure_init_token", return_value="secret-1"):
            handler.handle_message("ou_user", "chat-p2p", "/init bad-token", message_id="m-p2p")

        self.assertIn("初始化口令错误", bot.replies[-1][1])

    def test_group_mode_command_without_arg_shows_group_mode_card(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode", message_id="m-group")

        card = bot.cards[-1][1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊工作态")
        action_elements = self._action_elements(card)
        actions = action_elements[0]["actions"]
        self.assertEqual([item["text"]["content"] for item in actions], ["assistant", "all", "mention-only"])
        self.assertEqual(actions[0]["type"], "primary")
        self.assertEqual(action_elements[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_group_mode_command_can_use_cached_chat_type_without_message_context(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-group"] = {"sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode", message_id="m-group")

        self.assertEqual(bot.cards[-1][1]["header"]["title"]["content"], "Codex 群聊工作态")

    def test_group_mode_command_updates_group_mode_for_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group-mode assistant", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("已切换群聊工作态：`assistant`", bot.replies[-1][1])

    def test_group_mode_command_uses_sender_id_fallback_when_message_context_lacks_sender_open_id(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group"}

        handler.handle_message("ou_admin", "chat-group", "/group-mode all", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "all")
        self.assertIn("已切换群聊工作态：`all`", bot.replies[-1][1])

    def test_group_mode_command_rejects_all_when_thread_is_shared(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.chat_types["chat-other"] = "group"
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
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
        _bind_authoritative_thread(handler, "ou_user", "chat-group", thread)
        _bind_authoritative_thread(handler, "ou_user2", "chat-other", thread)

        handler.handle_message("ou_user", "chat-group", "/group-mode all", message_id="m-group")

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("不能与其他飞书会话共享", bot.replies[-1][1])
        self.assertIn("/new", bot.replies[-1][1])
        self.assertIn("/cd <目录>", bot.replies[-1][1])

    def test_group_mode_command_rejects_non_admin(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user2", "chat-group", "/group-mode all", message_id="m-group")

        self.assertIn("群里的 `/` 命令仅管理员可用", bot.replies[-1][1])
        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")

    def test_group_command_without_arg_shows_group_activation_card(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group", message_id="m-group")

        card = bot.cards[-1][1]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊授权")
        markdown = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("未激活", markdown)
        self.assertIn("/group activate", markdown)

    def test_group_command_activates_group_chat(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/group activate", message_id="m-group")

        snapshot = bot.get_group_activation_snapshot("chat-group")
        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")
        self.assertIn("已激活当前群聊", bot.replies[-1][1])

    def test_group_command_uses_sender_id_fallback_for_activation_actor(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group"}

        handler.handle_message("ou_admin", "chat-group", "/group activate", message_id="m-group")

        snapshot = bot.get_group_activation_snapshot("chat-group")
        self.assertTrue(snapshot["activated"])
        self.assertEqual(snapshot["activated_by"], "ou_admin")

    def test_group_mode_card_action_updates_group_mode(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_mode", "mode": "assistant", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(handler._feishu_platform.bot.get_group_mode("chat-group"), "assistant")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("assistant", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊工作态")
        self.assertEqual(self._action_elements(response["card"])[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_group_mode_card_action_rejects_all_when_thread_is_shared(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.chat_types["chat-other"] = "group"
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
        _bind_authoritative_thread(handler, "ou_user", "chat-group", thread)
        _bind_authoritative_thread(handler, "ou_user2", "chat-other", thread)

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_mode", "mode": "all", "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(bot.get_group_mode("chat-group"), "assistant")
        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "切换失败；已发送处理建议。")
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊工作态")
        self.assertIn("切换到 `all` 失败", bot.reply_parents[-1][1])
        self.assertIn("/new", bot.reply_parents[-1][1])
        self.assertIn("/cd <目录>", bot.reply_parents[-1][1])
        self.assertEqual(bot.reply_parents[-1][2], "m1")

    def test_group_activation_card_action_updates_group_status(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "chat-group",
            "m1",
            {"action": "set_group_activation", "activated": True, "_operator_open_id": "ou_admin"},
        ))

        self.assertTrue(handler._feishu_platform.bot.get_group_activation_snapshot("chat-group")["activated"])
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("已激活当前群聊", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 群聊授权")
        markdown = "\n".join(
            element.get("content", "")
            for element in response["card"]["elements"]
            if isinstance(element, dict) and element.get("tag") == "markdown"
        )
        self.assertIn("/group activate", markdown)
        self.assertIn("/group deactivate", markdown)

    def test_group_deactivation_fail_closes_member_pending_interactions_but_preserves_admin_pending(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.activate_group_chat("chat-group", activated_by="ou_admin")
        member_request_key = _store_canonical_pending_interaction(handler, {
            "rpc_request_id": "rpc-member",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-member"},
            "chat_id": "chat-group",
            "actor_open_id": "ou_member",
            "message_id": "card-member",
        })
        admin_request_key = _store_canonical_pending_interaction(handler, {
            "rpc_request_id": "rpc-admin",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-admin"},
            "chat_id": "chat-group",
            "actor_open_id": "ou_admin",
            "message_id": "card-admin",
        })

        handler._runtime_call(handler._feishu_surface.deactivate_group_chat, "chat-group")

        self.assertFalse(bot.get_group_activation_snapshot("chat-group")["activated"])
        self.assertEqual(
            handler._adapter.respond_calls,
            [{"request_id": "rpc-member", "connection_generation": 1, "result": {"decision": "cancel"}, "error": None}],
        )
        member_pending = handler._interaction_requests.pending_request_snapshot(
            member_request_key
        )
        assert member_pending is not None
        self.assertEqual(member_pending["status"], "submitted")
        self.assertTrue(member_pending["group_authority_revoked"])
        self.assertTrue(
            handler._interaction_requests.has_pending_request(admin_request_key)
        )
        self.assertEqual(bot.patches[-1][0], "card-member")
        self.assertIn("群聊已停用", bot.patches[-1][1])

    def test_inactive_group_active_turn_shared_approval_is_cancelled_and_revoked(
        self,
    ) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.activate_group_chat("chat-group", activated_by="ou_admin")
        _bind_authoritative_thread(
            handler,
            "ou_member",
            "chat-group",
            ThreadSummary(
                thread_id="thread-group",
                cwd="/tmp/project",
                name="group-thread",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="active",
            ),
        )
        with handler._lock:
            binding = handler._binding_runtime.existing_chat_binding_key_locked(
                "ou_member",
                "chat-group",
            )
        assert binding is not None
        self._activate_main_turn_lease(
            handler,
            "thread-group",
            handler._binding_runtime.feishu_interaction_holder(binding),
            "turn-autonomous",
        )
        handler._runtime_call(handler._feishu_surface.deactivate_group_chat, "chat-group")

        self._adapter_request(
            handler,
            "inactive-group-approval",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-group",
                "turnId": "turn-autonomous",
                "command": "pwd",
            },
        )

        request_key = handler._server_request_registry.pending_items()[0][0]
        identity = handler._server_request_registry.active_identity(request_key)
        assert identity is not None
        self.assertEqual(
            handler._adapter.respond_calls[-1]["result"],
            {"decision": "cancel"},
        )
        self.assertEqual(
            handler._server_request_registry.response_phase(identity),
            "submitted",
        )
        self.assertTrue(
            handler._server_request_registry.response_authority_is_revoked(identity)
        )
        self.assertEqual(bot.sent_messages, [])

    def test_deactivated_group_never_lets_admin_take_over_member_interaction(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.deactivate_group_chat("chat-group")
        _store_pending(handler, "member-request", {
            "rpc_request_id": "rpc-member",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-member"},
            "chat_id": "chat-group",
            "actor_open_id": "ou_member",
            "message_id": "card-member",
        })
        action = {
            "action": "interaction_approval",
            "request_id": "member-request",
            "response_action": "approve_once",
        }

        member_response = self._unpack_card_response(handler.handle_card_action(
            "ou_member",
            "chat-group",
            "card-member",
            {**action, "_operator_open_id": "ou_member"},
        ))

        self.assertEqual(member_response["toast_type"], "warning")
        self.assertIn("仅管理员或当前提问者", member_response["toast"])
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertTrue(handler._interaction_requests.has_pending_request("member-request"))

        admin_response = self._unpack_card_response(handler.handle_card_action(
            "ou_admin",
            "chat-group",
            "card-member",
            {**action, "_operator_open_id": "ou_admin"},
        ))

        self.assertEqual(admin_response["toast_type"], "warning")
        self.assertIn("仅管理员或当前提问者", admin_response["toast"])
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertTrue(handler._interaction_requests.has_pending_request("member-request"))

    def test_group_deactivation_unknown_member_fail_close_never_becomes_admin_approval(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.activate_group_chat("chat-group", activated_by="ou_admin")
        member_request_key = _store_canonical_pending_interaction(handler, {
            "rpc_request_id": "rpc-member",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-member"},
            "chat_id": "chat-group",
            "actor_open_id": "ou_member",
            "message_id": "card-member",
        })

        def unknown_cancel(request_id, *, result=None, error=None, connection_generation):
            del request_id, result, error, connection_generation
            raise CodexRpcTransportError("serverRequest/response", {"message": "connection lost"})

        handler._adapter.respond = unknown_cancel
        handler._runtime_call(handler._feishu_surface.deactivate_group_chat, "chat-group")
        pending = handler._interaction_requests.pending_request_snapshot(
            member_request_key
        )
        self.assertTrue(pending and pending.get("group_authority_revoked"))
        self.assertEqual(pending and pending.get("status"), "submitted_unknown")
        identity = handler._server_request_registry.active_identity(member_request_key)
        assert identity is not None
        self.assertEqual(
            handler._server_request_registry.response_phase(identity),
            "unknown",
        )

        # Reactivating the group restores authority for *new* requests only;
        # this one still has an unknown cancellation outcome and remains a
        # fail-closed blocker, not an admin fallback card.
        bot.activate_group_chat("chat-group", activated_by="ou_admin")
        response = self._unpack_card_response(handler.handle_card_action(
            "ou_admin",
            "chat-group",
            "card-member",
            {
                "action": "interaction_approval",
                "request_id": member_request_key,
                "response_action": "approve_once",
                "_operator_open_id": "ou_admin",
            },
        ))

        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("仅管理员或当前提问者", response["toast"])
        self.assertTrue(
            handler._interaction_requests.has_pending_request(member_request_key)
        )

    def test_group_command_accepts_group_chat_after_api_type_lookup(self) -> None:
        handler, bot = self._make_handler()
        bot.fetched_chat_types["oc_group123"] = "group"
        bot.message_contexts["m-group"] = {"sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "oc_group123", "/group-mode", message_id="m-group")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 群聊工作态")

    def test_group_command_binds_shared_state_from_message_context_before_chat_cache(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        handler.handle_message("ou_user", "chat-group", "/status", message_id="m-status")

        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertNotIn(("ou_user", "chat-group"), self._binding_keys(handler))
        self.assertIs(_runtime_state(handler, "ou_user", "chat-group"), _runtime_state(handler, "ou_user2", "chat-group"))

    def test_group_settings_card_action_uses_shared_chat_binding_key(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "chat-group",
                "m-group",
                {"action": "set_model", "model": "gpt-5.5", "_operator_open_id": "ou_admin"},
            )
        )

        self.assertEqual(_runtime_state(handler, "ou_user", "chat-group", "m-group")["model"], "gpt-5.5")
        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertNotIn(("ou_user", "chat-group"), self._binding_keys(handler))
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("gpt-5.5", response["toast"])

    def test_resolve_session_reuses_existing_group_state(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}

        first = handler._binding_runtime.resolve_session("ou_user", "chat-group", "m-group")
        second = handler._binding_runtime.resolve_session("ou_user2", "chat-group")

        self.assertEqual(first.binding, ("__group__", "chat-group"))
        self.assertEqual(second.binding, ("__group__", "chat-group"))
        self.assertIs(first.handle, second.handle)

    def test_permissions_command_updates_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions danger-full-access")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertIn("Danger Full Access", bot.replies[-1][1])
        self.assertIn(":danger-full-access", bot.replies[-1][1])

    def test_permissions_command_without_arg_shows_permissions_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 权限基线")
        self.assertIn("它只决定执行边界", card["elements"][0]["content"])
        self.assertIn("审批策略请单独使用 `/approval`", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(len(action_elements), 2)
        self.assertEqual(action_elements[0]["layout"], "trisection")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "返回帮助")

    def test_model_command_without_arg_shows_model_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 模型 / Effort")
        self.assertIn("model override: `auto`", card["elements"][0]["content"])
        self.assertIn("effort override: `auto`", card["elements"][0]["content"])
        self.assertIn("validation: `validated`", card["elements"][0]["content"])
        self.assertNotIn("startup profile", card["elements"][0]["content"])
        action_elements = self._action_elements(card)
        self.assertEqual(action_elements[0]["actions"][0]["text"]["content"], "auto")
        self.assertEqual(action_elements[1]["actions"][0]["text"]["content"], "auto")
        self.assertEqual(action_elements[1]["actions"][0]["type"], "primary")

    def test_effort_command_without_arg_shows_combined_runtime_card(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/effort")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 模型 / Effort")
        self.assertIn("effort override: `auto`", card["elements"][0]["content"])

    def test_approval_command_without_arg_shows_approval_boundary(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/approval")

        self.assertEqual(len(bot.cards), 1)
        _, card = bot.cards[0]
        self.assertEqual(card["header"]["title"]["content"], "Codex 审批策略")
        self.assertIn("只决定什么时候停下来等你确认", card["elements"][0]["content"])
        self.assertIn("优先使用 `/permissions`", card["elements"][0]["content"])

    def test_help_execute_approval_action_adds_return_help_and_preserves_it_after_toggle(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "help_execute_command", "command": "/approval", "title": "Codex 审批策略"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 审批策略")
        action_elements = self._action_elements(response["card"])
        self.assertEqual(action_elements[-1]["actions"][0]["text"]["content"], "返回帮助")
        policy_action = action_elements[0]["actions"][0]["value"]
        self.assertEqual(policy_action["help_origin"], "overview")

        updated = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            policy_action,
        ))

        self.assertEqual(updated["card"]["header"]["title"]["content"], "Codex 审批策略")
        self.assertEqual(self._action_elements(updated["card"])[-1]["actions"][0]["text"]["content"], "返回帮助")

    def test_show_help_page_action_ignores_help_origin_redecoration(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "msg-help",
            {"action": "show_help_page", "page": "overview", "help_origin": "overview"},
        ))

        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 工作台")
        self.assertEqual(len(self._action_elements(response["card"])), 3)

    def test_model_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_model", "model": "gpt-5.4"},
        ))

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["model"], "gpt-5.4")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("gpt-5.4", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_model_form_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "submit_model_override", "_form_value": {"model_override": "glm-4.5"}},
        ))

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["model"], "glm-4.5")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("glm-4.5", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_model_form_value_only_callback_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"_form_value": {"model_override": "glm-4.5"}},
        ))

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["model"], "glm-4.5")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("glm-4.5", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_effort_form_value_only_callback_updates_state_and_preserves_case(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"_form_value": {"reasoning_effort_override": "Future-Max"}},
        ))

        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["reasoning_effort"],
            "Future-Max",
        )
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Future-Max", response["toast"])
        self.assertIn("validation: deferred", response["toast"])

    def test_effort_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_reasoning_effort", "reasoning_effort": "high"},
        ))

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["reasoning_effort"], "high")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("high", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 模型 / Effort")

    def test_stale_effort_card_action_revalidates_against_current_model(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["model"] = "gpt-5.4"
        state["reasoning_effort"] = "high"

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_reasoning_effort", "reasoning_effort": "ultra"},
        ))

        self.assertEqual(state["reasoning_effort"], "high")
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("未保存", response["toast"])
        self.assertIn("validation: `validated`", response["card"]["elements"][0]["content"])

    def test_permissions_card_action_updates_state(self) -> None:
        handler, _ = self._make_handler()

        response = self._unpack_card_response(handler.handle_card_action(
            "ou_user",
            "c1",
            "m1",
            {"action": "set_permissions_profile", "profile": "danger-full-access"},
        ))

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(response["toast_type"], "success")
        self.assertIn("Danger Full Access", response["toast"])
        self.assertEqual(response["card"]["header"]["title"]["content"], "Codex 权限基线")
        self.assertEqual(self._action_elements(response["card"])[1]["actions"][0]["text"]["content"], "返回帮助")
