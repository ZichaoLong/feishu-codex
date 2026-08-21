import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.codex_protocol.client import AppServerEndpointMode
from bot.instance_resolution import CliInstanceTarget
from bot.runtime_admin.cli import (
    _attach_service,
    _attached_endpoint_adapter,
    _lifecycle_control_timeout_seconds,
    _open_web,
    main as runtime_admin_cli_main,
)
from bot.service_control_plane import ServiceControlResponseTimeoutError
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore


class RuntimeAdminCliDispatchTests(unittest.TestCase):
    def test_open_web_prints_bootstrap_url_and_opens_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            WebGatewayRuntimeStore(data_dir).save(
                endpoint="http://127.0.0.1:8766",
                bootstrap_token="token/value",
                owner_pid=os.getpid(),
            )
            stdout = io.StringIO()
            with patch("bot.runtime_admin.cli.webbrowser.open", return_value=True) as mock_open:
                with redirect_stdout(stdout):
                    result = _open_web(data_dir, no_browser=False)

        expected = "http://127.0.0.1:8766/#token=token%2Fvalue"
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), expected)
        mock_open.assert_called_once_with(expected, new=2)

    def test_open_web_rejects_missing_or_stale_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            with self.assertRaisesRegex(ValueError, "尚未发布 Focus Web Gateway"):
                _open_web(data_dir, no_browser=True)

            with patch(
                "bot.stores.web_gateway_runtime_store.process_identity",
                side_effect=("stale-incarnation", "replacement-incarnation"),
            ):
                with patch(
                    "bot.stores.web_gateway_runtime_store.process_exists",
                    return_value=True,
                ):
                    WebGatewayRuntimeStore(data_dir).save(
                        endpoint="http://127.0.0.1:8766",
                        bootstrap_token="token",
                        owner_pid=99999999,
                    )
                    stdout = io.StringIO()
                    with patch("bot.runtime_admin.cli.webbrowser.open") as mock_open:
                        with redirect_stdout(stdout):
                            with self.assertRaisesRegex(
                                ValueError,
                                "尚未发布 Focus Web Gateway",
                            ):
                                _open_web(data_dir, no_browser=False)

            self.assertEqual(stdout.getvalue(), "")
            mock_open.assert_not_called()
            self.assertFalse((data_dir / "web_gateway_runtime.json").exists())

    def test_main_thread_unarchive_passes_all_ids_to_batch_handler(self) -> None:
        with patch("bot.runtime_admin.cli._unarchive_threads", return_value=0) as mock_unarchive:
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
        with patch("bot.runtime_admin.cli._resolve_target_instance") as mock_resolve:
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

    def test_attached_endpoint_adapter_prefers_running_instance_resolution(self) -> None:
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
            "bot.runtime_admin.cli.load_config_file",
            return_value={"app_server_url": "ws://127.0.0.1:8765"},
        ) as mock_load_config:
            with patch(
                "bot.runtime_admin.cli.resolve_running_instance_app_server_url",
                return_value="ws://127.0.0.1:43210",
            ) as mock_resolve:
                adapter, _config, app_server_url = _attached_endpoint_adapter(
                    Path("/tmp/data-aft"),
                    running_entry=entry,
                )

        self.assertEqual(app_server_url, "ws://127.0.0.1:43210")
        self.assertIs(adapter._config.endpoint_mode, AppServerEndpointMode.ATTACHED_ENDPOINT)
        self.assertEqual(adapter._config.app_server_data_dir, "/tmp/data-aft")
        mock_load_config.assert_called_once_with("codex", directory=Path("/tmp/config-aft"))
        self.assertEqual(mock_resolve.call_args.args[0], entry)
        adapter.stop()

    def test_attached_endpoint_adapter_rejects_stopped_instance_before_loading_config(self) -> None:
        with patch("bot.runtime_admin.cli.load_config_file") as mock_load_config:
            with self.assertRaisesRegex(ValueError, "实例正在运行"):
                _attached_endpoint_adapter(Path("/tmp/data-aft"))

        mock_load_config.assert_not_called()

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
            with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
                with patch("bot.runtime_admin.cli._print_thread_list", return_value=0) as mock_print:
                    with patch(
                        "bot.runtime_admin.cli.sys.argv",
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
            "bot.runtime_admin.cli.load_config_file",
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
            with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
                with patch("bot.runtime_admin.cli._print_thread_goal", return_value=0) as mock_print:
                    with patch(
                        "bot.runtime_admin.cli.sys.argv",
                        ["focusctl", "--instance", "aft", "thread", "goal", "--thread-id", "thread-1"],
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(mock_print.call_args.args[0], Path(tmpdir))
        self.assertEqual(mock_print.call_args.args[1], {"thread_id": "thread-1"})
        self.assertEqual(mock_print.call_args.kwargs["instance_name"], "aft")

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
                    "bot.runtime_admin.cli.sys.argv",
                    ["focusctl", "--instance", "ghost", "service", "reset-backend"],
                ):
                    with patch("bot.runtime_admin.cli.sys.stderr", stderr):
                        with self.assertRaises(SystemExit) as exc:
                            runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("instance create ghost", stderr.getvalue())

    def test_service_reset_backend_malformed_result_exits_unknown_without_output(self) -> None:
        target = CliInstanceTarget(
            instance_name="corp-b",
            data_dir=Path("/tmp/corp-b-data"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch(
                "bot.runtime_admin.cli.sys.argv",
                ["focusctl", "service", "reset-backend"],
            ),
            patch(
                "bot.runtime_admin.cli._resolve_target_instance",
                return_value=target,
            ),
            patch(
                "bot.backend_reset.cli.control_request",
                return_value={},
            ) as control_request,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as caught,
        ):
            runtime_admin_cli_main()

        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("控制面请求结果未知", stderr.getvalue())
        self.assertIn("不要立即重试", stderr.getvalue())
        self.assertIn(
            "focusctl --instance corp-b service status",
            stderr.getvalue(),
        )
        control_request.assert_called_once_with(
            Path("/tmp/corp-b-data"),
            "service/reset-backend",
            {"force": False},
            timeout_seconds=30.0,
        )

    def test_attach_service_treats_response_timeout_as_accepted(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.cli._request",
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

    def test_main_thread_archive_batch_dispatches_all_targets(self) -> None:
        with patch("bot.runtime_admin.cli._archive_threads", return_value=1) as mock_archive:
            with patch(
                "bot.runtime_admin.cli.sys.argv",
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

    def test_main_web_open_uses_resolved_instance(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin.cli._open_web", return_value=0) as mock_open:
                with patch(
                    "bot.runtime_admin.cli.sys.argv",
                    ["focusctl", "--instance", "explorer", "web", "open", "--no-browser"],
                ):
                    with self.assertRaises(SystemExit) as exc:
                        runtime_admin_cli_main()

        self.assertEqual(exc.exception.code, 0)
        mock_open.assert_called_once_with(target.data_dir, no_browser=True)

    def test_main_thread_archive_batch_deduplicates_thread_ids(self) -> None:
        with patch("bot.runtime_admin.cli._archive_threads", return_value=0) as mock_archive:
            with patch(
                "bot.runtime_admin.cli.sys.argv",
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
        with patch("bot.runtime_admin.cli._clear_archived_thread_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin.cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin.cli.sys.argv",
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
        with patch("bot.runtime_admin.cli._clear_archived_thread_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin.cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin.cli.sys.argv",
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
        with patch("bot.runtime_admin.cli._clear_stale_bindings", return_value=0) as mock_clear:
            with patch("bot.runtime_admin.cli._resolve_target_instance") as mock_resolve:
                with patch(
                    "bot.runtime_admin.cli.sys.argv",
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
