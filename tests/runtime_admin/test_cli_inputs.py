import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.runtime_admin.cli_inputs import (
    build_runtime_admin_parser,
    image_send_target_params,
    prompt_text_from_args,
    thread_archive_inputs,
    thread_delete_input,
    thread_target_params,
    thread_unarchive_inputs,
)
from bot.version import __version__


class RuntimeAdminCliInputTests(unittest.TestCase):
    def test_top_level_help_includes_operator_guidance(self) -> None:
        parser = build_runtime_admin_parser()
        rendered = parser.format_help()

        self.assertIn("本地查看 / 管理面", rendered)
        self.assertIn("不是第二个 Codex 前端", rendered)
        self.assertIn("命令都可加 `--instance <name>`", rendered)
        self.assertIn("binding clear", rendered)
        self.assertIn("常用命令:", rendered)
        self.assertIn("thread archive --thread-name demo", rendered)
        self.assertIn("thread goal --thread-id <id>", rendered)
        self.assertIn("prompt send --binding-id <binding_id>", rendered)
        self.assertIn("thread archive --thread-id <id-1> --thread-id <id-2>", rendered)
        self.assertIn("thread list --archived --scope global", rendered)
        self.assertIn("thread unarchive --thread-id <id-1> --thread-id <id-2>", rendered)
        self.assertIn("thread delete --thread-id <id> --force", rendered)
        self.assertIn("thread clear-archived-bindings --thread-id <id> --dry-run", rendered)
        self.assertIn("focusctl web open", rendered)

    def test_top_level_version_prints_project_version(self) -> None:
        parser = build_runtime_admin_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"focusctl {__version__}")

    def test_web_help_distinguishes_local_bootstrap_from_external_origin(self) -> None:
        parser = build_runtime_admin_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["web", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("本机或 SSH local forwarding", rendered)
        self.assertIn("configured trusted HTTPS proxy", rendered)
        self.assertIn("直接打开其 HTTPS origin", rendered)
        self.assertIn("`web open` 不会输出 external URL", rendered)

        open_stdout = io.StringIO()
        with redirect_stdout(open_stdout):
            with self.assertRaises(SystemExit) as open_exc:
                parser.parse_args(["web", "open", "--help"])

        self.assertEqual(open_exc.exception.code, 0)
        open_help = open_stdout.getvalue()
        self.assertIn("本机或 SSH local forwarding", open_help)
        self.assertIn("configured trusted HTTPS proxy", open_help)
        self.assertIn("不会输出 external URL", open_help)

    def test_thread_help_includes_scope_and_selector_guidance(self) -> None:
        parser = build_runtime_admin_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["thread", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("Thread 管理面", rendered)
        self.assertIn("`list` 默认列当前目录线程", rendered)
        self.assertIn("显式指定目标 thread", rendered)
        self.assertIn("thread commands", rendered)
        self.assertIn("goal", rendered)
        self.assertIn("archive", rendered)
        self.assertIn("clear-archived-bindings", rendered)
        self.assertIn("detach", rendered)
        self.assertIn("attach", rendered)
        self.assertIn("persisted thread", rendered)

    def test_binding_help_includes_clear_semantics(self) -> None:
        parser = build_runtime_admin_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["binding", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("Binding 管理面", rendered)
        self.assertIn("Feishu 本地 binding 记录", rendered)
        self.assertIn("binding-local 设置", rendered)
        self.assertIn("不等于 `detach`", rendered)

    def test_binding_clear_accepts_binding_id(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["binding", "clear", "p2p:ou_user:chat-1"])

        self.assertEqual(args.binding_id, "p2p:ou_user:chat-1")

    def test_binding_clear_all_accepts_no_args(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["binding", "clear-all"])

        self.assertEqual(args.resource, "binding")
        self.assertEqual(args.action, "clear-all")

    def test_binding_clear_stale_accepts_dry_run(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["binding", "clear-stale", "--dry-run"])

        self.assertEqual(args.resource, "binding")
        self.assertEqual(args.action, "clear-stale")
        self.assertTrue(args.dry_run)

    def test_thread_status_accepts_explicit_thread_id(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "status", "--thread-id", "thread-1"])

        self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_status_accepts_explicit_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "status", "--thread-name", "demo"])

        self.assertEqual(thread_target_params(args), {"thread_name": "demo"})

    def test_thread_status_requires_explicit_selector(self) -> None:
        parser = build_runtime_admin_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status"])

    def test_thread_status_rejects_both_selectors(self) -> None:
        parser = build_runtime_admin_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status", "--thread-id", "thread-1", "--thread-name", "demo"])

    def test_thread_bindings_accepts_explicit_thread_id(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-id", "thread-1"])

        self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_bindings_accepts_explicit_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-name", "demo"])

        self.assertEqual(thread_target_params(args), {"thread_name": "demo"})

    def test_thread_goal_defaults_to_show(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "goal", "--thread-id", "thread-1"])

        self.assertEqual(args.goal_action, "show")
        self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_goal_show_accepts_explicit_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "goal", "show", "--thread-name", "demo"])

        self.assertEqual(args.goal_action, "show")
        self.assertEqual(thread_target_params(args), {"thread_name": "demo"})

    def test_thread_goal_set_accepts_objective_and_status(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(
            [
                "thread",
                "goal",
                "set",
                "--thread-id",
                "thread-1",
                "--objective",
                "ship goal support",
                "--status",
                "paused",
            ]
        )

        self.assertEqual(args.goal_action, "set")
        self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})
        self.assertEqual(args.objective, "ship goal support")
        self.assertEqual(args.status, "paused")

    def test_thread_goal_set_only_accepts_active_and_paused(self) -> None:
        parser = build_runtime_admin_parser()

        for status in ("active", "paused"):
            args = parser.parse_args(
                [
                    "thread",
                    "goal",
                    "set",
                    "--thread-id",
                    "thread-1",
                    "--status",
                    status,
                ]
            )
            self.assertEqual(args.goal_action, "set")
            self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})
            self.assertEqual(args.status, status)

    def test_thread_goal_set_rejects_removed_terminal_statuses(self) -> None:
        parser = build_runtime_admin_parser()

        for status in ("blocked", "usageLimited", "budgetLimited", "complete"):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "thread",
                        "goal",
                        "set",
                        "--thread-id",
                        "thread-1",
                        "--status",
                        status,
                    ]
                )

    def test_thread_goal_removed_pause_and_resume_subcommands_are_rejected(self) -> None:
        parser = build_runtime_admin_parser()

        for subcommand in ("pause", "resume"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["thread", "goal", subcommand, "--thread-id", "thread-1"])

    def test_thread_list_defaults_to_cwd_scope(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "list"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "list")
        self.assertEqual(args.scope, "cwd")
        self.assertEqual(args.cwd, "")

    def test_thread_list_accepts_global_scope_and_explicit_cwd(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "list", "--scope", "global", "--cwd", "/tmp/project"])

        self.assertEqual(args.scope, "global")
        self.assertEqual(args.cwd, "/tmp/project")

    def test_thread_list_accepts_archived_inventory(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "list", "--archived", "--scope", "global"])

        self.assertTrue(args.archived)
        self.assertEqual(args.scope, "global")

    def test_thread_unarchive_accepts_repeated_ids_and_delete_accepts_one_id(self) -> None:
        parser = build_runtime_admin_parser()

        unarchive = parser.parse_args(
            ["thread", "unarchive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )
        delete = parser.parse_args(["thread", "delete", "--thread-id", "thread-1", "--force"])

        self.assertEqual(thread_unarchive_inputs(unarchive), ["thread-1", "thread-2"])
        self.assertEqual(thread_delete_input(delete), "thread-1")
        self.assertTrue(delete.force)
        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "unarchive", "--thread-name", "demo"])

    def test_thread_delete_rejects_repeated_thread_ids(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(
            ["thread", "delete", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )

        with self.assertRaisesRegex(ValueError, "只允许提供一个.*--thread-id"):
            thread_delete_input(args)

    def test_thread_unarchive_help_points_to_archived_inventory(self) -> None:
        parser = build_runtime_admin_parser()
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["thread", "unarchive", "--help"])

        self.assertEqual(exc.exception.code, 0)
        self.assertIn(
            "focusctl thread list --archived --scope global",
            stdout.getvalue(),
        )
        self.assertIn("可重复提供 `--thread-id`", stdout.getvalue())

    def test_thread_detach_accepts_explicit_thread_id(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "detach", "--thread-id", "thread-1"])

        self.assertEqual(thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_detach_accepts_explicit_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "detach", "--thread-name", "demo"])

        self.assertEqual(thread_target_params(args), {"thread_name": "demo"})

    def test_thread_archive_accepts_explicit_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "archive", "--thread-name", "demo"])

        self.assertEqual(thread_archive_inputs(args), ([], "demo"))

    def test_thread_archive_rejects_repeated_thread_names(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(
            ["thread", "archive", "--thread-name", "demo-a", "--thread-name", "demo-b"]
        )

        with self.assertRaisesRegex(ValueError, "只允许提供一个.*--thread-name"):
            thread_archive_inputs(args)

    def test_thread_archive_accepts_repeated_thread_ids(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(
            ["thread", "archive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )

        self.assertEqual(args.thread_ids, ["thread-1", "thread-2"])

    def test_thread_clear_archived_bindings_accepts_thread_id_and_dry_run(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "clear-archived-bindings", "--thread-id", "thread-1", "--dry-run"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "clear-archived-bindings")
        self.assertEqual(args.thread_id, "thread-1")
        self.assertTrue(args.dry_run)

    def test_thread_clear_archived_bindings_accepts_all_and_dry_run(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "clear-archived-bindings", "--all", "--dry-run"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "clear-archived-bindings")
        self.assertTrue(args.all_archived)
        self.assertTrue(args.dry_run)

    def test_thread_clear_archived_bindings_rejects_missing_target(self) -> None:
        parser = build_runtime_admin_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "clear-archived-bindings"])

    def test_thread_clear_archived_bindings_rejects_thread_id_and_all(self) -> None:
        parser = build_runtime_admin_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "clear-archived-bindings", "--thread-id", "thread-1", "--all"])

    def test_image_send_accepts_explicit_thread_selector_and_path(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["image", "send", "--path", "./diagram.png", "--thread-id", "thread-1"])

        self.assertEqual(args.resource, "image")
        self.assertEqual(args.action, "send")
        self.assertEqual(args.path, "./diagram.png")
        self.assertEqual(image_send_target_params(args), ({"thread_id": "thread-1"}, "thread-1"))

    def test_image_send_falls_back_to_codex_thread_id_env(self) -> None:
        parser = build_runtime_admin_parser()

        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env-1"}, clear=False):
            args = parser.parse_args(["image", "send", "--path", "./diagram.png"])
            params, preferred_thread_id = image_send_target_params(args)

        self.assertEqual(params, {"thread_id": "thread-env-1"})
        self.assertEqual(preferred_thread_id, "thread-env-1")

    def test_image_send_requires_selector_when_env_missing(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(["image", "send", "--path", "./diagram.png"])

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "CODEX_THREAD_ID"):
                image_send_target_params(args)

    def test_prompt_send_accepts_inline_text(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(
            ["prompt", "send", "--binding-id", "p2p:ou_user:chat-1", "--text", "继续执行"]
        )

        self.assertEqual(args.resource, "prompt")
        self.assertEqual(args.action, "send")
        self.assertEqual(args.binding_id, "p2p:ou_user:chat-1")
        self.assertEqual(prompt_text_from_args(args), "继续执行")

    def test_prompt_send_reads_text_file(self) -> None:
        parser = build_runtime_admin_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            prompt_file = Path(tmpdir) / "prompt.txt"
            prompt_file.write_text("继续执行\n", encoding="utf-8")

            args = parser.parse_args(
                [
                    "prompt",
                    "send",
                    "--binding-id",
                    "p2p:ou_user:chat-1",
                    "--text-file",
                    str(prompt_file),
                ]
            )

            self.assertEqual(prompt_text_from_args(args), "继续执行\n")

    def test_parser_accepts_global_instance_selector(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["--instance", "corp-b", "service", "reset-backend"])

        self.assertEqual(args.instance, "corp-b")
        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")

    def test_service_reset_backend_accepts_without_force(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["service", "reset-backend"])

        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")
        self.assertFalse(args.force)

    def test_service_reset_backend_accepts_force_flag(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["service", "reset-backend", "--force"])

        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")
        self.assertTrue(args.force)
