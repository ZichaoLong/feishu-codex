import pathlib
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from bot.instance_layout import InstancePaths
from bot.service_manager import (
    LaunchdUserServiceManager,
    SystemdUserServiceManager,
    WindowsTaskSchedulerServiceManager,
    build_service_definition,
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
                with patch.object(manager, "_run", side_effect=lambda *args, **kwargs: run_calls.append(args)):
                    manager.ensure_service(definition)

            unit_path = root / "systemd" / "feishu-codex-corp-a.service"
            self.assertTrue(unit_path.exists())
            rendered = unit_path.read_text(encoding="utf-8")
            self.assertIn("Description=Feishu Codex (corp-a)", rendered)
            self.assertIn("--instance", rendered)
            self.assertEqual(run_calls, [("systemctl", "--user", "daemon-reload")])

    def test_launchd_manager_writes_plist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            definition = _definition(root)
            manager = LaunchdUserServiceManager()
            with patch("bot.service_manager.default_launch_agent_dir", return_value=root / "LaunchAgents"):
                manager.ensure_service(definition)

            plist_path = root / "LaunchAgents" / "io.feishu-codex.corp-a.plist"
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
            with patch.object(manager, "_run", side_effect=lambda *args, **kwargs: run_calls.append(args)):
                manager.ensure_service(definition)

            launcher_path = definition.paths.data_dir / "service-launch.cmd"
            self.assertTrue(launcher_path.exists())
            rendered = launcher_path.read_text(encoding="utf-8")
            self.assertIn("bot.__main__", rendered)
            self.assertEqual(run_calls[0][0:4], ("schtasks", "/Create", "/TN", "feishu-codex-corp-a"))


if __name__ == "__main__":
    unittest.main()
