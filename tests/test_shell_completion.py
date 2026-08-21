"""Shell completion runtime and installation regressions."""

import argparse
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.shell_completion import complete_words
from bot.shell_completion_install import (
    render_bash_completion_script,
    render_powershell_completion_script,
    render_zsh_completion_script,
)


class ShellCompletionTests(unittest.TestCase):
    @staticmethod
    def _add_parser_command(
        parser: argparse.ArgumentParser,
        path: tuple[str, ...],
        command: str,
    ) -> None:
        current = parser
        for component in path:
            subparsers = next(
                action
                for action in current._actions
                if isinstance(action, argparse._SubParsersAction)
            )
            current = subparsers.choices[component]
        subparsers = next(
            action
            for action in current._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        subparsers.add_parser(command, help="future test command")

    def test_rendered_script_embeds_python_path_and_registrations(self) -> None:
        rendered = render_bash_completion_script(venv_python=pathlib.Path("/tmp/venv/bin/python"))

        self.assertIn("/tmp/venv/bin/python", rendered)
        self.assertIn("complete -o bashdefault -o default -F _focus_complete_focus focus", rendered)
        self.assertIn("complete -o bashdefault -o default -F _focus_complete_focusctl focusctl", rendered)
        self.assertIn("complete -o bashdefault -o default -F _focus_complete_focusd focusd", rendered)
        self.assertIn("complete -o bashdefault -o default -F _focus_complete_fcodex fcodex", rendered)

    def test_rendered_zsh_script_embeds_python_path_and_compdef(self) -> None:
        rendered = render_zsh_completion_script(venv_python=pathlib.Path("/tmp/venv/bin/python"))

        self.assertIn("/tmp/venv/bin/python", rendered)
        self.assertIn("autoload -Uz compinit", rendered)
        self.assertIn("compdef _focus_complete_focus focus", rendered)
        self.assertIn("compdef _focus_complete_fcodex fcodex", rendered)

    def test_rendered_powershell_script_embeds_python_path_and_registrations(self) -> None:
        rendered = render_powershell_completion_script(venv_python=pathlib.Path("/tmp/venv/Scripts/python.exe"))

        self.assertIn("/tmp/venv/Scripts/python.exe", rendered)
        self.assertIn(
            "$script:FocusCompletionCommands = @('focus', 'focusctl', 'focusd', 'fcodex')",
            rendered,
        )
        self.assertIn("Register-ArgumentCompleter -Native -CommandName $commandName", rendered)
        self.assertIn("bot.shell_completion complete", rendered)
        self.assertIn("focusctl", rendered)

    def test_focusctl_completes_instance_option_and_remove_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            config_root = root / "config"
            data_root = root / "data"
            (config_root / "instances" / "corp-a").mkdir(parents=True, exist_ok=True)
            (data_root / "instances" / "corp-b").mkdir(parents=True, exist_ok=True)
            with patch.dict(
                os.environ,
                {
                    "FOCUS_CONFIG_ROOT": str(config_root),
                    "FOCUS_DATA_ROOT": str(data_root),
                },
                clear=False,
            ):
                instance_matches = complete_words("focusctl", ["focusctl", "--instance", ""], 2)
                remove_matches = complete_words("focusctl", ["focusctl", "instance", "remove", ""], 3)

        self.assertEqual(instance_matches, ["corp-a", "corp-b", "default"])
        self.assertEqual(remove_matches, ["corp-a", "corp-b"])

    def test_public_commands_complete_version_option(self) -> None:
        self.assertIn("--version", complete_words("focus", ["focus", "--"], 1))
        self.assertIn("--version", complete_words("focusctl", ["focusctl", "--"], 1))
        self.assertIn("--version", complete_words("focusd", ["focusd", "--"], 1))
        self.assertIn("--version", complete_words("fcodex", ["fcodex", "--"], 1))

    def test_wrapper_instance_completion_matches_position_independent_parser(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_root = pathlib.Path(tmpdir) / "config"
            (config_root / "instances" / "corp-a").mkdir(parents=True)
            with patch.dict(
                os.environ,
                {"FOCUS_CONFIG_ROOT": str(config_root)},
                clear=False,
            ):
                after_command = complete_words(
                    "fcodex",
                    ["fcodex", "resume", "--instance", ""],
                    3,
                )
                after_target = complete_words(
                    "focus",
                    ["focus", "resume", "thread-1", "--instance", ""],
                    4,
                )

        self.assertIn("corp-a", after_command)
        self.assertIn("corp-a", after_target)

    def test_focusctl_completes_thread_goal_subcommands(self) -> None:
        matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "goal", ""],
            3,
        )

        self.assertEqual(matches, ["show", "set", "clear"])

    def test_focusctl_completes_clear_archived_bindings(self) -> None:
        action_matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "clear"],
            2,
        )
        option_matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "clear-archived-bindings", "--"],
            3,
        )

        self.assertEqual(action_matches, ["clear-archived-bindings"])
        self.assertEqual(option_matches, ["--thread-id", "--all", "--dry-run", "--help"])

    def test_focusctl_completes_thread_lifecycle_commands(self) -> None:
        action_matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "un"],
            2,
        )
        list_options = complete_words(
            "focusctl",
            ["focusctl", "thread", "list", "--a"],
            3,
        )
        unarchive_options = complete_words(
            "focusctl",
            ["focusctl", "thread", "unarchive", "--"],
            3,
        )
        delete_options = complete_words(
            "focusctl",
            ["focusctl", "thread", "delete", "--"],
            3,
        )

        self.assertEqual(action_matches, ["unarchive"])
        self.assertEqual(list_options, ["--archived"])
        self.assertEqual(unarchive_options, ["--thread-id", "--help"])
        self.assertEqual(delete_options, ["--thread-id", "--force", "--help"])

    def test_focusctl_completion_projects_new_routed_parser_subcommand(self) -> None:
        from bot.focusctl import focusctl_command_schema
        from bot.runtime_admin import cli_inputs
        from bot.shell_completion import _command_schema

        original_build_parser = cli_inputs.build_runtime_admin_parser

        def build_parser_with_future_thread_action():
            parser = original_build_parser()
            self._add_parser_command(parser, ("thread",), "future-action")
            return parser

        try:
            with patch.object(
                cli_inputs,
                "build_runtime_admin_parser",
                side_effect=build_parser_with_future_thread_action,
            ):
                focusctl_command_schema.cache_clear()
                _command_schema.cache_clear()
                matches = complete_words(
                    "focusctl",
                    ["focusctl", "thread", "future"],
                    2,
                )
        finally:
            focusctl_command_schema.cache_clear()
            _command_schema.cache_clear()

        self.assertEqual(matches, ["future-action"])

    def test_focusctl_schema_rejects_unrouted_service_parser_action(self) -> None:
        from bot.focusctl import focusctl_command_schema
        from bot.runtime_admin import cli_inputs

        original_build_parser = cli_inputs.build_runtime_admin_parser

        def build_parser_with_unrouted_service_action():
            parser = original_build_parser()
            self._add_parser_command(parser, ("service",), "future-action")
            return parser

        try:
            with patch.object(
                cli_inputs,
                "build_runtime_admin_parser",
                side_effect=build_parser_with_unrouted_service_action,
            ):
                focusctl_command_schema.cache_clear()
                with self.assertRaisesRegex(
                    RuntimeError,
                    "runtime service routing drift",
                ):
                    focusctl_command_schema()
        finally:
            focusctl_command_schema.cache_clear()

    def test_focusctl_completes_binding_clear_stale(self) -> None:
        action_matches = complete_words(
            "focusctl",
            ["focusctl", "binding", "clear-"],
            2,
        )
        option_matches = complete_words(
            "focusctl",
            ["focusctl", "binding", "clear-stale", "--"],
            3,
        )

        self.assertEqual(action_matches, ["clear-all", "clear-stale"])
        self.assertEqual(option_matches, ["--dry-run", "--help"])

    def test_focusctl_completes_binding_list_refresh_names(self) -> None:
        option_matches = complete_words(
            "focusctl",
            ["focusctl", "binding", "list", "--"],
            3,
        )
        refresh_matches = complete_words(
            "focusctl",
            ["focusctl", "binding", "list", "--ref"],
            3,
        )

        self.assertEqual(option_matches, ["--refresh-names", "--help"])
        self.assertEqual(refresh_matches, ["--refresh-names"])

    def test_focusctl_completes_web_open(self) -> None:
        action_matches = complete_words(
            "focusctl",
            ["focusctl", "web", ""],
            2,
        )
        option_matches = complete_words(
            "focusctl",
            ["focusctl", "web", "open", "--"],
            3,
        )

        self.assertEqual(action_matches, ["open"])
        self.assertEqual(option_matches, ["--no-browser", "--help"])

    def test_focusctl_completes_thread_goal_set_options_and_status(self) -> None:
        option_matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "goal", "set", "--"],
            4,
        )
        status_matches = complete_words(
            "focusctl",
            ["focusctl", "thread", "goal", "set", "--status", "p"],
            5,
        )

        self.assertEqual(
            option_matches,
            ["--thread-id", "--thread-name", "--objective", "--status", "--help"],
        )
        self.assertEqual(status_matches, ["paused"])

    def test_fcodex_skips_known_upstream_option_values_when_completing_resume(self) -> None:
        matches = complete_words(
            "fcodex",
            ["fcodex", "-p", "demo", ""],
            3,
        )

        self.assertEqual(matches, ["resume"])


if __name__ == "__main__":
    unittest.main()
