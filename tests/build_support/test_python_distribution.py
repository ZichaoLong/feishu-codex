from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.build_support import python_distribution
from scripts.build_support.python_distribution import (
    PythonDistributionBuildError,
    build_validated_wheel,
    isolated_setuptools_build,
    validate_wheel_bot_payload,
)


class PythonDistributionBuildTests(unittest.TestCase):
    @staticmethod
    def _write_minimal_project(source: pathlib.Path) -> None:
        bot = source / "bot"
        bot.mkdir(parents=True)
        (bot / "__init__.py").write_text("VALUE = 'live'\n", encoding="utf-8")
        (bot / "worker.py").write_text("def run():\n    return 'live'\n", encoding="utf-8")
        (bot / "payload.txt").write_text("current payload\n", encoding="utf-8")
        focus_runtime = bot / "focus_runtime"
        focus_runtime.mkdir()
        (focus_runtime / "__init__.py").write_text("", encoding="utf-8")
        (focus_runtime / "runtime.py").write_text(
            "class FocusRuntime:\n    pass\n",
            encoding="utf-8",
        )
        (source / "pyproject.toml").write_text(
            "[build-system]\n"
            "requires = [\"setuptools>=68\", \"wheel\"]\n"
            "build-backend = \"setuptools.build_meta\"\n"
            "\n"
            "[project]\n"
            "name = \"focus-build-sentinel\"\n"
            "version = \"1.0.0\"\n"
            "\n"
            "[tool.setuptools.packages.find]\n"
            "include = [\"bot*\"]\n"
            "\n"
            "[tool.setuptools.package-data]\n"
            "bot = [\"payload.txt\"]\n",
            encoding="utf-8",
        )
        (source / "setup.cfg").write_text(
            "[build]\n"
            "build_lib = build/lib\n",
            encoding="utf-8",
        )

    def test_isolated_build_overrides_only_child_extra_config(self) -> None:
        with isolated_setuptools_build(
            base_environment={
                "DIST_EXTRA_CONFIG": "old.cfg",
                "FOCUS_SENTINEL": "kept",
                "SOURCE_DATE_EPOCH": "1700000000",
            }
        ) as isolated:
            root = isolated.root
            config_path = pathlib.Path(isolated.environment["DIST_EXTRA_CONFIG"])
            config_text = config_path.read_text(encoding="utf-8")
            self.assertEqual(isolated.environment["FOCUS_SENTINEL"], "kept")
            self.assertNotEqual(str(config_path), "old.cfg")
            self.assertEqual(
                isolated.environment["SOURCE_DATE_EPOCH"],
                python_distribution._REPRODUCIBLE_WHEEL_EPOCH,
            )
            self.assertIn(f"build_base = {isolated.build_base}", config_text)
            self.assertIn(f"egg_base = {isolated.egg_base}", config_text)
            self.assertTrue(isolated.egg_base.is_dir())

        self.assertFalse(root.exists())

    def test_distutils_config_escapes_percent_in_temporary_paths(self) -> None:
        path = pathlib.Path("profile%name") / "build"
        self.assertEqual(
            python_distribution._distutils_config_value(path),
            str(path).replace("%", "%%"),
        )

    def test_real_build_ignores_checkout_build_and_egg_info_ghosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = root / "source"
            output = root / "wheel"
            self._write_minimal_project(source)
            checkout_ghost = source / "build" / "lib" / "bot" / "focus_runtime.py"
            checkout_ghost.parent.mkdir(parents=True)
            checkout_ghost.write_text("GHOST = True\n", encoding="utf-8")
            stale_manifest = source / "focus_build_sentinel.egg-info" / "SOURCES.txt"
            stale_manifest.parent.mkdir()
            stale_manifest.write_text("bot/focus_runtime.py\n", encoding="utf-8")

            observed_commands: list[list[str]] = []
            real_run = python_distribution.subprocess.run

            def observed_run(command, **kwargs):
                observed_commands.append(list(command))
                return real_run(command, **kwargs)

            with patch.object(
                python_distribution.subprocess,
                "run",
                side_effect=observed_run,
            ):
                wheel_path = build_validated_wheel(
                    source_dir=source,
                    output_dir=output,
                    python_executable=sys.executable,
                )

            with zipfile.ZipFile(wheel_path) as wheel:
                names = set(wheel.namelist())
                self.assertIn("bot/worker.py", names)
                self.assertIn("bot/payload.txt", names)
                self.assertIn("bot/focus_runtime/__init__.py", names)
                self.assertIn("bot/focus_runtime/runtime.py", names)
                self.assertNotIn("bot/focus_runtime.py", names)
            self.assertEqual(checkout_ghost.read_text(encoding="utf-8"), "GHOST = True\n")
            self.assertEqual(
                {path.name for path in checkout_ghost.parent.iterdir()},
                {"focus_runtime.py"},
            )
            self.assertEqual(
                stale_manifest.read_text(encoding="utf-8"),
                "bot/focus_runtime.py\n",
            )
            self.assertEqual(
                tuple(source.glob("*.egg-info")),
                (stale_manifest.parent,),
            )
            self.assertIn(
                "--config-settings=--global-option=--no-user-cfg",
                observed_commands[0],
            )

    def test_real_build_is_byte_reproducible_across_caller_epochs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = root / "source"
            self._write_minimal_project(source)

            with patch.dict(
                python_distribution.os.environ,
                {"SOURCE_DATE_EPOCH": "1700000000"},
            ):
                first = build_validated_wheel(
                    source_dir=source,
                    output_dir=root / "first",
                    python_executable=sys.executable,
                )
            with patch.dict(
                python_distribution.os.environ,
                {"SOURCE_DATE_EPOCH": "1800000000"},
            ):
                second = build_validated_wheel(
                    source_dir=source,
                    output_dir=root / "second",
                    python_executable=sys.executable,
                )

            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_payload_validator_rejects_wheel_only_bot_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = root / "source"
            bot = source / "bot"
            bot.mkdir(parents=True)
            (bot / "__init__.py").write_text("", encoding="utf-8")
            manifest = root / "SOURCES.txt"
            manifest.write_text("bot/__init__.py\n", encoding="utf-8")
            wheel = root / "focus.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("bot/__init__.py", b"")
                archive.writestr("bot/ghost.py", b"GHOST = True\n")
                archive.writestr("focus-1.0.dist-info/METADATA", b"Name: focus\n")

            with self.assertRaises(PythonDistributionBuildError) as raised:
                validate_wheel_bot_payload(
                    wheel,
                    source_dir=source,
                    source_manifest=manifest,
                )

        self.assertIn("wheel-only=bot/ghost.py", str(raised.exception))

    def test_payload_validator_rejects_unowned_top_level_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            source = root / "source"
            bot = source / "bot"
            bot.mkdir(parents=True)
            (bot / "__init__.py").write_text("", encoding="utf-8")
            manifest = root / "SOURCES.txt"
            manifest.write_text("bot/__init__.py\n", encoding="utf-8")
            wheel = root / "focus.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("bot/__init__.py", b"")
                archive.writestr("ghost.py", b"GHOST = True\n")
                archive.writestr("focus-1.0.dist-info/METADATA", b"Name: focus\n")

            with self.assertRaises(PythonDistributionBuildError) as raised:
                validate_wheel_bot_payload(
                    wheel,
                    source_dir=source,
                    source_manifest=manifest,
                )

        self.assertIn("未声明的顶层payload", str(raised.exception))
