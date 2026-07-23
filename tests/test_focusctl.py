import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from bot import focusctl
from bot.version import __version__


class FocusctlEntrypointTests(unittest.TestCase):
    def test_version_prints_project_version(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                focusctl.main(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"focusctl {__version__}")

    def test_service_lifecycle_routes_to_manage_cli(self) -> None:
        with patch("bot.focusctl._run_manage") as mock_manage:
            focusctl.main(["--instance", "corp-a", "service", "restart"])

        mock_manage.assert_called_once_with(["--instance", "corp-a", "restart"])

    def test_service_status_routes_to_manage_cli_and_preserves_repeated_instances(self) -> None:
        with patch("bot.focusctl._run_manage") as mock_manage:
            focusctl.main(["--instance", "default", "--instance", "corp-a", "service", "status"])

        mock_manage.assert_called_once_with(["--instance", "default", "--instance", "corp-a", "status"])

    def test_service_status_help_uses_public_command_path(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                focusctl.main(["service", "status", "--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("usage: focusctl [--instance <name>] service status", rendered)
        self.assertNotIn("usage: focusctl status", rendered)

    def test_service_list_is_removed(self) -> None:
        stderr = io.StringIO()
        with patch("bot.focusctl.sys.stderr", stderr):
            with self.assertRaises(SystemExit) as exc:
                focusctl.main(["service", "list"])

        self.assertEqual(exc.exception.code, 2)
        self.assertIn("focusctl instance list", stderr.getvalue())

    def test_config_routes_to_manage_cli(self) -> None:
        with patch("bot.focusctl._run_manage") as mock_manage:
            focusctl.main(["config", "system", "--open"])

        mock_manage.assert_called_once_with(["config", "system", "--open"])

    def test_migrate_routes_to_manage_cli(self) -> None:
        with patch("bot.focusctl._run_manage") as mock_manage:
            focusctl.main(["migrate", "from-feishu-codex"])

        mock_manage.assert_called_once_with(["migrate", "from-feishu-codex"])

    def test_runtime_resource_routes_to_runtime_cli(self) -> None:
        with patch("bot.focusctl._run_runtime") as mock_runtime:
            focusctl.main(["thread", "list", "--scope", "cwd"])

        mock_runtime.assert_called_once_with(["thread", "list", "--scope", "cwd"])

    def test_help_mentions_thread_lifecycle_commands(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                focusctl.main(["--help"])

        self.assertEqual(exc.exception.code, 0)
        rendered = stdout.getvalue()
        self.assertIn("thread list --archived --scope global", rendered)
        self.assertIn("thread unarchive --thread-id <id-1> --thread-id <id-2>", rendered)
        self.assertIn("thread delete --thread-id <id> --force", rendered)


if __name__ == "__main__":
    unittest.main()
