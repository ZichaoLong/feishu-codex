import os
import tempfile
import unittest
from unittest.mock import ANY, Mock, patch
from io import StringIO
from pathlib import Path

from bot.adapters.base import (
    ThreadSummary,
)
from bot.fcodex.cli import (
    _default_data_dir,
    _launch_local_cwd_proxy,
    main as fcodex_main,
)
from bot.instance_resolution import (
    CliInstanceTarget,
    CliRuntimeTarget,
    resolve_cli_runtime_target,
)
from bot.local_websocket_auth import (
    FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
    FOCUS_SERVICE_TOKEN_ENV_VAR,
)
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.app_server_runtime_store import (
    AppServerRuntimeStore,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.version import __version__


class FCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        env_patcher = patch.dict(
            os.environ,
            {
                "FOCUS_INSTANCE": "",
                "FOCUS_DATA_DIR": "",
                "FOCUS_GLOBAL_DATA_DIR": "",
            },
            clear=False,
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        default_entry = InstanceRegistryEntry(
            instance_name="default",
            owner_pid=os.getpid(),
            service_token="",
            control_endpoint="tcp://127.0.0.1:9393",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="",
            data_dir=str(_default_data_dir()),
            started_at=1.0,
            updated_at=1.0,
        )
        patchers = [
            patch(
                "bot.instance_resolution.resolve_running_instance_app_server_url",
                return_value="ws://127.0.0.1:8765",
            ),
            patch(
                "bot.instance_resolution.list_running_instances",
                return_value=[default_entry],
            ),
            patch(
                "bot.instance_resolution.load_running_instance",
                side_effect=lambda name: default_entry if name == "default" else None,
            ),
            patch(
                "bot.instance_resolution.current_cli_instance_name",
                return_value="default",
            ),
            patch("bot.fcodex.cli.current_cli_instance_name", return_value="default"),
            patch("bot.fcodex.cli.list_running_instances", return_value=[default_entry]),
            patch(
                "bot.fcodex.cli.resolve_running_instance_app_server_url",
                return_value="ws://127.0.0.1:8765",
            ),
            patch(
                "bot.fcodex.cli.resolve_managed_codex_command", side_effect=lambda cmd: cmd
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_data_dir_falls_back_to_install_path_when_not_in_dev_layout(
        self,
    ) -> None:
        with patch.dict("bot.fcodex.cli.os.environ", {}, clear=True):
            with patch(
                "bot.fcodex.cli.default_data_root",
                return_value=Path("/home/tester/.local/share/focus"),
            ):
                self.assertEqual(
                    _default_data_dir(),
                    Path("/home/tester/.local/share/focus"),
                )

    def test_fcodex_injects_remote_url(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9100", Mock()),
            ) as mock_proxy:
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch(
                        "sys.argv",
                        ["fcodex", "resume", "019d2e94-a475-7bc1-b2f7-a3ce37628ede"],
                    ):
                        fcodex_main()

        mock_proxy.assert_called_once_with(
            "ws://127.0.0.1:8765",
            os.getcwd(),
            _default_data_dir(),
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )
        self.assertEqual(
            mock_exec.call_args.args[2][FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR],
            mock_proxy.call_args.kwargs["proxy_auth_token"],
        )

    def test_fcodex_passes_terminator_tail_unchanged_and_keeps_shell_cwd(self) -> None:
        shell_cwd = os.getcwd()
        upstream_tail = ["--", "--cd=/tmp", "--remote", "ws://upstream"]
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9100", Mock()),
            ) as mock_proxy:
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", *upstream_tail]):
                        fcodex_main()

        mock_proxy.assert_called_once_with(
            "ws://127.0.0.1:8765",
            shell_cwd,
            _default_data_dir(),
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1][-len(upstream_tail) :],
            upstream_tail,
        )

    def test_fcodex_adds_loopback_no_proxy_without_dropping_user_proxy_env(
        self,
    ) -> None:
        with patch.dict(
            "bot.fcodex.cli.os.environ",
            {
                "HTTP_PROXY": "http://proxy.example:8080",
                "HTTPS_PROXY": "http://proxy.example:8443",
                "NO_PROXY": "example.com,localhost",
            },
            clear=True,
        ):
            with patch(
                "bot.fcodex.cli.load_config_file",
                return_value={
                    "codex_command": "codex",
                    "app_server_url": "ws://127.0.0.1:8765",
                },
            ):
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9100", Mock()),
                ):
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex"]):
                            fcodex_main()

        env = mock_exec.call_args.args[2]
        self.assertEqual(env["HTTP_PROXY"], "http://proxy.example:8080")
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:8443")
        self.assertEqual(env["NO_PROXY"], "127.0.0.1,localhost,::1,example.com")
        self.assertEqual(env["no_proxy"], "127.0.0.1,localhost,::1,example.com")

    def test_fcodex_uses_resolved_codex_command_when_instance_override_omits_it(
        self,
    ) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={"app_server_url": "ws://127.0.0.1:8765"},
        ):
            with patch(
                "bot.fcodex.cli.resolve_managed_codex_command",
                return_value="/stable/node /stable/codex.js",
            ) as mock_resolve:
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9100", Mock()),
                ):
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "session"]):
                            fcodex_main()

        self.assertEqual(mock_resolve.call_args.args[0], "codex")
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "/stable/node",
                "/stable/codex.js",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "session",
            ],
        )

    def test_fcodex_uses_runtime_resolved_backend_url(self) -> None:
        fallback_url = "ws://127.0.0.1:43210"
        with patch(
            "bot.instance_resolution.resolve_running_instance_app_server_url",
            return_value=fallback_url,
        ):
            with patch(
                "bot.fcodex.cli.load_config_file",
                return_value={
                    "codex_command": "codex",
                    "app_server_url": "ws://127.0.0.1:8765",
                },
            ):
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9100", Mock()),
                ) as mock_proxy:
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with patch(
                            "sys.argv",
                            [
                                "fcodex",
                                "resume",
                                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
                            ],
                        ):
                            fcodex_main()

        mock_proxy.assert_called_once_with(
            fallback_url,
            os.getcwd(),
            _default_data_dir(),
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )

    def test_fcodex_does_not_inject_instance_default_profile_for_new_thread(
        self,
    ) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9100", Mock()),
            ):
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex"]):
                        fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
            ],
        )

    def test_fcodex_explicit_profile_is_passthrough_only(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9100", Mock()),
            ) as mock_proxy:
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "-p", "provider1"]):
                        fcodex_main()

        mock_proxy.assert_called_once_with(
            "ws://127.0.0.1:8765",
            os.getcwd(),
            _default_data_dir(),
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "-p",
                "provider1",
            ],
        )

    def test_fcodex_rejects_user_supplied_remote_transport_options(self) -> None:
        cases = (
            ["--remote", "ws://127.0.0.1:9900", "resume", "demo"],
            ["--instance", "corp-b", "--remote", "ws://127.0.0.1:9900"],
            ["--remote-auth-token-env", "USER_TOKEN_ENV"],
        )
        for user_args in cases:
            with self.subTest(user_args=user_args):
                stderr = StringIO()
                with (
                    patch("bot.fcodex.cli.load_config_file") as mock_load_config,
                    patch("bot.fcodex.cli.sys.stderr", stderr),
                    patch("sys.argv", ["fcodex", *user_args]),
                    self.assertRaises(SystemExit) as exc,
                ):
                    fcodex_main()

                self.assertEqual(exc.exception.code, 2)
                self.assertIn("内部参数", stderr.getvalue())
                self.assertIn("不接受用户指定外部 app-server", stderr.getvalue())
                mock_load_config.assert_not_called()

    def test_fcodex_rejects_explicit_uncreated_named_instance(self) -> None:
        stderr = StringIO()
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
                    "bot.fcodex.cli.load_config_file",
                    return_value={
                        "codex_command": "codex",
                        "app_server_url": "ws://127.0.0.1:8765",
                    },
                ):
                    with patch("bot.fcodex.cli.sys.stderr", stderr):
                        with patch(
                            "sys.argv", ["fcodex", "--instance", "ghost", "session"]
                        ):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("instance create ghost", stderr.getvalue())

    def test_fcodex_routes_resume_to_owner_instance(self) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        lease = ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="corp-b",
            owner_service_token="token-b",
            control_endpoint="tcp://127.0.0.1:9102",
            backend_url="ws://127.0.0.1:9102",
            attached_at=1.0,
            holders=(),
        )
        resolved_target = CliRuntimeTarget(
            instance_name="corp-b",
            data_dir=Path("/tmp/data-b"),
            app_server_url="ws://127.0.0.1:9102",
            service_token="token-b",
            running_entry=InstanceRegistryEntry(
                instance_name="corp-b",
                owner_pid=222,
                service_token="token-b",
                control_endpoint="tcp://127.0.0.1:9102",
                app_server_url="ws://127.0.0.1:9102",
                config_dir="/tmp/config-b",
                data_dir="/tmp/data-b",
                started_at=1.0,
                updated_at=1.0,
            ),
        )
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with (
                patch(
                    "bot.fcodex.cli.preview_thread_global_loaded_gate",
                    return_value=Mock(allowed=True),
                ),
                patch("bot.fcodex.cli.ThreadRuntimeLeaseStore.load", return_value=lease),
            ):
                with patch(
                    "bot.fcodex.cli.resolve_cli_runtime_target",
                    return_value=resolved_target,
                ) as mock_resolve_target:
                    with patch(
                        "bot.fcodex.cli._launch_local_cwd_proxy",
                        return_value=("ws://127.0.0.1:9200", Mock()),
                    ) as mock_proxy:
                        with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "resume", thread_id]):
                                fcodex_main()

        self.assertEqual(
            mock_resolve_target.call_args.kwargs["preferred_running_instance"], "corp-b"
        )
        self.assertFalse(
            mock_resolve_target.call_args.kwargs["allow_default_running_fallback"]
        )
        mock_proxy.assert_called_once_with(
            "ws://127.0.0.1:9102",
            os.getcwd(),
            Path("/tmp/data-b"),
            instance_name="corp-b",
            service_token="token-b",
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9200",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "resume",
                thread_id,
            ],
        )

    def test_runtime_target_prefers_instance_runtime_store_over_stale_registry_url(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            AppServerRuntimeStore(data_dir).save_owned_runtime(
                configured_url="ws://127.0.0.1:8765",
                active_url="ws://127.0.0.1:43210",
                owner_pid=os.getpid(),
                lifecycle_pid=os.getpid(),
                cleanup_token="test-cleanup-token",
            )
            running_entry = InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=os.getpid(),
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9393",
                app_server_url="ws://127.0.0.1:8765",
                config_dir="/tmp/config-explorer",
                data_dir=str(data_dir),
                started_at=1.0,
                updated_at=1.0,
            )

            with patch(
                "bot.instance_resolution.resolve_cli_instance_target",
                return_value=CliInstanceTarget(
                    instance_name="explorer",
                    data_dir=data_dir,
                    running_entry=running_entry,
                ),
            ):
                with patch(
                    "bot.instance_resolution.resolve_running_instance_app_server_url",
                    return_value="ws://127.0.0.1:43210",
                ):
                    resolved = resolve_cli_runtime_target(
                        explicit_instance="explorer",
                    )

        self.assertEqual(resolved.instance_name, "explorer")
        self.assertEqual(resolved.data_dir, data_dir)
        self.assertEqual(resolved.app_server_url, "ws://127.0.0.1:43210")
        self.assertEqual(resolved.service_token, "token-explorer")

    def test_runtime_target_rejects_running_instance_without_live_default_runtime(
        self,
    ) -> None:
        data_dir = Path("/tmp/data-explorer")
        running_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=os.getpid(),
            service_token="token-explorer",
            control_endpoint="tcp://127.0.0.1:9393",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-explorer",
            data_dir=str(data_dir),
            started_at=1.0,
            updated_at=1.0,
        )

        with patch(
            "bot.instance_resolution.resolve_cli_instance_target",
            return_value=CliInstanceTarget(
                instance_name="explorer",
                data_dir=data_dir,
                running_entry=running_entry,
            ),
        ):
            with patch(
                "bot.instance_resolution.resolve_running_instance_app_server_url",
                return_value="",
            ):
                with self.assertRaises(ValueError) as exc:
                    resolve_cli_runtime_target(
                        explicit_instance="explorer",
                    )

        self.assertIn("未发布可用的 app-server 地址", str(exc.exception))

    def test_runtime_target_rejects_stopped_instance_without_using_configured_port(
        self,
    ) -> None:
        data_dir = Path("/tmp/data-explorer")
        with patch(
            "bot.instance_resolution.resolve_cli_instance_target",
            return_value=CliInstanceTarget(
                instance_name="explorer",
                data_dir=data_dir,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "当前未运行"):
                resolve_cli_runtime_target(explicit_instance="explorer")

    def test_runtime_target_rejects_explicit_uncreated_named_instance(self) -> None:
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
                with self.assertRaisesRegex(ValueError, "instance create ghost"):
                    resolve_cli_runtime_target(
                        explicit_instance="ghost",
                    )

    def test_runtime_target_without_default_fallback_rejects_multiple_running_instances(
        self,
    ) -> None:
        with patch(
            "bot.instance_resolution.list_running_instances",
            return_value=[
                InstanceRegistryEntry(
                    instance_name="default",
                    owner_pid=os.getpid(),
                    service_token="token-default",
                    control_endpoint="tcp://127.0.0.1:9101",
                    app_server_url="ws://127.0.0.1:9101",
                    config_dir="/tmp/config-default",
                    data_dir="/tmp/data-default",
                    started_at=1.0,
                    updated_at=1.0,
                ),
                InstanceRegistryEntry(
                    instance_name="explorer",
                    owner_pid=os.getpid(),
                    service_token="token-explorer",
                    control_endpoint="tcp://127.0.0.1:9102",
                    app_server_url="ws://127.0.0.1:9102",
                    config_dir="/tmp/config-explorer",
                    data_dir="/tmp/data-explorer",
                    started_at=1.0,
                    updated_at=1.0,
                ),
            ],
        ):
            with patch(
                "bot.instance_resolution.load_running_instance", return_value=None
            ):
                with patch(
                    "bot.instance_resolution.current_cli_instance_name",
                    return_value="default",
                ):
                    with self.assertRaises(ValueError) as exc:
                        resolve_cli_runtime_target(
                            allow_default_running_fallback=False,
                        )

        self.assertIn("请显式传 `--instance <name>`", str(exc.exception))

    def test_fcodex_requires_explicit_instance_when_multiple_instances_are_running(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.resolve_cli_runtime_target",
                side_effect=ValueError(
                    "检测到多个运行中的实例，请显式传 `--instance <name>`。"
                ),
            ):
                with patch(
                    "bot.fcodex.cli.ThreadRuntimeLeaseStore.load", return_value=None
                ):
                    with patch("bot.fcodex.cli.sys.stderr", stderr):
                        with patch("sys.argv", ["fcodex", "resume", thread_id]):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("请显式传 `--instance <name>`", stderr.getvalue())
        self.assertIn("当前是 `fcodex resume <thread>` 路径", stderr.getvalue())

    def test_fcodex_top_level_help_shows_wrapper_contract(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.cli.sys.stdout", stdout):
            with patch("sys.argv", ["fcodex", "--help"]):
                with self.assertRaises(SystemExit) as exc:
                    fcodex_main()

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("fcodex 本地 wrapper", rendered)
        self.assertIn("--instance <name>", rendered)
        self.assertIn("codex --help", rendered)
        self.assertIn("focusctl --help", rendered)

    def test_fcodex_version_prints_project_version_without_loading_codex_config(
        self,
    ) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.cli.sys.stdout", stdout):
            with patch("bot.fcodex.cli.load_config_file") as mock_load_config:
                with patch("sys.argv", ["fcodex", "--version"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"fcodex {__version__}")
        mock_load_config.assert_not_called()

    def test_fcodex_top_level_help_accepts_instance_after_help_flag(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.cli.sys.stdout", stdout):
            with patch("sys.argv", ["fcodex", "--help", "--instance", "corp-a"]):
                with self.assertRaises(SystemExit) as exc:
                    fcodex_main()

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("fcodex 本地 wrapper", rendered)
        self.assertIn("--instance <name>", rendered)

    def test_fcodex_resume_help_shows_wrapper_resume_contract(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.cli.sys.stdout", stdout):
            with patch("sys.argv", ["fcodex", "resume", "--help"]):
                with self.assertRaises(SystemExit) as exc:
                    fcodex_main()

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("fcodex resume 本地 wrapper 语义", rendered)
        self.assertIn("loaded", rendered)
        self.assertIn("codex resume --help", rendered)
        self.assertIn("focusctl thread status", rendered)

    def test_fcodex_resume_help_accepts_instance_after_help_flag(self) -> None:
        stdout = StringIO()
        with patch("bot.fcodex.cli.sys.stdout", stdout):
            with patch(
                "sys.argv", ["fcodex", "resume", "--help", "--instance", "corp-a"]
            ):
                with self.assertRaises(SystemExit) as exc:
                    fcodex_main()

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("fcodex resume 本地 wrapper 语义", rendered)
        self.assertIn("codex resume --help", rendered)

    def test_fcodex_consumes_instance_outside_leading_prefix_position(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.resolve_cli_runtime_target",
                return_value=CliRuntimeTarget(
                    instance_name="explorer",
                    data_dir=Path("/tmp/data-explorer"),
                    app_server_url="ws://127.0.0.1:8765",
                ),
            ) as mock_resolve:
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9100", Mock()),
                ):
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with patch(
                            "sys.argv", ["fcodex", "session", "--instance", "explorer"]
                        ):
                            fcodex_main()

        self.assertEqual(mock_resolve.call_args.kwargs["explicit_instance"], "explorer")
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "session",
            ],
        )

    def test_fcodex_rejects_slash_threads_command(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/threads"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("不再支持 slash 自命令", stderr.getvalue())
        self.assertIn("focusctl thread list --scope cwd", stderr.getvalue())

    def test_fcodex_rejects_slash_help_command(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/help"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("focusctl", stderr.getvalue())
        self.assertIn("进入 TUI 后再使用 upstream `/help`", stderr.getvalue())

    def test_fcodex_rejects_slash_profile_command(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/profile", "provider2"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("fcodex -p <profile>", stderr.getvalue())

    def test_fcodex_rejects_slash_archive_command(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/archive", "thread-1"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("focusctl thread archive", stderr.getvalue())

    def test_fcodex_rejects_slash_resume_command(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/resume", "demo"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("fcodex resume <thread_id|thread_name>", stderr.getvalue())

    def test_fcodex_rejects_removed_dry_run_wrapper_entry(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "--dry-run", "/threads"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("不再提供 `--dry-run` wrapper 入口", stderr.getvalue())
        self.assertIn("focusctl thread list", stderr.getvalue())

    def test_fcodex_non_slash_text_is_passthrough_prompt(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9100", Mock()),
            ):
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "session"]):
                        fcodex_main()

        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "session",
            ],
        )

    def test_fcodex_rejects_wrapper_command_mixed_with_prefix_flags(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "--cd", "/tmp/project", "/threads"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("不再支持 slash 自命令", stderr.getvalue())
        self.assertIn("focusctl thread list --scope cwd", stderr.getvalue())

    def test_fcodex_rejects_unknown_slash_command_in_shell_wrapper(self) -> None:
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.sys.stderr", stderr):
                with patch("sys.argv", ["fcodex", "/cd", "/tmp/project"]):
                    with self.assertRaises(SystemExit) as exc:
                        fcodex_main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("不再支持 slash 自命令：`/cd`", stderr.getvalue())
        self.assertIn("其他 `/...` 命令请先进入 Codex TUI 再执行", stderr.getvalue())

    def test_fcodex_resume_resolves_name(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._resolve_resume_lookup_runtime_target",
                return_value=CliRuntimeTarget(
                    instance_name="default",
                    data_dir=_default_data_dir(),
                    app_server_url="ws://127.0.0.1:8765",
                ),
            ):
                with patch(
                    "bot.fcodex.cli._resolve_thread_target_via_attached_endpoint"
                ) as mock_resolve:
                    mock_resolve.return_value = (
                        ThreadSummary(
                            thread_id="019d2e94-a475-7bc1-b2f7-a3ce37628ede",
                            cwd="/tmp/project",
                            name="demo",
                            preview="hello",
                            created_at=0,
                            updated_at=0,
                            source="cli",
                            status="notLoaded",
                        ),
                        None,
                    )
                    with patch(
                        "bot.fcodex.cli._launch_local_cwd_proxy",
                        return_value=("ws://127.0.0.1:9100", Mock()),
                    ):
                        with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                            with patch("sys.argv", ["fcodex", "resume", "demo"]):
                                fcodex_main()

        self.assertEqual(mock_resolve.call_args.args[3], "demo")
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "resume",
                "019d2e94-a475-7bc1-b2f7-a3ce37628ede",
            ],
        )

    def test_fcodex_resume_name_routes_to_unique_running_instance_without_live_owner(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        running_instances = [
            InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=222,
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9102",
                app_server_url="ws://127.0.0.1:9102",
                config_dir="/tmp/config-explorer",
                data_dir="/tmp/data-explorer",
                started_at=1.0,
                updated_at=1.0,
            ),
        ]
        resolved_target = CliRuntimeTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/data-explorer"),
            app_server_url="ws://127.0.0.1:9102",
            service_token="token-explorer",
            running_entry=running_instances[0],
        )
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.list_running_instances", return_value=running_instances
            ):
                with patch(
                    "bot.fcodex.cli._resolve_resume_lookup_runtime_target",
                    return_value=CliRuntimeTarget(
                        instance_name="explorer",
                        data_dir=Path("/tmp/data-explorer"),
                        app_server_url="ws://127.0.0.1:9102",
                    ),
                ):
                    with patch(
                        "bot.fcodex.cli._resolve_thread_target_via_attached_endpoint",
                        return_value=(
                            ThreadSummary(
                                thread_id=thread_id,
                                cwd="/tmp/project",
                                name="demo",
                                preview="hello",
                                created_at=0,
                                updated_at=0,
                                source="cli",
                                status="notLoaded",
                            ),
                            None,
                        ),
                    ):
                        with patch(
                            "bot.fcodex.cli.resolve_cli_runtime_target",
                            return_value=resolved_target,
                        ) as mock_resolve_target:
                            with patch(
                                "bot.fcodex.cli._launch_local_cwd_proxy",
                                return_value=("ws://127.0.0.1:9200", Mock()),
                            ):
                                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                                    with patch(
                                        "sys.argv", ["fcodex", "resume", "demo"]
                                    ):
                                        fcodex_main()

        self.assertEqual(
            mock_resolve_target.call_args.kwargs["preferred_running_instance"],
            "explorer",
        )
        self.assertFalse(
            mock_resolve_target.call_args.kwargs["allow_default_running_fallback"]
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9200",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "resume",
                thread_id,
            ],
        )

    def test_fcodex_resume_name_requires_explicit_instance_when_running_instance_is_ambiguous(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        running_instances = [
            InstanceRegistryEntry(
                instance_name="default",
                owner_pid=111,
                service_token="token-default",
                control_endpoint="tcp://127.0.0.1:9101",
                app_server_url="ws://127.0.0.1:9101",
                config_dir="/tmp/config-default",
                data_dir="/tmp/data-default",
                started_at=1.0,
                updated_at=1.0,
            ),
            InstanceRegistryEntry(
                instance_name="explorer",
                owner_pid=222,
                service_token="token-explorer",
                control_endpoint="tcp://127.0.0.1:9102",
                app_server_url="ws://127.0.0.1:9102",
                config_dir="/tmp/config-explorer",
                data_dir="/tmp/data-explorer",
                started_at=1.0,
                updated_at=1.0,
            ),
        ]
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.list_running_instances", return_value=running_instances
            ):
                with patch(
                    "bot.instance_resolution.list_running_instances",
                    return_value=running_instances,
                ):
                    with patch(
                        "bot.fcodex.cli._resolve_resume_lookup_runtime_target",
                        return_value=CliRuntimeTarget(
                            instance_name="default",
                            data_dir=Path("/tmp/data-default"),
                            app_server_url="ws://127.0.0.1:9101",
                        ),
                    ):
                        with patch(
                            "bot.fcodex.cli._resolve_thread_target_via_attached_endpoint",
                            return_value=(
                                ThreadSummary(
                                    thread_id=thread_id,
                                    cwd="/tmp/project",
                                    name="demo",
                                    preview="hello",
                                    created_at=0,
                                    updated_at=0,
                                    source="cli",
                                    status="notLoaded",
                                ),
                                None,
                            ),
                        ):
                            with patch("bot.fcodex.cli.sys.stderr", stderr):
                                with patch("sys.argv", ["fcodex", "resume", "demo"]):
                                    with self.assertRaises(SystemExit) as exc:
                                        fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("请显式传 `--instance <name>`", stderr.getvalue())
        self.assertIn("当前是 `fcodex resume <thread>` 路径", stderr.getvalue())

    def test_fcodex_resume_with_explicit_instance_rejects_conflicting_live_owner(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        lease = ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="token-explorer",
            control_endpoint="tcp://127.0.0.1:9102",
            backend_url="ws://127.0.0.1:9102",
            attached_at=1.0,
            holders=(),
        )
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch("bot.fcodex.cli.ThreadRuntimeLeaseStore.load", return_value=lease):
                with patch("bot.fcodex.cli.sys.stderr", stderr):
                    with patch(
                        "sys.argv",
                        ["fcodex", "--instance", "default", "resume", thread_id],
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("live runtime owner 是 `explorer`", stderr.getvalue())
        self.assertIn("不能显式传 `--instance default`", stderr.getvalue())

    def test_fcodex_resume_rejects_when_other_running_instance_still_reports_loaded(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.resolve_cli_runtime_target",
                return_value=CliRuntimeTarget(
                    instance_name="explorer",
                    data_dir=Path("/tmp/data-explorer"),
                    app_server_url="ws://127.0.0.1:9102",
                    service_token="token-explorer",
                ),
            ):
                with patch(
                    "bot.fcodex.cli.preview_thread_global_loaded_gate",
                    return_value=Mock(
                        allowed=False,
                        reason_text=(
                            "当前 thread 仍由运行中的实例 `default` 保持为 loaded (`idle`)；"
                            "当前按 fail-close 拒绝跨实例继续。"
                        ),
                    ),
                ):
                    with patch("bot.fcodex.cli.sys.stderr", stderr):
                        with patch(
                            "sys.argv",
                            ["fcodex", "--instance", "explorer", "resume", thread_id],
                        ):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("拒绝跨实例继续", stderr.getvalue())

    def test_fcodex_resume_name_with_explicit_instance_rejects_conflicting_live_owner(
        self,
    ) -> None:
        thread_id = "019d2e94-a475-7bc1-b2f7-a3ce37628ede"
        lease = ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="token-explorer",
            control_endpoint="tcp://127.0.0.1:9102",
            backend_url="ws://127.0.0.1:9102",
            attached_at=1.0,
            holders=(),
        )
        stderr = StringIO()
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._resolve_resume_lookup_runtime_target",
                return_value=CliRuntimeTarget(
                    instance_name="default",
                    data_dir=Path("/tmp/data-default"),
                    app_server_url="ws://127.0.0.1:9101",
                ),
            ):
                with patch(
                    "bot.fcodex.cli._resolve_thread_target_via_attached_endpoint",
                    return_value=(
                        ThreadSummary(
                            thread_id=thread_id,
                            cwd="/tmp/project",
                            name="demo",
                            preview="hello",
                            created_at=0,
                            updated_at=0,
                            source="cli",
                            status="notLoaded",
                        ),
                        None,
                    ),
                ):
                    with patch(
                        "bot.fcodex.cli.ThreadRuntimeLeaseStore.load", return_value=lease
                    ):
                        with patch("bot.fcodex.cli.sys.stderr", stderr):
                            with patch(
                                "sys.argv",
                                ["fcodex", "--instance", "default", "resume", "demo"],
                            ):
                                with self.assertRaises(SystemExit) as exc:
                                    fcodex_main()

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("live runtime owner 是 `explorer`", stderr.getvalue())
        self.assertIn("不能显式传 `--instance default`", stderr.getvalue())

    def test_fcodex_threadless_launch_keeps_default_running_fallback(self) -> None:
        resolved_target = CliRuntimeTarget(
            instance_name="default",
            data_dir=_default_data_dir(),
            app_server_url="ws://127.0.0.1:8765",
        )
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli.resolve_cli_runtime_target", return_value=resolved_target
            ) as mock_resolve_target:
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9100", Mock()),
                ):
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with patch("sys.argv", ["fcodex", "session"]):
                            fcodex_main()

        self.assertTrue(
            mock_resolve_target.call_args.kwargs["allow_default_running_fallback"]
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9100",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                os.getcwd(),
                "session",
            ],
        )

    def test_fcodex_explicit_cd_is_forwarded_to_proxy(self) -> None:
        with patch(
            "bot.fcodex.cli.load_config_file",
            return_value={
                "codex_command": "codex",
                "app_server_url": "ws://127.0.0.1:8765",
            },
        ):
            with patch(
                "bot.fcodex.cli._launch_local_cwd_proxy",
                return_value=("ws://127.0.0.1:9101", Mock()),
            ) as mock_proxy:
                with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                    with patch("sys.argv", ["fcodex", "--cd", "/home/tester/project"]):
                        fcodex_main()

        mock_proxy.assert_called_once_with(
            "ws://127.0.0.1:8765",
            "/home/tester/project",
            _default_data_dir(),
            proxy_auth_token=ANY,
        )
        self.assertEqual(
            mock_exec.call_args[0][1],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9101",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                "/home/tester/project",
            ],
        )

    def test_fcodex_uses_subprocess_on_windows_and_cleans_proxy(self) -> None:
        proxy_process = Mock()
        proxy_process.poll.return_value = None
        child_process = Mock()
        child_process.wait.return_value = 7
        child_process.poll.return_value = 7
        with patch("bot.fcodex.cli.is_windows", return_value=True):
            with patch(
                "bot.fcodex.cli.load_config_file",
                return_value={
                    "codex_command": "codex",
                    "app_server_url": "ws://127.0.0.1:8765",
                },
            ):
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9101", proxy_process),
                ) as mock_proxy:
                    with patch(
                        "bot.fcodex.cli.subprocess.Popen", return_value=child_process
                    ) as mock_popen:
                        with patch(
                            "sys.argv", ["fcodex", "--cd", "/home/tester/project"]
                        ):
                            with self.assertRaises(SystemExit) as exc:
                                fcodex_main()

        self.assertEqual(exc.exception.code, 7)
        self.assertEqual(
            mock_popen.call_args.args[0],
            [
                "codex",
                "--remote",
                "ws://127.0.0.1:9101",
                "--remote-auth-token-env",
                FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
                "--cd",
                "/home/tester/project",
            ],
        )
        self.assertEqual(
            mock_popen.call_args.kwargs["env"]["FOCUS_INSTANCE"], "default"
        )
        self.assertEqual(
            mock_popen.call_args.kwargs["env"]["FOCUS_DATA_DIR"],
            str(_default_data_dir()),
        )
        self.assertEqual(
            mock_popen.call_args.kwargs["env"][FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR],
            mock_proxy.call_args.kwargs["proxy_auth_token"],
        )
        proxy_process.terminate.assert_called_once_with()
        proxy_process.wait.assert_called_once_with(timeout=1.0)

    def test_fcodex_windows_interrupt_cleans_codex_and_proxy(self) -> None:
        proxy_process = Mock()
        proxy_process.poll.return_value = None
        child_process = Mock()
        child_process.wait.side_effect = [KeyboardInterrupt, None]
        child_process.poll.return_value = None
        with patch("bot.fcodex.cli.is_windows", return_value=True):
            with patch(
                "bot.fcodex.cli.load_config_file",
                return_value={
                    "codex_command": "codex",
                    "app_server_url": "ws://127.0.0.1:8765",
                },
            ):
                with patch(
                    "bot.fcodex.cli._launch_local_cwd_proxy",
                    return_value=("ws://127.0.0.1:9101", proxy_process),
                ):
                    with patch(
                        "bot.fcodex.cli.subprocess.Popen", return_value=child_process
                    ):
                        with patch(
                            "sys.argv", ["fcodex", "--cd", "/home/tester/project"]
                        ):
                            with self.assertRaises(KeyboardInterrupt):
                                fcodex_main()

        child_process.terminate.assert_called_once_with()
        self.assertEqual(child_process.wait.call_args_list[0].args, ())
        self.assertEqual(child_process.wait.call_args_list[1].kwargs, {"timeout": 1.0})
        proxy_process.terminate.assert_called_once_with()
        proxy_process.wait.assert_called_once_with(timeout=1.0)

    def test_launch_local_cwd_proxy_passes_parent_pid(self) -> None:
        process = Mock()
        process.stdout.readline.return_value = "ws://127.0.0.1:9100\n"
        process.poll.return_value = None
        with patch("bot.fcodex.cli.os.getpid", return_value=4321):
            with patch(
                "bot.fcodex.cli.subprocess.Popen", return_value=process
            ) as mock_popen:
                proxy_url, _ = _launch_local_cwd_proxy(
                    "ws://127.0.0.1:8765",
                    "/tmp/project",
                    Path("/tmp/fcodex-data"),
                    service_token="svc-token",
                    proxy_auth_token="proxy-auth-token",
                )

        self.assertEqual(proxy_url, "ws://127.0.0.1:9100")
        cmd = mock_popen.call_args.args[0]
        self.assertIn("--data-dir", cmd)
        self.assertIn("/tmp/fcodex-data", cmd)
        self.assertIn("--parent-pid", cmd)
        self.assertIn("4321", cmd)
        self.assertNotIn("--service-token", cmd)
        self.assertEqual(
            mock_popen.call_args.kwargs["env"][FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR],
            "proxy-auth-token",
        )
        self.assertEqual(
            mock_popen.call_args.kwargs["env"][FOCUS_SERVICE_TOKEN_ENV_VAR],
            "svc-token",
        )
