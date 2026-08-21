#!/usr/bin/env python3
"""Run only reviewed Focus capability verification commands."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence

try:
    from scripts import focus_capabilities
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    import focus_capabilities  # type: ignore[no-redef]


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUNNER_ORDER = ("pytest", "vitest")
_GUARD_ORDER = (
    "import-cycles",
    "dependency-direction",
    "web-dependency-direction",
    "source-context",
    "web-wire",
    "web-typecheck",
)
_NODE_EXECUTABLE = "node.exe" if sys.platform == "win32" else "node"

if frozenset(_RUNNER_ORDER) != focus_capabilities.ALLOWED_RUNNERS:
    raise RuntimeError("runner command whitelist does not match the catalog schema")
if frozenset(_GUARD_ORDER) != focus_capabilities.ALLOWED_GUARDS:
    raise RuntimeError("guard command whitelist does not match the catalog schema")


class VerificationError(ValueError):
    """Raised when a requested verification is outside the reviewed catalog."""


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    label: str
    argv: tuple[str, ...]
    cwd: pathlib.Path


def _runner_command(
    runner: str,
    targets: tuple[str, ...],
    *,
    python_executable: str,
    repo_root: pathlib.Path,
) -> VerificationCommand:
    if runner == "pytest":
        return VerificationCommand(
            label="runner:pytest",
            argv=(python_executable, "-m", "pytest", "-q", *targets),
            cwd=repo_root,
        )
    if runner == "vitest":
        web_targets = tuple(
            pathlib.PurePosixPath(target).relative_to("web").as_posix()
            for target in targets
        )
        return VerificationCommand(
            label="runner:vitest",
            argv=(
                _NODE_EXECUTABLE,
                "node_modules/vitest/vitest.mjs",
                "run",
                *web_targets,
            ),
            cwd=repo_root / "web",
        )
    raise VerificationError(f"runner is not whitelisted: {runner!r}")


def _guard_command(
    guard: str, *, python_executable: str, repo_root: pathlib.Path
) -> VerificationCommand:
    specifications: dict[str, tuple[pathlib.Path, tuple[str, ...]]] = {
        "import-cycles": (
            repo_root,
            (python_executable, "scripts/check_import_cycles.py"),
        ),
        "dependency-direction": (
            repo_root,
            (python_executable, "scripts/check_dependency_direction.py"),
        ),
        "source-context": (
            repo_root,
            (python_executable, "scripts/check_source_context.py"),
        ),
        "web-dependency-direction": (
            repo_root / "web",
            (_NODE_EXECUTABLE, "scripts/check-focus-dependency-direction.mjs"),
        ),
        "web-wire": (
            repo_root,
            (python_executable, "scripts/generate_focus_web_wire.py", "--check"),
        ),
        "web-typecheck": (
            repo_root / "web",
            (
                _NODE_EXECUTABLE,
                "node_modules/vue-tsc/bin/vue-tsc.js",
                "--noEmit",
            ),
        ),
    }
    try:
        cwd, argv = specifications[guard]
    except KeyError as exc:
        raise VerificationError(f"guard is not whitelisted: {guard!r}") from exc
    return VerificationCommand(label=f"guard:{guard}", argv=argv, cwd=cwd)


def build_commands(
    catalog: focus_capabilities.CapabilityCatalog,
    capability_names: Sequence[str],
    *,
    python_executable: str,
    repo_root: pathlib.Path = REPO_ROOT,
) -> tuple[VerificationCommand, ...]:
    """Build a stable argv-only plan from reviewed runner and guard aliases."""

    if not capability_names:
        raise VerificationError("at least one capability is required")
    if not python_executable or "\0" in python_executable:
        raise VerificationError("--python must be one nonempty executable path")
    selected = tuple(catalog.require(name) for name in sorted(set(capability_names)))
    targets: dict[str, set[str]] = {runner: set() for runner in _RUNNER_ORDER}
    guards: set[str] = set()
    for capability in selected:
        for reference in (*capability.focused_tests, *capability.sentinels):
            if reference.runner not in targets:
                raise VerificationError(
                    f"runner is not whitelisted: {reference.runner!r}"
                )
            targets[reference.runner].update(reference.targets)
        guards.update(capability.guards)

    unknown_guards = guards - set(_GUARD_ORDER)
    if unknown_guards:
        raise VerificationError(f"guards are not whitelisted: {sorted(unknown_guards)}")
    root = pathlib.Path(repo_root).resolve()
    commands = [
        _runner_command(
            runner,
            tuple(sorted(targets[runner])),
            python_executable=python_executable,
            repo_root=root,
        )
        for runner in _RUNNER_ORDER
        if targets[runner]
    ]
    commands.extend(
        _guard_command(
            guard, python_executable=python_executable, repo_root=root
        )
        for guard in _GUARD_ORDER
        if guard in guards
    )
    return tuple(commands)


def command_payload(
    command: VerificationCommand, *, repo_root: pathlib.Path = REPO_ROOT
) -> dict[str, Any]:
    root = pathlib.Path(repo_root).resolve()
    cwd = command.cwd.resolve()
    if cwd == root:
        display_cwd = "."
    elif cwd.is_relative_to(root):
        display_cwd = cwd.relative_to(root).as_posix()
    else:
        display_cwd = str(cwd)
    return {"label": command.label, "argv": list(command.argv), "cwd": display_cwd}


def run_commands(
    commands: Sequence[VerificationCommand],
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> int:
    """Execute fixed argv vectors in order and stop at the first failure."""

    execute = subprocess.run if run is None else run
    for command in commands:
        print(json.dumps(command_payload(command), sort_keys=True), flush=True)
        try:
            completed = execute(list(command.argv), cwd=command.cwd, check=False)
        except OSError as exc:
            print(f"Focus verification could not start {command.label}: {exc}", file=sys.stderr)
            return 2
        if completed.returncode:
            print(
                f"Focus verification failed at {command.label} "
                f"with exit code {completed.returncode}.",
                file=sys.stderr,
            )
            return int(completed.returncode) if completed.returncode > 0 else 1
    return 0


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run reviewed focused tests, sentinels, and guards."
    )
    parser.add_argument("capability", nargs="+", help="Reviewed capability id(s).")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Exact Python executable used for pytest and Python guards.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the argv/cwd plan as JSON without executing it.",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        catalog = focus_capabilities.load_catalog()
        commands = build_commands(
            catalog,
            args.capability,
            python_executable=args.python,
        )
    except (focus_capabilities.CapabilityMapError, VerificationError) as exc:
        print(f"Focus verification plan is invalid: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {"commands": [command_payload(item) for item in commands]},
                sort_keys=True,
            )
        )
        return 0
    return run_commands(commands)


if __name__ == "__main__":
    raise SystemExit(main())
