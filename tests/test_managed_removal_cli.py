from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

from bot.install_lifecycle import ManagedInstallLifecycleError
from bot.manage_cli.entrypoint import main
from bot.manage_cli.errors import InstallLifecycleError
from bot.manage_cli.install_surface import (
    _handle_uninstall,
    _resolve_purge_roots,
)
from bot.manage_cli.provisioning import _MANAGED_ROOT_MARKER, _ensure_instance_scaffold
from bot.service_control_plane import ServiceControlError
from bot.service_manager import ServiceStatus
from bot.windows_removal_handoff import WindowsRemovalHandoffError


class ManagedRemovalCliTests(unittest.TestCase):
    @staticmethod
    def _isolated_install_env(root: pathlib.Path) -> dict[str, str]:
        config_root = root / "config"
        data_root = root / "data"
        return {
            "FOCUS_CONFIG_ROOT": str(config_root),
            "FOCUS_DATA_ROOT": str(data_root),
            "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
            "FOCUS_BIN_DIR": str(root / "bin"),
            "FOCUS_BASH_COMPLETION_DIR": str(root / "completion" / "bash"),
            "FOCUS_ZSH_COMPLETION_PATH": str(root / "completion" / "zsh" / "focus.zsh"),
            "FOCUS_ZSH_RC_PATH": str(root / "shells" / "zshrc"),
            "FOCUS_POWERSHELL_COMPLETION_PATH": str(
                root / "completion" / "powershell" / "focus.ps1"
            ),
            "FOCUS_POWERSHELL_PROFILE_PATH": str(root / "shells" / "profile.ps1"),
            "FOCUS_ENV_FILE": str(config_root / "focus.env"),
        }

    def test_purge_rejects_root_home_checkout_and_overwide_parent_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            base_env = self._isolated_install_env(root)
            checkout_root = pathlib.Path(__file__).resolve().parent.parent
            targets = pathlib.Path("/"), pathlib.Path.home(), checkout_root, checkout_root.parent, pathlib.Path("/tmp")
            for target in targets:
                with self.subTest(target=target):
                    env = {**base_env, "FOCUS_CONFIG_ROOT": str(target)}
                    with patch.dict(os.environ, env, clear=False):
                        with self.assertRaisesRegex(InstallLifecycleError, "拒绝 purge"):
                            _resolve_purge_roots()

    def test_uninstall_running_idle_admits_and_stops_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            events: list[str] = []

            class _Manager:
                installed = True
                running = True

                def status(self, definition) -> ServiceStatus:
                    events.append(f"status:{definition.instance_name}")
                    return ServiceStatus(installed=self.installed, running=self.running)

                def stop(self, definition) -> None:
                    events.append(f"stop:{definition.instance_name}")
                    self.running = False

                def start(self, definition) -> None:
                    events.append(f"unexpected-start:{definition.instance_name}")

                def uninstall(self, definition) -> None:
                    events.append(f"uninstall:{definition.instance_name}")
                    self.installed = False

                def uninstall_shared(self) -> None:
                    events.append("uninstall-shared")

            def request(_data_dir, method, _params=None):
                events.append(f"control:{method}")
                return {"instance_name": "default", "status": "prepared"}

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                managed_venv = pathlib.Path(env["FOCUS_DATA_ROOT"]) / ".venv"
                managed_venv.mkdir()
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_Manager()):
                    with patch("bot.manage_cli.install_surface.control_request", side_effect=request):
                        with patch("bot.manage_cli.install_surface._remove_wrappers"):
                            self.assertEqual(_handle_uninstall(purge=False), 0)

            self.assertFalse(managed_venv.exists())
            self.assertLess(events.index("control:service/prepare-offline-maintenance"), events.index("stop:default"))
            self.assertLess(events.index("stop:default"), events.index("uninstall:default"))
            self.assertNotIn("unexpected-start:default", events)

    def test_uninstall_active_instance_rejects_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._isolated_install_env(pathlib.Path(tmpdir))
            manager = Mock()
            manager.status.return_value = ServiceStatus(installed=True, running=True)
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=manager):
                    with patch(
                        "bot.manage_cli.install_surface.control_request",
                        side_effect=ServiceControlError("仍有 active turn"),
                    ):
                        with patch("bot.manage_cli.install_surface._remove_wrappers") as wrappers:
                            with self.assertRaisesRegex(InstallLifecycleError, "active turn"):
                                _handle_uninstall(purge=False)
            manager.stop.assert_not_called()
            manager.uninstall.assert_not_called()
            wrappers.assert_not_called()

    def test_uninstall_missing_windows_helper_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._isolated_install_env(pathlib.Path(tmpdir))
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch(
                        "bot.manage_cli.install_surface.prepare_windows_removal_handoff",
                        side_effect=WindowsRemovalHandoffError("找不到 PowerShell helper"),
                    ):
                        with patch("bot.manage_cli.install_surface.current_service_manager") as manager:
                            with patch("bot.manage_cli.install_surface._remove_wrappers") as wrappers:
                                with self.assertRaisesRegex(InstallLifecycleError, "找不到 PowerShell helper"):
                                    _handle_uninstall(purge=False)
            manager.assert_not_called()
            wrappers.assert_not_called()

    def test_windows_handoff_closure_error_keeps_exact_result_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            result_path = root / "result.json"
            events: list[str] = []
            handoff = Mock(armed=True)
            receipt = Mock(
                handoff_id="handoff-3",
                helper_pid=4323,
                result_path=result_path,
            )
            handoff.launch.side_effect = lambda: events.append("launch") or receipt

            class _Transaction:
                instance_names: tuple[str, ...] = ()

                def __enter__(self):
                    return self

                def yield_handoff_barrier(self) -> None:
                    events.append("yield")

                def __exit__(self, *_args) -> None:
                    raise ManagedInstallLifecycleError("closure failed")

            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                with (
                    patch("bot.manage_cli.install_surface.is_windows", return_value=True),
                    patch("bot.manage_cli.provisioning.is_windows", return_value=True),
                ):
                    with patch("bot.manage_cli.install_surface._prepare_windows_uninstall_handoff", return_value=handoff):
                        with patch("bot.manage_cli.install_surface.create_managed_install_transaction", return_value=_Transaction()):
                            with patch("bot.manage_cli.install_surface.current_service_manager"):
                                with patch("bot.manage_cli.install_surface._remove_wrappers"):
                                    with self.assertRaisesRegex(InstallLifecycleError, str(result_path)):
                                        _handle_uninstall(purge=False)
            handoff.launch.assert_called_once_with()
            self.assertEqual(events, ["yield", "launch"])

    def test_purge_rejects_overlapping_config_and_data_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "focus-config"
            env = {
                **self._isolated_install_env(root),
                "FOCUS_CONFIG_ROOT": str(config_root),
                "FOCUS_DATA_ROOT": str(config_root / "data"),
            }
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(InstallLifecycleError, "不能相同或互为父子目录"):
                    _resolve_purge_roots()

    def test_purge_rejects_unmarked_roots_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            config_root = pathlib.Path(env["FOCUS_CONFIG_ROOT"])
            data_root = pathlib.Path(env["FOCUS_DATA_ROOT"])
            config_root.mkdir(parents=True)
            data_root.mkdir(parents=True)
            with patch.dict(os.environ, env, clear=False):
                with patch("bot.manage_cli.install_surface.current_service_manager") as manager:
                    with patch("bot.manage_cli.install_surface._remove_wrappers") as wrappers:
                        with self.assertRaisesRegex(InstallLifecycleError, "完成 repair"):
                            _handle_uninstall(purge=True)
            manager.assert_not_called()
            wrappers.assert_not_called()
            self.assertTrue(config_root.exists())
            self.assertTrue(data_root.exists())

    def test_purge_rejects_marker_with_wrong_canonical_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            env = self._isolated_install_env(root)
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                marker = pathlib.Path(env["FOCUS_CONFIG_ROOT"]) / _MANAGED_ROOT_MARKER
                payload = json.loads(marker.read_text(encoding="utf-8"))
                payload["canonical_target"] = str(root / "another-config")
                marker.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(InstallLifecycleError, "canonical target 不一致"):
                    _resolve_purge_roots()

    def test_purge_cli_projects_unsafe_target_as_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                **self._isolated_install_env(pathlib.Path(tmpdir)),
                "FOCUS_CONFIG_ROOT": "/",
            }
            stderr = io.StringIO()
            with patch.dict(os.environ, env, clear=False):
                with redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(["purge"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("拒绝 purge", stderr.getvalue())

    def test_uninstall_cli_projects_venv_delete_failure_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = self._isolated_install_env(pathlib.Path(tmpdir))

            class _Manager:
                def status(self, definition) -> ServiceStatus:
                    del definition
                    return ServiceStatus(installed=False, running=False)

                def uninstall(self, definition) -> None:
                    del definition

            stderr = io.StringIO()
            with patch.dict(os.environ, env, clear=False):
                _ensure_instance_scaffold("default")
                (pathlib.Path(env["FOCUS_DATA_ROOT"]) / ".venv").mkdir()
                with patch("bot.manage_cli.install_surface.current_service_manager", return_value=_Manager()):
                    with patch("bot.manage_cli.install_surface._remove_wrappers"):
                        with patch(
                            "bot.install_lifecycle.shutil.rmtree",
                            side_effect=PermissionError("denied"),
                        ):
                            with redirect_stderr(stderr):
                                with self.assertRaises(SystemExit) as raised:
                                    main(["uninstall"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("uninstall 未完成", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
