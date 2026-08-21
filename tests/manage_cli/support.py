"""Shared support for Manage CLI capability tests."""

import pathlib
import unittest

from bot.cli_table import terminal_display_width as _terminal_display_width


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class ManageCliTestCase(unittest.TestCase):
    """Provide stateless helpers without defining reusable test cases."""

    @staticmethod
    def _visual_cell_starts(line: str, cells: list[str]) -> list[int]:
        starts: list[int] = []
        search_from = 0
        for cell in cells:
            index = line.index(cell, search_from)
            starts.append(_terminal_display_width(line[:index]))
            search_from = index + len(cell)
        return starts

    @staticmethod
    def _isolated_install_env(root: pathlib.Path) -> dict[str, str]:
        config_root = root / "config"
        data_root = root / "data"
        return {
            "FOCUS_CONFIG_ROOT": str(config_root),
            "FOCUS_DATA_ROOT": str(data_root),
            "FOCUS_GLOBAL_DATA_DIR": str(data_root / "_global"),
            "FOCUS_BIN_DIR": str(root / "bin"),
            "FOCUS_BASH_COMPLETION_DIR": str(root / "completion" / "bash"),
            "FOCUS_ZSH_COMPLETION_PATH": str(root / "completion" / "zsh" / "focus.zsh"),
            "FOCUS_ZSH_RC_PATH": str(root / "shells" / "zshrc"),
            "FOCUS_POWERSHELL_COMPLETION_PATH": str(
                root / "completion" / "powershell" / "focus.ps1"
            ),
            "FOCUS_POWERSHELL_PROFILE_PATH": str(root / "shells" / "profile.ps1"),
            "FOCUS_ENV_FILE": str(config_root / "focus.env"),
        }
