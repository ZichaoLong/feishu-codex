from __future__ import annotations

import argparse
import contextlib
import errno
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import install


class _FakeManagedInstallTransaction:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.restored_instances: tuple[str, ...] = ()

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        del exc, traceback
        self.events.append("complete" if exc_type is None else "abort")
        return False


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.install_transaction = _FakeManagedInstallTransaction()
        self.transaction_patcher = patch(
            "install._managed_install_transaction",
            return_value=self.install_transaction,
        )
        self.transaction_factory = self.transaction_patcher.start()
        self.addCleanup(self.transaction_patcher.stop)

    @staticmethod
    def _write_fake_python(path: pathlib.Path, *, supported: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"-c\" ]; then\n"
            f"  exit {0 if supported else 1}\n"
            "fi\n"
            "printf '%s\\n' \"$0\" > \"$FOCUS_TEST_SELECTED_PYTHON\"\n"
            "printf '%s\\n' \"$@\" > \"$FOCUS_TEST_INSTALL_ARGS\"\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    @staticmethod
    def _bundle(root: pathlib.Path, *, channel: str = "local") -> SimpleNamespace:
        wheel = root / "focus-4.0.0.dev0-py3-none-any.whl"
        lock = root / "requirements.lock"
        wheel.write_bytes(b"wheel")
        lock.write_text("aiohttp==3.14.3\n", encoding="utf-8")
        return SimpleNamespace(
            wheel_path=wheel,
            dependency_lock_path=lock,
            metadata=SimpleNamespace(
                version="4.0.0.dev0",
                channel=channel,
                build_id="build-1",
            ),
        )

    @staticmethod
    @contextlib.contextmanager
    def _yield_bundle(bundle):
        yield bundle

    def test_parse_args_defaults_to_stable_and_keeps_sources_exclusive(self) -> None:
        self.assertEqual(install._parse_args([]).channel, "stable")
        artifact = install._parse_args(["--artifact", "focus.zip"])
        self.assertIsNone(artifact.channel)
        self.assertEqual(artifact.artifact, pathlib.Path("focus.zip"))
        self.assertEqual(
            install._parse_args(["--channel", "development"]).channel,
            "development",
        )
        with self.assertRaises(SystemExit):
            install._parse_args(
                ["--channel", "stable", "--artifact", "focus.zip"]
            )

    def test_help_explains_bundle_channels_and_network_boundary(self) -> None:
        with patch("argparse.ArgumentParser._print_message") as output:
            with self.assertRaises(SystemExit) as raised:
                install._parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        rendered = "".join(call.args[0] for call in output.call_args_list if call.args)
        self.assertIn("--channel {stable,development}", rendered)
        self.assertIn("--artifact PATH", rendered)
        self.assertIn("Focus wheel（含 Web）", rendered)
        self.assertIn("pip 仍可能", rendered)
        self.assertIn("HTTP_PROXY", rendered)
        self.assertIn("每次重建 Focus 专用 .venv", rendered)
        self.assertIn("target/prefix/root/user", rendered)
        self.assertIn("build_install_bundle.py", rendered)

    def test_ensure_supported_python_rejects_non_cpython(self) -> None:
        with patch.object(install.sys, "implementation", SimpleNamespace(name="pypy")):
            with self.assertRaises(SystemExit) as raised:
                install._ensure_supported_python()
        self.assertIn("CPython 3.11", str(raised.exception))

    def test_ensure_supported_python_rejects_old_cpython(self) -> None:
        with patch.object(install.sys, "version_info", (3, 10)):
            with self.assertRaises(SystemExit) as raised:
                install._ensure_supported_python()
        self.assertIn("CPython 3.11", str(raised.exception))

    @unittest.skipIf(
        os.name == "nt" or shutil.which("bash") is None,
        "install.sh requires Unix bash",
    )
    def test_install_sh_honors_python_and_forwards_artifact(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            selected = root / "selected.txt"
            recorded_args = root / "args.txt"
            explicit_python = root / "custom python"
            artifact = root / "focus bundle.zip"
            self._write_fake_python(explicit_python, supported=True)
            environment = {
                **os.environ,
                "FOCUS_INSTALL_PYTHON": str(explicit_python),
                "FOCUS_TEST_SELECTED_PYTHON": str(selected),
                "FOCUS_TEST_INSTALL_ARGS": str(recorded_args),
            }

            result = subprocess.run(
                [
                    "bash",
                    str(repo_root / "install.sh"),
                    "--artifact",
                    str(artifact),
                    "--migrate-from-feishu-codex",
                ],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                pathlib.Path(selected.read_text(encoding="utf-8").strip()),
                explicit_python,
            )
            self.assertEqual(
                recorded_args.read_text(encoding="utf-8").splitlines(),
                [
                    str(repo_root / "install.py"),
                    "--artifact",
                    str(artifact),
                    "--migrate-from-feishu-codex",
                ],
            )

    @unittest.skipIf(
        os.name == "nt" or shutil.which("bash") is None,
        "install.sh requires Unix bash",
    )
    def test_install_sh_discovers_version_named_python_beyond_fixed_candidates(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            fake_bin = root / "bin"
            selected = root / "selected.txt"
            recorded_args = root / "args.txt"
            for name in (
                "python3.14",
                "python3.13",
                "python3.12",
                "python3.11",
                "python3",
                "python",
            ):
                self._write_fake_python(fake_bin / name, supported=False)
            discovered_python = fake_bin / "python3.27"
            self._write_fake_python(discovered_python, supported=True)
            environment = {
                **os.environ,
                "PATH": os.pathsep.join([str(fake_bin), os.environ.get("PATH", "")]),
                "FOCUS_INSTALL_PYTHON": "",
                "FOCUS_TEST_SELECTED_PYTHON": str(selected),
                "FOCUS_TEST_INSTALL_ARGS": str(recorded_args),
            }

            result = subprocess.run(
                ["bash", str(repo_root / "install.sh")],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                pathlib.Path(selected.read_text(encoding="utf-8").strip()),
                discovered_python,
            )

    @unittest.skipIf(
        os.name == "nt" or shutil.which("bash") is None,
        "install.sh requires Unix bash",
    )
    def test_install_sh_rejects_invalid_explicit_python_without_fallback(self) -> None:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            explicit_python = root / "python-old"
            self._write_fake_python(explicit_python, supported=False)
            environment = {
                **os.environ,
                "FOCUS_INSTALL_PYTHON": str(explicit_python),
                "FOCUS_TEST_SELECTED_PYTHON": str(root / "selected.txt"),
                "FOCUS_TEST_INSTALL_ARGS": str(root / "args.txt"),
            }
            result = subprocess.run(
                ["bash", str(repo_root / "install.sh")],
                cwd=repo_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FOCUS_INSTALL_PYTHON", result.stderr)
        self.assertIn("CPython 3.11+", result.stderr)

    def test_recreate_venv_explains_ensurepip_bootstrap_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_dir = pathlib.Path(tmpdir) / "data" / ".venv"
            failure = subprocess.CalledProcessError(1, ["python", "-m", "ensurepip"])
            with patch("install.venv.EnvBuilder.create", side_effect=failure):
                with self.assertRaises(SystemExit) as raised:
                    install._recreate_venv(venv_dir)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertIn("venv/ensurepip", str(raised.exception))
        self.assertIn("Debian/Ubuntu", str(raised.exception))
        self.assertIn("python3-venv", str(raised.exception))

    def test_recreate_venv_does_not_relabel_filesystem_failures(self) -> None:
        for failure in (
            PermissionError(errno.EACCES, "permission denied"),
            OSError(errno.ENOSPC, "no space left on device"),
        ):
            with self.subTest(errno=failure.errno):
                with tempfile.TemporaryDirectory() as tmpdir:
                    venv_dir = pathlib.Path(tmpdir) / "data" / ".venv"
                    with patch("install.venv.EnvBuilder.create", side_effect=failure):
                        with self.assertRaises(type(failure)) as raised:
                            install._recreate_venv(venv_dir)
                self.assertIs(raised.exception, failure)

    def test_recreate_venv_explicitly_disables_system_site_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            venv_dir = pathlib.Path(tmpdir) / "data" / ".venv"
            with patch("install.venv.EnvBuilder") as builder:
                install._recreate_venv(venv_dir)

        builder.assert_called_once_with(with_pip=True, system_site_packages=False)
        builder.return_value.create.assert_called_once_with(venv_dir)

    def test_venv_python_probe_requires_cpython_311_or_newer(self) -> None:
        venv_dir = pathlib.Path("/tmp/focus-venv")
        expected_python = install._venv_python_path(venv_dir)
        with patch(
            "install.subprocess.run",
            return_value=subprocess.CompletedProcess([str(expected_python)], 0),
        ) as run:
            self.assertTrue(install._venv_uses_supported_python(venv_dir))
        command = run.call_args.args[0]
        self.assertEqual(command[:3], [str(expected_python), "-I", "-c"])
        self.assertIn("sys.implementation.name == 'cpython'", command[3])
        self.assertIn("sys.version_info >= (3, 11)", command[3])

        for failure in (
            subprocess.CompletedProcess([str(expected_python)], 1),
            OSError("cannot execute"),
            subprocess.TimeoutExpired([str(expected_python)], 10),
        ):
            if isinstance(failure, subprocess.CompletedProcess):
                context = patch("install.subprocess.run", return_value=failure)
            else:
                context = patch("install.subprocess.run", side_effect=failure)
            with context:
                self.assertFalse(install._venv_uses_supported_python(venv_dir))

    def test_run_pip_install_fails_closed_without_adding_index(self) -> None:
        venv_python = pathlib.Path("/tmp/focus-venv/bin/python")
        failure = subprocess.CalledProcessError(1, ["pip"])
        with patch("install.subprocess.run", side_effect=failure) as run:
            with self.assertRaises(SystemExit) as raised:
                install._run_pip_install(venv_python, "focus.whl")
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [str(venv_python), "-I", "-m", "pip"])
        self.assertNotIn("--extra-index-url", command)
        self.assertIn("不会在失败后静默追加", str(raised.exception))

    def test_python_install_subprocess_env_removes_import_injection_only(self) -> None:
        environment = {
            "PYTHONPATH": "/opt/ascend/python",
            "PythonHome": "/opt/python",
            "PIP_INDEX_URL": "https://packages.example/simple",
            "HTTPS_PROXY": "http://proxy.example:8080",
            "FOCUS_TEST_VALUE": "preserved",
        }
        with patch.dict(install.os.environ, environment, clear=True):
            sanitized = install._python_install_subprocess_env()

        self.assertNotIn("PYTHONPATH", sanitized)
        self.assertNotIn("PythonHome", sanitized)
        self.assertEqual(
            sanitized,
            {
                "PIP_INDEX_URL": "https://packages.example/simple",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "FOCUS_TEST_VALUE": "preserved",
            },
        )

    def test_pip_destination_config_is_rejected_before_install(self) -> None:
        venv_python = pathlib.Path("/tmp/focus-venv/bin/python")
        result = subprocess.CompletedProcess(
            [str(venv_python)],
            0,
            stdout=(
                ":env:.index-url='https://packages.example/simple'\n"
                "global.target='/tmp/external'\n"
                "install.user='true'\n"
                "global.root-user-action='ignore'\n"
            ),
            stderr="",
        )
        with patch("install.subprocess.run", return_value=result) as run:
            with self.assertRaises(SystemExit) as raised:
                install._reject_pip_destination_overrides(venv_python)

        self.assertEqual(
            run.call_args.args[0],
            [str(venv_python), "-I", "-m", "pip", "config", "list"],
        )
        self.assertIn("target, user", str(raised.exception))
        self.assertNotIn("/tmp/external", str(raised.exception))

    def test_disabled_pip_user_config_does_not_block_install(self) -> None:
        venv_python = pathlib.Path("/tmp/focus-venv/bin/python")
        result = subprocess.CompletedProcess(
            [str(venv_python)],
            0,
            stdout="install.user='false'\nglobal.target=''\n",
            stderr="",
        )
        with patch("install.subprocess.run", return_value=result):
            install._reject_pip_destination_overrides(venv_python)

    def test_pip_check_uses_isolated_interpreter_and_sanitized_environment(self) -> None:
        venv_python = pathlib.Path("/tmp/focus-venv/bin/python")
        with patch.dict(
            install.os.environ,
            {"PYTHONPATH": "/opt/ascend/python", "HTTPS_PROXY": "proxy"},
            clear=True,
        ):
            with patch("install.subprocess.run") as run:
                install._run_pip_check(venv_python)

        self.assertEqual(
            run.call_args.args[0],
            [str(venv_python), "-I", "-m", "pip", "check"],
        )
        self.assertEqual(run.call_args.kwargs["env"], {"HTTPS_PROXY": "proxy"})

    def test_release_for_channel_uses_distinct_release_authorities(self) -> None:
        stable = {
            "draft": False,
            "prerelease": False,
            "tag_name": "4.0.0",
            "assets": [],
        }
        development = {
            "draft": False,
            "prerelease": True,
            "tag_name": install._DEVELOPMENT_RELEASE_TAG,
            "assets": [],
        }
        with patch(
            "install._download_github_json",
            side_effect=(stable, development),
        ) as download:
            self.assertIs(install._release_for_channel("stable"), stable)
            self.assertIs(install._release_for_channel("development"), development)
        self.assertTrue(download.call_args_list[0].args[0].endswith("/releases/latest"))
        self.assertTrue(
            download.call_args_list[1].args[0].endswith(
                "/releases/tags/development-builds"
            )
        )

    def test_release_for_channel_rejects_cross_channel_release(self) -> None:
        with patch(
            "install._download_github_json",
            return_value={
                "draft": False,
                "prerelease": True,
                "tag_name": "development-builds",
            },
        ):
            with self.assertRaisesRegex(SystemExit, "stable channel"):
                install._release_for_channel("stable")

    def test_asset_url_is_confined_to_repository_release_downloads(self) -> None:
        asset = {
            "name": "focus.zip",
            "size": 12,
            "browser_download_url": (
                "https://github.com/ZichaoLong/focus/releases/download/4.0/focus.zip"
            ),
        }
        self.assertEqual(
            install._asset_download_url(asset, expected_name="focus.zip"),
            (asset["browser_download_url"], 12),
        )
        asset["browser_download_url"] = "https://example.com/focus.zip"
        with self.assertRaisesRegex(SystemExit, "不受信任"):
            install._asset_download_url(asset, expected_name="focus.zip")

    def test_artifact_failure_happens_before_managed_transaction(self) -> None:
        @contextlib.contextmanager
        def fail_resolution(_args: argparse.Namespace):
            raise SystemExit("bundle hash mismatch")
            yield

        with patch("install._ensure_supported_python"):
            with patch("install._resolved_install_bundle", side_effect=fail_resolution):
                with patch("install._recreate_venv") as recreate:
                    with self.assertRaisesRegex(SystemExit, "hash mismatch"):
                        install.main(["--artifact", "broken.zip"])
        self.transaction_factory.assert_not_called()
        recreate.assert_not_called()

    def test_main_installs_validated_bundle_in_one_managed_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            bundle = self._bundle(root)
            data_root = root / "data"
            venv_dir = data_root / ".venv"
            venv_python = install._venv_python_path(venv_dir)
            pip_calls: list[tuple[pathlib.Path, tuple[str, ...]]] = []
            checked_calls: list[list[str]] = []
            install_steps: list[str] = []
            venv_dir.mkdir(parents=True)
            stale_payload = venv_dir / "foreign-package.pth"
            stale_payload.write_text("/opt/ascend/python\n", encoding="utf-8")

            def recreate(target: pathlib.Path) -> None:
                install_steps.append("recreate")
                shutil.rmtree(target)
                target.mkdir(parents=True)
                (target / "pyvenv.cfg").write_text("home = /python\n", encoding="utf-8")
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.write_text("", encoding="utf-8")

            def record_pip_install(
                python: pathlib.Path, *args: str
            ) -> None:
                install_steps.append("pip-install")
                pip_calls.append((python, tuple(args)))

            def record_pip_check(_python: pathlib.Path) -> None:
                install_steps.append("pip-check")

            def record_checked(command: list[str]) -> None:
                install_steps.append("bootstrap")
                checked_calls.append(list(command))

            with patch("install._ensure_supported_python"):
                with patch(
                    "install._resolved_install_bundle",
                    side_effect=lambda _args: self._yield_bundle(bundle),
                ):
                    with patch("bot.platform_paths.default_data_root", return_value=data_root):
                        with patch("install._recreate_venv", side_effect=recreate):
                            with patch("install._venv_uses_supported_python", return_value=True):
                                with patch("install._venv_has_pip", return_value=True):
                                    with patch("install._reject_pip_destination_overrides"):
                                        with patch(
                                            "install._run_pip_install",
                                            side_effect=record_pip_install,
                                        ):
                                            with patch(
                                                "install._run_pip_check",
                                                side_effect=record_pip_check,
                                            ):
                                                with patch(
                                                    "install._run_checked",
                                                    side_effect=record_checked,
                                                ):
                                                    install.main(["--artifact", "focus.zip"])

        self.assertEqual(
            pip_calls,
            [
                (
                    venv_python,
                    (
                        "--constraint",
                        str(bundle.dependency_lock_path),
                        "--force-reinstall",
                        str(bundle.wheel_path),
                    ),
                )
            ],
        )
        self.assertEqual(
            checked_calls,
            [[str(venv_python), "-I", "-m", "bot.manage_cli", "bootstrap-install"]],
        )
        self.assertEqual(
            install_steps,
            ["recreate", "pip-install", "pip-check", "bootstrap"],
        )
        self.assertFalse(stale_payload.exists())
        self.assertEqual(self.install_transaction.events, ["enter", "complete"])

    def test_main_rejects_rebuilt_venv_with_incompatible_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            bundle = self._bundle(root)
            data_root = root / "data"
            venv_python = install._venv_python_path(data_root / ".venv")

            def recreate(target: pathlib.Path) -> None:
                target.mkdir(parents=True)
                (target / "pyvenv.cfg").write_text(
                    "home = /old-python\n",
                    encoding="utf-8",
                )
                venv_python.parent.mkdir(parents=True, exist_ok=True)
                venv_python.write_text("", encoding="utf-8")

            with patch("install._ensure_supported_python"):
                with patch(
                    "install._resolved_install_bundle",
                    side_effect=lambda _args: self._yield_bundle(bundle),
                ):
                    with patch("bot.platform_paths.default_data_root", return_value=data_root):
                        with patch("install._venv_uses_supported_python", return_value=False):
                            with patch(
                                "install._recreate_venv",
                                side_effect=recreate,
                            ) as recreate_venv:
                                with patch("install._run_pip_install") as pip_install:
                                    with self.assertRaisesRegex(SystemExit, "重建后仍不是"):
                                        install.main(["--artifact", "focus.zip"])
        recreate_venv.assert_called_once_with(data_root / ".venv")
        pip_install.assert_not_called()
        self.assertEqual(self.install_transaction.events, ["enter", "abort"])

    def test_main_migration_uses_installed_package_not_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            bundle = self._bundle(root)
            data_root = root / "data"
            venv_dir = data_root / ".venv"
            venv_python = install._venv_python_path(venv_dir)
            venv_python.parent.mkdir(parents=True)
            (venv_dir / "pyvenv.cfg").write_text("home = /python\n", encoding="utf-8")
            venv_python.write_text("", encoding="utf-8")
            checked_calls: list[list[str]] = []
            with patch("install._ensure_supported_python"):
                with patch(
                    "install._resolved_install_bundle",
                    side_effect=lambda _args: self._yield_bundle(bundle),
                ):
                    with patch("bot.platform_paths.default_data_root", return_value=data_root):
                        with patch("install._venv_uses_supported_python", return_value=True):
                            with patch("install._recreate_venv"):
                                with patch("install._venv_has_pip", return_value=True):
                                    with patch("install._reject_pip_destination_overrides"):
                                        with patch("install._run_pip_install"):
                                            with patch("install._run_pip_check"):
                                                with patch(
                                                    "install._run_checked",
                                                    side_effect=lambda command: checked_calls.append(
                                                        list(command)
                                                    ),
                                                ):
                                                    install.main(
                                                        [
                                                            "--artifact",
                                                            "focus.zip",
                                                            "--migrate-from-feishu-codex",
                                                        ]
                                                    )
        self.assertEqual(
            checked_calls[-1],
            [
                str(venv_python),
                "-I",
                "-m",
                "bot.manage_cli",
                "migrate",
                "from-feishu-codex",
            ],
        )


if __name__ == "__main__":
    unittest.main()
