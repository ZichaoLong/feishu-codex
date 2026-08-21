import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bot.adapters.base import RuntimeModelSummary, RuntimeReasoningEffortOption
from bot.codex_settings_domain import CodexSettingsDomain, SettingsDomainPorts
from bot.feishu_command_syntax import feishu_visible_command_syntax


_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}
_DISPLAY_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")


class _SettingsPortsStub:
    def __init__(self) -> None:
        self.message_contexts: dict[str, dict[str, Any]] = {}
        self.bot_identity: dict[str, Any] = {}
        self.added_admin_open_ids: list[str] = []
        self.configured_bot_open_ids: list[str] = []
        self.runtime = SimpleNamespace(
            running=False,
            approval_policy="on-request",
            permissions_profile_id=":workspace",
            model="",
            reasoning_effort="",
            current_thread_id="thread-1",
        )
        self.session_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.debug_sender_snapshots: dict[str, dict[str, Any]] = {}
        self.models: list[RuntimeModelSummary] = [
            RuntimeModelSummary(
                model="gpt-known",
                default_reasoning_effort="high",
                supported_reasoning_efforts=[
                    RuntimeReasoningEffortOption(reasoning_effort="low", description="Fast"),
                    RuntimeReasoningEffortOption(reasoning_effort="high", description="Deep"),
                    RuntimeReasoningEffortOption(reasoning_effort="ultra", description="Native Ultra"),
                ],
            ),
            RuntimeModelSummary(
                model="gpt-no-effort",
                supported_reasoning_efforts=[],
            ),
            RuntimeModelSummary(model="gpt-no-metadata"),
        ]

    def get_message_context(self, message_id: str) -> dict[str, Any]:
        return dict(self.message_contexts.get(message_id, {}))

    def get_sender_display_name(
        self,
        *,
        user_id: str,
        open_id: str,
        sender_type: str,
    ) -> str:
        del user_id, sender_type
        return f"name:{open_id}"

    def debug_sender_name_resolution(self, open_id: str) -> dict[str, Any]:
        return dict(
            self.debug_sender_snapshots.get(
                open_id,
                {
                    "open_id": open_id,
                    "cache_hit": False,
                    "cached_name": "",
                    "resolved_name": open_id[:8],
                    "used_fallback": True,
                    "fallback_reason": "api_non_success",
                    "api_code": 999,
                    "api_msg": "denied",
                    "exception": "",
                    "source": "fallback",
                },
            )
        )

    def get_bot_identity_snapshot(self) -> dict[str, Any]:
        return dict(self.bot_identity)

    def add_admin_open_id(self, open_id: str) -> None:
        self.added_admin_open_ids.append(open_id)

    def set_configured_bot_open_id(self, open_id: str) -> None:
        self.configured_bot_open_ids.append(open_id)

    def resolve_session(self, sender_id: str, chat_id: str, message_id: str):
        self.session_calls.append((sender_id, chat_id, message_id))
        return SimpleNamespace(**vars(self.runtime))

    def list_models(self) -> list[RuntimeModelSummary]:
        return list(self.models)

    def update_runtime_settings(self, sender_id: str, chat_id: str, **kwargs: Any) -> None:
        self.update_calls.append((sender_id, chat_id, kwargs))
        for key in ("approval_policy", "permissions_profile_id", "model", "reasoning_effort"):
            if key in kwargs:
                setattr(self.runtime, key, kwargs[key])


def _make_domain(stub: _SettingsPortsStub) -> CodexSettingsDomain:
    return CodexSettingsDomain(
        ports=SettingsDomainPorts(
            get_message_context=stub.get_message_context,
            get_sender_display_name=stub.get_sender_display_name,
            debug_sender_name_resolution=stub.debug_sender_name_resolution,
            get_bot_identity_snapshot=stub.get_bot_identity_snapshot,
            add_admin_open_id=stub.add_admin_open_id,
            set_configured_bot_open_id=stub.set_configured_bot_open_id,
            resolve_session=stub.resolve_session,
            list_models=stub.list_models,
            update_runtime_settings=stub.update_runtime_settings,
        ),
        approval_policies=_APPROVAL_POLICIES,
    )


class CodexSettingsDomainTests(unittest.TestCase):
    def test_debug_contact_command_reports_live_diagnostics(self) -> None:
        stub = _SettingsPortsStub()
        stub.debug_sender_snapshots["ou_user"] = {
            "open_id": "ou_user",
            "cache_hit": True,
            "cached_name": "User",
            "resolved_name": "User",
            "used_fallback": False,
            "fallback_reason": "",
            "api_code": "",
            "api_msg": "",
            "exception": "",
            "source": "contact_api",
        }
        domain = _make_domain(stub)

        result = domain.handle_debug_contact_command("ou_user", "chat-a", "ou_user")

        self.assertIn("联系人解析诊断", result.text)
        self.assertIn("cache: `hit`", result.text)
        self.assertIn("resolved_name: `User`", result.text)
        self.assertIn("used_fallback: `no`", result.text)

    def test_debug_contact_command_requires_open_id_argument(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_debug_contact_command("ou_user", "chat-a", "")

        self.assertIn(_DISPLAY_DEBUG_CONTACT_COMMAND, result.text)

    def test_init_command_saves_admin_and_bot_identity(self) -> None:
        stub = _SettingsPortsStub()
        stub.message_contexts["msg-1"] = {
            "sender_open_id": "ou_user",
            "sender_user_id": "u-1",
            "sender_type": "user",
        }
        stub.bot_identity = {
            "discovered_open_id": "ou_bot",
        }
        domain = _make_domain(stub)
        saved_configs: list[dict[str, Any]] = []

        with patch("bot.codex_settings_domain.ensure_init_token", return_value="token-1"):
            with patch(
                "bot.codex_settings_domain.load_system_config_raw",
                return_value={
                    "app_id": "app-id",
                    "app_secret": "secret",
                    "admin_open_ids": [],
                },
            ):
                with patch("bot.codex_settings_domain.save_system_config", side_effect=saved_configs.append):
                    result = domain.handle_init_command("ou_user", "chat-a", "token-1", message_id="msg-1")

        self.assertIn("初始化结果", result.text)
        self.assertEqual(stub.added_admin_open_ids, ["ou_user"])
        self.assertEqual(stub.configured_bot_open_ids, ["ou_bot"])
        self.assertEqual(saved_configs[-1]["admin_open_ids"], ["ou_user"])
        self.assertEqual(saved_configs[-1]["bot_open_id"], "ou_bot")

    def test_init_command_rejects_invalid_existing_system_config_before_updates(self) -> None:
        stub = _SettingsPortsStub()
        stub.message_contexts["msg-1"] = {
            "sender_open_id": "ou_user",
            "sender_user_id": "u-1",
            "sender_type": "user",
        }
        stub.bot_identity = {"discovered_open_id": "ou_bot"}
        domain = _make_domain(stub)

        with patch("bot.codex_settings_domain.ensure_init_token", return_value="token-1"):
            with patch(
                "bot.codex_settings_domain.load_system_config_raw",
                return_value={
                    "app_id": "app-id",
                    "app_secret": "secret",
                    "admin_open_ids": "ou-admin",
                },
            ):
                with patch("bot.codex_settings_domain.save_system_config") as save_config:
                    result = domain.handle_init_command(
                        "ou_user",
                        "chat-a",
                        "token-1",
                        message_id="msg-1",
                    )

        self.assertIn("system.yaml 配置无效", result.text)
        self.assertIn("admin_open_ids", result.text)
        save_config.assert_not_called()
        self.assertEqual(stub.added_admin_open_ids, [])
        self.assertEqual(stub.configured_bot_open_ids, [])

    def test_init_command_rejects_unknown_existing_system_config_key(self) -> None:
        stub = _SettingsPortsStub()
        stub.message_contexts["msg-1"] = {
            "sender_open_id": "ou_user",
            "sender_user_id": "u-1",
            "sender_type": "user",
        }
        stub.bot_identity = {"discovered_open_id": "ou_bot"}
        domain = _make_domain(stub)

        with patch("bot.codex_settings_domain.ensure_init_token", return_value="token-1"):
            with patch(
                "bot.codex_settings_domain.load_system_config_raw",
                return_value={
                    "app_id": "app-id",
                    "app_secret": "secret",
                    "admin_open_ids": [],
                    "admin_open_id": "ou-stale",
                },
            ):
                with patch("bot.codex_settings_domain.save_system_config") as save_config:
                    result = domain.handle_init_command(
                        "ou_user",
                        "chat-a",
                        "token-1",
                        message_id="msg-1",
                    )

        self.assertIn("system.yaml 配置无效", result.text)
        self.assertIn("admin_open_id", result.text)
        save_config.assert_not_called()
        self.assertEqual(stub.added_admin_open_ids, [])
        self.assertEqual(stub.configured_bot_open_ids, [])

    def test_model_command_without_arg_returns_summary_card(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        stub.runtime.reasoning_effort = "high"
        domain = _make_domain(stub)

        result = domain.handle_model_command("ou_user", "chat-a", "", message_id="msg-1")

        self.assertIsNotNone(result.card)
        content = result.card["elements"][0]["content"]
        self.assertIn("model override: `gpt-known`", content)
        self.assertIn("effort override: `high`", content)
        self.assertIn("validation: `validated`", content)
        self.assertIn("共享 Codex thread", content)
        self.assertIn("可选 override", content)
        self.assertIn("`auto` 不发送对应字段", content)
        self.assertNotIn("安全基线", content)
        self.assertNotIn("不影响已打开的", content)

    def test_model_command_updates_runtime_settings(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_model_command("ou_user", "chat-a", "gpt-5.5", message_id="msg-1")

        self.assertIn("已切换当前会话的 model override：`gpt-5.5`", result.text)
        self.assertIn("可选 override", result.text)
        self.assertIn("`auto` 不发送对应字段", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "model": "gpt-5.5"})],
        )

    def test_model_command_same_value_still_records_explicit_intent(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = ""
        domain = _make_domain(stub)

        result = domain.handle_model_command("ou_user", "chat-a", "auto", message_id="msg-1")

        self.assertIn("当前会话的 model override 已是：`auto`", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "model": ""})],
        )

    def test_effort_command_same_value_still_records_explicit_intent(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.reasoning_effort = ""
        domain = _make_domain(stub)

        result = domain.handle_effort_command("ou_user", "chat-a", "auto", message_id="msg-1")

        self.assertIn("当前会话的 effort override 已是：`auto`", result.text)
        self.assertIn("可选 override", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "reasoning_effort": ""})],
        )

    def test_effort_command_preserves_custom_value_case_when_validation_is_deferred(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_effort_command("ou_user", "chat-a", "Future-Max", message_id="msg-1")

        self.assertIn("validation: `deferred`", result.text)
        self.assertEqual(stub.runtime.reasoning_effort, "Future-Max")
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "reasoning_effort": "Future-Max"})],
        )

    def test_effort_command_normalizes_canonical_value_and_preserves_ultra_path(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        domain = _make_domain(stub)

        result = domain.handle_effort_command("ou_user", "chat-a", "ULTRA", message_id="msg-1")

        self.assertEqual(stub.runtime.reasoning_effort, "ultra")
        self.assertIn("validation: `validated`", result.text)
        self.assertIn("原样发送给 Codex", result.text)

    def test_effort_command_rejects_known_unsupported_combination(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        domain = _make_domain(stub)

        result = domain.handle_effort_command("ou_user", "chat-a", "medium", message_id="msg-1")

        self.assertIn("未保存 effort override", result.text)
        self.assertIn("未声明支持 effort `medium`", result.text)
        self.assertEqual(stub.update_calls, [])

    def test_effort_command_defers_when_explicit_model_lacks_metadata(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-no-metadata"
        domain = _make_domain(stub)

        result = domain.handle_effort_command("ou_user", "chat-a", "Future-Max", message_id="msg-1")

        self.assertIn("validation: `deferred`", result.text)
        self.assertEqual(stub.runtime.reasoning_effort, "Future-Max")

    def test_model_command_rejects_model_change_that_conflicts_with_current_effort(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.reasoning_effort = "ultra"
        domain = _make_domain(stub)

        result = domain.handle_model_command("ou_user", "chat-a", "gpt-no-effort", message_id="msg-1")

        self.assertIn("未保存 model override", result.text)
        self.assertEqual(stub.update_calls, [])

    def test_auto_model_card_shows_only_auto_effort_button_and_custom_input(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        card = domain.handle_effort_command("ou_user", "chat-a", "").card

        effort_buttons = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
            if action.get("value", {}).get("action") == "set_reasoning_effort"
        ]
        self.assertEqual([button["text"]["content"] for button in effort_buttons], ["auto"])
        self.assertEqual(effort_buttons[0]["type"], "primary")
        self.assertNotIn("✓", effort_buttons[0]["text"]["content"])
        form_names = [
            element.get("name")
            for element in card["elements"]
            if element.get("tag") == "form"
        ]
        self.assertEqual(
            form_names,
            ["model_override_form", "reasoning_effort_override_form"],
        )

    def test_known_model_card_uses_metadata_order_and_highlights_without_checkmark(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        stub.runtime.reasoning_effort = "high"
        domain = _make_domain(stub)

        card = domain.handle_model_command("ou_user", "chat-a", "").card

        effort_buttons = [
            action
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
            if action.get("value", {}).get("action") == "set_reasoning_effort"
        ]
        self.assertEqual(
            [button["text"]["content"] for button in effort_buttons],
            ["auto", "low", "high", "ultra"],
        )
        self.assertEqual(
            [button["type"] for button in effort_buttons],
            ["default", "default", "primary", "default"],
        )
        self.assertTrue(all("✓" not in button["text"]["content"] for button in effort_buttons))
        self.assertIn(
            "原生 Ultra 路径",
            "\n".join(
                element.get("content", "")
                for element in card["elements"]
                if element.get("tag") == "markdown"
            ),
        )
        self.assertEqual(
            [
                element.get("name")
                for element in card["elements"]
                if element.get("tag") == "form"
            ],
            ["model_override_form"],
        )

    def test_rejected_persisted_combination_is_rendered_as_conflict(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        stub.runtime.reasoning_effort = "Future-Max"
        domain = _make_domain(stub)

        card = domain.handle_model_command("ou_user", "chat-a", "").card
        markdown = "\n".join(
            element.get("content", "")
            for element in card["elements"]
            if element.get("tag") == "markdown"
        )

        self.assertIn("validation: `rejected`", markdown)
        self.assertIn("当前组合冲突", markdown)
        self.assertNotIn("Future-Max", [
            action["text"]["content"]
            for element in card["elements"]
            if element.get("tag") == "action"
            for action in element.get("actions", [])
        ])

    def test_stale_effort_action_revalidates_current_model_before_persisting(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.model = "gpt-known"
        stub.runtime.reasoning_effort = "high"
        domain = _make_domain(stub)

        response = domain.handle_set_reasoning_effort(
            "ou_user",
            "chat-a",
            "msg-1",
            {"reasoning_effort": "medium"},
        )

        self.assertEqual(response.toast.type, "warning")
        self.assertIn("未保存", response.toast.content)
        self.assertEqual(stub.runtime.reasoning_effort, "high")
        self.assertEqual(stub.update_calls, [])

    def test_custom_effort_form_preserves_case_and_resolves_value_only_callback(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)
        action_value = {
            "_form_value": {
                "reasoning_effort_override": "Future-Max",
            }
        }

        self.assertEqual(
            domain.resolve_runtime_settings_form_submit_payload(action_value),
            {"action": "submit_reasoning_effort_override"},
        )
        response = domain.handle_submit_reasoning_effort_override(
            "ou_user",
            "chat-a",
            "msg-1",
            action_value,
        )

        self.assertEqual(response.toast.type, "success")
        self.assertIn("validation: deferred", response.toast.content)
        self.assertEqual(stub.runtime.reasoning_effort, "Future-Max")

    def test_permissions_command_updates_runtime_settings(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_permissions_command("ou_user", "chat-a", "danger-full-access", message_id="msg-1")

        self.assertIn("已切换权限基线：`Danger Full Access`", result.text)
        self.assertIn("共享 Codex thread", result.text)
        self.assertIn("安全基线", result.text)
        self.assertIn("发起每个 turn 时都会显式应用", result.text)
        self.assertIn("下一次 Feishu turn 会重新应用", result.text)
        self.assertNotIn("可选 override", result.text)
        self.assertNotIn("只影响当前飞书会话", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "permissions_profile_id": ":danger-full-access"})],
        )

    def test_approval_command_uses_safety_baseline_scope_text(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_approval_command("ou_user", "chat-a", "never", message_id="msg-1")

        self.assertIn("已切换审批策略：`never`", result.text)
        self.assertIn("共享 Codex thread", result.text)
        self.assertIn("安全基线", result.text)
        self.assertIn("发起每个 turn 时都会显式应用", result.text)
        self.assertNotIn("可选 override", result.text)
        self.assertNotIn("只影响当前飞书会话", result.text)

    def test_approval_and_permissions_cards_use_safety_baseline_scope_text(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        approval_card = domain.handle_approval_command("ou_user", "chat-a", "").card
        permissions_card = domain.handle_permissions_command("ou_user", "chat-a", "").card

        for card in (approval_card, permissions_card):
            content = card["elements"][0]["content"]
            self.assertIn("安全基线", content)
            self.assertIn("发起每个 turn 时都会显式应用", content)
            self.assertNotIn("可选 override", content)

    def test_set_permissions_profile_action_returns_updated_card(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.running = True
        domain = _make_domain(stub)

        response = domain.handle_set_permissions_profile(
            "ou_user",
            "chat-a",
            "msg-1",
            {"profile": "danger-full-access"},
        )

        self.assertEqual(response.toast.content, "已切换权限基线：Danger Full Access；下一轮生效")
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "permissions_profile_id": ":danger-full-access"})],
        )
        self.assertIsNotNone(response.card)

    def test_bot_status_command_uses_system_yaml_as_authority(self) -> None:
        stub = _SettingsPortsStub()
        stub.bot_identity = {
            "app_id": "cli-app",
            "configured_open_id": "ou_cfg",
            "discovered_open_id": "ou_live",
            "trigger_open_ids": ["ou_1", "ou_2"],
        }
        domain = _make_domain(stub)

        result = domain.handle_bot_status_command("chat-a")

        self.assertIn("configured bot_open_id: `ou_cfg`", result.text)
        self.assertIn("discovered open_id: `ou_live`", result.text)
        self.assertIn("运行时权威值：`system.yaml.bot_open_id`", result.text)


if __name__ == "__main__":
    unittest.main()
