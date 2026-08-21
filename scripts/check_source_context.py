#!/usr/bin/env python3
"""Report source-context review signals and require explicit oversized review."""

from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_POLICY_PATH = pathlib.Path(__file__).with_name("source-context-policy.json")
_SOURCE_ROOTS = (
    ".agents",
    "bot",
    "tests",
    "web/public",
    "web/src",
    "web/test",
    "web/scripts",
    "scripts",
)
_TOP_LEVEL_SOURCE_CONTAINERS = ("", "web")
_SOURCE_SUFFIXES = frozenset(
    {
        ".cjs",
        ".css",
        ".cts",
        ".js",
        ".jsx",
        ".less",
        ".mjs",
        ".mts",
        ".ps1",
        ".py",
        ".sass",
        ".scss",
        ".sh",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".vite",
        "__pycache__",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
_EXPECTED_CONFIG_KEYS = frozenset(
    {
        "alignment_threshold_bytes",
        "alignment_threshold_lines",
        "review_threshold_lines",
        "reviewed_sources",
    }
)
_REVIEW_CATEGORIES = frozenset({"focus_owned", "upstream_derived"})


@dataclass(frozen=True, slots=True)
class SourceMetrics:
    normalized_bytes: int
    lines: int


@dataclass(frozen=True, slots=True)
class ReviewedSource:
    category: str


@dataclass(frozen=True, slots=True)
class SourceContextPolicy:
    alignment_threshold_bytes: int
    alignment_threshold_lines: int
    review_threshold_lines: int
    reviewed_sources: dict[str, ReviewedSource]


@dataclass(frozen=True, slots=True)
class SourceContextWarning:
    path: str
    actual: SourceMetrics
    review_threshold_lines: int
    alignment_threshold_lines: int
    alignment_threshold_bytes: int

    def render(self) -> str:
        """Return one stable JSON record suitable for CI log processing."""

        return "SOURCE_CONTEXT_WARNING " + json.dumps(
            {
                "actual_bytes": self.actual.normalized_bytes,
                "actual_lines": self.actual.lines,
                "alignment_threshold_bytes": self.alignment_threshold_bytes,
                "alignment_threshold_lines": self.alignment_threshold_lines,
                "path": self.path,
                "review_threshold_lines": self.review_threshold_lines,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class SourceContextReport:
    errors: tuple[str, ...]
    warnings: tuple[SourceContextWarning, ...]


def _is_scanned_source_path(relative: pathlib.PurePosixPath) -> bool:
    if relative.suffix.lower() not in _SOURCE_SUFFIXES:
        return False
    if any(part in _GENERATED_DIRECTORY_NAMES for part in relative.parts):
        return False
    raw = relative.as_posix()
    if any(
        relative.parts[:-1] == pathlib.PurePosixPath(container).parts
        for container in _TOP_LEVEL_SOURCE_CONTAINERS
    ):
        return True
    return any(raw.startswith(f"{root}/") for root in _SOURCE_ROOTS)


def _load_policy() -> SourceContextPolicy:
    try:
        payload: Any = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {_POLICY_PATH.relative_to(_REPO_ROOT)}: {exc}") from exc
    if not isinstance(payload, dict) or frozenset(payload) != _EXPECTED_CONFIG_KEYS:
        raise ValueError(
            "source context policy must contain exactly "
            "`alignment_threshold_bytes`, `alignment_threshold_lines`, "
            "`review_threshold_lines`, and `reviewed_sources`"
        )
    alignment_threshold_bytes = payload["alignment_threshold_bytes"]
    alignment_threshold_lines = payload["alignment_threshold_lines"]
    review_threshold_lines = payload["review_threshold_lines"]
    raw_reviewed_sources = payload["reviewed_sources"]
    if type(alignment_threshold_bytes) is not int or alignment_threshold_bytes <= 0:
        raise ValueError(
            "alignment_threshold_bytes must be a positive integer"
        )
    if type(alignment_threshold_lines) is not int or alignment_threshold_lines <= 0:
        raise ValueError(
            "alignment_threshold_lines must be a positive integer"
        )
    if (
        type(review_threshold_lines) is not int
        or review_threshold_lines <= 0
        or review_threshold_lines >= alignment_threshold_lines
    ):
        raise ValueError(
            "review_threshold_lines must be a positive integer below "
            "alignment_threshold_lines"
        )
    if not isinstance(raw_reviewed_sources, dict):
        raise ValueError("reviewed_sources must be an object")
    reviewed_sources: dict[str, ReviewedSource] = {}
    for raw_path, raw_review in raw_reviewed_sources.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(
                "reviewed source paths must be nonempty repository-relative strings"
            )
        relative = pathlib.PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or "\\" in raw_path
            or ".." in relative.parts
            or relative.as_posix() != raw_path
        ):
            raise ValueError(
                "reviewed source paths must be normalized repository-relative POSIX paths"
            )
        if not _is_scanned_source_path(relative):
            raise ValueError(
                f"reviewed source path is outside the scanned source set: {raw_path}"
            )
        if not isinstance(raw_review, dict) or set(raw_review) != {"category"}:
            raise ValueError(
                f"reviewed source entry for {raw_path!r} must contain exactly category"
            )
        raw_category = raw_review["category"]
        if raw_category not in _REVIEW_CATEGORIES:
            raise ValueError(
                f"reviewed source category for {raw_path!r} must be one of "
                f"{', '.join(sorted(_REVIEW_CATEGORIES))}"
            )
        reviewed_sources[raw_path] = ReviewedSource(category=raw_category)
    return SourceContextPolicy(
        alignment_threshold_bytes=alignment_threshold_bytes,
        alignment_threshold_lines=alignment_threshold_lines,
        review_threshold_lines=review_threshold_lines,
        reviewed_sources=reviewed_sources,
    )


def _normalized_source_metrics(path: pathlib.Path) -> SourceMetrics:
    """Count source bytes and lines independently of the checkout's CRLF policy."""

    try:
        normalized = path.read_bytes().replace(b"\r\n", b"\n")
    except OSError as exc:
        raise ValueError(f"cannot read source: {exc}") from exc
    line_count = normalized.count(b"\n")
    if normalized and not normalized.endswith(b"\n"):
        line_count += 1
    return SourceMetrics(normalized_bytes=len(normalized), lines=line_count)


def _source_metrics() -> dict[str, SourceMetrics]:
    metrics: dict[str, SourceMetrics] = {}

    def inspect(path: pathlib.Path) -> None:
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            return
        relative = path.relative_to(_REPO_ROOT).as_posix()
        if not _is_scanned_source_path(pathlib.PurePosixPath(relative)):
            return
        try:
            metrics[relative] = _normalized_source_metrics(path)
        except ValueError as exc:
            raise ValueError(f"cannot inspect {relative}: {exc}") from exc

    for root_name in _SOURCE_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            raise ValueError(
                f"configured source root is missing or not a directory: {root_name}"
            )
        for path in root.rglob("*"):
            inspect(path)
    for container_name in _TOP_LEVEL_SOURCE_CONTAINERS:
        container = _REPO_ROOT / container_name
        if not container.is_dir():
            rendered = container_name or "."
            raise ValueError(
                "configured source container is missing or not a directory: "
                f"{rendered}"
            )
        for path in container.iterdir():
            inspect(path)
    return metrics


def check() -> SourceContextReport:
    policy = _load_policy()
    metrics_by_path = _source_metrics()
    errors: list[str] = []
    warnings: list[SourceContextWarning] = []
    for path in sorted(policy.reviewed_sources):
        if path not in metrics_by_path:
            errors.append(
                "reviewed source inventory points to a missing scanned source file: "
                f"{path}"
            )
    for path, actual in sorted(metrics_by_path.items()):
        if path in policy.reviewed_sources:
            continue
        needs_alignment = (
            actual.normalized_bytes >= policy.alignment_threshold_bytes
            or actual.lines >= policy.alignment_threshold_lines
        )
        if needs_alignment:
            errors.append(
                "unreviewed source reached a developer-alignment threshold: "
                f"{path} has {actual.normalized_bytes} bytes and {actual.lines} "
                f"lines; alignment thresholds are {policy.alignment_threshold_bytes} "
                f"bytes and {policy.alignment_threshold_lines} lines"
            )
        elif actual.lines >= policy.review_threshold_lines:
            warnings.append(
                SourceContextWarning(
                    path=path,
                    actual=actual,
                    review_threshold_lines=policy.review_threshold_lines,
                    alignment_threshold_lines=policy.alignment_threshold_lines,
                    alignment_threshold_bytes=policy.alignment_threshold_bytes,
                )
            )
    return SourceContextReport(errors=tuple(errors), warnings=tuple(warnings))


def main() -> int:
    try:
        report = check()
    except ValueError as exc:
        print(f"source-context guard configuration error: {exc}", file=sys.stderr)
        return 2
    for warning in report.warnings:
        print(warning.render(), file=sys.stderr)
    if report.errors:
        print("Source-context review alignment required:", file=sys.stderr)
        for error in report.errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Pause and report the ownership, context-pressure, discoverability, and "
            "behavior-tracing impact to the developer. The threshold is not a split "
            "requirement: after the developer chooses immediate organization, explicit "
            "deferral, or an intact single owner, record the reviewed source decision.",
            file=sys.stderr,
        )
        return 1
    print(
        "Source files match their review inventory and alignment thresholds "
        f"({len(report.warnings)} review warning(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
