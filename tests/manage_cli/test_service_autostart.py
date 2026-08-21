import io
import os
import pathlib
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch


from bot.instance_layout import resolve_instance_paths
from bot.manage_cli.provisioning import _ensure_instance_scaffold
from bot.manage_cli.service_commands import (
    _RuntimeStatusSummary,
    _handle_autostart_action,
    _handle_autostart_actions,
    _handle_service_action,
    _handle_service_actions,
    _print_service_runtime_summary,
)
from bot.service_control_plane import ServiceControlError
from bot.service_manager import AutostartStatus
from bot.stores.service_instance_lease import ServiceInstanceLease
from tests.manage_cli.support import ManageCliTestCase


class ManageCliServiceAutostartTests(ManageCliTestCase):
    def test_runtime_summary_distinguishes_web_gateway_states(self) -> None:
        cases = (
            (
                {
                    "web_gateway_enabled": True,
                    "web_gateway_url": "http://127.0.0.1:8766",
                },
                "web gateway: http://127.0.0.1:8766",
            ),
            (
                {"web_gateway_enabled": False, "web_gateway_url": ""},
                "web gateway: disabled",
            ),
            (
                {"web_gateway_enabled": True, "web_gateway_url": ""},
                "web gateway: unavailable",
            ),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    _print_service_runtime_summary(
                        _RuntimeStatusSummary(available=True, result=result)
                    )
                self.assertIn(expected, stdout.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            _print_service_runtime_summary(
                _RuntimeStatusSummary(
                    available=False,
                    result={},
                    reason="control plane unavailable",
                )
            )
        self.assertIn("web gateway: unavailable", stdout.getvalue())

    def test_handle_autostart_action_uses_manager_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def __init__(self) -> None:
                    self.enabled: list[str] = []

                def display_name(self, definition) -> str:
                    return definition.identifier

                def autostart_enable(self, definition) -> None:
                    self.enabled.append(definition.instance_name)

            manager = _DummyManager()
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=manager):
                        result = _handle_autostart_action("corp-a", "enable")

            self.assertEqual(result, 0)
            self.assertEqual(manager.enabled, ["corp-a"])
            self.assertIn("autostart enabled: focus-corp-a", stdout.getvalue())

    def test_handle_autostart_status_uses_platform_specific_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def autostart_status(self, definition) -> AutostartStatus:
                    return AutostartStatus(
                        enabled=True,
                        source="systemctl --user is-enabled focus@corp-a",
                        detail="enabled",
                    )

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=_DummyManager()):
                        result = _handle_autostart_action("corp-a", "status")

            self.assertEqual(result, 0)
            rendered = stdout.getvalue()
            self.assertIn("autostart: enabled", rendered)
            self.assertIn("systemctl --user is-enabled focus@corp-a: enabled", rendered)

    def test_handle_service_action_uses_manager_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def __init__(self) -> None:
                    self.started: list[str] = []

                def display_name(self, definition) -> str:
                    return f"focus@{definition.instance_name}"

                def start(self, definition) -> None:
                    self.started.append(definition.instance_name)

            manager = _DummyManager()
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=manager):
                        result = _handle_service_action("corp-a", "start")

            self.assertEqual(result, 0)
            self.assertEqual(manager.started, ["corp-a"])
            self.assertIn("started service: focus@corp-a", stdout.getvalue())

    def test_handle_service_status_uses_platform_specific_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(
                        installed=True,
                        running=False,
                        source="systemctl --user is-active focus@corp-a",
                        detail="activating",
                    )

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=_DummyManager()):
                        result = _handle_service_action("corp-a", "status")

            self.assertEqual(result, 3)
            rendered = stdout.getvalue()
            self.assertIn("service: stopped", rendered)
            self.assertIn("service source: systemctl --user is-active focus@corp-a: activating", rendered)

    def test_handle_service_status_prints_runtime_summary_when_service_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(
                        installed=True,
                        running=True,
                        source="systemctl --user is-active focus@corp-a",
                        detail="active",
                    )

            runtime_status = {
                "pid": 1234,
                "control_endpoint": "tcp://127.0.0.1:32001",
                "app_server_url": "ws://127.0.0.1:8765",
                "web_gateway_enabled": True,
                "web_gateway_url": "http://127.0.0.1:8766",
                "bootstrap_token": "must-not-be-rendered",
                "binding_count": 3,
                "bound_binding_count": 2,
                "attached_binding_count": 1,
                "thread_count": 2,
                "attached_thread_count": 1,
                "loaded_thread_count": 1,
                "running_binding_ids": ["p2p:ou:chat"],
                "backend_reset_status": "idle",
            }
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=_DummyManager()):
                        with patch("bot.manage_cli.service_commands.control_request", return_value=runtime_status):
                            result = _handle_service_action("corp-a", "status")

            self.assertEqual(result, 0)
            rendered = stdout.getvalue()
            self.assertIn("service: running", rendered)
            self.assertIn("runtime: available", rendered)
            self.assertIn("control endpoint: tcp://127.0.0.1:32001", rendered)
            self.assertIn("app server: ws://127.0.0.1:8765", rendered)
            self.assertIn("web gateway: http://127.0.0.1:8766", rendered)
            self.assertNotIn("must-not-be-rendered", rendered)
            self.assertIn("bindings: total=3 bound=2 attached=1", rendered)
            self.assertIn("threads: bound=2 feishu-attached=1 loaded=1", rendered)

    def test_handle_service_status_keeps_success_when_running_runtime_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(
                        installed=True,
                        running=True,
                        source="systemctl --user is-active focus@corp-a",
                        detail="active",
                    )

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                lease = ServiceInstanceLease(resolve_instance_paths("corp-a").data_dir)
                lease.acquire(control_endpoint="tcp://127.0.0.1:32001")
                try:
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        with patch("bot.manage_cli.service_commands.current_service_manager", return_value=_DummyManager()):
                            with patch(
                                "bot.manage_cli.service_commands.control_request",
                                side_effect=ServiceControlError("控制面连接失败"),
                            ):
                                result = _handle_service_action("corp-a", "status")
                finally:
                    lease.release()

            self.assertEqual(result, 0)
            rendered = stdout.getvalue()
            self.assertIn("service: running", rendered)
            self.assertIn("runtime: unavailable", rendered)
            self.assertIn("control endpoint: tcp://127.0.0.1:32001", rendered)
            self.assertIn("reason: 控制面连接失败", rendered)

    def test_handle_service_actions_supports_multiple_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def __init__(self) -> None:
                    self.status_calls: list[str] = []

                def status(self, definition):
                    self.status_calls.append(definition.instance_name)
                    from bot.service_manager import ServiceStatus

                    if definition.instance_name == "default":
                        return ServiceStatus(
                            installed=True,
                            running=True,
                            source="systemctl --user is-active focus",
                            detail="active",
                        )
                    return ServiceStatus(
                        installed=True,
                        running=False,
                        source="systemctl --user is-active focus@corp-a",
                        detail="inactive",
                    )

            manager = _DummyManager()
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=manager):
                        result = _handle_service_actions(["default", "corp-a"], "status")

            self.assertEqual(result, 3)
            self.assertEqual(manager.status_calls, ["default", "corp-a"])
            rendered = stdout.getvalue()
            self.assertIn("instance: default", rendered)
            self.assertIn("systemctl --user is-active focus: active", rendered)
            self.assertIn("instance: corp-a", rendered)
            self.assertIn("systemctl --user is-active focus@corp-a: inactive", rendered)

    def test_handle_autostart_actions_supports_multiple_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def __init__(self) -> None:
                    self.enabled: list[str] = []

                def display_name(self, definition) -> str:
                    return definition.identifier

                def autostart_enable(self, definition) -> None:
                    self.enabled.append(definition.instance_name)

            manager = _DummyManager()
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.service_commands.current_service_manager", return_value=manager):
                        result = _handle_autostart_actions(["default", "corp-a"], "enable")

            self.assertEqual(result, 0)
            self.assertEqual(manager.enabled, ["default", "corp-a"])
            rendered = stdout.getvalue()
            self.assertIn("instance: default", rendered)
            self.assertIn("autostart enabled: focus", rendered)
            self.assertIn("instance: corp-a", rendered)
            self.assertIn("autostart enabled: focus-corp-a", rendered)
