import os
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

from bot.fcodex import cli as fcodex
from bot.fcodex.cli import (
    _first_positional_index,
    _has_explicit_cwd,
    _has_explicit_remote,
    _management_command_error,
    _program_name,
    _resolve_effective_cwd,
)


class FcodexWrapperTests(unittest.TestCase):
    def test_program_name_prefers_installed_wrapper_command_env(self) -> None:
        with patch.dict(os.environ, {"FOCUS_WRAPPER_COMMAND": "fcodex"}, clear=False):
            with patch("sys.argv", ["-c", "--version"]):
                self.assertEqual(_program_name(), "fcodex")

    def test_program_name_ignores_unknown_wrapper_command_env(self) -> None:
        with patch.dict(os.environ, {"FOCUS_WRAPPER_COMMAND": "unexpected"}, clear=False):
            with patch("sys.argv", ["/tmp/focus", "--version"]):
                self.assertEqual(_program_name(), "focus")

    def test_management_command_reports_focusctl_hint(self) -> None:
        stderr = StringIO()

        with patch.dict(os.environ, {"FOCUS_WRAPPER_COMMAND": "focus"}, clear=False):
            with redirect_stderr(stderr):
                exit_code = _management_command_error(["migrate", "from-feishu-codex"])

        self.assertEqual(exit_code, 2)
        self.assertIn("不处理本地管理命令", stderr.getvalue())
        self.assertIn("focusctl migrate from-feishu-codex", stderr.getvalue())

    def test_main_rejects_management_command_before_loading_config(self) -> None:
        with patch.dict(os.environ, {"FOCUS_WRAPPER_COMMAND": "focus"}, clear=False):
            with patch("sys.argv", ["focus", "migrate", "from-feishu-codex"]):
                with patch.object(fcodex, "load_config_file") as load_config:
                    with self.assertRaises(SystemExit) as exc:
                        fcodex.main()

        self.assertEqual(exc.exception.code, 2)
        load_config.assert_not_called()

    def test_wrapper_terminator_hides_upstream_options_from_focus_parsing(self) -> None:
        self.assertIsNone(_first_positional_index(["--", "resume", "thread-1"]))
        self.assertFalse(_has_explicit_cwd(["--", "--cd=/tmp"]))
        self.assertFalse(_has_explicit_remote(["--", "--remote", "ws://upstream"]))

    def test_wrapper_terminator_does_not_change_effective_cwd(self) -> None:
        self.assertEqual(_resolve_effective_cwd(["--", "--cd=/tmp"]), os.getcwd())


if __name__ == "__main__":
    unittest.main()
