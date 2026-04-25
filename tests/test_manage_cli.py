import io
import os
import pathlib
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bot.instance_layout import resolve_instance_paths
from bot.manage_cli import (
    _ensure_instance_scaffold,
    _handle_install,
    _handle_instance_create,
    _handle_instance_list,
    _handle_instance_remove,
    _write_wrapper,
)
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.stores.service_instance_lease import ServiceInstanceLease


class ManageCliTests(unittest.TestCase):
    def test_handle_install_creates_scaffold_and_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_BIN_DIR": str(bin_dir),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                result = _handle_install()

            self.assertEqual(result, 0)
            self.assertTrue((config_root / "system.yaml").exists())
            self.assertTrue((config_root / "codex.yaml").exists())
            self.assertTrue((config_root / "init.token").exists())
            self.assertTrue(env_file.exists())
            self.assertTrue((bin_dir / "feishu-codex").exists())
            self.assertTrue((bin_dir / "feishu-codexd").exists())
            self.assertTrue((bin_dir / "feishu-codexctl").exists())
            self.assertTrue((bin_dir / "fcodex").exists())
            self.assertEqual(stat.S_IMODE((config_root / "system.yaml").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((config_root / "init.token").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)

    def test_write_wrapper_creates_windows_cmd_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            with patch("bot.manage_cli.is_windows", return_value=True):
                with patch("bot.manage_cli._venv_python", return_value=pathlib.Path("C:/Python311/python.exe")):
                    _write_wrapper(root / "feishu-codex", "bot.manage_cli")

            wrapper_path = root / "feishu-codex.cmd"
            self.assertTrue(wrapper_path.exists())
            rendered = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('"C:/Python311/python.exe" -m bot.manage_cli %*', rendered)

    def test_write_wrapper_creates_unix_shell_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            wrapper_path = root / "feishu-codex"
            with patch("bot.manage_cli.is_windows", return_value=False):
                with patch("bot.manage_cli._venv_python", return_value=pathlib.Path("/tmp/venv/bin/python")):
                    _write_wrapper(wrapper_path, "bot.manage_cli")

            self.assertTrue(wrapper_path.exists())
            rendered = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('exec "/tmp/venv/bin/python" -m bot.manage_cli "$@"', rendered)
            self.assertEqual(stat.S_IMODE(wrapper_path.stat().st_mode), 0o755)

    def test_handle_instance_remove_deletes_named_instance_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
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

                manager = _DummyManager()
                with patch("bot.manage_cli.current_service_manager", return_value=manager):
                    result = _handle_instance_remove("corp-a")

            self.assertEqual(result, 0)
            self.assertEqual(manager.identifiers, ["feishu-codex-corp-a"])
            self.assertFalse(paths.config_dir.exists())
            self.assertFalse(paths.data_dir.exists())
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_handle_instance_create_initializes_named_instance_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                result = _handle_instance_create("corp-a")
                paths = resolve_instance_paths("corp-a")

            self.assertEqual(result, 0)
            self.assertTrue((paths.config_dir / "system.yaml").exists())
            self.assertTrue((paths.config_dir / "codex.yaml").exists())
            self.assertTrue((paths.config_dir / "init.token").exists())
            self.assertTrue(paths.data_dir.exists())
            self.assertTrue((data_root / "_global").exists())
            self.assertTrue(env_file.exists())

    def test_handle_instance_create_default_uses_root_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                result = _handle_instance_create("default")

            self.assertEqual(result, 0)
            self.assertTrue((config_root / "system.yaml").exists())
            self.assertTrue((config_root / "codex.yaml").exists())
            self.assertTrue((config_root / "init.token").exists())
            self.assertTrue(data_root.exists())
            self.assertFalse((config_root / "instances" / "default").exists())
            self.assertFalse((data_root / "instances" / "default").exists())

    def test_handle_instance_remove_rejects_default_instance(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能删除 `default` 实例"):
            _handle_instance_remove("default")

    def test_handle_instance_list_includes_default_root_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            self.assertEqual(output_lines[0], "instance\tstate\tconfig_dir\tdata_dir")
            self.assertEqual(output_lines[1], f"default\tstopped\t{config_root}\t{data_root}")

    def test_handle_instance_list_marks_running_named_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
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
                with redirect_stdout(stdout):
                    result = _handle_instance_list()

            self.assertEqual(result, 0)
            output_lines = stdout.getvalue().strip().splitlines()
            self.assertEqual(output_lines[0], "instance\tstate\tconfig_dir\tdata_dir")
            self.assertEqual(output_lines[1], f"corp-a\trunning\t{paths.config_dir}\t{paths.data_dir}")
            self.assertEqual(output_lines[2], f"default\tstopped\t{config_root}\t{data_root}")

    def test_handle_instance_remove_rejects_live_service_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"
            with patch.dict(
                os.environ,
                {
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
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

                with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                    with self.assertRaisesRegex(ValueError, "仍有运行中的 service owner"):
                        _handle_instance_remove("corp-a")


if __name__ == "__main__":
    unittest.main()
