#!/usr/bin/env python3
"""Check repository-document links, roles, bilingual shape, and work lifecycle."""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]

_INLINE_LINK_RE = re.compile(
    r"!?\[(?P<label>(?:\\.|[^\]])*)\]\((?P<body><[^>]+>|(?:\\.|[^)])+)\)"
)
_REFERENCE_LINK_RE = re.compile(
    r"^\[[^\]]+\]:\s*(?P<body><[^>]+>|\S+)", re.MULTILINE
)
_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$", re.MULTILINE)
_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<marks>#{2,6})\s+(?P<number>\d+(?:\.\d+)*)(?:\.|\s|$)",
    re.MULTILINE,
)
_EXPLICIT_ANCHOR_RE = re.compile(
    r"<(?:a\s+(?:name|id)|[^>]+\sid)=[\"'](?P<anchor>[^\"']+)[\"']",
    re.IGNORECASE,
)
_WORK_FILE_REFERENCE_RE = re.compile(
    r"(?:(?:docs|\.\.?)/)?_work/[A-Za-z0-9_./-]+\.(?:md|tsv|json|ya?ml)"
)
_SUPERSEDED_DECISION_RE = re.compile(
    r"^>.*(?:superseded\s+(?:as|by)|partially\s+superseded|"
    r"当前行为合同已.*取代|部分内容已.*取代)",
    re.IGNORECASE | re.MULTILINE,
)
_ACTIVE_STATUS_RE = re.compile(r"^(?:状态：|Status:\s*)active(?:[；;]|\s*$)", re.MULTILINE)
_LOCAL_LINE_LABEL_RE = re.compile(r":\d+(?:-\d+)?$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: {self.kind}: {self.detail}"


@dataclass(frozen=True, slots=True)
class MarkdownLink:
    label: str
    target: str


def _markdown_files(repo_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    readme = repo_root / "README.md"
    if readme.is_file():
        files.append(readme)
    docs_root = repo_root / "docs"
    if docs_root.is_dir():
        files.extend(
            path
            for path in docs_root.rglob("*.md")
            if "_work" not in path.relative_to(docs_root).parts
        )
    return tuple(sorted(files))


def _link_target(body: str) -> str:
    value = body.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def markdown_links(text: str) -> tuple[MarkdownLink, ...]:
    links = [
        MarkdownLink(match.group("label"), _link_target(match.group("body")))
        for match in _INLINE_LINK_RE.finditer(text)
    ]
    links.extend(
        MarkdownLink("", _link_target(match.group("body")))
        for match in _REFERENCE_LINK_RE.finditer(text)
    )
    return tuple(links)


def _heading_slug(title: str) -> str:
    value = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", title)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").replace("*", "")
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = "".join(
        character
        for character in value
        if character in "-_ " or character.isalnum() or character.isspace()
    )
    return re.sub(r"\s+", "-", value)


def markdown_anchors(text: str) -> frozenset[str]:
    anchors = set(match.group("anchor") for match in _EXPLICIT_ANCHOR_RE.finditer(text))
    seen: dict[str, int] = {}
    for match in _HEADING_RE.finditer(text):
        slug = _heading_slug(match.group("title"))
        if not slug:
            continue
        duplicate = seen.get(slug, 0)
        seen[slug] = duplicate + 1
        anchors.add(slug if duplicate == 0 else f"{slug}-{duplicate}")
    return frozenset(anchors)


def numbered_heading_skeleton(text: str) -> tuple[tuple[int, str], ...]:
    return tuple(
        (len(match.group("marks")), match.group("number"))
        for match in _NUMBERED_HEADING_RE.finditer(text)
    )


def _is_external_target(target: str) -> bool:
    return bool(_SCHEME_RE.match(target)) or target.startswith("//")


def _relative(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _link_violations(path: Path, repo_root: Path) -> list[Violation]:
    text = path.read_text(encoding="utf-8")
    relative = _relative(path, repo_root)
    violations: list[Violation] = []
    for link in markdown_links(text):
        target = link.target
        if not target or _is_external_target(target):
            continue
        split = urlsplit(target)
        local_path = unquote(split.path)
        resolved = path if not local_path else (path.parent / local_path).resolve()
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError:
            violations.append(Violation(relative, "local link escapes repository", target))
            continue
        if not resolved.exists():
            violations.append(Violation(relative, "missing local link target", target))
            continue
        if link.label and _LOCAL_LINE_LABEL_RE.search(link.label.strip("` ")):
            violations.append(
                Violation(relative, "unstable local line-number label", link.label)
            )
        if split.fragment and resolved.is_file() and resolved.suffix.lower() == ".md":
            anchor = unquote(split.fragment)
            anchors = markdown_anchors(resolved.read_text(encoding="utf-8"))
            if anchor not in anchors:
                violations.append(
                    Violation(relative, "missing local heading anchor", target)
                )
    return violations


def _bilingual_skeleton_violations(repo_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for directory in ("architecture", "contracts", "decisions"):
        for chinese in sorted((repo_root / "docs" / directory).glob("*.zh-CN.md")):
            english = chinese.with_name(chinese.name.replace(".zh-CN.md", ".md"))
            if not english.is_file():
                continue
            chinese_skeleton = numbered_heading_skeleton(
                chinese.read_text(encoding="utf-8")
            )
            english_skeleton = numbered_heading_skeleton(
                english.read_text(encoding="utf-8")
            )
            if chinese_skeleton != english_skeleton:
                violations.append(
                    Violation(
                        _relative(chinese, repo_root),
                        "bilingual numbered-heading mismatch",
                        f"Chinese {chinese_skeleton!r} != English {english_skeleton!r}",
                    )
                )
    return violations


def _work_lifecycle_violations(repo_root: Path) -> list[Violation]:
    work_root = repo_root / "docs" / "_work"
    if not work_root.is_dir():
        return []
    files = tuple(sorted(path for path in work_root.iterdir() if path.is_file()))
    if not files:
        return []
    markdown_files = tuple(path for path in files if path.suffix == ".md")
    if len(markdown_files) != 1:
        return [
            Violation(
                "docs/_work",
                "invalid work lifecycle",
                f"expected one active Markdown ledger, found {len(markdown_files)}",
            )
        ]
    ledger = markdown_files[0]
    header = "\n".join(ledger.read_text(encoding="utf-8").splitlines()[:20])
    if _ACTIVE_STATUS_RE.search(header) is None:
        return [
            Violation(
                _relative(ledger, repo_root),
                "invalid work lifecycle",
                "the sole Markdown ledger is not marked active in its first 20 lines",
            )
        ]
    return []


def repository_violations(repo_root: Path = REPO_ROOT) -> tuple[Violation, ...]:
    violations: list[Violation] = []
    markdown_files = _markdown_files(repo_root)
    for path in markdown_files:
        text = path.read_text(encoding="utf-8")
        violations.extend(_link_violations(path, repo_root))
        work_reference = _WORK_FILE_REFERENCE_RE.search(text)
        if work_reference is not None:
            violations.append(
                Violation(
                    _relative(path, repo_root),
                    "durable document references a work file",
                    work_reference.group(0),
                )
            )
        if path.parent == repo_root / "docs" / "decisions":
            marker = _SUPERSEDED_DECISION_RE.search(text)
            if marker is not None:
                violations.append(
                    Violation(
                        _relative(path, repo_root),
                        "active decision declares superseded content",
                        marker.group(0),
                    )
                )
    violations.extend(_bilingual_skeleton_violations(repo_root))
    violations.extend(_work_lifecycle_violations(repo_root))
    return tuple(sorted(violations, key=lambda item: (item.path, item.kind, item.detail)))


def main() -> int:
    violations = repository_violations()
    for violation in violations:
        print(f"document-integrity: {violation.render()}", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
