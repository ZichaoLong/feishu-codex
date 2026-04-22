import os
import pathlib
import stat
import tempfile
import unittest
from unittest.mock import patch

from bot.manage_cli import _handle_install, _write_wrapper


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


if __name__ == "__main__":
    unittest.main()
