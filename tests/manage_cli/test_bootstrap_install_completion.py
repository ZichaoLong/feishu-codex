import io
import json
import os
import pathlib
import shlex
import stat
import subprocess
import tempfile
import unittest
import venv
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import yaml

from bot.instance_layout import resolve_instance_paths
from bot.install_templates import CODEX_YAML_TEMPLATE, SYSTEM_YAML_TEMPLATE
from bot.manage_cli.errors import InstallLifecycleError
from bot.manage_cli.install_surface import (
    _handle_bootstrap_install,
    _handle_uninstall,
)
from bot.manage_cli.provisioning import (
    _MANAGED_ROOT_MARKER,
    _ensure_instance_scaffold,
    _write_wrapper,
)
from bot.service_manager import ServiceManagerError, ServiceStatus
from bot.stores.service_instance_lease import ServiceInstanceLease
from tests.manage_cli.support import ManageCliTestCase


class ManageCliBootstrapInstallCompletionTests(ManageCliTestCase):
    def test_handle_bootstrap_install_rebuilds_wrappers_and_known_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
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
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                stdout = io.StringIO()
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
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
            self.assertTrue((bin_dir / "focus").exists())
            self.assertTrue((bin_dir / "focusd").exists())
            self.assertTrue((bin_dir / "focusctl").exists())
            self.assertTrue((bin_dir / "fcodex").exists())
            self.assertTrue((bash_completion_dir / "focus").exists())
            self.assertTrue((bash_completion_dir / "focusd").exists())
            self.assertTrue((bash_completion_dir / "focusctl").exists())
            self.assertTrue((bash_completion_dir / "fcodex").exists())
            self.assertTrue(zsh_completion_path.exists())
            self.assertTrue(zsh_rc_path.exists())
            self.assertTrue(powershell_completion_path.exists())
            self.assertTrue(powershell_profile_path.exists())
            self.assertEqual(stat.S_IMODE((config_root / "system.yaml").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((config_root / "init.token").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertEqual(
                {definition.identifier for definition in ensured_definitions},
                {"focus", "focus-corp-a"},
            )
            commands_by_identifier = {
                definition.identifier: definition.daemon_command for definition in ensured_definitions
            }
            managed_python = str(data_root / ".venv" / "bin" / "python")
            self.assertEqual(
                commands_by_identifier["focus"],
                (
                    managed_python,
                    "-I",
                    "-m",
                    "bot.__main__",
                    "--instance",
                    "default",
                ),
            )
            self.assertEqual(
                commands_by_identifier["focus-corp-a"],
                (
                    managed_python,
                    "-I",
                    "-m",
                    "bot.__main__",
                    "--instance",
                    "corp-a",
                ),
            )
            rendered = (bin_dir / "focus").read_text(encoding="utf-8")
            self.assertIn(
                f'exec {shlex.join((managed_python, "-I", "-m", "bot.fcodex.cli"))} "$@"',
                rendered,
            )
            rendered_completion = (bash_completion_dir / "focus").read_text(encoding="utf-8")
            self.assertIn("-I -m bot.shell_completion complete", rendered_completion)
            self.assertIn("complete -o bashdefault -o default -F _focus_complete_focus focus", rendered_completion)
            self.assertIn('source "', zsh_rc_path.read_text(encoding="utf-8"))
            self.assertIn("Register-ArgumentCompleter", powershell_completion_path.read_text(encoding="utf-8"))
            self.assertIn("Test-Path", powershell_profile_path.read_text(encoding="utf-8"))
            summary = stdout.getvalue()
            self.assertIn(f"Bash completion: {bash_completion_dir}", summary)
            self.assertIn(f"zsh completion: {zsh_completion_path}", summary)
            self.assertIn(f"PowerShell completion: {powershell_completion_path}", summary)
            self.assertIn("已重建实例: corp-a, default。不覆盖各实例现有用户配置", summary)
            self.assertIn("  - Codex TUI 工作入口 focus --help 或 fcodex --help", summary)
            self.assertIn("  - 本地管理 focusctl --help", summary)
            self.assertIn(f"命令目录尚不在当前 PATH：{bin_dir}", summary)
            self.assertIn(f"export PATH={shlex.quote(str(bin_dir))}:\"$PATH\"", summary)
            self.assertIn("  1. 配置飞书应用、provider 环境变量", summary)
            self.assertIn("    - focusctl config system --open", summary)
            self.assertIn("    - focusctl config env --open（按需）", summary)
            self.assertIn("  5. 如需在某个目录下启用 FOCUS 附带 skills（可选）", summary)
            self.assertIn("    - 先 cd 到目标目录，再执行 focusctl skill install", summary)
            self.assertIn("    - 如需移除，回到同一目录执行 focusctl skill uninstall", summary)
            self.assertIn("    - 注意：focusctl uninstall/purge 不会删除各工作区中的 .agents/skills", summary)
            self.assertIn("  6. Shell completion", summary)
            self.assertIn("Bash：新开一个 Bash shell 通常会自动生效", summary)
            self.assertIn("zsh：已写入自动加载钩子", summary)
            self.assertIn("PowerShell：已写入自动加载 profile", summary)

    def test_handle_bootstrap_install_is_idempotent_across_consecutive_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            environment = {
                **self._isolated_install_env(root),
                "HOME": str(root),
                "FNM_DIR": "",
                "NVM_DIR": "",
            }
            ensured_identifiers: list[str] = []

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    ensured_identifiers.append(definition.identifier)

            with patch.dict(os.environ, environment, clear=False):
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=False),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=False),
                ):
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                        with patch("bot.manage_cli.install_surface.shutil.which", return_value=None):
                            with redirect_stdout(io.StringIO()):
                                self.assertEqual(_handle_bootstrap_install(), 0)

                                config_root = pathlib.Path(environment["FOCUS_CONFIG_ROOT"])
                                data_root = pathlib.Path(environment["FOCUS_DATA_ROOT"])
                                bin_dir = pathlib.Path(environment["FOCUS_BIN_DIR"])
                                bash_dir = pathlib.Path(environment["FOCUS_BASH_COMPLETION_DIR"])
                                zsh_rc = pathlib.Path(environment["FOCUS_ZSH_RC_PATH"])
                                powershell_profile = pathlib.Path(environment["FOCUS_POWERSHELL_PROFILE_PATH"])
                                init_token = (config_root / "init.token").read_bytes()
                                wrapper_snapshot = {
                                    path.name: path.read_bytes()
                                    for path in bin_dir.iterdir()
                                    if path.is_file()
                                }
                                (config_root / "system.yaml").write_text(
                                    "app_id: preserved-app\n",
                                    encoding="utf-8",
                                )
                                (config_root / "codex.yaml").write_text(
                                    "codex_command: /preserved/codex\n",
                                    encoding="utf-8",
                                )
                                data_marker = data_root / "preserve-me.txt"
                                data_marker.write_text("preserved\n", encoding="utf-8")

                                self.assertEqual(_handle_bootstrap_install(), 0)

            self.assertEqual(ensured_identifiers, ["focus", "focus"])
            self.assertEqual(
                sorted(path.name for path in bin_dir.iterdir() if path.is_file()),
                ["fcodex", "focus", "focusctl", "focusd"],
            )
            self.assertEqual(
                {path.name: path.read_bytes() for path in bin_dir.iterdir() if path.is_file()},
                wrapper_snapshot,
            )
            self.assertEqual(
                sorted(path.name for path in bash_dir.iterdir() if path.is_file()),
                ["fcodex", "focus", "focusctl", "focusd"],
            )
            self.assertEqual((config_root / "init.token").read_bytes(), init_token)
            self.assertEqual(
                (config_root / "system.yaml").read_text(encoding="utf-8"),
                "app_id: preserved-app\n",
            )
            self.assertEqual(
                (config_root / "codex.yaml").read_text(encoding="utf-8"),
                "codex_command: /preserved/codex\n",
            )
            self.assertEqual(data_marker.read_text(encoding="utf-8"), "preserved\n")
            self.assertEqual(zsh_rc.read_text(encoding="utf-8").count("# >>> focus zsh completion >>>"), 1)
            self.assertEqual(
                powershell_profile.read_text(encoding="utf-8").count(
                    "# >>> focus PowerShell completion >>>"
                ),
                1,
            )

    def test_handle_bootstrap_install_on_windows_adds_bin_dir_to_user_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"
            metadata_path = config_root / "install-state" / "windows-user-path.json"
            user_path_state = {"raw": r"C:\Windows\System32", "type": 2}

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                stdout = io.StringIO()
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.install_surface._read_windows_user_path_value",
                            return_value=(user_path_state["raw"], user_path_state["type"]),
                        ):
                            with patch(
                                "bot.manage_cli.install_surface._write_windows_user_path_value",
                                side_effect=lambda raw_path, *, value_type: user_path_state.update(
                                    {"raw": raw_path, "type": value_type}
                                ),
                            ):
                                with patch("bot.manage_cli.install_surface.shutil.which", return_value=None):
                                    with patch(
                                        "bot.manage_cli.install_surface.detect_stable_codex_command",
                                        return_value="C:/stable/node C:/stable/codex.js",
                                    ):
                                        with redirect_stdout(stdout):
                                            self.assertEqual(_handle_bootstrap_install(), 0)

            self.assertIn(str(bin_dir), user_path_state["raw"])
            self.assertTrue(metadata_path.exists())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["bin_dir"], str(bin_dir))
            self.assertTrue(metadata["added_to_user_path"])
            rendered = stdout.getvalue()
            self.assertIn("Windows 用户 PATH: 已确保包含命令目录", rendered)
            self.assertNotIn("警告: 未检测到 `codex` 命令", rendered)
            self.assertNotIn("PowerShell completion:", rendered)
            self.assertNotIn("Shell completion", rendered)

    def test_handle_bootstrap_install_on_windows_removes_existing_shell_completion_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"
            user_path_state = {"raw": r"C:\Windows\System32", "type": 2}

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                powershell_completion_path.parent.mkdir(parents=True, exist_ok=True)
                powershell_completion_path.write_text("Register-ArgumentCompleter\n", encoding="utf-8")
                powershell_profile_path.parent.mkdir(parents=True, exist_ok=True)
                powershell_profile_path.write_text(
                    "\n".join(
                        [
                            "# >>> focus PowerShell completion >>>",
                            f"if (Test-Path '{powershell_completion_path}') {{ . '{powershell_completion_path}' }}",
                            "# <<< focus PowerShell completion <<<",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                stdout = io.StringIO()
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.install_surface._read_windows_user_path_value",
                            return_value=(user_path_state["raw"], user_path_state["type"]),
                        ):
                            with patch(
                                "bot.manage_cli.install_surface._write_windows_user_path_value",
                                side_effect=lambda raw_path, *, value_type: user_path_state.update(
                                    {"raw": raw_path, "type": value_type}
                                ),
                            ):
                                with redirect_stdout(stdout):
                                    result = _handle_bootstrap_install()

            self.assertEqual(result, 0)
            self.assertFalse(powershell_profile_path.exists())
            self.assertFalse(powershell_completion_path.exists())
            self.assertNotIn("PowerShell completion:", stdout.getvalue())

    def test_handle_uninstall_on_windows_removes_only_managed_user_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"
            metadata_path = config_root / "install-state" / "windows-user-path.json"
            original_user_path = r"C:\Windows\System32"
            user_path_state = {"raw": original_user_path, "type": 2}

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

                def uninstall(self, definition) -> None:
                    del definition

                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.install_surface._read_windows_user_path_value",
                            side_effect=lambda: (user_path_state["raw"], user_path_state["type"]),
                        ):
                            with patch(
                                "bot.manage_cli.install_surface._write_windows_user_path_value",
                                side_effect=lambda raw_path, *, value_type: user_path_state.update(
                                    {"raw": raw_path, "type": value_type}
                                ),
                            ):
                                self.assertEqual(_handle_bootstrap_install(), 0)
                                self.assertTrue(metadata_path.exists())
                                self.assertIn(str(bin_dir), user_path_state["raw"])
                                handoff = Mock()
                                handoff.armed = True
                                handoff.launch.return_value = Mock(
                                    handoff_id="handoff-1",
                                    helper_pid=4321,
                                    result_path=root / "result.json",
                                )
                                with patch(
                                    "bot.manage_cli.install_surface._prepare_windows_uninstall_handoff",
                                    return_value=handoff,
                                ):
                                    stdout = io.StringIO()
                                    with redirect_stdout(stdout):
                                        self.assertEqual(_handle_uninstall(purge=False), 0)
                                handoff.launch.assert_called_once_with()
                                self.assertIn("当前命令只报告 handoff", stdout.getvalue())
                                self.assertIn(str(root / "result.json"), stdout.getvalue())
                                self.assertNotIn("受管 .venv，配置", stdout.getvalue())

            self.assertEqual(user_path_state["raw"], original_user_path)
            self.assertFalse(metadata_path.exists())

    def test_handle_uninstall_on_windows_preserves_preexisting_user_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"
            metadata_path = config_root / "install-state" / "windows-user-path.json"
            original_user_path = f"{bin_dir};C:\\Windows\\System32"
            user_path_state = {"raw": original_user_path, "type": 2}

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

                def uninstall(self, definition) -> None:
                    del definition

                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                        with patch(
                            "bot.manage_cli.install_surface._read_windows_user_path_value",
                            side_effect=lambda: (user_path_state["raw"], user_path_state["type"]),
                        ):
                            with patch(
                                "bot.manage_cli.install_surface._write_windows_user_path_value",
                                side_effect=lambda raw_path, *, value_type: user_path_state.update(
                                    {"raw": raw_path, "type": value_type}
                                ),
                            ):
                                self.assertEqual(_handle_bootstrap_install(), 0)
                                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                                self.assertFalse(metadata["added_to_user_path"])
                                handoff = Mock()
                                handoff.armed = True
                                handoff.launch.return_value = Mock(
                                    handoff_id="handoff-2",
                                    helper_pid=4322,
                                    result_path=root / "result.json",
                                )
                                with patch(
                                    "bot.manage_cli.install_surface._prepare_windows_uninstall_handoff",
                                    return_value=handoff,
                                ):
                                    self.assertEqual(_handle_uninstall(purge=False), 0)
                                handoff.launch.assert_called_once_with()

            self.assertEqual(user_path_state["raw"], original_user_path)
            self.assertFalse(metadata_path.exists())

    def test_ensure_instance_scaffold_persists_native_codex_path_for_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            env_file = config_root / "focus.env"
            native_codex = root / "standalone" / "codex"
            native_codex.parent.mkdir(parents=True)
            native_codex.write_bytes(b"\x7fELF" + b"\0" * 64)
            native_codex.chmod(0o755)

            def _which(name: str) -> str | None:
                return str(native_codex) if name == "codex" else None

            with patch.dict(
                os.environ,
                {
                    "HOME": str(root),
                    "FNM_DIR": "",
                    "NVM_DIR": "",
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                with patch("bot.codex_command_resolver._is_windows", return_value=False):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        _ensure_instance_scaffold("default")

            actual_config = yaml.safe_load((config_root / "codex.yaml").read_text(encoding="utf-8"))
            self.assertEqual(
                actual_config["codex_command"],
                shlex.join([str(native_codex.resolve())]),
            )
            self.assertEqual((config_root / "codex.yaml.example").read_text(encoding="utf-8"), CODEX_YAML_TEMPLATE)

    def test_handle_bootstrap_install_preserves_existing_user_config_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                paths = resolve_instance_paths("corp-a")
                (paths.config_dir / "system.yaml").write_text("app_id: custom-app\n", encoding="utf-8")
                (paths.config_dir / "codex.yaml").write_text("model: custom-model\n", encoding="utf-8")
                (paths.config_dir / "init.token").write_text("custom-token\n", encoding="utf-8")
                env_file.write_text("OPENAI_API_KEY=custom-key\n", encoding="utf-8")
                if os.name != "nt":
                    os.chmod(paths.config_dir / "system.yaml", 0o644)
                    os.chmod(paths.config_dir / "init.token", 0o644)
                    os.chmod(env_file, 0o644)
                data_marker = paths.data_dir / "keep.txt"
                data_marker.write_text("preserve me\n", encoding="utf-8")
                (paths.config_dir / "system.yaml.example").write_text("stale-system-example\n", encoding="utf-8")
                (paths.config_dir / "codex.yaml.example").write_text("stale-codex-example\n", encoding="utf-8")

                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    result = _handle_bootstrap_install()

            self.assertEqual(result, 0)
            self.assertEqual((paths.config_dir / "system.yaml").read_text(encoding="utf-8"), "app_id: custom-app\n")
            self.assertEqual((paths.config_dir / "codex.yaml").read_text(encoding="utf-8"), "model: custom-model\n")
            self.assertEqual((paths.config_dir / "init.token").read_text(encoding="utf-8"), "custom-token\n")
            self.assertEqual(env_file.read_text(encoding="utf-8"), "OPENAI_API_KEY=custom-key\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE((paths.config_dir / "system.yaml").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((paths.config_dir / "init.token").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
            self.assertEqual(data_marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual((paths.config_dir / "system.yaml.example").read_text(encoding="utf-8"), SYSTEM_YAML_TEMPLATE)
            self.assertEqual((paths.config_dir / "codex.yaml.example").read_text(encoding="utf-8"), CODEX_YAML_TEMPLATE)
            self.assertTrue((bash_completion_dir / "focus").exists())
            self.assertTrue(zsh_completion_path.exists())
            self.assertTrue(zsh_rc_path.exists())
            self.assertTrue(powershell_completion_path.exists())
            self.assertTrue(powershell_profile_path.exists())

    def test_handle_bootstrap_install_preserves_existing_default_instance_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("default")
                default_codex = config_root / "codex.yaml"
                default_codex.write_text("mirror_watchdog_seconds: 999999\n", encoding="utf-8")

                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    result = _handle_bootstrap_install()

            self.assertEqual(result, 0)
            self.assertEqual(default_codex.read_text(encoding="utf-8"), "mirror_watchdog_seconds: 999999\n")

    def test_instance_scaffold_writes_private_role_bound_root_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")

            for role, managed_root in (
                ("config", pathlib.Path(env["FOCUS_CONFIG_ROOT"])),
                ("data", pathlib.Path(env["FOCUS_DATA_ROOT"])),
            ):
                marker = managed_root / _MANAGED_ROOT_MARKER
                payload = json.loads(marker.read_text(encoding="utf-8"))
                self.assertEqual(payload["managed_by"], "focus")
                self.assertEqual(payload["role"], role)
                self.assertEqual(payload["canonical_target"], str(managed_root.resolve()))
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
                self.assertEqual(list(managed_root.glob(f".{_MANAGED_ROOT_MARKER}.*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "creating symlinks is not reliably available on Windows CI")
    def test_instance_scaffold_refuses_symlink_managed_root_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            config_root.mkdir(parents=True)
            outside = root / "outside-marker.json"
            outside.write_text("do not replace\n", encoding="utf-8")
            (config_root / _MANAGED_ROOT_MARKER).symlink_to(outside)

            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(InstallLifecycleError, "不能是符号链接"):
                    _ensure_instance_scaffold("default")

            self.assertEqual(outside.read_text(encoding="utf-8"), "do not replace\n")

    def test_instance_scaffold_uses_windows_permission_policy_without_posix_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            stderr = io.StringIO()
            with patch.dict(os.environ, self._isolated_install_env(root), clear=False):
                with patch("bot.file_permissions.is_windows", return_value=True):
                    with patch("bot.file_permissions.os.chmod") as chmod:
                        with redirect_stderr(stderr):
                            _ensure_instance_scaffold("default")

            chmod.assert_not_called()

    def test_write_wrapper_creates_windows_cmd_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            with patch("bot.manage_cli.provisioning.is_windows", return_value=True):
                with patch("bot.manage_cli.provisioning._venv_python", return_value=pathlib.Path("C:/Python311/python.exe")):
                    _write_wrapper(root / "focus", "bot.manage_cli.entrypoint", wrapper_command="focus")

            wrapper_path = root / "focus.cmd"
            self.assertTrue(wrapper_path.exists())
            rendered = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('set "FOCUS_WRAPPER_COMMAND=focus"', rendered)
            self.assertIn(
                '"C:/Python311/python.exe" -I -m bot.manage_cli.entrypoint %*',
                rendered,
            )

    def test_write_wrapper_creates_unix_shell_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            wrapper_path = root / "focus"
            with patch("bot.manage_cli.provisioning.is_windows", return_value=False):
                with patch("bot.manage_cli.provisioning._venv_python", return_value=pathlib.Path("/tmp/venv/bin/python")):
                    _write_wrapper(wrapper_path, "bot.manage_cli.entrypoint", wrapper_command="focus")

            self.assertTrue(wrapper_path.exists())
            rendered = wrapper_path.read_text(encoding="utf-8")
            self.assertIn("FOCUS_WRAPPER_COMMAND='focus'", rendered)
            self.assertIn("export FOCUS_WRAPPER_COMMAND", rendered)
            self.assertIn(
                'exec /tmp/venv/bin/python -I -m bot.manage_cli.entrypoint "$@"',
                rendered,
            )
            self.assertEqual(stat.S_IMODE(wrapper_path.stat().st_mode), 0o755)

    @unittest.skipIf(os.name == "nt", "Unix wrapper integration")
    def test_unix_wrapper_ignores_hostile_cwd_and_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            managed_venv = root / "managed-venv"
            venv.EnvBuilder(with_pip=False).create(managed_venv)
            managed_python = managed_venv / "bin" / "python"
            purelib_result = subprocess.run(
                [
                    str(managed_python),
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            managed_package = (
                pathlib.Path(purelib_result.stdout.strip()) / "focus_isolation_probe"
            )
            managed_package.mkdir()
            (managed_package / "__init__.py").write_text("", encoding="utf-8")
            (managed_package / "__main__.py").write_text(
                "import os\nprint('managed:' + os.environ['FOCUS_TEST_VALUE'])\n",
                encoding="utf-8",
            )

            hostile_cwd = root / "hostile"
            hostile_package = hostile_cwd / "focus_isolation_probe"
            hostile_package.mkdir(parents=True)
            (hostile_package / "__init__.py").write_text("", encoding="utf-8")
            (hostile_package / "__main__.py").write_text(
                "print('hostile')\n",
                encoding="utf-8",
            )
            wrapper = root / "focus-probe"
            with patch(
                "bot.manage_cli.provisioning._venv_python",
                return_value=managed_python,
            ):
                _write_wrapper(wrapper, "focus_isolation_probe")

            environment = {
                **os.environ,
                "PYTHONPATH": str(hostile_cwd),
                "FOCUS_TEST_VALUE": "preserved",
            }
            result = subprocess.run(
                [str(wrapper)],
                cwd=hostile_cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "managed:preserved")

    def test_purge_stops_before_deletion_when_service_uninstall_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            data_root = pathlib.Path(env["FOCUS_DATA_ROOT"])

            class _FailingManager:
                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

                def uninstall(self, definition) -> None:
                    raise ServiceManagerError(f"cannot stop {definition.identifier}")

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_FailingManager()):
                    with patch("bot.manage_cli.install_surface._remove_wrappers") as remove_wrappers:
                        with self.assertRaisesRegex(InstallLifecycleError, "不会继续删除"):
                            _handle_uninstall(purge=True)

            remove_wrappers.assert_not_called()
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_purge_stops_when_service_definition_still_exists_after_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            data_root = pathlib.Path(env["FOCUS_DATA_ROOT"])

            class _IncompleteManager:
                def uninstall(self, definition) -> None:
                    del definition

                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=True, running=False)

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_IncompleteManager()):
                    with patch("bot.manage_cli.install_surface._remove_wrappers") as remove_wrappers:
                        with self.assertRaisesRegex(InstallLifecycleError, "定义或进程仍然存在"):
                            _handle_uninstall(purge=True)

            remove_wrappers.assert_not_called()
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_purge_stops_when_uninstall_returns_but_service_owner_is_still_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            data_root = pathlib.Path(env["FOCUS_DATA_ROOT"])

            class _NoopManager:
                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

                def uninstall(self, definition) -> None:
                    del definition

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                service_lease = ServiceInstanceLease(data_root)
                service_lease.acquire(control_endpoint="http://127.0.0.1:1")
                try:
                    with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_NoopManager()):
                        with patch("bot.manage_cli.install_surface._remove_wrappers") as remove_wrappers:
                            with self.assertRaisesRegex(InstallLifecycleError, "maintenance 操作"):
                                _handle_uninstall(purge=True)
                finally:
                    service_lease.release()

            remove_wrappers.assert_not_called()
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_purge_reports_delete_failure_without_claiming_success_or_ignoring_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"]).resolve()

            class _DummyManager:
                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

                def uninstall(self, definition) -> None:
                    del definition

            stdout = io.StringIO()
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    with patch("bot.manage_cli.install_surface._remove_wrappers"):
                        with patch(
                            "bot.install_lifecycle.shutil.rmtree",
                            side_effect=PermissionError("denied"),
                        ) as rmtree:
                            with redirect_stdout(stdout):
                                with self.assertRaisesRegex(InstallLifecycleError, "不会报告成功"):
                                    _handle_uninstall(purge=True)

            self.assertEqual(rmtree.call_args.args, (config_root,))
            self.assertEqual(rmtree.call_args.kwargs, {})
            self.assertNotIn("已删除配置、数据", stdout.getvalue())

    def test_purge_deletes_only_verified_managed_roots_after_service_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            data_root = pathlib.Path(env["FOCUS_DATA_ROOT"])
            uninstall_identifiers: list[str] = []
            shared_uninstalls: list[bool] = []

            class _DummyManager:
                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

                def uninstall(self, definition) -> None:
                    uninstall_identifiers.append(definition.identifier)

                def uninstall_shared(self) -> None:
                    shared_uninstalls.append(True)

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("corp-a")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    self.assertEqual(_handle_uninstall(purge=True), 0)

            self.assertEqual(set(uninstall_identifiers), {"focus", "focus-corp-a"})
            self.assertEqual(shared_uninstalls, [True])
            self.assertFalse(config_root.exists())
            self.assertFalse(data_root.exists())

    def test_handle_uninstall_removes_shell_completion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            powershell_profile_path = root / "shells" / "profile.ps1"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

                def uninstall(self, definition) -> None:
                    del definition

                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                    "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                    "FOCUS_BIN_DIR": str(bin_dir),
                    "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                    "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                    "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                    "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                    "FOCUS_POWERSHELL_PROFILE_PATH": str(powershell_profile_path),
                    "FOCUS_ENV_FILE": str(env_file),
                    "HOME": str(root),
                },
                clear=False,
            ):
                _ensure_instance_scaffold("corp-a")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    self.assertEqual(_handle_bootstrap_install(), 0)
                    self.assertTrue((bash_completion_dir / "focus").exists())
                    self.assertTrue(zsh_completion_path.exists())
                    self.assertTrue(zsh_rc_path.exists())
                    self.assertTrue(powershell_completion_path.exists())
                    self.assertTrue(powershell_profile_path.exists())
                    managed_venv = data_root / ".venv"
                    managed_venv.mkdir()
                    (managed_venv / "payload").write_text("installed", encoding="utf-8")
                    self.assertEqual(_handle_uninstall(purge=False), 0)

            self.assertFalse((bash_completion_dir / "focus").exists())
            self.assertFalse((bash_completion_dir / "focusd").exists())
            self.assertFalse((bash_completion_dir / "focusctl").exists())
            self.assertFalse((bash_completion_dir / "fcodex").exists())
            self.assertFalse(zsh_completion_path.exists())
            self.assertFalse(zsh_rc_path.exists())
            self.assertFalse(powershell_completion_path.exists())
            self.assertFalse(powershell_profile_path.exists())
            self.assertFalse(managed_venv.exists())
            self.assertTrue(data_root.exists())
            self.assertTrue(config_root.exists())

    def test_handle_uninstall_removes_powershell_profile_block_without_runtime_env_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            bin_dir = root / "bin"
            bash_completion_dir = root / "completion" / "bash"
            zsh_completion_path = root / "completion" / "zsh" / "focus.zsh"
            zsh_rc_path = root / "shells" / "zshrc"
            powershell_completion_path = root / "completion" / "powershell" / "focus.ps1"
            install_profile_path = root / "shells" / "install-profile.ps1"
            uninstall_profile_path = root / "shells" / "uninstall-profile.ps1"
            metadata_path = config_root / "shell-completion" / "powershell-install-paths.json"
            env_file = config_root / "focus.env"

            class _DummyManager:
                def ensure_service(self, definition) -> None:
                    del definition

                def uninstall(self, definition) -> None:
                    del definition

                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

            install_env = {
                "FOCUS_CONFIG_ROOT": str(config_root),
                "FOCUS_DATA_ROOT": str(data_root),
                "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                "FOCUS_BIN_DIR": str(bin_dir),
                "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                "FOCUS_POWERSHELL_PROFILE_PATH": str(install_profile_path),
                "FOCUS_ENV_FILE": str(env_file),
            }
            uninstall_env = {
                "FOCUS_CONFIG_ROOT": str(config_root),
                "FOCUS_DATA_ROOT": str(data_root),
                "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
                "FOCUS_BIN_DIR": str(bin_dir),
                "FOCUS_BASH_COMPLETION_DIR": str(bash_completion_dir),
                "FOCUS_ZSH_COMPLETION_PATH": str(zsh_completion_path),
                "FOCUS_ZSH_RC_PATH": str(zsh_rc_path),
                "FOCUS_POWERSHELL_COMPLETION_PATH": str(powershell_completion_path),
                "FOCUS_POWERSHELL_PROFILE_PATH": str(uninstall_profile_path),
                "FOCUS_ENV_FILE": str(env_file),
            }

            with patch.dict(os.environ, install_env, clear=False):
                _ensure_instance_scaffold("corp-a")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    self.assertEqual(_handle_bootstrap_install(), 0)

            self.assertTrue(powershell_completion_path.exists())
            self.assertTrue(install_profile_path.exists())
            self.assertTrue(metadata_path.exists())

            with patch.dict(os.environ, uninstall_env, clear=False):
                os.environ.pop("FOCUS_POWERSHELL_PROFILE_PATH", None)
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_DummyManager()):
                    self.assertEqual(_handle_uninstall(purge=False), 0)

            self.assertFalse(powershell_completion_path.exists())
            self.assertFalse(install_profile_path.exists())
            self.assertFalse(metadata_path.exists())
            self.assertFalse(uninstall_profile_path.exists())
