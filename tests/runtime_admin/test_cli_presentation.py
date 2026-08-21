import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

from bot.adapters.base import ThreadSummary
from bot.codex_config import CodexConfig
from bot.runtime_admin.cli import (
    _binding_list_refresh_target_count,
    _binding_list_refresh_target_resolution_counts,
    _binding_list_refresh_timeout_seconds,
    _clear_thread_goal,
    _print_binding_list,
    _print_binding_status,
    _print_thread_goal,
    _print_thread_list,
    _print_thread_status,
    _render_table,
    _send_binding_prompt,
    _send_thread_image,
    _set_thread_goal,
    _terminal_display_width,
)
from bot.system_config import SystemConfig


class RuntimeAdminCliPresentationTests(unittest.TestCase):
    def _visual_cell_starts(self, line: str, cells: list[str]) -> list[int]:
        starts: list[int] = []
        offset = 0
        for cell in cells:
            start = line.find(cell, offset)
            self.assertNotEqual(start, -1)
            starts.append(_terminal_display_width(line[:start]))
            offset = start + len(cell)
        return starts

    def test_send_binding_prompt_reports_denial(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "binding_id": "p2p:ou_user:chat-1",
            "thread_id": "thread-1",
            "started": False,
            "turn_id": "",
            "reason_code": "prompt_denied_by_running_turn",
            "reason": "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            "display_mode": "silent",
            "synthetic_source": "schedule",
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _send_binding_prompt(
                    Path("/tmp/instance-data"),
                    binding_id="p2p:ou_user:chat-1",
                    text="继续执行",
                    synthetic_source="schedule",
                    instance_name="explorer",
                )

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("started: no", rendered)
        self.assertIn("reason code: prompt_denied_by_running_turn", rendered)

    def test_send_binding_prompt_reports_queued_as_success(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "binding_id": "p2p:ou_user:chat-1",
            "thread_id": "thread-1",
            "started": False,
            "queued": True,
            "queue_position": 2,
            "turn_id": "",
            "reason_code": "",
            "reason": "",
            "display_mode": "silent",
            "synthetic_source": "schedule",
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _send_binding_prompt(
                    Path("/tmp/instance-data"),
                    binding_id="p2p:ou_user:chat-1",
                    text="继续执行",
                    synthetic_source="schedule",
                    instance_name="explorer",
                )

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("started: no", rendered)
        self.assertIn("queued: yes", rendered)
        self.assertIn("queue_position: 2", rendered)

    def test_render_table_aligns_wide_characters(self) -> None:
        headers = ["THREAD_ID", "PROVIDER", "CWD", "TITLE"]
        rows = [
            ["thread-1", "openai", "/tmp/项目", "修复对齐"],
            ["thread-22", "-", "/tmp/demo", "ascii title"],
        ]

        rendered = _render_table(headers, rows)

        self.assertEqual(_terminal_display_width("项目"), 4)
        self.assertEqual(_terminal_display_width("e\u0301"), 1)
        self.assertNotIn("\t", "\n".join(rendered))
        header_starts = self._visual_cell_starts(rendered[0], headers)
        self.assertEqual(self._visual_cell_starts(rendered[1], rows[0]), header_starts)
        self.assertEqual(self._visual_cell_starts(rendered[2], rows[1]), header_starts)

    def test_thread_list_renders_aligned_columns_without_tabs(self) -> None:
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/项目一",
                name="修复对齐",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
                model_provider="openai",
            ),
            ThreadSummary(
                thread_id="thread-22",
                cwd="/tmp/demo",
                name="ascii title",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
                model_provider=None,
            ),
        ]

        class _FakeAdapter:
            def stop(self) -> None:
                return None

        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.cli._attached_endpoint_adapter",
            return_value=(_FakeAdapter(), CodexConfig(), "ws://127.0.0.1:8765"),
        ):
            with patch("bot.runtime_admin.cli.list_global_threads", return_value=threads):
                with redirect_stdout(stdout):
                    result = _print_thread_list(Path("/tmp/instance-data"), scope="global", cwd="")

        self.assertEqual(result, 0)
        lines = stdout.getvalue().splitlines()
        self.assertNotIn("\t", "\n".join(lines))
        header = ["THREAD_ID", "PROVIDER", "CWD", "TITLE"]
        row1 = ["thread-1", "openai", "/tmp/项目一", "修复对齐"]
        row2 = ["thread-22", "-", "/tmp/demo", "ascii title"]
        header_starts = self._visual_cell_starts(lines[0], header)
        self.assertEqual(self._visual_cell_starts(lines[1], row1), header_starts)
        self.assertEqual(self._visual_cell_starts(lines[2], row2), header_starts)

    def test_thread_list_collapses_and_truncates_long_multiline_titles(self) -> None:
        title = "第一行\n第二行 " + ("很长" * 60)
        threads = [
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="",
                preview=title,
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
                model_provider="openai",
            ),
        ]

        class _FakeAdapter:
            def stop(self) -> None:
                return None

        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.cli._attached_endpoint_adapter",
            return_value=(_FakeAdapter(), CodexConfig(), "ws://127.0.0.1:8765"),
        ):
            with patch("bot.runtime_admin.cli.list_global_threads", return_value=threads):
                with redirect_stdout(stdout):
                    result = _print_thread_list(Path("/tmp/instance-data"), scope="global", cwd="")

        self.assertEqual(result, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("第一行 第二行", lines[1])
        self.assertTrue(lines[1].endswith("…"))

    def test_thread_list_archived_forwards_archived_filter(self) -> None:
        class _FakeAdapter:
            def stop(self) -> None:
                return None

        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.cli._attached_endpoint_adapter",
            return_value=(_FakeAdapter(), CodexConfig(), "ws://127.0.0.1:8765"),
        ):
            with patch("bot.runtime_admin.cli.list_global_threads", return_value=[]) as mock_list:
                with redirect_stdout(stdout):
                    result = _print_thread_list(
                        Path("/tmp/instance-data"),
                        scope="global",
                        cwd="",
                        archived=True,
                    )

        self.assertEqual(result, 0)
        self.assertTrue(mock_list.call_args.kwargs["archived"])
        self.assertIn("已归档", stdout.getvalue())

    def test_binding_list_renders_aligned_columns_without_tabs(self) -> None:
        snapshot = {
            "bindings": [
                {
                    "binding_id": "p2p:ou_user:chat-1",
                    "binding_kind": "p2p",
                    "sender_id": "ou_user",
                    "chat_id": "chat-1",
                    "chat_display_name": "Alice",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "thread-1234567890",
                    "thread_name": "Renamed thread",
                    "working_dir": "/tmp/项目二",
                },
                {
                    "binding_id": "group:chat-2",
                    "binding_kind": "group",
                    "sender_id": "__group__",
                    "chat_id": "chat-2",
                    "chat_display_name": "",
                    "binding_state": "detached",
                    "feishu_runtime_state": "idle",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/demo",
                },
            ]
        }
        stdout = io.StringIO()
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_binding_list(Path("/tmp/instance-data"))

        self.assertEqual(result, 0)
        lines = stdout.getvalue().splitlines()
        self.assertNotIn("\t", "\n".join(lines))
        header = ["BINDING_ID", "KIND", "CHAT", "STATE", "RUNTIME", "THREAD", "CWD"]
        row1 = [
            "p2p:ou_user:chat-1",
            "p2p",
            "Alice",
            "bound",
            "attached",
            "thread-1… Renamed thread",
            "/tmp/项目二",
        ]
        row2 = ["group:chat-2", "group", "chat-2", "detached", "idle", "-", "/tmp/demo"]
        header_starts = self._visual_cell_starts(lines[0], header)
        self.assertEqual(self._visual_cell_starts(lines[1], row1), header_starts)
        self.assertEqual(self._visual_cell_starts(lines[2], row2), header_starts)

    def test_binding_list_thread_column_ignores_cached_title_and_preview(self) -> None:
        snapshot = {
            "bindings": [
                {
                    "binding_id": "p2p:ou_user:chat-1",
                    "binding_kind": "p2p",
                    "sender_id": "ou_user",
                    "chat_id": "chat-1234567890",
                    "chat_display_name": "",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "thread-1234567890",
                    "thread_name": "",
                    "thread_title": "cached title must not display",
                    "thread_preview": "preview must not display",
                    "working_dir": "/tmp/project",
                },
                {
                    "binding_id": "p2p:ou_user2:chat-2",
                    "binding_kind": "p2p",
                    "sender_id": "ou_user2",
                    "chat_id": "chat-2",
                    "chat_display_name": "Very long chat display name " + ("x" * 80),
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "thread-2234567890",
                    "thread_name": "Line one\nLine two " + ("y" * 80),
                    "working_dir": "/tmp/project",
                },
            ]
        }
        stdout = io.StringIO()
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_binding_list(Path("/tmp/instance-data"))

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("thread-1…", rendered)
        self.assertNotIn("cached title", rendered)
        self.assertNotIn("preview", rendered)
        self.assertIn("Line one Line two", rendered)
        self.assertNotIn("\nLine two", rendered)
        self.assertIn("…", rendered)

    def test_binding_list_prompts_refresh_when_chat_name_cache_misses(self) -> None:
        snapshot = {
            "bindings": [
                {
                    "binding_id": "group:chat-1",
                    "binding_kind": "group",
                    "sender_id": "__group__",
                    "chat_id": "chat-1234567890",
                    "chat_display_name": "",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/project",
                },
            ],
            "chat_display_name_cache_miss_count": 1,
        }
        stdout = io.StringIO()
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_binding_list(Path("/tmp/instance-data"))

        self.assertEqual(result, 0)
        self.assertIn("focusctl binding list --refresh-names", stdout.getvalue())

    def test_binding_list_refresh_prompt_preserves_non_default_instance(self) -> None:
        snapshot = {
            "bindings": [
                {
                    "binding_id": "group:chat-1",
                    "binding_kind": "group",
                    "sender_id": "__group__",
                    "chat_id": "chat-1234567890",
                    "chat_display_name": "",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/project",
                },
            ],
            "chat_display_name_cache_miss_count": 1,
        }
        stdout = io.StringIO()
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_binding_list(Path("/tmp/instance-data"), instance_name="explorer")

        self.assertEqual(result, 0)
        self.assertIn("focusctl --instance explorer binding list --refresh-names", stdout.getvalue())

    def test_binding_list_refresh_names_estimates_timeout_from_unique_targets(self) -> None:
        snapshot = {
            "bindings": [
                {
                    "binding_id": "group:chat-1",
                    "binding_kind": "group",
                    "sender_id": "__group__",
                    "chat_id": "chat-1",
                    "chat_display_name": "Project Group",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/project",
                },
                {
                    "binding_id": "group:chat-1-secondary",
                    "binding_kind": "group",
                    "sender_id": "__group__",
                    "chat_id": "chat-1",
                    "chat_display_name": "Project Group",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/project",
                },
                {
                    "binding_id": "p2p:ou_user:chat-2",
                    "binding_kind": "p2p",
                    "sender_id": "ou_user",
                    "chat_id": "chat-2",
                    "chat_display_name": "Alice",
                    "binding_state": "bound",
                    "feishu_runtime_state": "attached",
                    "thread_id": "",
                    "thread_name": "",
                    "working_dir": "/tmp/project",
                },
            ],
            "chat_display_name_cache_miss_count": 0,
        }
        stdout = io.StringIO()
        with (
            patch("bot.runtime_admin.cli._request", side_effect=[snapshot, snapshot]) as request,
            patch(
                "bot.runtime_admin.cli.load_config",
                return_value=SystemConfig(
                    app_id="app-id",
                    app_secret="secret",
                    request_timeout_seconds=2.0,
                ),
            ),
            redirect_stdout(stdout),
        ):
            result = _print_binding_list(Path("/tmp/instance-data"), refresh_names=True)

        self.assertEqual(result, 0)
        self.assertEqual(
            request.call_args_list,
            [
                call(
                    Path("/tmp/instance-data"),
                    "binding/list",
                    timeout_seconds=3.0,
                ),
                call(
                    Path("/tmp/instance-data"),
                    "binding/list",
                    {"refresh_names": True},
                    timeout_seconds=7.0,
                ),
            ],
        )
        self.assertIn("name refresh targets: resolved=2 unresolved=0", stdout.getvalue())

    def test_binding_list_refresh_target_count_deduplicates_lookup_targets(self) -> None:
        bindings = [
            {"binding_kind": "group", "sender_id": "__group__", "chat_id": "chat-1"},
            {"binding_kind": "group", "sender_id": "__group__", "chat_id": "chat-1"},
            {"binding_kind": "p2p", "sender_id": "ou_user", "chat_id": "chat-2"},
            {"binding_kind": "p2p", "sender_id": "ou_user", "chat_id": "chat-3"},
            {"binding_kind": "p2p", "sender_id": "", "chat_id": "chat-4"},
            {"binding_kind": "unknown", "sender_id": "ou_other", "chat_id": "chat-5"},
        ]

        self.assertEqual(_binding_list_refresh_target_count(bindings), 2)

    def test_binding_list_refresh_target_resolution_counts_deduplicate_targets(self) -> None:
        bindings = [
            {
                "binding_kind": "group",
                "sender_id": "__group__",
                "chat_id": "chat-1",
                "chat_display_name": "Project Group",
            },
            {
                "binding_kind": "group",
                "sender_id": "__group__",
                "chat_id": "chat-1",
                "chat_display_name": "Project Group",
            },
            {
                "binding_kind": "p2p",
                "sender_id": "ou_user",
                "chat_id": "chat-2",
                "chat_display_name": "",
            },
            {
                "binding_kind": "p2p",
                "sender_id": "ou_user",
                "chat_id": "chat-3",
                "chat_display_name": "",
            },
        ]

        self.assertEqual(_binding_list_refresh_target_resolution_counts(bindings), (1, 1))

    def test_binding_list_refresh_timeout_uses_system_request_timeout(self) -> None:
        bindings = [
            {"binding_kind": "group", "sender_id": "__group__", "chat_id": "chat-1"},
            {"binding_kind": "p2p", "sender_id": "ou_user", "chat_id": "chat-2"},
        ]

        with patch(
            "bot.runtime_admin.cli.load_config",
            return_value=SystemConfig(
                app_id="app-id",
                app_secret="secret",
                request_timeout_seconds=4.0,
            ),
        ):
            self.assertEqual(_binding_list_refresh_timeout_seconds(bindings), 11.0)

    def test_binding_list_refresh_timeout_does_not_hide_invalid_system_config(self) -> None:
        bindings = [
            {"binding_kind": "group", "sender_id": "__group__", "chat_id": "chat-1"},
        ]

        with patch(
            "bot.runtime_admin.cli.load_config",
            side_effect=ValueError("request_timeout_seconds 必须是数字"),
        ):
            with self.assertRaisesRegex(ValueError, "request_timeout_seconds"):
                _binding_list_refresh_timeout_seconds(bindings)

    def test_binding_status_renders_resolved_instance_name(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "binding_id": "p2p:ou_user:chat-1",
            "binding_kind": "p2p",
            "chat_id": "chat-1",
            "sender_id": "ou_user",
            "working_dir": "/tmp/project",
            "binding_state": "bound",
            "thread_id": "thread-1",
            "thread_title": "demo",
            "feishu_runtime_state": "attached",
            "backend_thread_status": "idle",
            "backend_running_turn": False,
            "live_runtime_owner": {"label": "explorer"},
            "live_runtime_holder_labels": ["service@explorer(pid=1234)"],
            "interaction_owner": {"label": "none"},
            "next_prompt_allowed": True,
            "detach_available": True,
            "detach_reason_code": "",
            "detach_reason": "",
            "approval_policy": "on-request",
            "permissions_profile_id": ":workspace",
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_binding_status(Path("/tmp/instance-data"), "p2p:ou_user:chat-1", instance_name="explorer")

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("binding: p2p:ou_user:chat-1", rendered)
        self.assertIn("current-instance interaction owner: none", rendered)

    def test_thread_status_renders_resolved_instance_name(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "backend_thread_status": "notLoaded",
            "backend_running_turn": False,
            "live_runtime_owner": {"label": "explorer"},
            "live_runtime_holder_labels": ["service@explorer(pid=1234)"],
            "bound_binding_ids": [],
            "attached_binding_ids": [],
            "detached_binding_ids": [],
            "interaction_owner": {"label": "none"},
            "detach_available": False,
            "detach_reason_code": "unsubscribe_not_applicable_no_binding",
            "detach_reason": "当前没有 Feishu 绑定指向该线程。",
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_thread_status(
                    Path("/tmp/instance-data"),
                    {"thread_name": "demo"},
                    instance_name="explorer",
                )

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("thread: thread-1 demo", rendered)
        self.assertIn("current-instance interaction owner: none", rendered)

    def test_print_thread_goal_renders_goal_snapshot(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "goal": {
                "thread_id": "thread-1",
                "objective": "ship goal support",
                "status": "active",
                "token_budget": 100,
                "tokens_used": 12,
                "time_used_seconds": 34,
                "created_at": 1712476800,
                "updated_at": 1712476801,
            },
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _print_thread_goal(
                    Path("/tmp/instance-data"),
                    {"thread_id": "thread-1"},
                    instance_name="explorer",
                )

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("thread: thread-1 demo", rendered)
        self.assertIn("objective: ship goal support", rendered)
        self.assertIn("status: active (进行中)", rendered)
        self.assertIn("token budget: 100", rendered)
        self.assertIn("tokens used: 12", rendered)

    def test_set_thread_goal_compacts_empty_fields(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "goal": {
                "thread_id": "thread-1",
                "objective": "ship goal support",
                "status": "paused",
                "token_budget": None,
                "tokens_used": 12,
                "time_used_seconds": 34,
                "created_at": 1712476800,
                "updated_at": 1712476801,
            },
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot) as mock_request:
            with redirect_stdout(stdout):
                result = _set_thread_goal(
                    Path("/tmp/instance-data"),
                    {"thread_id": "thread-1"},
                    status="paused",
                    instance_name="explorer",
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            mock_request.call_args.args,
            (
                Path("/tmp/instance-data"),
                "thread/goal/set",
                {
                    "thread_id": "thread-1",
                    "status": "paused",
                },
            ),
        )
        self.assertIn("note: 当前 thread goal 已更新。", stdout.getvalue())

    def test_set_and_clear_thread_goal_render_operation_notes(self) -> None:
        stdout = io.StringIO()
        paused = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "goal": {
                "thread_id": "thread-1",
                "objective": "ship goal support",
                "status": "paused",
                "token_budget": None,
                "tokens_used": 12,
                "time_used_seconds": 34,
                "created_at": 1712476800,
                "updated_at": 1712476801,
            },
        }
        cleared = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "goal": None,
            "cleared": True,
        }
        with patch("bot.runtime_admin.cli._request", side_effect=[paused, cleared]):
            with redirect_stdout(stdout):
                self.assertEqual(
                    _set_thread_goal(
                        Path("/tmp/instance-data"),
                        {"thread_id": "thread-1"},
                        status="paused",
                        instance_name="explorer",
                    ),
                    0,
                )
                self.assertEqual(
                    _clear_thread_goal(Path("/tmp/instance-data"), {"thread_id": "thread-1"}, instance_name="explorer"),
                    0,
                )

        rendered = stdout.getvalue()
        self.assertIn("note: 当前 thread goal 已更新。", rendered)
        self.assertIn("note: 当前 thread goal 已清除。", rendered)
        self.assertIn("goal: （无）", rendered)

    def test_send_thread_image_reports_partial_delivery(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "local_path": "/tmp/generated.png",
            "delivered_binding_ids": ["p2p:ou_user:chat-1"],
            "failed_binding_ids": ["p2p:ou_other:chat-2"],
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with redirect_stdout(stdout):
                result = _send_thread_image(
                    Path("/tmp/instance-data"),
                    {"thread_id": "thread-1"},
                    local_path="/tmp/generated.png",
                    instance_name="explorer",
                )

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("delivered bindings: p2p:ou_user:chat-1", rendered)
        self.assertIn("failed bindings: p2p:ou_other:chat-2", rendered)
