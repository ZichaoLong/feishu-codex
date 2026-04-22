import unittest
from types import SimpleNamespace
from typing import Any

from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary
from bot.codex_settings_domain import CodexSettingsDomain, SettingsDomainPorts
from bot.profile_resolution import DefaultProfileResolution


_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_POLICIES = {"read-only", "workspace-write", "danger-full-access"}
_PERMISSIONS_PRESETS = {
    "read-only": {
        "label": "Read Only",
        "approval_policy": "on-request",
        "sandbox": "read-only",
    },
    "default": {
        "label": "Default",
        "approval_policy": "on-request",
        "sandbox": "workspace-write",
    },
    "full-access": {
        "label": "Full Access",
        "approval_policy": "never",
        "sandbox": "danger-full-access",
    },
}


class _SettingsPortsStub:
    def __init__(self) -> None:
        self.message_contexts: dict[str, dict[str, Any]] = {}
        self.bot_identity: dict[str, Any] = {}
        self.added_admin_open_ids: list[str] = []
        self.configured_bot_open_ids: list[str] = []
        self.runtime = SimpleNamespace(
            running=False,
            approval_policy="on-request",
            sandbox="workspace-write",
            collaboration_mode="default",
        )
        self.runtime_config = RuntimeConfigSummary(
            profiles=[
                RuntimeProfileSummary(name="default", model_provider="openai"),
                RuntimeProfileSummary(name="work", model_provider="anthropic"),
            ],
        )
        self.profile_resolution = DefaultProfileResolution(
            effective_profile="default",
            available_profiles=("default", "work"),
        )
        self.saved_profiles: list[str] = []
        self.runtime_view_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.resolution_calls: list[RuntimeConfigSummary | None] = []
        self.debug_sender_snapshots: dict[str, dict[str, Any]] = {}

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

    def get_bot_identity_snapshot(self) -> dict[str, Any]:
        return dict(self.bot_identity)

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

    def add_admin_open_id(self, open_id: str) -> None:
        self.added_admin_open_ids.append(open_id)

    def set_configured_bot_open_id(self, open_id: str) -> None:
        self.configured_bot_open_ids.append(open_id)

    def save_default_profile(self, profile: str) -> None:
        self.saved_profiles.append(profile)

    def get_runtime_view(self, sender_id: str, chat_id: str, message_id: str):
        self.runtime_view_calls.append((sender_id, chat_id, message_id))
        return self.runtime

    def update_runtime_settings(self, sender_id: str, chat_id: str, **kwargs: Any) -> None:
        self.update_calls.append((sender_id, chat_id, kwargs))

    def safe_read_runtime_config(self) -> RuntimeConfigSummary | None:
        return self.runtime_config

    def current_default_profile_resolution(
        self,
        runtime_config: RuntimeConfigSummary | None,
    ) -> DefaultProfileResolution:
        self.resolution_calls.append(runtime_config)
        return self.profile_resolution


def _make_domain(stub: _SettingsPortsStub) -> CodexSettingsDomain:
    return CodexSettingsDomain(
        ports=SettingsDomainPorts(
            get_message_context=stub.get_message_context,
            get_sender_display_name=stub.get_sender_display_name,
            debug_sender_name_resolution=stub.debug_sender_name_resolution,
            get_bot_identity_snapshot=stub.get_bot_identity_snapshot,
            add_admin_open_id=stub.add_admin_open_id,
            set_configured_bot_open_id=stub.set_configured_bot_open_id,
            save_default_profile=stub.save_default_profile,
            adapter_model_provider="",
            get_runtime_view=stub.get_runtime_view,
            update_runtime_settings=stub.update_runtime_settings,
            safe_read_runtime_config=stub.safe_read_runtime_config,
            current_default_profile_resolution=stub.current_default_profile_resolution,
        ),
        approval_policies=_APPROVAL_POLICIES,
        sandbox_policies=_SANDBOX_POLICIES,
        permissions_presets=_PERMISSIONS_PRESETS,
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

        self.assertIn("/debug-contact <open_id>", result.text)

    def test_profile_command_saves_profile_via_port_and_returns_card(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_profile_command("ou_user", "chat-a", "work", message_id="msg-1")

        self.assertEqual(stub.saved_profiles, ["work"])
        self.assertEqual(stub.runtime_view_calls, [("ou_user", "chat-a", "msg-1")])
        self.assertEqual(stub.resolution_calls, [stub.runtime_config])
        self.assertIsNotNone(result.card)
        content = result.card["elements"][0]["content"]
        self.assertIn("已切换默认 profile：`work`", content)
        action_buttons = result.card["elements"][2]["actions"]
        buttons_by_profile = {button["text"]["content"]: button for button in action_buttons}
        self.assertEqual(buttons_by_profile["work"]["type"], "primary")

    def test_approval_command_updates_only_approval_policy(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_approval_command("ou_user", "chat-a", "never", message_id="msg-1")

        self.assertIn("已切换审批策略：`never`", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "approval_policy": "never"})],
        )

    def test_permissions_command_updates_approval_and_sandbox_together(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_permissions_command(
            "ou_user",
            "chat-a",
            "full-access",
            message_id="msg-1",
        )

        self.assertIn("已切换权限预设：`Full Access`", result.text)
        self.assertEqual(
            stub.update_calls,
            [
                (
                    "ou_user",
                    "chat-a",
                    {
                        "message_id": "msg-1",
                        "approval_policy": "never",
                        "sandbox": "danger-full-access",
                    },
                )
            ],
        )

    def test_mode_command_updates_only_collaboration_mode(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_mode_command("ou_user", "chat-a", "plan", message_id="msg-1")

        self.assertIn("已切换协作模式：`plan`", result.text)
        self.assertEqual(
            stub.update_calls,
            [("ou_user", "chat-a", {"message_id": "msg-1", "collaboration_mode": "plan"})],
        )


if __name__ == "__main__":
    unittest.main()
