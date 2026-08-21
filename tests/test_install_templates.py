import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import yaml

import bot.install_templates as install_templates
from bot.system_config import DEFAULT_SYSTEM_CONFIG
from bot.codex_command_resolver import resolve_managed_codex_command
from bot.install_templates import CODEX_YAML_TEMPLATE, SYSTEM_YAML_TEMPLATE, detect_stable_codex_command, render_initial_codex_yaml


class InstallTemplateTests(unittest.TestCase):
    @staticmethod
    def _write_native_executable(path: pathlib.Path, *, magic: bytes = b"\x7fELF") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(magic + b"\0" * 64)
        path.chmod(0o755)

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

            with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(stable_node), str(stable_codex)]))

    def test_detect_stable_codex_command_supports_nvm_default_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            nvm_root = root / ".nvm"
            version_root = nvm_root / "versions" / "node" / "v24.15.0" / "bin"
            version_root.mkdir(parents=True)
            (nvm_root / "alias").mkdir(parents=True)
            stable_node = version_root / "node"
            stable_node.write_text("", encoding="utf-8")
            stable_codex_js = (
                nvm_root
                / "versions"
                / "node"
                / "v24.15.0"
                / "lib"
                / "node_modules"
                / "@openai"
                / "codex"
                / "bin"
                / "codex.js"
            )
            stable_codex_js.parent.mkdir(parents=True)
            stable_codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            stable_codex = version_root / "codex"
            stable_codex.symlink_to(stable_codex_js)
            (nvm_root / "alias" / "default").write_text("v24.15.0\n", encoding="utf-8")

            with patch.dict("os.environ", {"HOME": str(root), "FNM_DIR": "", "NVM_DIR": ""}, clear=False):
                with patch("bot.codex_command_resolver.shutil.which", return_value=None):
                    command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(stable_node), str(stable_codex_js)]))

    def test_detect_and_resolve_standalone_native_codex_as_absolute_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            native_codex = root / "standalone" / "codex"
            self._write_native_executable(native_codex)

            def _which(name: str) -> str | None:
                return str(native_codex) if name == "codex" else None

            with patch.dict(
                "os.environ",
                {"HOME": str(root), "FNM_DIR": "", "NVM_DIR": ""},
                clear=False,
            ):
                with patch("bot.codex_command_resolver._is_windows", return_value=False):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        detected = detect_stable_codex_command()
                        resolved = resolve_managed_codex_command("codex")

        expected = shlex.join([str(native_codex.resolve())])
        self.assertEqual(detected, expected)
        self.assertEqual(resolved, expected)

    def test_detect_standalone_native_codex_supports_mach_o(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            native_codex = root / "standalone" / "codex"
            self._write_native_executable(native_codex, magic=b"\xcf\xfa\xed\xfe")

            def _which(name: str) -> str | None:
                return str(native_codex) if name == "codex" else None

            with patch.dict(
                "os.environ",
                {"HOME": str(root), "FNM_DIR": "", "NVM_DIR": ""},
                clear=False,
            ):
                with patch("bot.codex_command_resolver._is_windows", return_value=False):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(native_codex.resolve())]))

    def test_detect_standalone_native_codex_preserves_stable_symlink_and_beats_unused_nvm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            native_codex = root / "Cellar" / "codex" / "1.2.3" / "bin" / "codex"
            self._write_native_executable(native_codex)
            stable_codex = root / "bin" / "codex"
            stable_codex.parent.mkdir(parents=True)
            stable_codex.symlink_to(native_codex)

            nvm_root = root / ".nvm"
            nvm_bin = nvm_root / "versions" / "node" / "v24.0.0" / "bin"
            nvm_bin.mkdir(parents=True)
            (nvm_bin / "node").write_text("", encoding="utf-8")
            nvm_codex = nvm_bin / "codex"
            nvm_codex.write_text("#!/usr/bin/env node\n", encoding="utf-8")

            def _which(name: str) -> str | None:
                return str(stable_codex) if name == "codex" else None

            with patch.dict(
                "os.environ",
                {"HOME": str(root), "FNM_DIR": "", "NVM_DIR": str(nvm_root)},
                clear=False,
            ):
                with patch("bot.codex_command_resolver._is_windows", return_value=False):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(stable_codex.absolute())]))

    def test_detect_stable_codex_command_does_not_treat_arbitrary_script_as_native(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            scripted_codex = root / "bin" / "codex"
            scripted_codex.parent.mkdir(parents=True)
            scripted_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            scripted_codex.chmod(0o755)

            def _which(name: str) -> str | None:
                return str(scripted_codex) if name == "codex" else None

            with patch.dict(
                "os.environ",
                {"HOME": str(root), "FNM_DIR": "", "NVM_DIR": ""},
                clear=False,
            ):
                with patch("bot.codex_command_resolver._is_windows", return_value=False):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        detected = detect_stable_codex_command()
                        resolved = resolve_managed_codex_command("codex")

        self.assertIsNone(detected)
        self.assertEqual(resolved, "codex")

    def test_detect_stable_codex_command_on_windows_supports_global_npm_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            appdata = root / "AppData" / "Roaming"
            npm_dir = appdata / "npm"
            wrapper = npm_dir / "codex.cmd"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("@echo off\r\n", encoding="utf-8")
            codex_js = npm_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            codex_js.parent.mkdir(parents=True)
            codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            node = root / "Program Files" / "nodejs" / "node.exe"
            node.parent.mkdir(parents=True)
            node.write_text("", encoding="utf-8")

            def _which(name: str) -> str | None:
                if name == "codex":
                    return str(wrapper)
                if name == "node":
                    return str(node)
                return None

            with patch("bot.codex_command_resolver._is_windows", return_value=True):
                with patch.dict(
                    "os.environ",
                    {
                        "APPDATA": str(appdata),
                        "ProgramFiles": str(root / "Program Files"),
                        "ProgramFiles(x86)": "",
                        "HOME": str(root),
                    },
                    clear=False,
                ):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        command = detect_stable_codex_command()

        self.assertEqual(
            command,
            shlex.join(
                [
                    str(node).replace("\\", "/"),
                    str(codex_js).replace("\\", "/"),
                ]
            ),
        )

    def test_detect_stable_codex_command_on_windows_supports_native_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            native_codex = root / "standalone" / "codex.exe"
            native_codex.parent.mkdir(parents=True)
            payload = bytearray(0x84)
            payload[:2] = b"MZ"
            payload[0x3C:0x40] = (0x80).to_bytes(4, byteorder="little")
            payload[0x80:0x84] = b"PE\0\0"
            native_codex.write_bytes(payload)

            def _which(name: str) -> str | None:
                return str(native_codex) if name == "codex" else None

            with patch("bot.codex_command_resolver._is_windows", return_value=True):
                with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                    command = detect_stable_codex_command()

        self.assertEqual(command, shlex.join([str(native_codex.resolve()).replace("\\", "/")]))

    def test_resolve_managed_codex_command_normalizes_explicit_nvm_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            installation_root = root / ".nvm" / "versions" / "node" / "v24.15.0"
            wrapper = installation_root / "bin" / "codex"
            wrapper.parent.mkdir(parents=True)
            node = installation_root / "bin" / "node"
            node.write_text("", encoding="utf-8")
            codex_js = installation_root / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            codex_js.parent.mkdir(parents=True)
            codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            wrapper.symlink_to(codex_js)

            command = resolve_managed_codex_command(str(wrapper))

        self.assertEqual(command, shlex.join([str(node), str(codex_js)]))

    def test_resolve_managed_codex_command_on_windows_prefers_current_npm_wrapper_with_explicit_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            appdata = root / "AppData" / "Roaming"
            npm_dir = appdata / "npm"
            wrapper = npm_dir / "codex.cmd"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("@echo off\r\n", encoding="utf-8")
            codex_js = npm_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            codex_js.parent.mkdir(parents=True)
            codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            node = root / "Program Files" / "nodejs" / "node.exe"
            node.parent.mkdir(parents=True)
            node.write_text("", encoding="utf-8")

            def _which(name: str) -> str | None:
                if name == "codex":
                    return str(wrapper)
                if name == "node":
                    return str(node)
                return None

            with patch("bot.codex_command_resolver._is_windows", return_value=True):
                with patch.dict(
                    "os.environ",
                    {
                        "APPDATA": str(appdata),
                        "ProgramFiles": str(root / "Program Files"),
                        "ProgramFiles(x86)": "",
                        "HOME": str(root),
                    },
                    clear=False,
                ):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        command = resolve_managed_codex_command("codex")

        self.assertEqual(
            command,
            shlex.join(
                [
                    str(node).replace("\\", "/"),
                    str(codex_js).replace("\\", "/"),
                ]
            ),
        )

    def test_resolve_managed_codex_command_on_windows_falls_back_to_appdata_npm_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            appdata = root / "AppData" / "Roaming"
            npm_dir = appdata / "npm"
            wrapper = npm_dir / "codex.cmd"
            wrapper.parent.mkdir(parents=True)
            wrapper.write_text("@echo off\r\n", encoding="utf-8")
            codex_js = npm_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            codex_js.parent.mkdir(parents=True)
            codex_js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            node = root / "Program Files" / "nodejs" / "node.exe"
            node.parent.mkdir(parents=True)
            node.write_text("", encoding="utf-8")

            def _which(name: str) -> str | None:
                if name == "node":
                    return str(node)
                return None

            with patch("bot.codex_command_resolver._is_windows", return_value=True):
                with patch.dict(
                    "os.environ",
                    {
                        "APPDATA": str(appdata),
                        "ProgramFiles": str(root / "Program Files"),
                        "ProgramFiles(x86)": "",
                        "HOME": str(root),
                    },
                    clear=False,
                ):
                    with patch("bot.codex_command_resolver.shutil.which", side_effect=_which):
                        command = resolve_managed_codex_command("codex")

        self.assertEqual(
            command,
            shlex.join(
                [
                    str(node).replace("\\", "/"),
                    str(codex_js).replace("\\", "/"),
                ]
            ),
        )

    def test_render_initial_codex_yaml_embeds_detected_stable_command(self) -> None:
        with patch("bot.install_templates.detect_stable_codex_command", return_value="/stable/node /stable/codex"):
            rendered = render_initial_codex_yaml()

        self.assertIn("已自动探测到稳定的 Codex 启动命令", rendered)
        self.assertIn("codex_command: /stable/node /stable/codex", rendered)
        active_lines = [
            line
            for line in rendered.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            yaml.safe_load("\n".join(active_lines)),
            {
                "web_enabled": True,
                "codex_command": "/stable/node /stable/codex",
            },
        )

    def test_render_initial_codex_yaml_keeps_generic_template_without_stable_command(self) -> None:
        with patch("bot.install_templates.detect_stable_codex_command", return_value=None):
            rendered = render_initial_codex_yaml()

        self.assertEqual(rendered, CODEX_YAML_TEMPLATE)

    def test_codex_yaml_template_no_longer_documents_thread_memory_seed(self) -> None:
        self.assertNotIn("new_thread_memory_mode_seed", CODEX_YAML_TEMPLATE)

    def test_codex_yaml_template_documents_sparse_instance_override_behavior(self) -> None:
        self.assertIn("override 文件", CODEX_YAML_TEMPLATE)
        self.assertIn("命名实例不会继承 default 实例", CODEX_YAML_TEMPLATE)
        self.assertNotIn("managed_startup_profile", CODEX_YAML_TEMPLATE)
        self.assertIn("mirror_watchdog_seconds", CODEX_YAML_TEMPLATE)

    def test_load_template_falls_back_to_packaged_resource_when_repo_example_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("bot.install_templates._repo_root", return_value=pathlib.Path(tmpdir)):
                self.assertEqual(
                    install_templates._load_template("system.yaml.example"),
                    SYSTEM_YAML_TEMPLATE,
                )
                self.assertEqual(
                    install_templates._load_template("codex.yaml.example"),
                    CODEX_YAML_TEMPLATE,
                )

    def test_system_yaml_template_mentions_real_request_timeout_default(self) -> None:
        self.assertIn(
            f"# request_timeout_seconds: {int(DEFAULT_SYSTEM_CONFIG.request_timeout_seconds)}",
            SYSTEM_YAML_TEMPLATE,
        )

    def test_repo_codex_yaml_example_matches_install_template(self) -> None:
        repo_example = (
            pathlib.Path(__file__).resolve().parents[1] / "config" / "codex.yaml.example"
        ).read_text(encoding="utf-8")
        self.assertEqual(repo_example, CODEX_YAML_TEMPLATE)

    def test_codex_yaml_template_discloses_both_remote_web_paths(self) -> None:
        self.assertIn("本机/SSH 使用 focusctl web open", CODEX_YAML_TEMPLATE)
        self.assertIn("configured trusted HTTPS proxy", CODEX_YAML_TEMPLATE)
        self.assertIn("canonical DNS、IPv4 或 compressed IPv6 literal", CODEX_YAML_TEMPLATE)
        self.assertIn("Proxy/private network 必须先拥有认证/ACL", CODEX_YAML_TEMPLATE)
        self.assertIn("项目 README 链接的“Focus Web 自部署外部访问”", CODEX_YAML_TEMPLATE)
        self.assertNotIn("docs/decisions/focus-web-external-access", CODEX_YAML_TEMPLATE)

    def test_repo_system_yaml_example_matches_install_template(self) -> None:
        repo_example = (
            pathlib.Path(__file__).resolve().parents[1] / "config" / "system.yaml.example"
        ).read_text(encoding="utf-8")
        self.assertEqual(repo_example, SYSTEM_YAML_TEMPLATE)

    def test_packaged_template_files_match_repo_examples(self) -> None:
        packaged_dir = install_templates._packaged_template_dir()
        repo_root = pathlib.Path(__file__).resolve().parents[1]

        self.assertEqual(
            (packaged_dir / "system.yaml.example").read_text(encoding="utf-8"),
            (repo_root / "config" / "system.yaml.example").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (packaged_dir / "codex.yaml.example").read_text(encoding="utf-8"),
            (repo_root / "config" / "codex.yaml.example").read_text(encoding="utf-8"),
        )

    def test_pyproject_includes_packaged_template_payload(self) -> None:
        pyproject_path = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

        package_data = data["tool"]["setuptools"]["package-data"]
        self.assertEqual(package_data["bot.install_template_data"], ["*.example"])
        self.assertEqual(
            package_data["bot.web_assets"],
            ["dist/**", "THIRD_PARTY_NOTICES.md"],
        )

    def test_python_dependency_inputs_and_locks_cover_their_declared_layers(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
        def requirement_name(value: str) -> str:
            base = value.split(";", 1)[0].split("[", 1)[0]
            for separator in ("=", "<", ">"):
                base = base.split(separator, 1)[0]
            return base.strip().lower()

        def direct_requirements(filename: str) -> set[str]:
            return {
                line.strip()
                for line in (repo_root / filename).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }

        def locked_names(filename: str) -> set[str]:
            return {
                requirement_name(line)
                for line in (repo_root / filename).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#") and "==" in line
            }

        runtime_names = {requirement_name(item) for item in pyproject["project"]["dependencies"]}
        build_inputs = direct_requirements("requirements-build.in")
        dev_inputs = direct_requirements("requirements-dev.in")
        build_names = {requirement_name(item) for item in build_inputs}
        dev_names = {requirement_name(item) for item in dev_inputs}

        self.assertEqual(build_names, {"setuptools", "wheel"})
        self.assertEqual(dev_names, {"colorama", "pytest", "ruff"})
        self.assertEqual(
            {item for item in dev_inputs if item.startswith("ruff")},
            {"ruff==0.12.12"},
        )
        self.assertGreaterEqual(locked_names("requirements.lock"), runtime_names | build_names)
        self.assertGreaterEqual(
            locked_names("requirements-dev.lock"),
            runtime_names | build_names | dev_names,
        )

    @unittest.skipIf(shutil.which("bash") is None, "dependency lock script requires bash")
    def test_python_dependency_lock_script_requires_explicit_upgrade(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        script = repo_root / "scripts" / "lock-python-dependencies.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = pathlib.Path(tmpdir)
            call_log = fake_bin / "uv-calls.log"
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"${1:-}\" == \"--version\" ]]; then\n"
                "  printf 'uv 0.8.14\\n'\n"
                "  exit 0\n"
                "fi\n"
                "{\n"
                "  for argument in \"$@\"; do printf '<%s>' \"$argument\"; done\n"
                "  printf '\\n'\n"
                "} >> \"$UV_CALL_LOG\"\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]),
                "UV_CALL_LOG": str(call_log),
            }

            default_result = subprocess.run(
                ["bash", str(script)],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(default_result.returncode, 0, default_result.stderr)
            default_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(default_calls), 2)
            self.assertTrue(all("<--upgrade>" not in call for call in default_calls))
            self.assertTrue(
                all(
                    "<--custom-compile-command><bash scripts/lock-python-dependencies.sh>"
                    in call
                    for call in default_calls
                )
            )

            call_log.unlink()
            upgrade_result = subprocess.run(
                ["bash", str(script), "--upgrade"],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(upgrade_result.returncode, 0, upgrade_result.stderr)
            upgrade_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(upgrade_calls), 2)
            self.assertTrue(all("<--upgrade>" in call for call in upgrade_calls))

            invalid_result = subprocess.run(
                ["bash", str(script), "--unexpected"],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid_result.returncode, 2)
            self.assertIn("[--upgrade]", invalid_result.stderr)

if __name__ == "__main__":
    unittest.main()
