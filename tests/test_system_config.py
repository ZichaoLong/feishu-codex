from __future__ import annotations

import os
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

from bot.config import load_config, load_config_file, save_system_config
from bot.system_config import DEFAULT_SYSTEM_CONFIG, SystemConfig


class SystemConfigTests(unittest.TestCase):
    def test_defaults_are_owned_by_the_typed_schema(self) -> None:
        config = SystemConfig.from_dict({}, require_credentials=False)

        self.assertEqual(config.request_timeout_seconds, 5.0)
        self.assertEqual(config.feishu_ws_proxy, "env")
        self.assertEqual(config.admin_open_ids, ())
        self.assertEqual(config.trigger_open_ids, ())
        self.assertEqual(config.group_history_fetch_limit, 50)
        self.assertEqual(config.group_history_fetch_lookback_seconds, 86400)
        self.assertFalse(config.debug_raw_card_ingress)

    def test_file_admission_requires_nonempty_credentials(self) -> None:
        invalid_configs = (
            {},
            {"app_id": "app-id"},
            {"app_id": "", "app_secret": "secret"},
            {"app_id": "app-id", "app_secret": None},
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "app_id|app_secret"):
                    SystemConfig.from_dict(config, require_credentials=True)

        config = SystemConfig.from_dict(
            {"app_id": " app-id ", "app_secret": " secret "},
            require_credentials=True,
        )
        self.assertEqual(config.app_id, "app-id")
        self.assertEqual(config.app_secret, "secret")

    def test_unknown_and_non_string_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "admin_open_id"):
            SystemConfig.from_dict(
                {
                    "app_id": "app-id",
                    "app_secret": "secret",
                    "admin_open_id": "ou-a",
                }
            )
        with self.assertRaisesRegex(ValueError, "顶层键必须是字符串"):
            SystemConfig.from_dict({1: "value"}, require_credentials=False)

    def test_boolean_and_number_types_are_not_coerced(self) -> None:
        invalid_cases = (
            ({"debug_raw_card_ingress": "false"}, "debug_raw_card_ingress"),
            ({"debug_raw_card_ingress": 0}, "debug_raw_card_ingress"),
            ({"request_timeout_seconds": "5"}, "request_timeout_seconds"),
            ({"request_timeout_seconds": True}, "request_timeout_seconds"),
            ({"group_history_fetch_limit": "50"}, "group_history_fetch_limit"),
            ({"group_history_fetch_limit": False}, "group_history_fetch_limit"),
            (
                {"group_history_fetch_lookback_seconds": 1.5},
                "group_history_fetch_lookback_seconds",
            ),
        )
        for raw, field in invalid_cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, field):
                    SystemConfig.from_dict(raw, require_credentials=False)

    def test_numeric_ranges_are_fail_closed(self) -> None:
        invalid_cases = (
            ({"request_timeout_seconds": 0}, "request_timeout_seconds"),
            ({"request_timeout_seconds": -1}, "request_timeout_seconds"),
            ({"request_timeout_seconds": float("nan")}, "request_timeout_seconds"),
            ({"request_timeout_seconds": float("inf")}, "request_timeout_seconds"),
            ({"group_history_fetch_limit": -1}, "group_history_fetch_limit"),
            (
                {"group_history_fetch_lookback_seconds": -1},
                "group_history_fetch_lookback_seconds",
            ),
        )
        for raw, field in invalid_cases:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, field):
                    SystemConfig.from_dict(raw, require_credentials=False)

        config = SystemConfig.from_dict(
            {
                "request_timeout_seconds": 0.25,
                "group_history_fetch_limit": 0,
                "group_history_fetch_lookback_seconds": 0,
            },
            require_credentials=False,
        )
        self.assertEqual(config.request_timeout_seconds, 0.25)
        self.assertEqual(config.group_history_fetch_limit, 0)
        self.assertEqual(config.group_history_fetch_lookback_seconds, 0)

    def test_open_id_fields_are_lists_of_unique_nonempty_strings(self) -> None:
        invalid_values = (
            "ou-a",
            ("ou-a",),
            ["ou-a", 1],
            ["ou-a", None],
            [""],
            ["   "],
            ["ou-a", " ou-a "],
        )
        for field in ("admin_open_ids", "trigger_open_ids"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        SystemConfig.from_dict(
                            {field: value},
                            require_credentials=False,
                        )

        config = SystemConfig.from_dict(
            {
                "admin_open_ids": [" ou-a ", "ou-b"],
                "trigger_open_ids": [],
            },
            require_credentials=False,
        )
        self.assertEqual(config.admin_open_ids, ("ou-a", "ou-b"))
        self.assertEqual(config.trigger_open_ids, ())

    def test_proxy_mode_is_a_closed_enum(self) -> None:
        with self.assertRaisesRegex(ValueError, "feishu_ws_proxy"):
            SystemConfig.from_dict(
                {"feishu_ws_proxy": "container"},
                require_credentials=False,
            )

        config = SystemConfig.from_dict(
            {"feishu_ws_proxy": " DISABLED "},
            require_credentials=False,
        )
        self.assertEqual(config.feishu_ws_proxy, "disabled")

    def test_load_config_returns_typed_validated_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            (directory / "system.yaml").write_text(
                "app_id: app-id\napp_secret: secret\nrequest_timeout_seconds: 2\n",
                encoding="utf-8",
            )

            config = load_config(directory=directory)

        self.assertIsInstance(config, SystemConfig)
        self.assertEqual(config.request_timeout_seconds, 2.0)

    def test_load_config_rejects_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            (directory / "system.yaml").write_text(
                "- app_id\n- app_secret\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "顶层必须是 YAML mapping"):
                load_config(directory=directory)

    def test_yaml_loader_distinguishes_empty_documents_from_explicit_null(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            path = directory / "codex.yaml"

            for rendered in ("", "  \n# sparse override\n"):
                with self.subTest(rendered=rendered):
                    path.write_text(rendered, encoding="utf-8")
                    self.assertEqual(load_config_file("codex", directory=directory), {})

            for rendered in ("null\n", "~\n", "---\n"):
                with self.subTest(rendered=rendered):
                    path.write_text(rendered, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "YAML null"):
                        load_config_file("codex", directory=directory)

    def test_yaml_loader_rejects_duplicate_keys_instead_of_last_value_winning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            (directory / "system.yaml").write_text(
                "app_id: first\napp_id: second\napp_secret: secret\n",
                encoding="utf-8",
            )
            (directory / "codex.yaml").write_text(
                "approval_policy: never\napproval_policy: on-request\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "duplicate key.*app_id"):
                load_config(directory=directory)
            with self.assertRaisesRegex(ValueError, "duplicate key.*approval_policy"):
                load_config_file("codex", directory=directory)

    def test_save_validates_the_complete_document_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = pathlib.Path(raw)
            path = directory / "system.yaml"
            path.write_text("original\n", encoding="utf-8")
            with patch.dict(os.environ, {"FOCUS_CONFIG_DIR": raw}):
                with self.assertRaisesRegex(ValueError, "debug_raw_card_ingress"):
                    save_system_config(
                        {
                            "app_id": "app-id",
                            "app_secret": "secret",
                            "debug_raw_card_ingress": "false",
                        }
                    )

            self.assertEqual(path.read_text(encoding="utf-8"), "original\n")

    def test_example_key_inventories_match_the_schema(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        paths = (
            repo_root / "config" / "system.yaml.example",
            repo_root / "bot" / "install_template_data" / "system.yaml.example",
        )
        rendered_templates = []
        for path in paths:
            rendered = path.read_text(encoding="utf-8")
            rendered_templates.append(rendered)
            example_keys = {
                match.group(1)
                for line in rendered.splitlines()
                if (match := re.match(r"^(?:# )?([a-z][a-z0-9_]*):(?:\s|$)", line))
            }
            self.assertEqual(example_keys, SystemConfig.accepted_keys())
            projected_defaults = {
                "request_timeout_seconds": str(
                    int(DEFAULT_SYSTEM_CONFIG.request_timeout_seconds)
                ),
                "feishu_ws_proxy": DEFAULT_SYSTEM_CONFIG.feishu_ws_proxy,
                "group_history_fetch_limit": str(
                    DEFAULT_SYSTEM_CONFIG.group_history_fetch_limit
                ),
                "group_history_fetch_lookback_seconds": str(
                    DEFAULT_SYSTEM_CONFIG.group_history_fetch_lookback_seconds
                ),
                "debug_raw_card_ingress": str(
                    DEFAULT_SYSTEM_CONFIG.debug_raw_card_ingress
                ).lower(),
            }
            for key, value in projected_defaults.items():
                self.assertIn(f"# {key}: {value}", rendered)

        self.assertEqual(rendered_templates[0], rendered_templates[1])


if __name__ == "__main__":
    unittest.main()
