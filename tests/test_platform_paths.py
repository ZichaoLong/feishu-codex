import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.platform_paths import default_config_root, default_data_root


class PlatformPathsTests(unittest.TestCase):
    def test_default_roots_use_linux_machine_paths_even_inside_repo_checkout(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("bot.platform_paths.pathlib.Path.home", return_value=Path("/home/tester")):
                with patch("bot.platform_paths.is_windows", return_value=False):
                    with patch("bot.platform_paths.is_macos", return_value=False):
                        self.assertEqual(default_config_root(), Path("/home/tester/.config/feishu-codex"))
                        self.assertEqual(default_data_root(), Path("/home/tester/.local/share/feishu-codex"))

    def test_default_roots_use_macos_machine_paths(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("bot.platform_paths.pathlib.Path.home", return_value=Path("/Users/tester")):
                with patch("bot.platform_paths.is_windows", return_value=False):
                    with patch("bot.platform_paths.is_macos", return_value=True):
                        self.assertEqual(
                            default_config_root(),
                            Path("/Users/tester/Library/Application Support/feishu-codex/config"),
                        )
                        self.assertEqual(
                            default_data_root(),
                            Path("/Users/tester/Library/Application Support/feishu-codex/data"),
                        )

    def test_default_roots_use_windows_machine_paths(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APPDATA": r"C:\Users\tester\AppData\Roaming",
                "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
            },
            clear=True,
        ):
            with patch("bot.platform_paths.pathlib.Path.home", return_value=Path(r"C:\Users\tester")):
                with patch("bot.platform_paths.is_windows", return_value=True):
                    with patch("bot.platform_paths.is_macos", return_value=False):
                        self.assertEqual(
                            default_config_root(),
                            Path(r"C:\Users\tester\AppData\Roaming/feishu-codex/config"),
                        )
                        self.assertEqual(
                            default_data_root(),
                            Path(r"C:\Users\tester\AppData\Local/feishu-codex/data"),
                        )

    def test_default_roots_honor_explicit_env_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "FC_CONFIG_ROOT": "/tmp/custom-config-root",
                "FC_DATA_ROOT": "/tmp/custom-data-root",
            },
            clear=True,
        ):
            self.assertEqual(default_config_root(), Path("/tmp/custom-config-root"))
            self.assertEqual(default_data_root(), Path("/tmp/custom-data-root"))


if __name__ == "__main__":
    unittest.main()
