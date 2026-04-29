import io
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from bot.instance_layout import resolve_instance_paths
from bot.install_templates import CODEX_YAML_TEMPLATE, SYSTEM_YAML_TEMPLATE
from bot.manage_cli import (
    _build_parser,
    _handle_autostart_action,
    _handle_autostart_actions,
    _ensure_instance_scaffold,
    _handle_bootstrap_install,
    _handle_config,
    _handle_instance_create,
    _handle_instance_list,
    _handle_instance_remove,
    _handle_service_action,
    _handle_service_actions,
    _write_wrapper,
    main,
)
from bot.service_manager import AutostartStatus
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.stores.service_instance_lease import ServiceInstanceLease


class ManageCliTests(unittest.TestCase):
    def test_import_manage_cli_does_not_emit_lark_pkg_resources_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import bot.manage_cli"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pkg_resources is deprecated as an API", result.stderr)

    def test_import_daemon_entry_does_not_emit_lark_pkg_resources_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import bot.__main__"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pkg_resources is deprecated as an API", result.stderr)

    def test_top_level_help_includes_examples_and_command_descriptions(self) -> None:
        parser = _build_parser()
        rendered = parser.format_help()

        self.assertIn("跨平台本地管理 CLI", rendered)
        self.assertIn("首次安装与修复都请从仓库根目录执行 `bash install.sh`", rendered)
        self.assertIn("常见流程:", rendered)
        self.assertIn("首次安装 / 修复", rendered)
        self.assertIn("bash install.sh", rendered)
        self.assertIn("autostart", rendered)
        self.assertIn("feishu-codex instance create corp-a", rendered)
        self.assertIn("feishu-codex --instance default --instance corp-a status", rendered)
        self.assertIn("创建、列出、删除命名实例", rendered)
        self.assertIn("查看或打开当前实例相关配置文件", rendered)
        self.assertNotIn("    install            ", rendered)
        self.assertNotIn("bootstrap-install", rendered)

    def test_parser_collects_repeated_instance_flags(self) -> None:
        parser = _build_parser()

        args = parser.parse_args(["--instance", "default", "--instance", "corp-a", "status"])

        self.assertEqual(args.instance, ["default", "corp-a"])

    def test_instance_help_includes_subcommand_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["instance", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("实例管理", rendered)
        self.assertIn("instance commands", rendered)
        self.assertIn("create", rendered)
        self.assertIn("remove", rendered)
        self.assertIn("不接受顶层 `--instance`", rendered)

    def test_autostart_help_includes_subcommand_guidance(self) -> None:
        parser = _build_parser()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["autostart", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("登录后自动启动", rendered)
        self.assertIn("enable", rendered)
        self.assertIn("disable", rendered)
        self.assertIn("status", rendered)

    def test_public_install_subcommand_is_not_available(self) -> None:
        parser = _build_parser()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                parser.parse_args(["install"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("公开命令中已无 `install`", stderr.getvalue())
        self.assertNotIn("bootstrap-install", stderr.getvalue())

    def test_handle_bootstrap_install_rebuilds_wrappers_and_known_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "feishu-codex.env"
            ensured_definitions: list[object] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_definitions.append(definition)

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
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                    with redirect_stdout(stdout):
                        result = _handle_bootstrap_install()

            self.assertEqual(result, 0)
            self.assertTrue((config_root / "system.yaml").exists())
            self.assertTrue((config_root / "codex.yaml").exists())
            self.assertTrue((config_root / "init.token").exists())
            self.assertTrue((config_root / "instances" / "corp-a" / "system.yaml").exists())
            self.assertTrue((config_root / "instances" / "corp-a" / "codex.yaml").exists())
            self.assertTrue((config_root / "instances" / "corp-a" / "init.token").exists())
            self.assertTrue(env_file.exists())
            self.assertTrue((bin_dir / "feishu-codex").exists())
            self.assertTrue((bin_dir / "feishu-codexd").exists())
            self.assertTrue((bin_dir / "feishu-codexctl").exists())
            self.assertTrue((bin_dir / "fcodex").exists())
            self.assertEqual(stat.S_IMODE((config_root / "system.yaml").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((config_root / "init.token").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertEqual(
                {definition.identifier for definition in ensured_definitions},
                {"feishu-codex", "feishu-codex-corp-a"},
            )
            commands_by_identifier = {
                definition.identifier: definition.daemon_command for definition in ensured_definitions
            }
            self.assertEqual(
                commands_by_identifier["feishu-codex"],
                (str(bin_dir / "feishu-codex"), "--instance", "default", "run"),
            )
            self.assertEqual(
                commands_by_identifier["feishu-codex-corp-a"],
                (str(bin_dir / "feishu-codex"), "--instance", "corp-a", "run"),
            )
            rendered = (bin_dir / "feishu-codex").read_text(encoding="utf-8")
            self.assertIn(f'exec "{data_root / ".venv" / "bin" / "python"}" -m bot.manage_cli "$@"', rendered)
            summary = stdout.getvalue()
            self.assertIn("已重建实例: corp-a, default。不覆盖各实例现有用户配置", summary)
            self.assertIn("  - 本地服务进程管理 feishu-codex --help", summary)
            self.assertIn("  - 本地查看、管理 binding / thread 状态  feishu-codexctl --help", summary)
            self.assertIn("  1. 配置飞书应用、provider 环境变量", summary)
            self.assertIn("    - feishu-codex config --open system", summary)
            self.assertIn("    - feishu-codex config --open env（按需）", summary)

    def test_ensure_instance_scaffold_writes_detected_initial_codex_command_without_changing_example(self) -> None:
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
                with patch(
                    "bot.manage_cli.render_initial_codex_yaml",
                    return_value="codex_command: /stable/node /stable/codex.js\n",
                ):
                    _ensure_instance_scaffold("default")

            self.assertEqual(
                (config_root / "codex.yaml").read_text(encoding="utf-8"),
                "codex_command: /stable/node /stable/codex.js\n",
            )
            self.assertEqual((config_root / "codex.yaml.example").read_text(encoding="utf-8"), CODEX_YAML_TEMPLATE)

    def test_handle_bootstrap_install_preserves_existing_user_config_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "feishu-codex.env"

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

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
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")
                (paths.config_dir / "system.yaml").write_text("app_id: custom-app\n", encoding="utf-8")
                (paths.config_dir / "codex.yaml").write_text("model: custom-model\n", encoding="utf-8")
                (paths.config_dir / "init.token").write_text("custom-token\n", encoding="utf-8")
                env_file.write_text("OPENAI_API_KEY=custom-key\n", encoding="utf-8")
                data_marker = paths.data_dir / "keep.txt"
                data_marker.write_text("preserve me\n", encoding="utf-8")
                (paths.config_dir / "system.yaml.example").write_text("stale-system-example\n", encoding="utf-8")
                (paths.config_dir / "codex.yaml.example").write_text("stale-codex-example\n", encoding="utf-8")

                with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                    result = _handle_bootstrap_install()

            self.assertEqual(result, 0)
            self.assertEqual((paths.config_dir / "system.yaml").read_text(encoding="utf-8"), "app_id: custom-app\n")
            self.assertEqual((paths.config_dir / "codex.yaml").read_text(encoding="utf-8"), "model: custom-model\n")
            self.assertEqual((paths.config_dir / "init.token").read_text(encoding="utf-8"), "custom-token\n")
            self.assertEqual(env_file.read_text(encoding="utf-8"), "OPENAI_API_KEY=custom-key\n")
            self.assertEqual(data_marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual((paths.config_dir / "system.yaml.example").read_text(encoding="utf-8"), SYSTEM_YAML_TEMPLATE)
            self.assertEqual((paths.config_dir / "codex.yaml.example").read_text(encoding="utf-8"), CODEX_YAML_TEMPLATE)

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
            bin_dir = root / "bin"
            env_file = config_root / "feishu-codex.env"
            ensured_definitions: list[object] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_definitions.append(definition)

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
                with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                    result = _handle_instance_create("corp-a")
                    paths = resolve_instance_paths("corp-a")

            self.assertEqual(result, 0)
            self.assertTrue((paths.config_dir / "system.yaml").exists())
            self.assertTrue((paths.config_dir / "codex.yaml").exists())
            self.assertTrue((paths.config_dir / "init.token").exists())
            self.assertTrue(paths.data_dir.exists())
            self.assertTrue((data_root / "_global").exists())
            self.assertTrue(env_file.exists())
            self.assertEqual([definition.identifier for definition in ensured_definitions], ["feishu-codex-corp-a"])
            self.assertEqual(
                ensured_definitions[0].daemon_command,
                (str(bin_dir / "feishu-codex"), "--instance", "corp-a", "run"),
            )

    def test_handle_instance_create_default_uses_root_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            env_file = config_root / "feishu-codex.env"
            ensured_definitions: list[object] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_definitions.append(definition)

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
                with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                    result = _handle_instance_create("default")

            self.assertEqual(result, 0)
            self.assertTrue((config_root / "system.yaml").exists())
            self.assertTrue((config_root / "codex.yaml").exists())
            self.assertTrue((config_root / "init.token").exists())
            self.assertTrue(data_root.exists())
            self.assertFalse((config_root / "instances" / "default").exists())
            self.assertFalse((data_root / "instances" / "default").exists())
            self.assertEqual([definition.identifier for definition in ensured_definitions], ["feishu-codex"])
            self.assertEqual(
                ensured_definitions[0].daemon_command,
                (str(bin_dir / "feishu-codex"), "--instance", "default", "run"),
            )

    def test_handle_autostart_action_uses_manager_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

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
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=manager):
                        result = _handle_autostart_action("corp-a", "enable")

            self.assertEqual(result, 0)
            self.assertEqual(manager.enabled, ["corp-a"])
            self.assertIn("autostart enabled: feishu-codex-corp-a", stdout.getvalue())

    def test_handle_autostart_status_uses_platform_specific_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

            class _DummyManager:
                def autostart_status(self, definition) -> AutostartStatus:
                    return AutostartStatus(
                        enabled=True,
                        source="systemctl --user is-enabled feishu-codex@corp-a",
                        detail="enabled",
                    )

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
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                        result = _handle_autostart_action("corp-a", "status")

            self.assertEqual(result, 0)
            rendered = stdout.getvalue()
            self.assertIn("autostart: enabled", rendered)
            self.assertIn("systemctl --user is-enabled feishu-codex@corp-a: enabled", rendered)

    def test_handle_service_action_uses_manager_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

            class _DummyManager:
                def __init__(self) -> None:
                    self.started: list[str] = []

                def display_name(self, definition) -> str:
                    return f"feishu-codex@{definition.instance_name}"

                def start(self, definition) -> None:
                    self.started.append(definition.instance_name)

            manager = _DummyManager()
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
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=manager):
                        result = _handle_service_action("corp-a", "start")

            self.assertEqual(result, 0)
            self.assertEqual(manager.started, ["corp-a"])
            self.assertIn("started service: feishu-codex@corp-a", stdout.getvalue())

    def test_handle_service_status_uses_platform_specific_source_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

            class _DummyManager:
                def status(self, definition):
                    del definition
                    from bot.service_manager import ServiceStatus

                    return ServiceStatus(
                        installed=True,
                        running=False,
                        source="systemctl --user is-active feishu-codex@corp-a",
                        detail="activating",
                    )

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
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=_DummyManager()):
                        result = _handle_service_action("corp-a", "status")

            self.assertEqual(result, 3)
            rendered = stdout.getvalue()
            self.assertIn("service: installed", rendered)
            self.assertIn("running: no", rendered)
            self.assertIn("systemctl --user is-active feishu-codex@corp-a: activating", rendered)

    def test_handle_service_actions_supports_multiple_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

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
                            source="systemctl --user is-active feishu-codex",
                            detail="active",
                        )
                    return ServiceStatus(
                        installed=True,
                        running=False,
                        source="systemctl --user is-active feishu-codex@corp-a",
                        detail="inactive",
                    )

            manager = _DummyManager()
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
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=manager):
                        result = _handle_service_actions(["default", "corp-a"], "status")

            self.assertEqual(result, 3)
            self.assertEqual(manager.status_calls, ["default", "corp-a"])
            rendered = stdout.getvalue()
            self.assertIn("instance: default", rendered)
            self.assertIn("systemctl --user is-active feishu-codex: active", rendered)
            self.assertIn("instance: corp-a", rendered)
            self.assertIn("systemctl --user is-active feishu-codex@corp-a: inactive", rendered)

    def test_handle_autostart_actions_supports_multiple_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "feishu-codex.env"

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
                    "FC_CONFIG_ROOT": str(config_root),
                    "FC_DATA_ROOT": str(data_root),
                    "FC_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    with patch("bot.manage_cli.current_service_manager", return_value=manager):
                        result = _handle_autostart_actions(["default", "corp-a"], "enable")

            self.assertEqual(result, 0)
            self.assertEqual(manager.enabled, ["default", "corp-a"])
            rendered = stdout.getvalue()
            self.assertIn("instance: default", rendered)
            self.assertIn("autostart enabled: feishu-codex", rendered)
            self.assertIn("instance: corp-a", rendered)
            self.assertIn("autostart enabled: feishu-codex-corp-a", rendered)

    def test_main_rejects_multiple_instances_for_run(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                main(["--instance", "default", "--instance", "corp-a", "run"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("`run` 当前只支持单个实例", stderr.getvalue())

    def test_main_rejects_top_level_instance_for_instance_subcommands(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as exc:
                main(["--instance", "default", "instance", "list"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("`feishu-codex instance ...` 不接受顶层 `--instance`", stderr.getvalue())

    def test_named_instance_commands_do_not_implicitly_create_instance(self) -> None:
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
                with self.assertRaisesRegex(ValueError, "instance create corp-a"):
                    _handle_service_action("corp-a", "start")
                with self.assertRaisesRegex(ValueError, "instance create corp-a"):
                    _handle_config("corp-a", "system", open_editor=False)

            self.assertFalse((config_root / "instances" / "corp-a").exists())
            self.assertFalse((data_root / "instances" / "corp-a").exists())

    def test_config_env_does_not_require_named_instance(self) -> None:
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
                    result = _handle_config("corp-a", "env", open_editor=False)

            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue().strip(), str(env_file))
            self.assertTrue(env_file.exists())
            self.assertFalse((config_root / "instances" / "corp-a").exists())
            self.assertFalse((data_root / "instances" / "corp-a").exists())

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
