import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.__main__ import main
from bot.system_config import SystemConfig
from bot.version import __version__


class MainEntrypointTests(unittest.TestCase):
    def test_main_version_prints_project_version(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                main(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"focusd {__version__}")

    def test_main_uses_five_second_default_feishu_request_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_dir = root / "config"
            data_dir = root / "data"
            config_dir.mkdir()
            data_dir.mkdir()
            (config_dir / "system.yaml").write_text(
                'app_id: "app-id"\napp_secret: "app-secret"\n',
                encoding="utf-8",
            )
            paths = SimpleNamespace(config_dir=config_dir, data_dir=data_dir)
            bot = Mock()
            bot_cls = Mock(return_value=bot)

            with patch("bot.__main__.validate_instance_name", return_value="default"):
                with patch("bot.__main__.apply_instance_environment", return_value=paths):
                    with patch("bot.__main__.load_env_file"):
                        with patch("bot.__main__.configure_logging"):
                            with patch("bot.__main__.ensure_init_token"):
                                with patch("bot.__main__._suppress_known_third_party_runtime_warnings"):
                                    with patch.dict(
                                        sys.modules,
                                        {"bot.standalone": SimpleNamespace(CodexBot=bot_cls)},
                                    ):
                                        main([])

        bot_cls.assert_called_once_with(
            system_config=SystemConfig(
                app_id="app-id",
                app_secret="app-secret",
            )
        )
        bot.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
