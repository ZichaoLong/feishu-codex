import unittest
from types import SimpleNamespace
from typing import Any

from bot.adapters.base import RuntimeConfigSummary, RuntimeProfileSummary
from bot.codex_config_reader import ResolvedProfileConfig
from bot.codex_settings_domain import CodexSettingsDomain, SettingsDomainPorts
from bot.profile_resolution import DefaultProfileResolution
from bot.stores.thread_resume_profile_store import ThreadResumeProfileRecord


_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}
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
            current_thread_id="thread-1",
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
        self.saved_thread_profiles: list[tuple[str, str, str, str]] = []
        self.current_thread_profile: ThreadResumeProfileRecord | None = None
        self.thread_profile_mutable = (True, "")
        self.thread_reprofile_plan = SimpleNamespace(
            status="direct-write",
            reason_text="",
            diagnostics=(),
        )
        self.reset_backend_calls: list[bool] = []
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

    def load_thread_resume_profile(self, thread_id: str) -> ThreadResumeProfileRecord | None:
        if self.current_thread_profile is None:
            return None
        if self.current_thread_profile.thread_id != thread_id:
            return None
        return self.current_thread_profile

    def save_thread_resume_profile(
        self,
        thread_id: str,
        profile: str,
        model: str,
        model_provider: str,
    ) -> ThreadResumeProfileRecord:
        self.saved_thread_profiles.append((thread_id, profile, model, model_provider))
        self.current_thread_profile = ThreadResumeProfileRecord(
            thread_id=thread_id,
            profile=profile,
            model=model,
            model_provider=model_provider,
            updated_at=1.0,
        )
        return self.current_thread_profile

    def check_thread_resume_profile_mutable(self, thread_id: str) -> tuple[bool, str]:
        del thread_id
        return self.thread_profile_mutable

    def plan_thread_reprofile(self, thread_id: str):
        del thread_id
        return self.thread_reprofile_plan

    def reset_current_instance_backend(self, force: bool) -> dict[str, Any]:
        self.reset_backend_calls.append(bool(force))
        return {
            "force": bool(force),
            "released_binding_ids": ["p2p:ou_user:chat-a"],
            "interrupted_binding_ids": [],
            "fail_closed_request_count": 0,
            "purged_thread_ids": ["thread-1"],
            "app_server_url": "ws://127.0.0.1:8765",
        }

    def resolve_profile_resume_config(self, profile: str) -> ResolvedProfileConfig:
        return ResolvedProfileConfig(
            model=f"{profile}-model",
            model_provider=f"{profile}-provider",
        )

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
            load_thread_resume_profile=stub.load_thread_resume_profile,
            save_thread_resume_profile=stub.save_thread_resume_profile,
            check_thread_resume_profile_mutable=stub.check_thread_resume_profile_mutable,
            plan_thread_reprofile=stub.plan_thread_reprofile,
            reset_current_instance_backend=stub.reset_current_instance_backend,
            resolve_profile_resume_config=stub.resolve_profile_resume_config,
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

    def test_profile_command_saves_bound_thread_profile_via_port_and_returns_card(self) -> None:
        stub = _SettingsPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_profile_command("ou_user", "chat-a", "work", message_id="msg-1")

        self.assertEqual(
            stub.saved_thread_profiles,
            [("thread-1", "work", "work-model", "work-provider")],
        )
        self.assertEqual(stub.runtime_view_calls, [("ou_user", "chat-a", "msg-1")])
        self.assertIsNotNone(result.card)
        self.assertEqual(result.card["header"]["title"]["content"], "Codex Thread Profile")
        content = result.card["elements"][0]["content"]
        self.assertIn("已切换当前 thread 的 profile：`work`", content)
        action_buttons = result.card["elements"][2]["actions"]
        buttons_by_profile = {button["text"]["content"]: button for button in action_buttons}
        self.assertEqual(buttons_by_profile["work"]["type"], "primary")

    def test_profile_command_offers_backend_reset_when_thread_not_globally_unloaded(self) -> None:
        stub = _SettingsPortsStub()
        stub.thread_reprofile_plan = SimpleNamespace(
            status="reset-available",
            reason_text="当前 thread 尚未满足 verifiably globally unloaded；可通过 reset 当前实例 backend 后再写入 profile。",
            diagnostics=("当前实例：`default`",),
        )
        domain = _make_domain(stub)

        result = domain.handle_profile_command("ou_user", "chat-a", "work", message_id="msg-1")

        self.assertEqual(stub.saved_thread_profiles, [])
        self.assertIsNotNone(result.card)
        content = result.card["elements"][0]["content"]
        self.assertIn("当前还不能直接切换到 `work`。", content)
        self.assertIn("可继续执行：应用该 profile，并重置当前实例 backend。", content)
        self.assertIn("当前不能直接写入：当前 thread 尚未满足 verifiably globally unloaded", content)
        reset_action = result.card["elements"][-1]["actions"]
        self.assertEqual(
            [button["text"]["content"] for button in reset_action],
            ["应用并重置 backend"],
        )

    def test_apply_profile_with_backend_reset_saves_profile_after_reset(self) -> None:
        stub = _SettingsPortsStub()
        stub.thread_reprofile_plan = SimpleNamespace(
            status="reset-available",
            reason_text="当前 thread 尚未满足 verifiably globally unloaded；可通过 reset 当前实例 backend 后再写入 profile。",
            diagnostics=(),
        )

        def _reset_backend(force: bool) -> dict[str, Any]:
            stub.reset_backend_calls.append(bool(force))
            stub.thread_reprofile_plan = SimpleNamespace(
                status="direct-write",
                reason_text="当前 thread 已 verifiably globally unloaded，可直接写入 profile。",
                diagnostics=(),
            )
            return {
                "force": bool(force),
                "released_binding_ids": ["p2p:ou_user:chat-a"],
                "interrupted_binding_ids": [],
                "fail_closed_request_count": 0,
                "purged_thread_ids": ["thread-1"],
                "app_server_url": "ws://127.0.0.1:8765",
            }

        stub.reset_current_instance_backend = _reset_backend
        domain = _make_domain(stub)

        response = domain.handle_apply_profile_with_backend_reset(
            "ou_user",
            "chat-a",
            "msg-1",
            {"profile": "work", "force": False},
        )

        self.assertEqual(stub.reset_backend_calls, [False])
        self.assertEqual(
            stub.saved_thread_profiles,
            [("thread-1", "work", "work-model", "work-provider")],
        )
        self.assertEqual(response.toast.type, "success")
        self.assertIn("已应用 `work` 并重置 backend", response.toast.content)
        self.assertIsNotNone(response.card)

    def test_profile_command_rejects_when_unbound(self) -> None:
        stub = _SettingsPortsStub()
        stub.runtime.current_thread_id = ""
        domain = _make_domain(stub)

        result = domain.handle_profile_command("ou_user", "chat-a", "work", message_id="msg-1")

        self.assertIn("当前还没有绑定 thread", result.text)
        self.assertEqual(stub.saved_thread_profiles, [])

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
