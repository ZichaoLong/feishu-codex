#!/usr/bin/env python3
"""Reject non-portable Codex upstream references in tracked repository surfaces."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]

_NEGATIVE_SYNTAX_TEST_PATH = (
    "tests/repo_navigation/test_portable_upstream_references.py"
)
_UPSTREAM_NAME = "co" + "dex"
_GITHUB_PREFIX = "https://github.com/openai/" + _UPSTREAM_NAME
_FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_PATH_COMPONENT = r"[^\\/\s`\"'<>()\[\]{},;:]+"
_CHECKOUT_DIRECTORY = r"(?:openai[-_])?" + _UPSTREAM_NAME + r"(?:\.git)?"
_PATH_END = r"(?=$|[\\/\s`\"'<>()\[\]{},;:#?])"
_POSIX_CHECKOUT_ROOT = (
    r"(?:home/" + _PATH_COMPONENT + r"|Users/" + _PATH_COMPONENT
    + r"|root|workspaces?|work|workspace|opt|srv|mnt|tmp|var/tmp|data|code|src|repos?)"
)
_MACHINE_CHECKOUT_RES = (
    re.compile(
        r"(?<![A-Za-z0-9_:/\.\-~$}%])(?P<value>/"
        + _POSIX_CHECKOUT_ROOT
        + r"(?:/"
        + _PATH_COMPONENT
        + r")*/"
        + _CHECKOUT_DIRECTORY
        + r")"
        + _PATH_END,
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.-])(?P<value>"
        r"(?:~|\$HOME|\$\{HOME\}|\$USERPROFILE|\$\{USERPROFILE\}|%USERPROFILE%)"
        r"(?:[\\/]"
        + _PATH_COMPONENT
        + r")*[\\/]"
        + _CHECKOUT_DIRECTORY
        + r")"
        + _PATH_END,
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_.-])(?P<value>(?:\.\.[\\/])+(?:"
        + _PATH_COMPONENT
        + r"[\\/])*"
        + _CHECKOUT_DIRECTORY
        + r")"
        + _PATH_END,
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?P<value>[A-Za-z]:[\\/](?:"
        + _PATH_COMPONENT
        + r"[\\/])*"
        + _CHECKOUT_DIRECTORY
        + r")"
        + _PATH_END,
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\\A-Za-z0-9])(?P<value>\\\\(?:"
        + _PATH_COMPONENT
        + r"[\\/])+"
        + _CHECKOUT_DIRECTORY
        + r")"
        + _PATH_END,
        re.IGNORECASE,
    ),
)
_UPSTREAM_LABEL_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<value>(?:openai/)?"
    + _UPSTREAM_NAME
    + r"@(?P<ref>[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9_-])?)"
    + r"(?P<path>:[^\s;,\])}]+)?)"
    + r"(?=$|[\s`\"'<>()\[\]{},;.!?])",
    re.IGNORECASE,
)
_PERMALINK_RE = re.compile(
    re.escape(_GITHUB_PREFIX)
    + r"/(?P<kind>blob|commit|tree)/(?P<ref>[^/\s#?)]+)"
)


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    kind: str
    value: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.value}"


def is_scanned_surface(path: PurePosixPath) -> bool:
    """Return whether a tracked path must be checked for host dependencies."""

    return path.as_posix() != _NEGATIVE_SYNTAX_TEST_PATH


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_violations(path: str, text: str) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for pattern in _MACHINE_CHECKOUT_RES:
        for match in pattern.finditer(text):
            violations.append(
                Violation(
                    path=path,
                    line=_line_number(text, match.start()),
                    kind="machine-local upstream checkout",
                    value=match.group("value"),
                )
            )

    for match in _UPSTREAM_LABEL_RE.finditer(text):
        reference = match.group("ref")
        shorthand_path = match.group("path")
        if shorthand_path is None and _FULL_COMMIT_RE.fullmatch(reference):
            continue
        violations.append(
            Violation(
                path=path,
                line=_line_number(text, match.start()),
                kind=(
                    "non-portable upstream shorthand"
                    if shorthand_path is not None
                    else "unpinned upstream label"
                ),
                value=match.group("value"),
            )
        )

    for match in _PERMALINK_RE.finditer(text):
        reference = match.group("ref")
        if _FULL_COMMIT_RE.fullmatch(reference) is None:
            violations.append(
                Violation(
                    path=path,
                    line=_line_number(text, match.start()),
                    kind="unpinned upstream permalink",
                    value=match.group(0),
                )
            )

    return tuple(violations)


def _tracked_paths(repo_root: Path) -> tuple[PurePosixPath, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return tuple(
        PurePosixPath(value.decode("utf-8", errors="surrogateescape"))
        for value in result.stdout.split(b"\0")
        if value
    )


def repository_violations(repo_root: Path = REPO_ROOT) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    for relative_path in _tracked_paths(repo_root):
        if not is_scanned_surface(relative_path):
            continue
        path = repo_root / relative_path
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(find_violations(relative_path.as_posix(), text))
    return tuple(
        sorted(
            violations,
            key=lambda item: (item.path, item.line, item.kind, item.value),
        )
    )


def main() -> int:
    violations = repository_violations()
    for violation in violations:
        print(f"portable-upstream-reference: {violation.render()}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
