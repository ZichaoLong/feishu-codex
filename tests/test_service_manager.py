import pathlib
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bot.instance_layout import InstancePaths
from bot.service_manager import (
    LaunchdUserServiceManager,
    ServiceManagerError,
    SystemdUserServiceManager,
    WindowsTaskSchedulerServiceManager,
    build_service_definition,
    current_service_manager,
)


def _definition(root: pathlib.Path):
    paths = InstancePaths(
        instance_name="corp-a",
        config_dir=root / "config",
        data_dir=root / "data",
        global_data_dir=root / "global",
    )
    return build_service_definition(
        instance_name="corp-a",
        paths=paths,
        daemon_command=["/tmp/venv/bin/python", "-m", "bot.__main__", "--instance", "corp-a"],
    )


class ServiceManagerTests(unittest.TestCase):
    def test_systemd_manager_writes_unit_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            run_calls: list[tuple[str, ...]] = []
            manager = SystemdUserServiceManager()
            with patch("bot.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(
                    manager,
                    "_run",
                    side_effect=lambda *args, **kwargs: (run_calls.append(args), subprocess.CompletedProcess(args, 0, stdout="", stderr=""))[1],
                ):
                    manager.ensure_service(definition)

            unit_path = root / "systemd" / "feishu-codex@.service"
            self.assertTrue(unit_path.exists())
            rendered = unit_path.read_text(encoding="utf-8")
            self.assertIn("Description=Feishu Codex (%i)", rendered)
            self.assertIn("WorkingDirectory=", rendered)
            self.assertIn("%i", rendered)
            self.assertEqual(run_calls, [("systemctl", "--user", "daemon-reload")])

    def test_launchd_manager_writes_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("bot.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)

            plist_path = definition.paths.data_dir / "service.plist"
            self.assertTrue(plist_path.exists())
            payload = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(payload["Label"], "io.feishu-codex.corp-a")
            self.assertEqual(payload["ProgramArguments"][-2:], ["--instance", "corp-a"])

    def test_windows_manager_writes_launcher_and_registers_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            run_calls: list[tuple[str, ...]] = []
            manager = WindowsTaskSchedulerServiceManager()
            with patch.object(
                manager,
                "_run",
                side_effect=lambda *args, **kwargs: (run_calls.append(args), subprocess.CompletedProcess(args, 1 if "/Query" in args else 0, stdout="", stderr=""))[1],
            ):
                manager.ensure_service(definition)

            launcher_path = definition.paths.data_dir / "service-launch.cmd"
            xml_path = definition.paths.data_dir / "service-task.xml"
            self.assertTrue(launcher_path.exists())
            self.assertTrue(xml_path.exists())
            rendered = launcher_path.read_text(encoding="utf-8")
            self.assertIn("bot.__main__", rendered)
            self.assertEqual(run_calls[0][0:4], ("schtasks", "/Query", "/TN", "feishu-codex-corp-a"))
            self.assertEqual(run_calls[1][0:4], ("schtasks", "/Create", "/TN", "feishu-codex-corp-a"))

    def test_systemd_manager_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:3] == ("systemctl", "--user", "is-active"):
                    return subprocess.CompletedProcess(args, 0, stdout="active\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("bot.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    manager.start(definition)
                    status = manager.status(definition)
                    manager.uninstall(definition)
                    manager.uninstall_shared()

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(status.detail, "active")
            self.assertEqual(calls[0][0], ("systemctl", "--user", "daemon-reload"))
            self.assertEqual(calls[1][0], ("systemctl", "--user", "start", "feishu-codex@corp-a"))
            self.assertEqual(calls[2][0], ("systemctl", "--user", "is-active", "feishu-codex@corp-a"))
            self.assertEqual(calls[3][0], ("systemctl", "--user", "disable", "feishu-codex@corp-a"))
            self.assertEqual(calls[4][0], ("systemctl", "--user", "stop", "feishu-codex@corp-a"))
            self.assertEqual(calls[5][0], ("systemctl", "--user", "daemon-reload"))
            self.assertEqual(calls[6][0], ("systemctl", "--user", "daemon-reload"))
            self.assertFalse((root / "systemd" / "feishu-codex@.service").exists())

    def test_systemd_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:3] == ("systemctl", "--user", "is-enabled"):
                    return subprocess.CompletedProcess(args, 0, stdout="enabled\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("bot.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with patch.object(manager, "_run", side_effect=_run):
                    manager.ensure_service(definition)
                    manager.autostart_enable(definition)
                    status = manager.autostart_status(definition)
                    manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, "systemctl --user is-enabled feishu-codex@corp-a")
            self.assertEqual(status.detail, "enabled")
            self.assertEqual(calls[1][0], ("systemctl", "--user", "enable", "feishu-codex@corp-a"))
            self.assertEqual(calls[2][0], ("systemctl", "--user", "is-enabled", "feishu-codex@corp-a"))
            self.assertEqual(calls[3][0], ("systemctl", "--user", "disable", "feishu-codex@corp-a"))

    def test_launchd_manager_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("launchctl", "print"):
                    return subprocess.CompletedProcess(args, 0, stdout="state = running\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch("bot.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                with patch.object(manager, "_uid_domain", return_value="gui/501"):
                    with patch.object(manager, "_run", side_effect=_run):
                        manager.ensure_service(definition)
                        manager.start(definition)
                        status = manager.status(definition)
                        manager.uninstall(definition)

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(calls[0][0], ("launchctl", "bootout", "gui/501", "io.feishu-codex.corp-a"))
            self.assertEqual(calls[1][0], ("launchctl", "bootstrap", "gui/501", str(root / "data" / "service.plist")))
            self.assertEqual(calls[2][0], ("launchctl", "kickstart", "-k", "gui/501/io.feishu-codex.corp-a"))
            self.assertEqual(calls[3][0], ("launchctl", "print", "gui/501/io.feishu-codex.corp-a"))
            self.assertEqual(calls[4][0], ("launchctl", "bootout", "gui/501", "io.feishu-codex.corp-a"))
            self.assertFalse((root / "data" / "service.plist").exists())

    def test_launchd_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("bot.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                status = manager.autostart_status(definition)
                manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, "LaunchAgent io.feishu-codex.corp-a")
            self.assertEqual(status.detail, str(root / "LaunchAgents" / "io.feishu-codex.corp-a.plist"))
            self.assertFalse((root / "LaunchAgents" / "io.feishu-codex.corp-a.plist").exists())

    def test_launchd_autostart_status_detects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("bot.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                (root / "data" / "service.plist").unlink()
                status = manager.autostart_status(definition)

            self.assertFalse(status.enabled)
            self.assertEqual(status.source, "LaunchAgent io.feishu-codex.corp-a")
            self.assertEqual(status.detail, "launch agent symlink is dangling")

    def test_windows_manager_lifecycle_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
                if args[:2] == ("schtasks", "/Query"):
                    return subprocess.CompletedProcess(args, 0, stdout="Status: Running\n", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_run):
                manager.ensure_service(definition)
                manager.start(definition)
                status = manager.status(definition)
                manager.uninstall(definition)

            self.assertTrue(status.installed)
            self.assertTrue(status.running)
            self.assertEqual(calls[0][0][:4], ("schtasks", "/Query", "/TN", "feishu-codex-corp-a"))
            self.assertEqual(calls[1][0][:4], ("schtasks", "/Create", "/TN", "feishu-codex-corp-a"))
            self.assertEqual(calls[2][0], ("schtasks", "/Run", "/TN", "feishu-codex-corp-a"))
            self.assertEqual(calls[3][0], ("schtasks", "/Query", "/TN", "feishu-codex-corp-a", "/FO", "LIST", "/V"))
            self.assertEqual(calls[4][0], ("schtasks", "/End", "/TN", "feishu-codex-corp-a"))
            self.assertEqual(calls[5][0], ("schtasks", "/Delete", "/TN", "feishu-codex-corp-a", "/F"))
            self.assertFalse((definition.paths.data_dir / "service-launch.cmd").exists())
            self.assertFalse((definition.paths.data_dir / "service-task.xml").exists())

    def test_windows_autostart_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = WindowsTaskSchedulerServiceManager()
            calls: list[tuple[tuple[str, ...], dict]] = []

            enabled_xml = """<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger /></Triggers>
</Task>
"""

            def _run(*args, **kwargs):
                calls.append((args, kwargs))
                if args[:2] == ("schtasks", "/Query") and "/XML" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=enabled_xml, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            with patch.object(manager, "_run", side_effect=_run):
                manager.ensure_service(definition)
                manager.autostart_enable(definition)
                status = manager.autostart_status(definition)
                manager.autostart_disable(definition)

            self.assertTrue(status.enabled)
            self.assertEqual(status.source, "schtasks /Query /TN feishu-codex-corp-a /XML")
            self.assertEqual(status.detail, "logon trigger enabled")
            create_calls = [call for call, _ in calls if call[:2] == ("schtasks", "/Create")]
            self.assertGreaterEqual(len(create_calls), 3)

    def test_systemd_start_requires_installed_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = SystemdUserServiceManager()
            with patch("bot.service_manager.default_systemd_user_dir", return_value=root / "systemd"):
                with self.assertRaisesRegex(ServiceManagerError, "install.sh"):
                    manager.start(definition)

    def test_current_service_manager_factory(self) -> None:
        with patch("bot.service_manager.is_windows", return_value=True):
            self.assertIsInstance(current_service_manager(), WindowsTaskSchedulerServiceManager)
        with patch("bot.service_manager.is_windows", return_value=False):
            with patch("bot.service_manager.is_macos", return_value=True):
                self.assertIsInstance(current_service_manager(), LaunchdUserServiceManager)
        with patch("bot.service_manager.is_windows", return_value=False):
            with patch("bot.service_manager.is_macos", return_value=False):
                with patch("bot.service_manager.is_linux", return_value=True):
                    self.assertIsInstance(current_service_manager(), SystemdUserServiceManager)

    def test_current_service_manager_rejects_unsupported_platform(self) -> None:
        with patch("bot.service_manager.is_windows", return_value=False):
            with patch("bot.service_manager.is_macos", return_value=False):
                with patch("bot.service_manager.is_linux", return_value=False):
                    with self.assertRaises(ServiceManagerError):
                        current_service_manager()


if __name__ == "__main__":
    unittest.main()
