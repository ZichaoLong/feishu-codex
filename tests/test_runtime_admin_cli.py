import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

from bot.adapters.base import ThreadSummary
from bot.runtime_admin_cli import (
    _archive_thread,
    _archive_threads,
    _attach_service,
    _build_thread_presence_checker,
    _build_parser,
    _binding_list_refresh_target_count,
    _binding_list_refresh_target_resolution_counts,
    _binding_list_refresh_timeout_seconds,
    _cleanup_archived_thread_bindings_in_scope,
    _cleanup_archived_thread_bindings_in_other_instances,
    _clear_archived_thread_bindings,
    _clear_archived_thread_bindings_from_store,
    _clear_stale_bindings,
    _clear_stale_bindings_from_store,
    _confirm_delete_thread,
    _delete_thread,
    _thread_delete_input,
    _list_archived_thread_ids_from_running_instance,
    _lifecycle_control_timeout_seconds,
    _clear_thread_goal,
    _image_send_target_params,
    _print_binding_list,
    _print_thread_goal,
    _print_thread_list,
    _prompt_text_from_args,
    _print_binding_status,
    _render_table,
    _send_thread_image,
    _send_binding_prompt,
    _set_thread_goal,
    _resolve_thread_archive_name,
    _resolve_thread_archive_target,
    _resolve_thread_archive_targets,
    _remote_adapter,
    _terminal_display_width,
    _thread_archive_inputs,
    _thread_unarchive_inputs,
    _print_thread_status,
    _thread_target_params,
    _unarchive_thread,
    _unarchive_threads,
    main as runtime_admin_cli_main,
)
from bot.codex_protocol.client import CodexRpcError
from bot.instance_resolution import CliInstanceTarget
from bot.service_control_plane import (
    ServiceControlError,
    ServiceControlOutcomeUnknownError,
    ServiceControlResponseTimeoutError,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.service_instance_lease import ServiceInstanceLease, ServiceInstanceMaintenanceLeaseError
from bot.version import __version__


class RuntimeAdminCliTests(unittest.TestCase):
    def _visual_cell_starts(self, line: str, cells: list[str]) -> list[int]:
        starts: list[int] = []
        offset = 0
        for cell in cells:
            start = line.find(cell, offset)
            self.assertNotEqual(start, -1)
            starts.append(_terminal_display_width(line[:start]))
            offset = start + len(cell)
        return starts

    def test_top_level_help_includes_operator_guidance(self) -> None:
        parser = _build_parser()
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

    def test_top_level_version_prints_project_version(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"focusctl {__version__}")

    def test_thread_help_includes_scope_and_selector_guidance(self) -> None:
        parser = _build_parser()
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
        parser = _build_parser()
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
        parser = _build_parser()

        args = parser.parse_args(["binding", "clear", "p2p:ou_user:chat-1"])

        self.assertEqual(args.binding_id, "p2p:ou_user:chat-1")

    def test_binding_clear_all_accepts_no_args(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["binding", "clear-all"])

        self.assertEqual(args.resource, "binding")
        self.assertEqual(args.action, "clear-all")

    def test_binding_clear_stale_accepts_dry_run(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["binding", "clear-stale", "--dry-run"])

        self.assertEqual(args.resource, "binding")
        self.assertEqual(args.action, "clear-stale")
        self.assertTrue(args.dry_run)

    def test_thread_status_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "status", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_status_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "status", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_status_requires_explicit_selector(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status"])

    def test_thread_status_rejects_both_selectors(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "status", "--thread-id", "thread-1", "--thread-name", "demo"])

    def test_thread_bindings_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_bindings_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "bindings", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_goal_defaults_to_show(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "goal", "--thread-id", "thread-1"])

        self.assertEqual(args.goal_action, "show")
        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_goal_show_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "goal", "show", "--thread-name", "demo"])

        self.assertEqual(args.goal_action, "show")
        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_goal_set_accepts_objective_and_status(self) -> None:
        parser = _build_parser()

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
        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})
        self.assertEqual(args.objective, "ship goal support")
        self.assertEqual(args.status, "paused")

    def test_thread_goal_set_only_accepts_active_and_paused(self) -> None:
        parser = _build_parser()

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
            self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})
            self.assertEqual(args.status, status)

    def test_thread_goal_set_rejects_removed_terminal_statuses(self) -> None:
        parser = _build_parser()

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
        parser = _build_parser()

        for subcommand in ("pause", "resume"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["thread", "goal", subcommand, "--thread-id", "thread-1"])

    def test_thread_list_defaults_to_cwd_scope(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "list"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "list")
        self.assertEqual(args.scope, "cwd")
        self.assertEqual(args.cwd, "")

    def test_thread_list_accepts_global_scope_and_explicit_cwd(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "list", "--scope", "global", "--cwd", "/tmp/project"])

        self.assertEqual(args.scope, "global")
        self.assertEqual(args.cwd, "/tmp/project")

    def test_thread_list_accepts_archived_inventory(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "list", "--archived", "--scope", "global"])

        self.assertTrue(args.archived)
        self.assertEqual(args.scope, "global")

    def test_thread_unarchive_accepts_repeated_ids_and_delete_accepts_one_id(self) -> None:
        parser = _build_parser()

        unarchive = parser.parse_args(
            ["thread", "unarchive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )
        delete = parser.parse_args(["thread", "delete", "--thread-id", "thread-1", "--force"])

        self.assertEqual(_thread_unarchive_inputs(unarchive), ["thread-1", "thread-2"])
        self.assertEqual(_thread_delete_input(delete), "thread-1")
        self.assertTrue(delete.force)
        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "unarchive", "--thread-name", "demo"])

    def test_thread_delete_rejects_repeated_thread_ids(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["thread", "delete", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )

        with self.assertRaisesRegex(ValueError, "只允许提供一个.*--thread-id"):
            _thread_delete_input(args)

    def test_main_thread_unarchive_passes_all_ids_to_batch_handler(self) -> None:
        with patch("bot.runtime_admin_cli._unarchive_threads", return_value=0) as mock_unarchive:
            with self.assertRaises(SystemExit) as exc:
                runtime_admin_cli_main(
                    [
                        "--instance",
                        "explorer",
                        "thread",
                        "unarchive",
                        "--thread-id",
                        "thread-1",
                        "--thread-id",
                        "thread-2",
                    ]
                )

        self.assertEqual(exc.exception.code, 0)
        mock_unarchive.assert_called_once_with(
            ["thread-1", "thread-2"],
            explicit_instance="explorer",
        )

    def test_main_thread_delete_rejects_repeated_ids_before_target_resolution(self) -> None:
        stderr = io.StringIO()
        with patch("bot.runtime_admin_cli._resolve_target_instance") as mock_resolve:
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exc:
                    runtime_admin_cli_main(
                        [
                            "thread",
                            "delete",
                            "--thread-id",
                            "thread-1",
                            "--thread-id",
                            "thread-2",
                            "--force",
                        ]
                    )

        self.assertEqual(exc.exception.code, 2)
        mock_resolve.assert_not_called()
        self.assertIn("只允许提供一个 `--thread-id`", stderr.getvalue())

    def test_thread_unarchive_help_points_to_archived_inventory(self) -> None:
        parser = _build_parser()
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

    def test_remote_adapter_prefers_running_instance_resolution(self) -> None:
        entry = InstanceRegistryEntry(
            instance_name="aft",
            owner_pid=1234,
            service_token="token-aft",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-aft",
            data_dir="/tmp/data-aft",
            started_at=1.0,
            updated_at=1.0,
        )
        with patch(
            "bot.runtime_admin_cli.load_config_file",
            return_value={"app_server_url": "ws://127.0.0.1:8765"},
        ) as mock_load_config:
            with patch(
                "bot.runtime_admin_cli.resolve_running_instance_app_server_url",
                return_value="ws://127.0.0.1:43210",
            ) as mock_resolve:
                adapter, _, app_server_url = _remote_adapter(Path("/tmp/data-aft"), running_entry=entry)

        self.assertEqual(app_server_url, "ws://127.0.0.1:43210")
        mock_load_config.assert_called_once_with("codex", directory=Path("/tmp/config-aft"))
        self.assertEqual(mock_resolve.call_args.args[0], entry)
        self.assertEqual(mock_resolve.call_args.kwargs["configured_app_server_url"], "ws://127.0.0.1:8765")
        adapter.stop()

    def test_main_thread_list_passes_running_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            entry = InstanceRegistryEntry(
                instance_name="aft",
                owner_pid=1234,
                service_token="token-aft",
                control_endpoint="tcp://127.0.0.1:9000",
                app_server_url="ws://127.0.0.1:8765",
                config_dir="/tmp/config-aft",
                data_dir=tmpdir,
                started_at=1.0,
                updated_at=1.0,
            )
            target = CliInstanceTarget(
                instance_name="aft",
                data_dir=Path(tmpdir),
                running_entry=entry,
            )
            with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
                with patch("bot.runtime_admin_cli._print_thread_list", return_value=0) as mock_print:
                    with patch(
                        "bot.runtime_admin_cli.sys.argv",
                        ["focusctl", "--instance", "aft", "thread", "list"],
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_print.call_args.kwargs["scope"], "cwd")
        self.assertEqual(mock_print.call_args.kwargs["running_entry"], entry)

    def test_lifecycle_timeout_uses_running_instance_config_dir(self) -> None:
        entry = InstanceRegistryEntry(
            instance_name="aft",
            owner_pid=1234,
            service_token="token-aft",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-aft",
            data_dir="/tmp/data-aft",
            started_at=1.0,
            updated_at=1.0,
        )
        with patch(
            "bot.runtime_admin_cli.load_config_file",
            return_value={
                "request_timeout_seconds": 47,
                "connect_timeout_seconds": 11,
            },
        ) as mock_load_config:
            unarchive_timeout = _lifecycle_control_timeout_seconds(
                Path("/tmp/data-aft"),
                operation="unarchive",
                running_entry=entry,
            )
            archive_timeout = _lifecycle_control_timeout_seconds(
                Path("/tmp/data-aft"),
                operation="archive",
                running_entry=entry,
            )
            delete_timeout = _lifecycle_control_timeout_seconds(
                Path("/tmp/data-aft"),
                operation="delete",
                running_entry=entry,
            )

        self.assertEqual(unarchive_timeout, 121.0)
        self.assertEqual(archive_timeout, 168.0)
        self.assertEqual(delete_timeout, 168.0)
        self.assertEqual(mock_load_config.call_count, 3)
        for call_args in mock_load_config.call_args_list:
            self.assertEqual(call_args.args, ("codex",))
            self.assertEqual(call_args.kwargs, {"directory": Path("/tmp/config-aft")})

    def test_main_thread_goal_show_dispatches_to_goal_printer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = CliInstanceTarget(
                instance_name="aft",
                data_dir=Path(tmpdir),
            )
            with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
                with patch("bot.runtime_admin_cli._print_thread_goal", return_value=0) as mock_print:
                    with patch(
                        "bot.runtime_admin_cli.sys.argv",
                        ["focusctl", "--instance", "aft", "thread", "goal", "--thread-id", "thread-1"],
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_print.call_args.args[0], Path(tmpdir))
        self.assertEqual(mock_print.call_args.args[1], {"thread_id": "thread-1"})
        self.assertEqual(mock_print.call_args.kwargs["instance_name"], "aft")

    def test_thread_detach_accepts_explicit_thread_id(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "detach", "--thread-id", "thread-1"])

        self.assertEqual(_thread_target_params(args), {"thread_id": "thread-1"})

    def test_thread_detach_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "detach", "--thread-name", "demo"])

        self.assertEqual(_thread_target_params(args), {"thread_name": "demo"})

    def test_thread_archive_accepts_explicit_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "archive", "--thread-name", "demo"])

        self.assertEqual(_thread_archive_inputs(args), ([], "demo"))

    def test_thread_archive_rejects_repeated_thread_names(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["thread", "archive", "--thread-name", "demo-a", "--thread-name", "demo-b"]
        )

        with self.assertRaisesRegex(ValueError, "只允许提供一个.*--thread-name"):
            _thread_archive_inputs(args)

    def test_thread_archive_accepts_repeated_thread_ids(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(
            ["thread", "archive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )

        self.assertEqual(args.thread_ids, ["thread-1", "thread-2"])

    def test_thread_archive_rejects_mixing_thread_id_and_thread_name(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "archive", "--thread-id", "thread-1", "--thread-name", "demo"])

        with self.assertRaisesRegex(ValueError, "不能同时提供"):
            _resolve_thread_archive_targets(args)

    def test_thread_clear_archived_bindings_accepts_thread_id_and_dry_run(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "clear-archived-bindings", "--thread-id", "thread-1", "--dry-run"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "clear-archived-bindings")
        self.assertEqual(args.thread_id, "thread-1")
        self.assertTrue(args.dry_run)

    def test_thread_clear_archived_bindings_accepts_all_and_dry_run(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["thread", "clear-archived-bindings", "--all", "--dry-run"])

        self.assertEqual(args.resource, "thread")
        self.assertEqual(args.action, "clear-archived-bindings")
        self.assertTrue(args.all_archived)
        self.assertTrue(args.dry_run)

    def test_thread_clear_archived_bindings_rejects_missing_target(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "clear-archived-bindings"])

    def test_thread_clear_archived_bindings_rejects_thread_id_and_all(self) -> None:
        parser = _build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["thread", "clear-archived-bindings", "--thread-id", "thread-1", "--all"])

    def test_image_send_accepts_explicit_thread_selector_and_path(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["image", "send", "--path", "./diagram.png", "--thread-id", "thread-1"])

        self.assertEqual(args.resource, "image")
        self.assertEqual(args.action, "send")
        self.assertEqual(args.path, "./diagram.png")
        self.assertEqual(_image_send_target_params(args), ({"thread_id": "thread-1"}, "thread-1"))

    def test_image_send_falls_back_to_codex_thread_id_env(self) -> None:
        parser = _build_parser()

        with patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-env-1"}, clear=False):
            args = parser.parse_args(["image", "send", "--path", "./diagram.png"])
            params, preferred_thread_id = _image_send_target_params(args)

        self.assertEqual(params, {"thread_id": "thread-env-1"})
        self.assertEqual(preferred_thread_id, "thread-env-1")

    def test_image_send_requires_selector_when_env_missing(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["image", "send", "--path", "./diagram.png"])

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "CODEX_THREAD_ID"):
                _image_send_target_params(args)

    def test_prompt_send_accepts_inline_text(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(
            ["prompt", "send", "--binding-id", "p2p:ou_user:chat-1", "--text", "继续执行"]
        )

        self.assertEqual(args.resource, "prompt")
        self.assertEqual(args.action, "send")
        self.assertEqual(args.binding_id, "p2p:ou_user:chat-1")
        self.assertEqual(_prompt_text_from_args(args), "继续执行")

    def test_prompt_send_reads_text_file(self) -> None:
        parser = _build_parser()
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

            self.assertEqual(_prompt_text_from_args(args), "继续执行\n")

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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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

    def test_parser_accepts_global_instance_selector(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["--instance", "corp-b", "service", "reset-backend"])

        self.assertEqual(args.instance, "corp-b")
        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")

    def test_main_rejects_explicit_uncreated_named_instance(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(root / "config"),
                    "FOCUS_DATA_ROOT": str(root / "data"),
                    "FOCUS_INSTANCE": "",
                },
                clear=False,
            ):
                with patch(
                    "bot.runtime_admin_cli.sys.argv",
                    ["focusctl", "--instance", "ghost", "service", "reset-backend"],
                ):
                    with patch("bot.runtime_admin_cli.sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("instance create ghost", stderr.getvalue())

    def test_service_reset_backend_accepts_without_force(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["service", "reset-backend"])

        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")
        self.assertFalse(args.force)

    def test_service_reset_backend_accepts_force_flag(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["service", "reset-backend", "--force"])

        self.assertEqual(args.resource, "service")
        self.assertEqual(args.action, "reset-backend")
        self.assertTrue(args.force)

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
        with patch("bot.runtime_admin_cli._remote_adapter", return_value=(_FakeAdapter(), {"thread_list_query_limit": 100}, "ws://127.0.0.1:8765")):
            with patch("bot.runtime_admin_cli.list_global_threads", return_value=threads):
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
        with patch("bot.runtime_admin_cli._remote_adapter", return_value=(_FakeAdapter(), {"thread_list_query_limit": 100}, "ws://127.0.0.1:8765")):
            with patch("bot.runtime_admin_cli.list_global_threads", return_value=threads):
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
            "bot.runtime_admin_cli._remote_adapter",
            return_value=(_FakeAdapter(), {"thread_list_query_limit": 100}, "ws://127.0.0.1:8765"),
        ):
            with patch("bot.runtime_admin_cli.list_global_threads", return_value=[]) as mock_list:
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
            patch("bot.runtime_admin_cli._request", side_effect=[snapshot, snapshot]) as request,
            patch("bot.runtime_admin_cli.load_system_config_raw", return_value={"request_timeout_seconds": 2}),
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

        with patch("bot.runtime_admin_cli.load_system_config_raw", return_value={"request_timeout_seconds": 4}):
            self.assertEqual(_binding_list_refresh_timeout_seconds(bindings), 11.0)

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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot) as mock_request:
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
        with patch("bot.runtime_admin_cli._request", side_effect=[paused, cleared]):
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
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
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

    def test_archive_thread_cleans_other_instances_after_archive(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": ["p2p:ou_user:chat-1"],
            "cleanup_errors": [],
        }
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        calls: list[tuple[Path, str, dict[str, object]]] = []

        def _fake_request(
            data_dir: Path,
            method: str,
            params: dict[str, object],
            *,
            timeout_seconds: float = 3.0,
        ):
            del timeout_seconds
            calls.append((data_dir, method, params))
            if method == "thread/archive":
                return snapshot
            self.assertEqual(method, "thread/clear-archived-bindings")
            self.assertEqual(params, {"thread_id": "thread-1", "dry_run": False})
            return {"thread_id": "thread-1", "cleared_binding_ids": ["p2p:ou_other:chat-2"]}

        with patch("bot.runtime_admin_cli._request", side_effect=_fake_request):
            with patch("bot.runtime_admin_cli.list_running_instances", return_value=[explorer_entry]):
                with patch("bot.runtime_admin_cli.list_known_instance_names", return_value=["default", "explorer"]):
                    with redirect_stdout(stdout):
                        result = _archive_thread(
                            Path("/tmp/default-data"),
                            {"thread_id": "thread-1"},
                            instance_name="default",
                        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [(str(data_dir), method) for data_dir, method, _params in calls],
            [
                ("/tmp/default-data", "thread/archive"),
                ("/tmp/explorer-data", "thread/clear-archived-bindings"),
            ],
        )
        rendered = stdout.getvalue()
        self.assertIn("instance: default", rendered)
        self.assertIn("cleared bindings in this instance: p2p:ou_user:chat-1", rendered)
        self.assertIn("explorer (control-plane): p2p:ou_other:chat-2", rendered)
        self.assertIn("其他可达运行实例与已知非运行实例", rendered)

    def test_archive_thread_reports_cleanup_failure(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin_cli._request", return_value=snapshot):
            with patch(
                "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_other_instances",
                return_value=(
                    [],
                    [{"instance_name": "explorer", "mode": "control-plane", "reason": "down"}],
                ),
            ):
                with redirect_stdout(stdout):
                    result = _archive_thread(
                        Path("/tmp/instance-data"),
                        {"thread_id": "thread-1"},
                        instance_name="explorer",
                    )

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("cleanup warnings:", rendered)
        self.assertIn("explorer (control-plane): down", rendered)
        self.assertIn("已尝试清理其他可达运行实例", rendered)
        self.assertNotIn("已同时清理其他可达运行实例", rendered)

    def test_archive_thread_rejects_unresolved_name_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "只接受已解析的 thread_id"):
            _archive_thread(
                Path("/tmp/default-data"),
                {"thread_name": "demo"},
                instance_name="default",
            )

    def test_archive_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch("bot.runtime_admin_cli._request", return_value={}):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_archive_thread_rejects_non_string_cleanup_items_as_unknown(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [1],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin_cli._request", return_value=result_payload):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())

    def test_archive_thread_rejects_invalid_outcome_cleanup_combination(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "skipped",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin_cli._request", return_value=result_payload):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("focus_cleanup", stdout.getvalue())

    def test_cleanup_archived_thread_bindings_clears_stopped_known_instance_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "explorer-data"
            store = ChatBindingStore(data_dir)
            binding = ("ou_user", "chat-1")
            store.save(
                binding,
                {
                    "working_dir": "/tmp/project",
                    "current_thread_id": "thread-1",
                    "current_thread_title": "demo",
                    "feishu_runtime_state": "detached",
                    "approval_policy": "never",
                    "permissions_profile_id": ":danger-full-access",
                    "model": "",
                    "reasoning_effort": "",
                },
            )
            with patch("bot.runtime_admin_cli.list_running_instances", return_value=[]):
                with patch("bot.runtime_admin_cli.list_known_instance_names", return_value=["default", "explorer"]):
                    with patch(
                        "bot.runtime_admin_cli.resolve_instance_paths",
                        return_value=CliInstanceTarget(instance_name="explorer", data_dir=data_dir),
                    ):
                        cleanup_results, cleanup_failures = (
                            _cleanup_archived_thread_bindings_in_other_instances(
                                "thread-1",
                                target_instance_name="default",
                                target_data_dir=Path("/tmp/default-data"),
                            )
                        )
            self.assertEqual(ChatBindingStore(data_dir).load(binding), None)

        self.assertEqual(cleanup_failures, [])
        self.assertEqual(
            cleanup_results,
            [
                {
                    "instance_name": "explorer",
                    "mode": "local-store",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ],
        )

    def test_cleanup_archived_thread_bindings_dry_run_does_not_clear_stopped_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "explorer-data"
            store = ChatBindingStore(data_dir)
            binding = ("ou_user", "chat-1")
            store.save(
                binding,
                {
                    "working_dir": "/tmp/project",
                    "current_thread_id": "thread-1",
                    "current_thread_title": "demo",
                    "feishu_runtime_state": "detached",
                    "approval_policy": "never",
                    "permissions_profile_id": ":danger-full-access",
                    "model": "",
                    "reasoning_effort": "",
                },
            )

            cleared = _clear_archived_thread_bindings_from_store(data_dir, "thread-1", dry_run=True)

            self.assertEqual(cleared, ["p2p:ou_user:chat-1"])
            self.assertIsNotNone(store.load(binding))

    def test_cleanup_archived_thread_bindings_scope_explicit_instance_only(self) -> None:
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=explorer_entry,
        )

        def _fake_request(data_dir: Path, method: str, params: dict[str, object]):
            self.assertEqual(data_dir, Path("/tmp/explorer-data"))
            self.assertEqual(method, "thread/clear-archived-bindings")
            self.assertEqual(params, {"thread_id": "thread-1", "dry_run": True})
            return {"thread_id": "thread-1", "would_clear_binding_ids": ["p2p:ou_user:chat-1"]}

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin_cli._request", side_effect=_fake_request):
                with patch("bot.runtime_admin_cli.list_running_instances") as mock_list_running:
                    cleanup_results, cleanup_failures = _cleanup_archived_thread_bindings_in_scope(
                        "thread-1",
                        explicit_instance="explorer",
                        dry_run=True,
                    )

        mock_list_running.assert_not_called()
        self.assertEqual(cleanup_failures, [])
        self.assertEqual(
            cleanup_results,
            [
                {
                    "instance_name": "explorer",
                    "mode": "control-plane",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ],
        )

    def test_clear_archived_thread_bindings_public_command_prints_dry_run(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_scope",
            return_value=(
                [
                    {
                        "instance_name": "explorer",
                        "mode": "local-store",
                        "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                    }
                ],
                [],
            ),
        ) as mock_cleanup:
            with redirect_stdout(stdout):
                result = _clear_archived_thread_bindings("thread-1", dry_run=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_cleanup.call_args.kwargs["explicit_instance"], "")
        self.assertTrue(mock_cleanup.call_args.kwargs["dry_run"])
        rendered = stdout.getvalue()
        self.assertIn("thread: thread-1", rendered)
        self.assertIn("scope: all known instances", rendered)
        self.assertIn("mode: dry-run", rendered)
        self.assertIn("would clear bindings:", rendered)
        self.assertIn("explorer (local-store): p2p:ou_user:chat-1", rendered)

    def test_clear_archived_thread_bindings_all_requires_running_instance_to_query(self) -> None:
        with patch("bot.runtime_admin_cli.list_running_instances", return_value=[]):
            with self.assertRaisesRegex(ValueError, "至少一个运行中的实例"):
                _clear_archived_thread_bindings(all_archived=True)

    def test_clear_archived_thread_bindings_all_rejects_stopped_explicit_instance(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with self.assertRaisesRegex(ValueError, "目标实例正在运行"):
                _clear_archived_thread_bindings(all_archived=True, explicit_instance="explorer")

    def test_clear_archived_thread_bindings_all_cleans_each_archived_thread(self) -> None:
        stdout = io.StringIO()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )

        def _fake_cleanup(thread_id: str, **kwargs):
            self.assertEqual(kwargs["explicit_instance"], "")
            self.assertTrue(kwargs["dry_run"])
            if thread_id == "thread-2":
                return [], []
            return [
                {
                    "instance_name": "explorer",
                    "mode": "local-store",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ], []

        with patch(
            "bot.runtime_admin_cli._resolve_archived_thread_listing_target",
            return_value=("explorer", Path("/tmp/explorer-data"), explorer_entry),
        ) as mock_resolve:
            with patch(
                "bot.runtime_admin_cli._list_archived_thread_ids_from_running_instance",
                return_value=["thread-2", "thread-1"],
            ) as mock_list_archived:
                with patch(
                    "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_scope",
                    side_effect=_fake_cleanup,
                ) as mock_cleanup:
                    with redirect_stdout(stdout):
                        result = _clear_archived_thread_bindings(all_archived=True, dry_run=True)

        self.assertEqual(result, 0)
        mock_resolve.assert_called_once_with("")
        self.assertEqual(mock_list_archived.call_args.kwargs["running_entry"], explorer_entry)
        self.assertEqual([call.args[0] for call in mock_cleanup.call_args_list], ["thread-2", "thread-1"])
        rendered = stdout.getvalue()
        self.assertIn("archived query instance: explorer", rendered)
        self.assertIn("archived threads: 2", rendered)
        self.assertIn("scope: all known instances", rendered)
        self.assertIn("thread: thread-1", rendered)
        self.assertIn("would clear bindings:", rendered)
        self.assertIn(
            "summary: archived_threads=2 threads_with_bindings=1 would_clear_bindings=1 cleanup_failed=0",
            rendered,
        )

    def test_list_archived_thread_ids_pages_with_archived_filter(self) -> None:
        class _PagedArchivedAdapter:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.stopped = False

            def list_threads(self, **kwargs):
                self.calls.append(kwargs)
                if kwargs.get("cursor") is None:
                    return [
                        ThreadSummary(
                            thread_id="thread-1",
                            cwd="/tmp/project",
                            name="demo 1",
                            preview="",
                            created_at=1,
                            updated_at=1,
                            source="cli",
                            status="notLoaded",
                        )
                    ], "cursor-1"
                return [
                    ThreadSummary(
                        thread_id="thread-2",
                        cwd="/tmp/project",
                        name="demo 2",
                        preview="",
                        created_at=2,
                        updated_at=2,
                        source="cli",
                        status="notLoaded",
                    )
                ], None

            def stop(self) -> None:
                self.stopped = True

        adapter = _PagedArchivedAdapter()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )

        with patch("bot.runtime_admin_cli._remote_adapter", return_value=(adapter, {}, "ws://127.0.0.1:9002")):
            archived_thread_ids = _list_archived_thread_ids_from_running_instance(
                Path("/tmp/explorer-data"),
                running_entry=explorer_entry,
            )

        self.assertEqual(archived_thread_ids, ["thread-1", "thread-2"])
        self.assertTrue(adapter.stopped)
        self.assertEqual(adapter.calls[0]["archived"], True)
        self.assertEqual(adapter.calls[0]["model_providers"], [])
        self.assertEqual(adapter.calls[1]["cursor"], "cursor-1")

    def test_clear_archived_thread_bindings_from_store_only_clears_matching_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            matched = ("ou_user", "chat-1")
            retained = ("ou_user", "chat-2")
            for binding, thread_id in ((matched, "thread-1"), (retained, "thread-2")):
                store.save(
                    binding,
                    {
                        "working_dir": "/tmp/project",
                        "current_thread_id": thread_id,
                        "current_thread_title": "demo",
                        "feishu_runtime_state": "detached",
                        "approval_policy": "never",
                        "permissions_profile_id": ":danger-full-access",
                        "model": "",
                        "reasoning_effort": "",
                    },
                )

            cleared = _clear_archived_thread_bindings_from_store(data_dir, "thread-1")

            self.assertEqual(cleared, ["p2p:ou_user:chat-1"])
            self.assertEqual(store.load(matched), None)
            self.assertIsNotNone(store.load(retained))

    def test_clear_archived_thread_bindings_from_store_keeps_marker_when_lease_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            binding = ("ou_user", "chat-1")
            store.save(
                binding,
                {
                    "working_dir": "/tmp/project",
                    "current_thread_id": "thread-1",
                    "current_thread_title": "demo",
                    "feishu_runtime_state": "detached",
                    "approval_policy": "never",
                    "permissions_profile_id": ":danger-full-access",
                    "model": "",
                    "reasoning_effort": "",
                },
            )

            with patch(
                "bot.runtime_admin_cli._release_offline_binding_interaction_lease",
                side_effect=OSError("lease cleanup failed"),
            ):
                with self.assertRaisesRegex(OSError, "lease cleanup failed"):
                    _clear_archived_thread_bindings_from_store(data_dir, "thread-1")

            self.assertIsNotNone(store.load(binding))

    def test_offline_binding_cleanup_refuses_running_service_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            service_lease = ServiceInstanceLease(data_dir)
            service_lease.acquire()
            self.addCleanup(service_lease.release)

            with self.assertRaises(ServiceInstanceMaintenanceLeaseError):
                _clear_archived_thread_bindings_from_store(data_dir, "thread-1")

    def test_clear_stale_bindings_from_store_only_clears_missing_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            stale = ("ou_user", "chat-stale")
            retained = ("ou_user", "chat-live")
            unknown = ("ou_user", "chat-unknown")
            for binding, thread_id in (
                (stale, "thread-stale"),
                (retained, "thread-live"),
                (unknown, "thread-unknown"),
            ):
                store.save(
                    binding,
                    {
                        "working_dir": "/tmp/project",
                        "current_thread_id": thread_id,
                        "current_thread_title": "demo",
                        "feishu_runtime_state": "detached",
                        "approval_policy": "never",
                        "permissions_profile_id": ":danger-full-access",
                        "model": "",
                        "reasoning_effort": "",
                    },
                )

            def _presence(thread_id: str):
                if thread_id == "thread-stale":
                    return "stale", "not found"
                if thread_id == "thread-unknown":
                    return "unknown", "timeout"
                return "present", ""

            result = _clear_stale_bindings_from_store(data_dir, _presence)

            self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:chat-stale"])
            self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
            self.assertEqual(result["unknown_threads"], [{"thread_id": "thread-unknown", "reason": "timeout"}])
            self.assertEqual(store.load(stale), None)
            self.assertIsNotNone(store.load(retained))
            self.assertIsNotNone(store.load(unknown))

    def test_clear_stale_bindings_from_store_keeps_marker_when_lease_release_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            binding = ("ou_user", "chat-stale")
            store.save(
                binding,
                {
                    "working_dir": "/tmp/project",
                    "current_thread_id": "thread-stale",
                    "current_thread_title": "demo",
                    "feishu_runtime_state": "detached",
                    "approval_policy": "never",
                    "permissions_profile_id": ":danger-full-access",
                    "model": "",
                    "reasoning_effort": "",
                },
            )

            with patch(
                "bot.runtime_admin_cli._release_offline_binding_interaction_lease",
                side_effect=OSError("lease cleanup failed"),
            ):
                with self.assertRaisesRegex(OSError, "lease cleanup failed"):
                    _clear_stale_bindings_from_store(
                        data_dir,
                        lambda _thread_id: ("stale", "not found"),
                    )

            self.assertIsNotNone(store.load(binding))

    def test_clear_stale_bindings_from_store_dry_run_does_not_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            store = ChatBindingStore(data_dir)
            binding = ("ou_user", "chat-stale")
            store.save(
                binding,
                {
                    "working_dir": "/tmp/project",
                    "current_thread_id": "thread-stale",
                    "current_thread_title": "demo",
                    "feishu_runtime_state": "detached",
                    "approval_policy": "never",
                    "permissions_profile_id": ":danger-full-access",
                    "model": "",
                    "reasoning_effort": "",
                },
            )

            result = _clear_stale_bindings_from_store(
                data_dir,
                lambda _thread_id: ("stale", "not found"),
                dry_run=True,
            )

            self.assertEqual(result["cleared_binding_ids"], [])
            self.assertEqual(result["would_clear_binding_ids"], ["p2p:ou_user:chat-stale"])
            self.assertIsNotNone(store.load(binding))

    def test_thread_presence_checker_uses_metadata_only_read_and_treats_not_loaded_as_stale(self) -> None:
        class _FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, bool]] = []
                self.stopped = False

            def read_thread(self, thread_id: str, *, include_turns: bool = False):
                self.calls.append((thread_id, include_turns))
                raise CodexRpcError("thread/read", {"message": f"thread not loaded: {thread_id}"})

            def stop(self) -> None:
                self.stopped = True

        adapter = _FakeAdapter()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )

        with patch("bot.runtime_admin_cli._remote_adapter", return_value=(adapter, {}, "ws://127.0.0.1:9002")):
            _adapter, check = _build_thread_presence_checker(
                Path("/tmp/explorer-data"),
                running_entry=explorer_entry,
            )
            result = check("thread-stale")

        self.assertIs(_adapter, adapter)
        self.assertEqual(result[0], "stale")
        self.assertEqual(adapter.calls, [("thread-stale", False)])

    def test_clear_stale_bindings_explicit_running_instance_uses_control_plane(self) -> None:
        stdout = io.StringIO()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=explorer_entry,
        )

        def _fake_request(data_dir: Path, method: str, params: dict[str, object]):
            self.assertEqual(data_dir, Path("/tmp/explorer-data"))
            self.assertEqual(method, "binding/clear-stale")
            self.assertEqual(params, {"dry_run": True})
            return {
                "would_clear_binding_ids": ["p2p:ou_user:chat-stale"],
                "stale_thread_ids": ["thread-stale"],
                "unknown_threads": [],
                "dry_run": True,
            }

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin_cli._request", side_effect=_fake_request):
                with redirect_stdout(stdout):
                    result = _clear_stale_bindings(explicit_instance="explorer", dry_run=True)

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("scope: explorer", rendered)
        self.assertIn("mode: dry-run", rendered)
        self.assertIn("explorer (control-plane)", rendered)
        self.assertIn("would clear stale bindings: p2p:ou_user:chat-stale", rendered)

    def test_clear_stale_bindings_returns_nonzero_when_threads_are_unknown(self) -> None:
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=explorer_entry,
        )

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin_cli._request",
                return_value={
                    "cleared_binding_ids": [],
                    "stale_thread_ids": [],
                    "unknown_threads": [{"thread_id": "thread-unknown", "reason": "lookup_error"}],
                },
            ):
                with redirect_stdout(io.StringIO()):
                    result = _clear_stale_bindings(explicit_instance="explorer")

        self.assertEqual(result, 1)

    def test_attach_service_treats_response_timeout_as_accepted(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin_cli._request",
            side_effect=ServiceControlResponseTimeoutError("控制面请求已发送，但等待响应超时：tcp://127.0.0.1:32001"),
        ) as mock_request:
            with redirect_stdout(stdout):
                result = _attach_service(Path("/tmp/focus-data"))

        self.assertEqual(result, 0)
        mock_request.assert_called_once_with(Path("/tmp/focus-data"), "service/attach", timeout_seconds=30.0)
        rendered = stdout.getvalue()
        self.assertIn("runtime attach: accepted", rendered)
        self.assertIn("waiting for result timed out", rendered)
        self.assertIn("后台可能仍在继续恢复推送", rendered)
        self.assertIn("focusctl binding list", rendered)

    def test_archive_threads_batches_partial_failures(self) -> None:
        stdout = io.StringIO()
        target_a = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        target_b = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        def _fake_request(
            data_dir: Path,
            method: str,
            params: dict[str, str],
            *,
            timeout_seconds: float = 3.0,
        ):
            del timeout_seconds
            self.assertEqual(method, "thread/archive")
            if params["thread_id"] == "thread-2":
                raise ServiceControlError("busy")
            return {
                "thread_id": params["thread_id"],
                "thread_title": "demo",
                "working_dir": str(data_dir),
                "upstream_outcome": "success",
                "focus_cleanup": "complete",
                "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                "cleanup_errors": [],
            }

        with patch("bot.runtime_admin_cli._lease_owner_instance", side_effect=["explorer", "default"]):
            with patch("bot.runtime_admin_cli._resolve_target_instance", side_effect=[target_a, target_b]):
                with patch("bot.runtime_admin_cli._request", side_effect=_fake_request):
                    with patch(
                        "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_other_instances",
                        return_value=([], []),
                    ):
                        with redirect_stdout(stdout):
                            result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("batch archive: total=2", rendered)
        self.assertIn("[1/2] thread: thread-1", rendered)
        self.assertIn("[2/2] thread: thread-2", rendered)
        self.assertIn("instance: explorer", rendered)
        self.assertIn("instance: default", rendered)
        self.assertIn("status: archived", rendered)
        self.assertIn("status: failed", rendered)
        self.assertIn("summary: archived=1 failed=1", rendered)

    def test_archive_threads_stops_immediately_on_unknown_outcome(self) -> None:
        stdout = io.StringIO()
        target = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        with patch("bot.runtime_admin_cli._lease_owner_instance", return_value="default"):
            with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
                with patch(
                    "bot.runtime_admin_cli._request",
                    side_effect=ServiceControlOutcomeUnknownError("response lost"),
                ) as mock_request:
                    with redirect_stdout(stdout):
                        result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 3)
        self.assertEqual(mock_request.call_count, 1)
        self.assertIn("status: unknown", stdout.getvalue())
        self.assertIn("batch 已停止", stdout.getvalue())

    def test_unarchive_thread_success_does_not_create_binding(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "skipped",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin_cli._validate_unarchive_binding_preflight") as mock_preflight:
            with patch("bot.runtime_admin_cli._request", return_value=result_payload) as mock_request:
                with redirect_stdout(stdout):
                    result = _unarchive_thread(
                        Path("/tmp/default-data"),
                        "thread-1",
                        instance_name="default",
                    )

        self.assertEqual(result, 0)
        mock_preflight.assert_called_once_with("thread-1")
        self.assertEqual(mock_request.call_args.args[:3], (
            Path("/tmp/default-data"),
            "thread/unarchive",
            {"thread_id": "thread-1"},
        ))
        rendered = stdout.getvalue()
        self.assertIn("恢复为未归档状态并回到常规列表", rendered)
        self.assertIn("当前仍未加载，也未创建 binding", rendered)
        self.assertIn("focus resume thread-1", rendered)
        self.assertIn("/resume thread-1", rendered)

    def test_unarchive_threads_runs_each_target_and_reports_summary(self) -> None:
        stdout = io.StringIO()
        running_entry = InstanceRegistryEntry(
            instance_name="default",
            owner_pid=1234,
            service_token="token-default",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-default",
            data_dir="/tmp/data-default",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="default",
            data_dir=Path("/tmp/data-default"),
            running_entry=running_entry,
        )

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin_cli._unarchive_thread", return_value=0) as mock_unarchive:
                with redirect_stdout(stdout):
                    result = _unarchive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 0)
        self.assertEqual(mock_unarchive.call_count, 2)
        self.assertEqual(
            [item.args[1] for item in mock_unarchive.call_args_list],
            ["thread-1", "thread-2"],
        )
        self.assertIn("summary: unarchived=2 failed=0", stdout.getvalue())

    def test_unarchive_threads_continues_after_failure_but_stops_on_unknown(self) -> None:
        stdout = io.StringIO()
        running_entry = InstanceRegistryEntry(
            instance_name="default",
            owner_pid=1234,
            service_token="token-default",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-default",
            data_dir="/tmp/data-default",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="default",
            data_dir=Path("/tmp/data-default"),
            running_entry=running_entry,
        )

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin_cli._unarchive_thread",
                side_effect=[ValueError("blocked"), 0, 3, 0],
            ) as mock_unarchive:
                with redirect_stdout(stdout):
                    result = _unarchive_threads(
                        ["thread-1", "thread-2", "thread-3", "thread-4"]
                    )

        self.assertEqual(result, 3)
        self.assertEqual(mock_unarchive.call_count, 3)
        rendered = stdout.getvalue()
        self.assertIn("reason: blocked", rendered)
        self.assertIn("summary: unarchived=1 failed=1 unknown=1", rendered)
        self.assertIn("batch 已停止", rendered)

    def test_unarchive_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch("bot.runtime_admin_cli._validate_unarchive_binding_preflight"):
            with patch("bot.runtime_admin_cli._request", return_value={}):
                with redirect_stdout(stdout):
                    result = _unarchive_thread(
                        Path("/tmp/default-data"),
                        "thread-1",
                        instance_name="default",
                    )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_delete_thread_success_uses_root_only_contract(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin_cli._validate_delete_binding_preflight") as mock_preflight:
            with patch("bot.runtime_admin_cli._request", return_value=result_payload):
                with patch(
                    "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_other_instances",
                    return_value=([], []),
                ):
                    with redirect_stdout(stdout):
                        result = _delete_thread(
                            Path("/tmp/default-data"),
                            "thread-1",
                            instance_name="default",
                            force=True,
                        )

        self.assertEqual(result, 0)
        mock_preflight.assert_called_once_with("thread-1")
        self.assertIn("可能同时删除 spawned descendants", stdout.getvalue())
        self.assertIn("binding clear-stale --dry-run", stdout.getvalue())

    def test_delete_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch("bot.runtime_admin_cli._validate_delete_binding_preflight"):
            with patch("bot.runtime_admin_cli._request", return_value={}):
                with patch(
                    "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_other_instances"
                ) as mock_cleanup:
                    with redirect_stdout(stdout):
                        result = _delete_thread(
                            Path("/tmp/default-data"),
                            "thread-1",
                            instance_name="default",
                            force=True,
                        )

        self.assertEqual(result, 3)
        mock_cleanup.assert_not_called()
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_delete_confirmation_requires_force_when_noninteractive(self) -> None:
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "必须显式提供 `--force`"):
                _confirm_delete_thread("thread-1", force=False)

    def test_delete_force_still_prints_focus_safety_scope(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            confirmed = _confirm_delete_thread("thread-1", force=True)

        self.assertTrue(confirmed)
        self.assertIn("仅协调本机已知 Focus/fcodex runtime", stdout.getvalue())

    def test_archive_threads_continues_after_target_resolution_failure(self) -> None:
        stdout = io.StringIO()
        target_b = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        with patch("bot.runtime_admin_cli._lease_owner_instance", side_effect=["explorer", "default"]):
            with patch(
                "bot.runtime_admin_cli._resolve_target_instance",
                side_effect=[ValueError("ambiguous instance"), target_b],
            ):
                with patch(
                    "bot.runtime_admin_cli._request",
                    return_value={
                        "thread_id": "thread-2",
                        "thread_title": "demo",
                        "working_dir": "/tmp/default-data",
                        "upstream_outcome": "success",
                        "focus_cleanup": "complete",
                        "cleared_binding_ids": [],
                        "cleanup_errors": [],
                    },
                ):
                    with patch(
                        "bot.runtime_admin_cli._cleanup_archived_thread_bindings_in_other_instances",
                        return_value=([], []),
                    ):
                        with redirect_stdout(stdout):
                            result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("ambiguous instance", rendered)
        self.assertIn("status: failed", rendered)
        self.assertIn("status: archived", rendered)
        self.assertIn("summary: archived=1 failed=1", rendered)

    def test_resolve_thread_archive_target_prefers_live_runtime_owner_for_thread_name(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["thread", "archive", "--thread-name", "demo"])
        bootstrap = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))
        owner_target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        with patch("bot.runtime_admin_cli._resolve_target_instance", side_effect=[bootstrap, owner_target]):
            with patch("bot.runtime_admin_cli._resolve_thread_archive_name", return_value="thread-1"):
                with patch("bot.runtime_admin_cli._lease_owner_instance", return_value="explorer"):
                    target, target_params = _resolve_thread_archive_target(args)

        self.assertEqual(target.instance_name, "explorer")
        self.assertEqual(target.data_dir, Path("/tmp/explorer-data"))
        self.assertEqual(target_params, {"thread_id": "thread-1"})

    def test_resolve_thread_archive_target_resolves_name_before_explicit_instance_mutation(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["--instance", "explorer", "thread", "archive", "--thread-name", "demo"]
        )
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        with patch("bot.runtime_admin_cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin_cli._resolve_thread_archive_name",
                return_value="thread-1",
            ) as mock_resolve_name:
                resolved_target, target_params = _resolve_thread_archive_target(args)

        self.assertIs(resolved_target, target)
        self.assertEqual(target_params, {"thread_id": "thread-1"})
        mock_resolve_name.assert_called_once_with(target, "demo")

    def test_resolve_thread_archive_name_reports_read_only_failure_as_safe_to_retry(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        class FailingAdapter:
            def list_threads(self, **_kwargs):
                raise TimeoutError("page timed out")

            def stop(self) -> None:
                self.stopped = True

        adapter = FailingAdapter()
        adapter.stopped = False
        with patch(
            "bot.runtime_admin_cli._remote_adapter",
            return_value=(adapter, {"thread_list_query_limit": 100}, "ws://127.0.0.1:8765"),
        ):
            with self.assertRaisesRegex(ValueError, "mutation 尚未发送，可安全重试"):
                _resolve_thread_archive_name(target, "demo")

        self.assertTrue(adapter.stopped)

    def test_resolve_thread_archive_name_uses_read_only_paginated_resolver(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        class FakeAdapter:
            def stop(self) -> None:
                self.stopped = True

        adapter = FakeAdapter()
        adapter.stopped = False
        summary = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        with patch(
            "bot.runtime_admin_cli._remote_adapter",
            return_value=(adapter, {"thread_list_query_limit": 37}, "ws://127.0.0.1:8765"),
        ):
            with patch(
                "bot.runtime_admin_cli.resolve_resume_target_by_name",
                return_value=summary,
            ) as mock_resolve:
                thread_id = _resolve_thread_archive_name(target, "demo")

        self.assertEqual(thread_id, "thread-1")
        self.assertTrue(adapter.stopped)
        mock_resolve.assert_called_once_with(adapter, name="demo", limit=37)

    def test_resolve_thread_archive_targets_prefers_live_runtime_owner_for_each_thread_id(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["thread", "archive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )
        target_a = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        target_b = CliInstanceTarget(instance_name="aft", data_dir=Path("/tmp/aft-data"))

        with patch("bot.runtime_admin_cli._lease_owner_instance", side_effect=["explorer", "aft"]):
            with patch("bot.runtime_admin_cli._resolve_target_instance", side_effect=[target_a, target_b]):
                targets = _resolve_thread_archive_targets(args)

        self.assertEqual(
            targets,
            [
                (target_a, {"thread_id": "thread-1"}),
                (target_b, {"thread_id": "thread-2"}),
            ],
        )

    def test_main_thread_archive_batch_dispatches_all_targets(self) -> None:
        with patch("bot.runtime_admin_cli._archive_threads", return_value=1) as mock_archive:
            with patch(
                "bot.runtime_admin_cli.sys.argv",
                [
                    "focusctl",
                    "thread",
                    "archive",
                    "--thread-id",
                    "thread-1",
                    "--thread-id",
                    "thread-2",
                ],
            ):
                with self.assertRaises(SystemExit) as exc:
                    runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 1)
        self.assertEqual(mock_archive.call_args.args[0], ["thread-1", "thread-2"])
        self.assertEqual(mock_archive.call_args.kwargs["explicit_instance"], "")

    def test_main_thread_archive_batch_deduplicates_thread_ids(self) -> None:
        with patch("bot.runtime_admin_cli._archive_threads", return_value=0) as mock_archive:
            with patch(
                "bot.runtime_admin_cli.sys.argv",
                [
                    "focusctl",
                    "thread",
                    "archive",
                    "--thread-id",
                    "thread-1",
                    "--thread-id",
                    "thread-2",
                    "--thread-id",
                    "thread-1",
                ],
            ):
                with self.assertRaises(SystemExit) as exc:
                    runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_archive.call_args.args[0], ["thread-1", "thread-2"])

    def test_main_thread_clear_archived_bindings_dispatches_before_default_target_resolution(self) -> None:
        with patch("bot.runtime_admin_cli._clear_archived_thread_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin_cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin_cli.sys.argv",
                    [
                        "focusctl",
                        "thread",
                        "clear-archived-bindings",
                        "--thread-id",
                        "thread-1",
                        "--dry-run",
                    ],
                ):
                    with self.assertRaises(SystemExit) as exc:
                        runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_clear.call_args.args[0], "thread-1")
        self.assertFalse(mock_clear.call_args.kwargs["all_archived"])
        self.assertEqual(mock_clear.call_args.kwargs["explicit_instance"], "")
        self.assertTrue(mock_clear.call_args.kwargs["dry_run"])
        mock_resolve.assert_not_called()

    def test_main_thread_clear_archived_bindings_all_dispatches_before_default_target_resolution(self) -> None:
        with patch("bot.runtime_admin_cli._clear_archived_thread_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin_cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin_cli.sys.argv",
                    [
                        "focusctl",
                        "thread",
                        "clear-archived-bindings",
                        "--all",
                        "--dry-run",
                    ],
                ):
                    with self.assertRaises(SystemExit) as exc:
                        runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_clear.call_args.args[0], "")
        self.assertTrue(mock_clear.call_args.kwargs["all_archived"])
        self.assertEqual(mock_clear.call_args.kwargs["explicit_instance"], "")
        self.assertTrue(mock_clear.call_args.kwargs["dry_run"])
        mock_resolve.assert_not_called()

    def test_main_binding_clear_stale_dispatches_before_default_target_resolution(self) -> None:
        with patch("bot.runtime_admin_cli._clear_stale_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin_cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin_cli.sys.argv",
                    [
                        "focusctl",
                        "binding",
                        "clear-stale",
                        "--dry-run",
                    ],
                ):
                    with self.assertRaises(SystemExit) as exc:
                        runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_clear.call_args.kwargs["explicit_instance"], "")
        self.assertTrue(mock_clear.call_args.kwargs["dry_run"])
        mock_resolve.assert_not_called()
