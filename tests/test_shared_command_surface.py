import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.approval_settings_cards import (
    build_approval_policy_card,
    build_permissions_profile_card,
)
from bot.cards import (
    build_ask_user_card,
    build_backend_reset_card,
    build_execution_card,
    build_group_activation_card,
    build_group_mode_card,
    build_rename_card,
    build_threads_card,
)
from bot.interaction_approval_cards import build_command_approval_card
from bot.focus_runtime.runtime import FocusRuntime as CodexHandler
from bot.codex_help_domain import CodexHelpDomain
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.shared_command_surface import get_shared_command, iter_shared_commands

_DISPLAY_LOCAL_RESUME_COMMAND = feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>")


def _help_session(**overrides):
    values = {
        "working_dir": "/tmp/project",
        "current_thread_id": "",
        "current_thread_title": "",
        "feishu_runtime_state": "attached",
        "permissions_profile_id": ":workspace",
        "model": "",
        "reasoning_effort": "",
        **overrides,
    }
    return SimpleNamespace(
        working_dir=values["working_dir"],
        current_thread_id=values["current_thread_id"],
        current_thread_title=values["current_thread_title"],
        thread=SimpleNamespace(feishu_runtime_state=values["feishu_runtime_state"]),
        permissions_profile_id=values["permissions_profile_id"],
        model=values["model"],
        reasoning_effort=values["reasoning_effort"],
    )


class _StubAdapter:
    def __init__(self, *args, **kwargs) -> None:
        del args
        del kwargs

    def stop(self) -> None:
        return None

    def list_loaded_thread_ids(self) -> list[str]:
        return []

    def list_threads_all(self, **_kwargs) -> list[object]:
        return []

    def read_thread(self, _thread_id: str, **_kwargs):
        return None

    def compact_thread(self, _thread_id: str) -> None:
        return None

    def rename_thread(self, _thread_id: str, _name: str) -> None:
        return None

    def get_thread_goal(self, _thread_id: str):
        return None

    def interrupt_turn(self, **_kwargs) -> None:
        return None

    def connection_generation(self, **_kwargs) -> int:
        return 1

    def unsubscribe_thread(self, _thread_id: str) -> None:
        return None

    def unarchive_thread(self, _thread_id: str):
        return None

    def respond_with_existing_backend_authority(
        self,
        _request_id: int | str,
        **_kwargs,
    ) -> None:
        return None

    def require_owned_backend_lifecycle(self) -> None:
        return None


class SharedCommandSurfaceTests(unittest.TestCase):
    def _make_handler(self) -> CodexHandler:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        config_patch = patch(
            "bot.focus_runtime.runtime.load_config_file",
            return_value={"mirror_watchdog_seconds": 999999},
        )
        adapter_patch = patch(
            "bot.focus_runtime.runtime.CodexAppServerAdapter",
            _StubAdapter,
        )
        config_patch.start()
        adapter_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(adapter_patch.stop)
        handler = CodexHandler(data_dir=pathlib.Path(tempdir.name))
        self.addCleanup(handler.shutdown)
        return handler

    def _assert_no_plugin_key(self, value) -> None:
        if isinstance(value, dict):
            self.assertNotIn("plugin", value)
            for nested in value.values():
                self._assert_no_plugin_key(nested)
            return
        if isinstance(value, list):
            for nested in value:
                self._assert_no_plugin_key(nested)

    def test_shared_commands_are_present_in_handler_routes(self) -> None:
        handler = self._make_handler()

        for spec in iter_shared_commands():
            self.assertTrue(
                handler._feishu_surface._inbound_surface.has_command_route(
                    spec.slash_name
                )
            )

    def test_handler_exposes_preflight_command_route(self) -> None:
        handler = self._make_handler()

        self.assertTrue(
            handler._feishu_surface._inbound_surface.has_command_route(
                "/preflight"
            )
        )

    def test_reset_backend_shared_summary_uses_recovery_wording(self) -> None:
        command = get_shared_command("reset-backend")

        self.assertEqual(command.feishu_summary, "预览并重置当前实例 backend，用于恢复或排障。")

    def test_steer_shared_command_is_explicit_exact_turn_text(self) -> None:
        command = get_shared_command("steer")

        self.assertEqual(command.feishu_usage, "/steer 〈text〉")
        self.assertIn("exact active turn", command.feishu_summary)

    def test_help_thread_and_threads_cards_reuse_shared_command_specs(self) -> None:
        resume_command = get_shared_command("resume")
        help_domain = CodexHelpDomain(
            local_thread_safety_rule="测试规则",
            resolve_session=lambda sender_id, chat_id, message_id="": _help_session(),
        )

        overview = help_domain.reply_help("chat-1").card
        start_help = help_domain.reply_help("chat-1", "thread").card
        thread_settings_help = help_domain.reply_help("chat-1", "thread-settings").card
        threads_card = build_threads_card(
            threads=[
                {
                    "thread_id": "thread-1",
                    "title": "Demo",
                    "cwd": "/tmp/project",
                    "updated_at": 0,
                    "source": "cli",
                    "status": "idle",
                }
            ],
            current_thread_id="thread-1",
            working_dir="/tmp/project",
            total_count=1,
            shown_count=1,
            expanded=True,
        )
        execution_card = build_execution_card("log", [], running=True)

        overview_markdown = overview["elements"][0]["content"]
        start_markdown = start_help["elements"][0]["content"]
        thread_settings_markdown = thread_settings_help["elements"][0]["content"]
        threads_markdown = threads_card["elements"][0]["content"]

        self.assertIn("线程：`未绑定`", overview_markdown)
        self.assertIn("本轮：权限 `Workspace` | 模型 `Auto` | 推理 `Auto`", overview_markdown)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", start_markdown)
        self.assertIn("`focusctl thread list --scope cwd`", start_markdown)
        self.assertIn("测试规则", start_markdown)
        self.assertNotIn("`/profile", thread_settings_markdown)
        self.assertIn(f"`{_DISPLAY_LOCAL_RESUME_COMMAND}`", threads_markdown)
        self.assertIn("`focusctl thread list --scope cwd`", threads_markdown)
        self.assertIn(f"`{resume_command.feishu_usage}`", threads_markdown)
        self.assertEqual(
            [item["text"]["content"] for item in start_help["elements"][2]["actions"]],
            ["新建线程", "恢复线程"],
        )
        self.assertEqual(
            [item["text"]["content"] for item in thread_settings_help["elements"][2]["actions"]],
            ["查看 Goal", "压缩上下文"],
        )
        self.assertEqual(execution_card["header"]["title"]["content"], "Codex 执行过程（执行中）")
        self.assertNotIn("`/help`", json.dumps(execution_card, ensure_ascii=False))

    def test_help_overview_turn_summary_uses_labeled_segments(self) -> None:
        help_domain = CodexHelpDomain(
            local_thread_safety_rule="测试规则",
            resolve_session=lambda sender_id, chat_id, message_id="": _help_session(
                permissions_profile_id=":danger-full-access",
                model="gpt-5.5",
                reasoning_effort="high",
            ),
        )

        overview_markdown = help_domain.reply_help("chat-1").card["elements"][0]["content"]

        self.assertIn(
            "本轮：权限 `Full` | 模型 `gpt-5.5` | 推理 `High`",
            overview_markdown,
        )

    def test_generated_cards_do_not_emit_plugin_payload_keys(self) -> None:
        help_domain = CodexHelpDomain(
            local_thread_safety_rule="测试规则",
            resolve_session=lambda sender_id, chat_id, message_id="": _help_session(),
        )
        cards = [
            help_domain.reply_help("chat-1").card,
            help_domain.reply_help("chat-1", "thread").card,
            build_backend_reset_card(content="预览", force=False),
            build_execution_card("log", [], running=True),
            build_command_approval_card(
                "req-1",
                command="ls",
                cwd="/tmp/project",
                reason="需要执行",
                actions=[
                    {"id": "approve_once", "label": "允许本次", "style": "primary"},
                    {"id": "reject", "label": "拒绝", "style": "danger"},
                ],
            ),
            build_approval_policy_card("on-request", running=True),
            build_permissions_profile_card(":workspace", running=True),
            build_group_mode_card("assistant", can_manage=True),
            build_group_activation_card(
                activated=True,
                activated_by="ou-1",
                activated_at=1712476800000,
                can_manage=True,
            ),
            build_ask_user_card(
                "req-2",
                [
                    {
                        "id": "q1",
                        "header": "确认",
                        "question": "是否继续？",
                        "options": [
                            {"label": "继续", "description": "继续执行"},
                            {"label": "停止", "description": "中止本轮"},
                        ],
                    }
                ],
            ),
            build_threads_card(
                threads=[
                    {
                        "thread_id": "thread-1",
                        "title": "Demo",
                        "cwd": "/tmp/project",
                        "updated_at": 0,
                        "source": "cli",
                        "status": "idle",
                    }
                ],
                current_thread_id="thread-1",
                working_dir="/tmp/project",
                total_count=1,
                shown_count=1,
                expanded=True,
            ),
            build_rename_card(
                {
                    "thread_id": "thread-1",
                    "cwd": "/tmp/project",
                    "title": "Demo",
                }
            ),
        ]

        for card in cards:
            assert card is not None
            self._assert_no_plugin_key(card)
