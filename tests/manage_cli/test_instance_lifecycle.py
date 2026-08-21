import io
import os
import pathlib
import shutil
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch


from bot.instance_layout import resolve_instance_paths
from bot.manage_cli.errors import InstallLifecycleError
from bot.manage_cli.instance_commands import (
    _handle_instance_create,
    _handle_instance_list,
    _handle_instance_remove,
)
from bot.manage_cli.provisioning import _ensure_instance_scaffold
from bot.service_manager import ServiceManagerError, ServiceStatus
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.stores.service_instance_lease import ServiceInstanceLease
from tests.manage_cli.support import ManageCliTestCase


class ManageCliInstanceLifecycleTests(ManageCliTestCase):
    def test_handle_instance_remove_deletes_named_instance_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"
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
                paths = resolve_instance_paths("corp-a")

                class _DummyManager:
                    def __init__(self) -> None:
                        self.identifiers: list[str] = []

                    def uninstall(self, definition) -> None:
                        self.identifiers.append(definition.identifier)

                    def status(self, definition) -> ServiceStatus:
                        del definition
                        return ServiceStatus(installed=False, running=False)

                    def is_instance_uninstalled(self, definition, status) -> bool:
                        del definition
                        return not status.installed and not status.running

                manager = _DummyManager()
                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=manager):
                    result = _handle_instance_remove("corp-a")

            self.assertEqual(result, 0)
            self.assertEqual(manager.identifiers, ["focus-corp-a"])
            self.assertFalse(paths.config_dir.exists())
            self.assertFalse(paths.data_dir.exists())
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_handle_instance_create_initializes_named_instance_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "focus.env"
            ensured_definitions: list[object] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_definitions.append(definition)

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                    result = _handle_instance_create("corp-a")
                    paths = resolve_instance_paths("corp-a")

            self.assertEqual(result, 0)
            self.assertTrue((paths.config_dir / "system.yaml").exists())
            self.assertTrue((paths.config_dir / "codex.yaml").exists())
            self.assertTrue((paths.config_dir / "init.token").exists())
            self.assertTrue(paths.data_dir.exists())
            self.assertTrue((data_root / "_global").exists())
            self.assertTrue(env_file.exists())
            self.assertEqual([definition.identifier for definition in ensured_definitions], ["focus-corp-a"])
            self.assertEqual(
                ensured_definitions[0].daemon_command,
                (
                    str(data_root / ".venv" / "bin" / "python"),
                    "-I",
                    "-m",
                    "bot.__main__",
                    "--instance",
                    "corp-a",
                ),
            )

    def test_handle_instance_create_default_uses_root_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "focus.env"
            ensured_definitions: list[object] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_definitions.append(definition)

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                    result = _handle_instance_create("default")

            self.assertEqual(result, 0)
            self.assertTrue((config_root / "system.yaml").exists())
            self.assertTrue((config_root / "codex.yaml").exists())
            self.assertTrue((config_root / "init.token").exists())
            self.assertTrue(data_root.exists())
            self.assertFalse((config_root / "instances" / "default").exists())
            self.assertFalse((data_root / "instances" / "default").exists())
            self.assertEqual([definition.identifier for definition in ensured_definitions], ["focus"])
            self.assertEqual(
                ensured_definitions[0].daemon_command,
                (
                    str(data_root / ".venv" / "bin" / "python"),
                    "-I",
                    "-m",
                    "bot.__main__",
                    "--instance",
                    "default",
                ),
            )

    def test_handle_instance_remove_rejects_default_instance(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能删除 `default` 实例"):
            _handle_instance_remove("default")

    def test_handle_instance_list_includes_default_root_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(installed=True, running=False, source="systemctl", detail="inactive")

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                stdout = io.StringIO()
                with patch("bot.manage_cli.instance_commands.list_running_instances", return_value=[]):
                    with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                        with redirect_stdout(stdout):
                            result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            self.assertNotIn("\t", stdout.getvalue())
            header = ["INSTANCE", "SERVICE", "RUNTIME", "PID", "APP_SERVER", "CONFIG_DIR", "DATA_DIR"]
            row = ["default", "stopped", "-", "-", "-", str(config_root), str(data_root)]
            self.assertEqual(
                self._visual_cell_starts(output_lines[1], row),
                self._visual_cell_starts(output_lines[0], header),
            )

    def test_handle_instance_list_marks_running_named_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    from bot.service_manager import ServiceStatus

                    if definition.instance_name == "corp-a":
                        return ServiceStatus(installed=True, running=True, source="systemctl", detail="active")
                    return ServiceStatus(installed=True, running=False, source="systemctl", detail="inactive")

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")
                store = InstanceRegistryStore()
                store.register(
                    build_instance_registry_entry(
                        instance_name="corp-a",
                        service_token="svc-token",
                        control_endpoint="http://127.0.0.1:1",
                        app_server_url="http://127.0.0.1:2",
                        config_dir=paths.config_dir,
                        data_dir=paths.data_dir,
                        owner_pid=os.getpid(),
                    )
                )
                stdout = io.StringIO()
                with patch(
                    "bot.manage_cli.instance_commands.list_running_instances",
                    return_value=[build_instance_registry_entry(
                        instance_name="corp-a",
                        service_token="svc-token",
                        control_endpoint="http://127.0.0.1:1",
                        app_server_url="http://127.0.0.1:2",
                        config_dir=paths.config_dir,
                        data_dir=paths.data_dir,
                        owner_pid=os.getpid(),
                    )],
                ):
                    with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.service_commands.control_request",
                            return_value={
                                "pid": 4321,
                                "control_endpoint": "http://127.0.0.1:1",
                                "app_server_url": "http://127.0.0.1:2",
                            },
                        ):
                            with redirect_stdout(stdout):
                                result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            self.assertNotIn("\t", stdout.getvalue())
            header = ["INSTANCE", "SERVICE", "RUNTIME", "PID", "APP_SERVER", "CONFIG_DIR", "DATA_DIR"]
            row1 = ["corp-a", "running", "available", "4321", "http://127.0.0.1:2", str(paths.config_dir), str(paths.data_dir)]
            row2 = ["default", "stopped", "-", "-", "-", str(config_root), str(data_root)]
            header_starts = self._visual_cell_starts(output_lines[0], header)
            self.assertEqual(self._visual_cell_starts(output_lines[1], row1), header_starts)
            self.assertEqual(self._visual_cell_starts(output_lines[2], row2), header_starts)

    def test_handle_instance_list_shows_runtime_when_service_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(installed=True, running=False, source="systemctl", detail="inactive")

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")
                entry = build_instance_registry_entry(
                    instance_name="corp-a",
                    service_token="svc-token",
                    control_endpoint="http://127.0.0.1:1",
                    app_server_url="http://127.0.0.1:2",
                    config_dir=paths.config_dir,
                    data_dir=paths.data_dir,
                    owner_pid=os.getpid(),
                )
                stdout = io.StringIO()
                with patch("bot.manage_cli.instance_commands.list_running_instances", return_value=[entry]):
                    with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.service_commands.control_request",
                            return_value={
                                "pid": 4321,
                                "control_endpoint": "http://127.0.0.1:1",
                                "app_server_url": "http://127.0.0.1:2",
                            },
                        ):
                            with redirect_stdout(stdout):
                                result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            header = ["INSTANCE", "SERVICE", "RUNTIME", "PID", "APP_SERVER", "CONFIG_DIR", "DATA_DIR"]
            row = ["corp-a", "stopped", "available", "4321", "http://127.0.0.1:2", str(paths.config_dir), str(paths.data_dir)]
            self.assertEqual(
                self._visual_cell_starts(output_lines[1], row),
                self._visual_cell_starts(output_lines[0], header),
            )

    def test_handle_instance_list_shows_runtime_when_service_state_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceManagerError

                    raise ServiceManagerError("systemd unavailable")

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")
                entry = build_instance_registry_entry(
                    instance_name="corp-a",
                    service_token="svc-token",
                    control_endpoint="http://127.0.0.1:1",
                    app_server_url="http://127.0.0.1:2",
                    config_dir=paths.config_dir,
                    data_dir=paths.data_dir,
                    owner_pid=os.getpid(),
                )
                stdout = io.StringIO()
                with patch("bot.manage_cli.instance_commands.list_running_instances", return_value=[entry]):
                    with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.service_commands.control_request",
                            return_value={
                                "pid": 4321,
                                "control_endpoint": "http://127.0.0.1:1",
                                "app_server_url": "http://127.0.0.1:2",
                            },
                        ):
                            with redirect_stdout(stdout):
                                result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            header = ["INSTANCE", "SERVICE", "RUNTIME", "PID", "APP_SERVER", "CONFIG_DIR", "DATA_DIR"]
            row = ["corp-a", "unknown", "available", "4321", "http://127.0.0.1:2", str(paths.config_dir), str(paths.data_dir)]
            self.assertEqual(
                self._visual_cell_starts(output_lines[1], row),
                self._visual_cell_starts(output_lines[0], header),
            )

    def test_handle_instance_remove_rejects_live_service_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"
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
                paths = resolve_instance_paths("corp-a")
                lease = ServiceInstanceLease(paths.data_dir)
                lease.acquire(control_endpoint="http://127.0.0.1:1")
                self.addCleanup(lease.release)

                class _DummyManager:
                    def uninstall(self, definition) -> None:
                        return None

                    def status(self, definition) -> ServiceStatus:
                        del definition
                        return ServiceStatus(installed=False, running=False)

                    def is_instance_uninstalled(self, definition, status) -> bool:
                        del definition
                        return not status.installed and not status.running

                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                    with self.assertRaisesRegex(InstallLifecycleError, "maintenance 所有权"):
                        _handle_instance_remove("corp-a")

                self.assertTrue(paths.config_dir.exists())
                self.assertTrue(paths.data_dir.exists())

    def test_handle_instance_remove_stops_before_deletion_when_uninstall_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")

                class _FailingManager:
                    def uninstall(self, definition) -> None:
                        raise ServiceManagerError(f"cannot remove {definition.identifier}")

                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_FailingManager()):
                    with self.assertRaisesRegex(InstallLifecycleError, "uninstall 失败"):
                        _handle_instance_remove("corp-a")

                self.assertTrue(paths.config_dir.exists())
                self.assertTrue(paths.data_dir.exists())

    def test_handle_instance_remove_stops_when_registration_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")

                class _IncompleteManager:
                    def uninstall(self, definition) -> None:
                        del definition

                    def status(self, definition) -> ServiceStatus:
                        del definition
                        return ServiceStatus(installed=True, running=False)

                    def is_instance_uninstalled(self, definition, status) -> bool:
                        del definition
                        return not status.installed and not status.running

                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_IncompleteManager()):
                    with self.assertRaisesRegex(InstallLifecycleError, "注册或进程仍然存在"):
                        _handle_instance_remove("corp-a")

                self.assertTrue(paths.config_dir.exists())
                self.assertTrue(paths.data_dir.exists())

    def test_handle_instance_remove_reports_partial_delete_failure_without_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            stdout = io.StringIO()
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")

                class _DummyManager:
                    def uninstall(self, definition) -> None:
                        del definition

                    def status(self, definition) -> ServiceStatus:
                        del definition
                        return ServiceStatus(installed=False, running=False)

                    def is_instance_uninstalled(self, definition, status) -> bool:
                        del definition
                        return not status.installed and not status.running

                real_rmtree = shutil.rmtree

                def _remove(path, *args, **kwargs):
                    if pathlib.Path(path) == paths.data_dir:
                        raise PermissionError("denied")
                    return real_rmtree(path, *args, **kwargs)

                with patch("bot.manage_cli.instance_commands.current_service_manager", return_value=_DummyManager()):
                    with patch("bot.manage_cli.instance_commands.shutil.rmtree", side_effect=_remove):
                        with redirect_stdout(stdout):
                            with self.assertRaisesRegex(InstallLifecycleError, "本次已删除：config"):
                                _handle_instance_remove("corp-a")

                self.assertFalse(paths.config_dir.exists())
                self.assertTrue(paths.data_dir.exists())
                self.assertNotIn("已删除实例", stdout.getvalue())
