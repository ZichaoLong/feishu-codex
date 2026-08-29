from __future__ import annotations

import pathlib
import re
import sys
import unittest
from unittest.mock import patch

from bot.adapters.codex_app_server import CodexAppServerConfig
from bot.codex_config import CodexConfig
from bot.fcodex.cli import main as fcodex_main


class CodexConfigTests(unittest.TestCase):
    def test_parser_defaults_are_the_dataclass_defaults(self) -> None:
        self.assertEqual(CodexConfig.from_dict({}), CodexConfig())
        self.assertEqual(CodexConfig().web_disconnect_grace_seconds, 1200.0)
        self.assertEqual(CodexConfig().web_display_name, "Focus Web")
        self.assertEqual(CodexConfig().web_trusted_proxy_origin, "")
        self.assertEqual(CodexConfig().web_trusted_proxy_proof_sha256, "")

    def test_unknown_key_is_rejected_instead_of_changing_a_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "approval_polciy"):
            CodexConfig.from_dict({"approval_polciy": "on-request"})

    def test_wrong_scalar_types_are_rejected(self) -> None:
        cases = (
            ({"web_enabled": "false"}, "web_enabled"),
            ({"web_display_name": 42}, "web_display_name"),
            ({"show_history_preview_on_resume": 0}, "show_history_preview_on_resume"),
            ({"web_port": "8080"}, "web_port"),
            ({"web_trusted_proxy_origin": 1}, "web_trusted_proxy_origin"),
            (
                {"web_trusted_proxy_proof_sha256": False},
                "web_trusted_proxy_proof_sha256",
            ),
            ({"request_timeout_seconds": "30"}, "request_timeout_seconds"),
            ({"model": False}, "model"),
        )
        for config, key in cases:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    CodexConfig.from_dict(config)

    def test_source_kinds_must_be_a_nonempty_string_list(self) -> None:
        invalid_values = ("cli", [], ["cli", 1], [""])
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "source_kinds"):
                    CodexConfig.from_dict({"source_kinds": value})

        config = CodexConfig.from_dict({"source_kinds": ["cli", "appServer"]})

        self.assertEqual(config.source_kinds, ("cli", "appServer"))

    def test_strings_are_trimmed_without_removing_intentional_empty_semantics(self) -> None:
        config = CodexConfig.from_dict(
            {
                "codex_command": "  codex --profile focus  ",
                "web_display_name": "  Workstation A  ",
                "service_name": "  focus-web  ",
                "model": "   ",
                "service_tier": "  flex  ",
                "reasoning_effort": "  high  ",
                "source_kinds": [" cli ", " appServer "],
            }
        )

        self.assertEqual(config.codex_command, "codex --profile focus")
        self.assertEqual(config.web_display_name, "Workstation A")
        self.assertEqual(config.service_name, "focus-web")
        self.assertEqual(config.model, "")
        self.assertEqual(config.service_tier, "flex")
        self.assertEqual(config.reasoning_effort, "high")
        self.assertEqual(config.source_kinds, ("cli", "appServer"))

        with self.assertRaisesRegex(ValueError, "service_name"):
            CodexConfig.from_dict({"service_name": "   "})
        with self.assertRaisesRegex(ValueError, "web_display_name"):
            CodexConfig.from_dict({"web_display_name": "   "})

    def test_owned_app_server_url_is_an_upstream_supported_loopback_listener(self) -> None:
        invalid_values = (
            "http://127.0.0.1:8765",
            "wss://127.0.0.1:8765",
            "ws://localhost:8765",
            "ws://0.0.0.0:8765",
            "ws://192.168.1.2:8765",
            "ws://127.0.0.1:8765/rpc",
            "ws://127.0.0.1:8765/",
            "ws://127.0.0.1",
            "ws://127.0.0.1:0",
            "ws://user@127.0.0.1:8765",
            "ws://127.0.0.1:8765?token=secret",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "app_server_url"):
                    CodexConfig.from_dict({"app_server_url": value})

        ipv4 = CodexConfig.from_dict({"app_server_url": "ws://127.0.0.2:8765"})
        ipv6 = CodexConfig.from_dict({"app_server_url": "ws://[::1]:8765"})
        normalized = CodexConfig.from_dict(
            {"app_server_url": "  WS://127.0.0.1:8765  "}
        )

        self.assertEqual(ipv4.app_server_url, "ws://127.0.0.2:8765")
        self.assertEqual(ipv6.app_server_url, "ws://[::1]:8765")
        self.assertEqual(normalized.app_server_url, "ws://127.0.0.1:8765")

    def test_external_app_server_deployment_mode_is_removed(self) -> None:
        with self.assertRaisesRegex(ValueError, "app_server_mode.*已移除"):
            CodexConfig.from_dict({"app_server_mode": "remote"})

        with self.assertRaisesRegex(ValueError, "app_server_url"):
            CodexConfig.from_dict(
                {"app_server_url": "wss://codex.example.test:443/rpc"}
            )

    def test_web_network_settings_are_admitted_by_the_schema(self) -> None:
        for host in ("0.0.0.0", "192.168.1.2", "focus.example.test"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(ValueError, "web_host"):
                    CodexConfig.from_dict({"web_host": host})

        with self.assertRaisesRegex(ValueError, "web_session_ttl_seconds"):
            CodexConfig.from_dict({"web_session_ttl_seconds": 59.9})

        config = CodexConfig.from_dict(
            {"web_host": "LOCALHOST", "web_session_ttl_seconds": 60}
        )

        self.assertEqual(config.web_host, "localhost")
        self.assertEqual(config.web_session_ttl_seconds, 60.0)

    def test_trusted_proxy_config_is_an_atomic_exact_remote_mode(self) -> None:
        proof_sha256 = "0123456789abcdef" * 4
        for config in (
            {"web_trusted_proxy_origin": "https://focus.example.test"},
            {"web_trusted_proxy_proof_sha256": proof_sha256},
        ):
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "同时为空或同时有值"):
                    CodexConfig.from_dict(config)

        invalid_digests = (
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            f" {'a' * 64}",
        )
        for digest in invalid_digests:
            with self.subTest(digest=digest):
                with self.assertRaisesRegex(
                    ValueError,
                    "web_trusted_proxy_proof_sha256",
                ):
                    CodexConfig.from_dict(
                        {
                            "web_enabled": True,
                            "web_port": 8443,
                            "web_trusted_proxy_origin": "https://focus.example.test",
                            "web_trusted_proxy_proof_sha256": digest,
                        }
                    )

        base = {
            "web_port": 8443,
            "web_trusted_proxy_origin": "https://focus.example.test",
            "web_trusted_proxy_proof_sha256": proof_sha256,
        }
        with self.assertRaisesRegex(ValueError, "web_enabled"):
            CodexConfig.from_dict(base)
        with self.assertRaisesRegex(ValueError, "web_port"):
            CodexConfig.from_dict({**base, "web_enabled": True, "web_port": 0})
        with self.assertRaisesRegex(ValueError, "web_trusted_proxy_origin"):
            CodexConfig.from_dict(
                {
                    **base,
                    "web_enabled": True,
                    "web_trusted_proxy_origin": "https://focus.example.test/",
                }
            )
        with self.assertRaisesRegex(ValueError, "web_trusted_proxy_origin"):
            CodexConfig.from_dict(
                {
                    **base,
                    "web_enabled": True,
                    "web_trusted_proxy_origin": "https://localhost",
                }
            )

        config = CodexConfig.from_dict({**base, "web_enabled": True})

        self.assertTrue(config.web_enabled)
        self.assertEqual(config.web_host, "127.0.0.1")
        self.assertEqual(config.web_port, 8443)
        self.assertEqual(
            config.web_trusted_proxy_origin,
            "https://focus.example.test",
        )
        self.assertEqual(config.web_trusted_proxy_proof_sha256, proof_sha256)

    def test_approval_policy_is_a_closed_enum_with_one_deprecated_alias(self) -> None:
        with self.assertRaisesRegex(ValueError, "approval_policy"):
            CodexConfig.from_dict({"approval_policy": "on_requst"})

        config = CodexConfig.from_dict({"approval_policy": "on-failure"})

        self.assertEqual(config.approval_policy, "on-request")

    def test_approvals_reviewer_stays_on_the_reviewed_user_route(self) -> None:
        with self.assertRaisesRegex(ValueError, "approvals_reviewer"):
            CodexConfig.from_dict({"approvals_reviewer": "auto_review"})

        config = CodexConfig.from_dict({"approvals_reviewer": "USER"})

        self.assertEqual(config.approvals_reviewer, "user")

    def test_personality_matches_the_upstream_closed_enum(self) -> None:
        with self.assertRaisesRegex(ValueError, "personality"):
            CodexConfig.from_dict({"personality": "cheerful"})

        config = CodexConfig.from_dict({"personality": "FRIENDLY"})

        self.assertEqual(config.personality, "friendly")

    def test_permissions_profile_is_the_closed_focus_builtin_set(self) -> None:
        for value in (":workspcae", "workspace", "custom-profile"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "permissions_profile_id"):
                    CodexConfig.from_dict({"permissions_profile_id": value})

        config = CodexConfig.from_dict({"permissions_profile_id": ":WORKSPACE"})

        self.assertEqual(config.permissions_profile_id, ":workspace")

    def test_adapter_from_dict_uses_the_complete_component_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "web_enabeld"):
            CodexAppServerConfig.from_dict({"web_enabeld": True})

        adapter_config = CodexAppServerConfig.from_dict(
            {
                "web_enabled": True,
                "source_kinds": ["cli"],
                "request_timeout_seconds": 12,
            }
        )

        self.assertEqual(adapter_config.source_kinds, ["cli"])
        self.assertEqual(adapter_config.request_timeout_seconds, 12.0)

    def test_fcodex_validates_complete_config_before_normal_launch(self) -> None:
        with patch("bot.fcodex.cli.load_env_file"):
            with patch(
                "bot.fcodex.cli.load_config_file",
                return_value={"approval_polciy": "on-request"},
            ):
                with patch.object(
                    sys,
                    "argv",
                    ["fcodex"],
                ):
                    with patch("bot.fcodex.cli.os.execvpe") as mock_exec:
                        with self.assertRaisesRegex(ValueError, "approval_polciy"):
                            fcodex_main()

        mock_exec.assert_not_called()

    def test_example_key_inventory_matches_the_schema(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        template = (repo_root / "config" / "codex.yaml.example").read_text(encoding="utf-8")
        example_keys = {
            match.group(1)
            for line in template.splitlines()
            if (match := re.match(r"^(?:# )?([a-z][a-z0-9_]*):(?:\s|$)", line))
        }

        self.assertEqual(example_keys, CodexConfig.accepted_keys())


if __name__ == "__main__":
    unittest.main()
