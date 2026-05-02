import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.cards import (
    build_approval_policy_card,
    build_ask_user_card,
    build_backend_reset_card,
    build_collaboration_mode_card,
    build_command_approval_card,
    build_execution_card,
    build_group_activation_card,
    build_group_mode_card,
    build_permissions_preset_card,
    build_profile_card,
    build_rename_card,
    build_sandbox_policy_card,
    build_sessions_card,
)
from bot.codex_handler import CodexHandler
from bot.codex_help_domain import CodexHelpDomain
from bot.shared_command_surface import get_shared_command, iter_shared_commands


class _StubAdapter:
    def __init__(self, *args, **kwargs) -> None:
        del args
        del kwargs

    def stop(self) -> None:
        return None


class SharedCommandSurfaceTests(unittest.TestCase):
    def _make_handler(self) -> CodexHandler:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        config_patch = patch(
            "bot.codex_handler.load_config_file",
            return_value={"mirror_watchdog_seconds": 999999},
        )
        adapter_patch = patch("bot.codex_handler.CodexAppServerAdapter", _StubAdapter)
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
            self.assertTrue(handler._inbound_surface.has_command_route(spec.slash_name))

    def test_handler_exposes_preflight_command_route(self) -> None:
        handler = self._make_handler()

        self.assertTrue(handler._inbound_surface.has_command_route("/preflight"))

    def test_help_thread_and_session_cards_reuse_shared_command_specs(self) -> None:
        session_command = get_shared_command("session")
        resume_command = get_shared_command("resume")
        help_domain = CodexHelpDomain(local_thread_safety_rule="测试规则")

        overview = help_domain.reply_help("chat-1").card
        thread_help = help_domain.reply_help("chat-1", "thread").card
        sessions_card = build_sessions_card(
            sessions=[
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
        thread_markdown = thread_help["elements"][0]["content"]
        sessions_markdown = sessions_card["elements"][0]["content"]

        self.assertIn("`fcodex resume <thread_id|thread_name>`", overview_markdown)
        self.assertIn("`feishu-codexctl thread list --scope cwd`", overview_markdown)
        self.assertIn(f"`{session_command.feishu_usage}`", thread_markdown)
        self.assertIn(f"`{resume_command.feishu_usage}`", thread_markdown)
        self.assertIn("`fcodex resume <thread_id|thread_name>`", thread_markdown)
        self.assertIn("`fcodex resume <thread_id|thread_name>`", sessions_markdown)
        self.assertIn("`feishu-codexctl thread list --scope cwd`", sessions_markdown)
        self.assertIn(f"`{resume_command.feishu_usage}`", sessions_markdown)
        self.assertEqual(execution_card["header"]["title"]["content"], "Codex 执行过程（执行中）")
        self.assertNotIn("`/help`", json.dumps(execution_card, ensure_ascii=False))

    def test_generated_cards_do_not_emit_plugin_payload_keys(self) -> None:
        help_domain = CodexHelpDomain(local_thread_safety_rule="测试规则")
        cards = [
            help_domain.reply_help("chat-1").card,
            help_domain.reply_help("chat-1", "thread").card,
            build_profile_card(content="切换 profile", profile_names=["p1"], current_profile="p1"),
            build_backend_reset_card(content="预览", force=False),
            build_execution_card("log", [], running=True),
            build_command_approval_card("req-1", command="ls", cwd="/tmp/project", reason="需要执行"),
            build_approval_policy_card("on-request", running=True),
            build_sandbox_policy_card("workspace-write", running=True),
            build_permissions_preset_card("on-request", "workspace-write", running=True),
            build_collaboration_mode_card("plan", running=True),
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
            build_sessions_card(
                sessions=[
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
