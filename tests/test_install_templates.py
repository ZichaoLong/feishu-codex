import pathlib
import shlex
import tempfile
import unittest
from unittest.mock import patch

import yaml

from bot.install_templates import CODEX_YAML_TEMPLATE, detect_stable_codex_command, render_initial_codex_yaml


class InstallTemplateTests(unittest.TestCase):
    def test_detect_stable_codex_command_prefers_fnm_default_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fnm_root = root / "fnm"
            default_installation = fnm_root / "aliases" / "default"
            (default_installation / "bin").mkdir(parents=True)
            (default_installation / "lib" / "node_modules" / "@openai" / "codex" / "bin").mkdir(parents=True)
            stable_node = default_installation / "bin" / "node"
            stable_node.write_text("", encoding="utf-8")
            stable_codex_js = default_installation / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            stable_codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            stable_codex = default_installation / "bin" / "codex"
            stable_codex.symlink_to(stable_codex_js)
            fnm_executable = fnm_root / "fnm"
            fnm_executable.write_text("", encoding="utf-8")

            session_bin = root / "run" / "fnm_multishells" / "123" / "bin"
            session_bin.mkdir(parents=True)
            (session_bin / "node").symlink_to(stable_node)
            (session_bin / "codex").symlink_to(stable_codex)

            def _which(name: str) -> str | None:
                mapping = {
                    "fnm": str(fnm_executable),
                    "node": str(session_bin / "node"),
                    "codex": str(session_bin / "codex"),
                }
                return mapping.get(name)

            with patch("bot.install_templates.shutil.which", side_effect=_which):
                command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(stable_node), str(stable_codex)]))

    def test_render_initial_codex_yaml_embeds_detected_stable_command(self) -> None:
        with patch("bot.install_templates.detect_stable_codex_command", return_value="/stable/node /stable/codex"):
            rendered = render_initial_codex_yaml()

        self.assertIn("已自动探测到稳定的 fnm Codex 启动命令", rendered)
        self.assertIn("codex_command: /stable/node /stable/codex", rendered)
        active_lines = [
            line
            for line in rendered.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            yaml.safe_load("\n".join(active_lines)),
            {"codex_command": "/stable/node /stable/codex"},
        )

    def test_render_initial_codex_yaml_keeps_generic_template_without_stable_command(self) -> None:
        with patch("bot.install_templates.detect_stable_codex_command", return_value=None):
            rendered = render_initial_codex_yaml()

        self.assertEqual(rendered, CODEX_YAML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
