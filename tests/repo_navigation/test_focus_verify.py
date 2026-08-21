from __future__ import annotations

import contextlib
import io
import pathlib
import subprocess
import unittest

from scripts import focus_capabilities, focus_verify


def _capability(
    name: str,
    *,
    focused: tuple[focus_capabilities.VerificationReference, ...] = (),
    sentinels: tuple[focus_capabilities.VerificationReference, ...] = (),
    guards: tuple[str, ...] = (),
) -> focus_capabilities.Capability:
    return focus_capabilities.Capability(
        name=name,
        entries=(),
        owners=(),
        contracts=(),
        focused_tests=focused,
        sentinels=sentinels,
        guards=guards,
    )


class FocusVerificationTests(unittest.TestCase):
    def test_executable_whitelists_match_catalog_schema(self) -> None:
        self.assertEqual(
            frozenset(focus_verify._RUNNER_ORDER),
            focus_capabilities.ALLOWED_RUNNERS,
        )
        self.assertEqual(
            frozenset(focus_verify._GUARD_ORDER),
            focus_capabilities.ALLOWED_GUARDS,
        )

    def test_plan_deduplicates_targets_and_uses_fixed_runner_guard_order(self) -> None:
        catalog = focus_capabilities.CapabilityCatalog(
            capabilities=(
                _capability(
                    "alpha",
                    focused=(
                        focus_capabilities.VerificationReference(
                            runner="pytest", targets=("tests/test_b.py",)
                        ),
                        focus_capabilities.VerificationReference(
                            runner="vitest",
                            targets=("web/src/focus/b.test.ts",),
                        ),
                    ),
                    sentinels=(
                        focus_capabilities.VerificationReference(
                            runner="pytest", targets=("tests/test_shared.py",)
                        ),
                    ),
                    guards=("source-context", "import-cycles"),
                ),
                _capability(
                    "beta",
                    focused=(
                        focus_capabilities.VerificationReference(
                            runner="pytest",
                            targets=("tests/test_a.py", "tests/test_shared.py"),
                        ),
                    ),
                    sentinels=(
                        focus_capabilities.VerificationReference(
                            runner="vitest", targets=("web/test/a.test.ts",)
                        ),
                    ),
                    guards=(
                        "dependency-direction",
                        "web-dependency-direction",
                        "web-typecheck",
                    ),
                ),
            )
        )

        commands = focus_verify.build_commands(
            catalog,
            ("beta", "alpha", "beta"),
            python_executable="/tmp/Python With Spaces/python",
            repo_root=pathlib.Path("/tmp/focus repo"),
        )

        self.assertEqual(
            tuple(command.label for command in commands),
            (
                "runner:pytest",
                "runner:vitest",
                "guard:import-cycles",
                "guard:dependency-direction",
                "guard:web-dependency-direction",
                "guard:source-context",
                "guard:web-typecheck",
            ),
        )
        self.assertEqual(commands[0].argv[0], "/tmp/Python With Spaces/python")
        self.assertEqual(
            commands[0].argv[4:],
            ("tests/test_a.py", "tests/test_b.py", "tests/test_shared.py"),
        )
        self.assertEqual(
            commands[1].argv,
            (
                focus_verify._NODE_EXECUTABLE,
                "node_modules/vitest/vitest.mjs",
                "run",
                "src/focus/b.test.ts",
                "test/a.test.ts",
            ),
        )
        self.assertEqual(
            commands[-1].argv,
            (
                focus_verify._NODE_EXECUTABLE,
                "node_modules/vue-tsc/bin/vue-tsc.js",
                "--noEmit",
            ),
        )

    def test_every_guard_alias_has_exact_argv_and_cwd(self) -> None:
        catalog = focus_capabilities.CapabilityCatalog(
            capabilities=(
                _capability(
                    "all-guards",
                    guards=tuple(sorted(focus_capabilities.ALLOWED_GUARDS)),
                ),
            )
        )
        root = pathlib.Path("/tmp/focus-root")
        commands = focus_verify.build_commands(
            catalog,
            ("all-guards",),
            python_executable="/exact/python",
            repo_root=root,
        )

        self.assertEqual(
            tuple((item.label, item.argv, item.cwd) for item in commands),
            (
                (
                    "guard:import-cycles",
                    ("/exact/python", "scripts/check_import_cycles.py"),
                    root,
                ),
                (
                    "guard:dependency-direction",
                    ("/exact/python", "scripts/check_dependency_direction.py"),
                    root,
                ),
                (
                    "guard:web-dependency-direction",
                    (
                        focus_verify._NODE_EXECUTABLE,
                        "scripts/check-focus-dependency-direction.mjs",
                    ),
                    root / "web",
                ),
                (
                    "guard:source-context",
                    ("/exact/python", "scripts/check_source_context.py"),
                    root,
                ),
                (
                    "guard:web-wire",
                    ("/exact/python", "scripts/generate_focus_web_wire.py", "--check"),
                    root,
                ),
                (
                    "guard:web-typecheck",
                    (
                        focus_verify._NODE_EXECUTABLE,
                        "node_modules/vue-tsc/bin/vue-tsc.js",
                        "--noEmit",
                    ),
                    root / "web",
                ),
            ),
        )

    def test_unknown_capability_runner_and_guard_fail_before_execution(self) -> None:
        catalog = focus_capabilities.CapabilityCatalog(
            capabilities=(
                _capability(
                    "bad-runner",
                    focused=(
                        focus_capabilities.VerificationReference(
                            runner="python", targets=("anything",)
                        ),
                    ),
                ),
                _capability("bad-guard", guards=("bash-command",)),
            )
        )
        cases = ("missing", "bad-runner", "bad-guard")
        for name in cases:
            with self.subTest(name=name):
                with self.assertRaises(
                    (focus_capabilities.CapabilityMapError, focus_verify.VerificationError)
                ):
                    focus_verify.build_commands(
                        catalog, (name,), python_executable="python"
                    )

    def test_run_uses_argv_without_shell_and_preserves_cwd(self) -> None:
        command = focus_verify.VerificationCommand(
            label="runner:pytest",
            argv=("/tmp/python with spaces", "-m", "pytest", "-q", "tests/test_x.py"),
            cwd=pathlib.Path("/tmp/focus repo"),
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)

        with contextlib.redirect_stdout(io.StringIO()):
            result = focus_verify.run_commands((command,), run=run)

        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0][0], "/tmp/python with spaces")
        self.assertEqual(calls[0][1], {"cwd": command.cwd, "check": False})
        self.assertNotIn("shell", calls[0][1])

    def test_first_child_failure_stops_later_commands(self) -> None:
        commands = (
            focus_verify.VerificationCommand(
                label="first", argv=("first",), cwd=pathlib.Path("/tmp")
            ),
            focus_verify.VerificationCommand(
                label="second", argv=("second",), cwd=pathlib.Path("/tmp")
            ),
        )
        calls: list[list[str]] = []

        def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 7)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = focus_verify.run_commands(commands, run=run)

        self.assertEqual(result, 7)
        self.assertEqual(calls, [["first"]])

    def test_process_start_failure_returns_configuration_error(self) -> None:
        command = focus_verify.VerificationCommand(
            label="guard:web-wire",
            argv=("python", "scripts/generate_focus_web_wire.py", "--check"),
            cwd=pathlib.Path("/tmp"),
        )

        def run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise OSError("missing executable")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = focus_verify.run_commands((command,), run=run)

        self.assertEqual(result, 2)

    def test_real_catalog_dry_run_emits_json_without_execution(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = focus_verify.main(
                ["focus-runtime", "--dry-run", "--python", "/exact/python"]
            )

        self.assertEqual(result, 0)
        self.assertIn('"/exact/python"', output.getvalue())
        self.assertIn('"guard:dependency-direction"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
