import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.__main__ import main


class MainEntrypointTests(unittest.TestCase):
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
                                    with patch("bot.standalone.CodexBot", bot_cls):
                                        main([])

        bot_cls.assert_called_once_with(
            "app-id",
            "app-secret",
            request_timeout_seconds=5.0,
            system_config={"app_id": "app-id", "app_secret": "app-secret"},
        )
        bot.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
