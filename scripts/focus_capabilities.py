#!/usr/bin/env python3
"""Load the reviewed Focus capability navigation catalog fail closed."""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG_PATH = pathlib.Path(__file__).with_name("focus_capabilities.json")
ALLOWED_RUNNERS = frozenset({"pytest", "vitest"})
ALLOWED_GUARDS = frozenset(
    {
        "dependency-direction",
        "import-cycles",
        "source-context",
        "web-dependency-direction",
        "web-typecheck",
        "web-wire",
    }
)

_CAPABILITY_KEYS = frozenset(
    {"entry", "owner", "contract", "focused-test", "sentinel", "guard"}
)
_SOURCE_KEYS = frozenset({"path", "symbol"})
_CONTRACT_KEYS = frozenset({"path", "heading"})
_VERIFICATION_KEYS = frozenset({"runner", "targets"})
_CAPABILITY_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_PYTEST_NODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\r\n]+\])?\Z")
_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_EXPORTED_SCRIPT_DECLARATION = (
    r"^export[ \t]+(?:default[ \t]+)?(?:declare[ \t]+)?"
    r"(?:(?:async[ \t]+)?function|class|interface|type|const|let|var)[ \t]+{symbol}\b"
)
_VUE_SETUP_FUNCTION_DECLARATION = r"^(?:async[ \t]+)?function[ \t]+{symbol}\b"
_RAW_HTML_CONTAINER = re.compile(
    r"^[ \t]{0,3}<(?P<tag>script|pre|style|textarea)(?:[ \t>]|$)",
    re.IGNORECASE,
)
_GENERIC_HTML_BLOCK = re.compile(r"^[ \t]{0,3}</?[A-Za-z][^>]*>")


class CapabilityMapError(ValueError):
    """Raised when the reviewed capability catalog is missing or ambiguous."""


@dataclass(frozen=True, slots=True)
class SourceReference:
    path: str
    symbol: str


@dataclass(frozen=True, slots=True)
class ContractReference:
    path: str
    heading: str


@dataclass(frozen=True, slots=True)
class VerificationReference:
    runner: str
    targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReferenceLocation:
    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    entries: tuple[SourceReference, ...]
    owners: tuple[SourceReference, ...]
    contracts: tuple[ContractReference, ...]
    focused_tests: tuple[VerificationReference, ...]
    sentinels: tuple[VerificationReference, ...]
    guards: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityCatalog:
    capabilities: tuple[Capability, ...]

    def require(self, name: str) -> Capability:
        for capability in self.capabilities:
            if capability.name == name:
                return capability
        available = ", ".join(item.name for item in self.capabilities)
        raise CapabilityMapError(
            f"unknown Focus capability {name!r}; available: {available}"
        )


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityMapError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _require_object(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CapabilityMapError(f"{context} must be an object")
    return value


def _require_exact_keys(
    value: dict[str, Any], *, expected: frozenset[str], context: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise CapabilityMapError(
            f"{context} keys must be exactly {sorted(expected)}; "
            f"missing={missing}, unknown={unknown}"
        )


def _require_nonempty_string(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CapabilityMapError(f"{context} must be a nonempty trimmed string")
    if "\n" in value or "\r" in value or "\0" in value:
        raise CapabilityMapError(f"{context} must be one line without NUL")
    return value


def _normalized_relative_path(raw: Any, *, context: str) -> pathlib.PurePosixPath:
    value = _require_nonempty_string(raw, context=context)
    relative = pathlib.PurePosixPath(value)
    if (
        relative.is_absolute()
        or "\\" in value
        or not relative.parts
        or ".." in relative.parts
        or relative.as_posix() != value
    ):
        raise CapabilityMapError(
            f"{context} must be a normalized repository-relative POSIX path"
        )
    return relative


def _resolve_repository_path(
    raw: Any,
    *,
    repo_root: pathlib.Path,
    context: str,
) -> tuple[str, pathlib.Path]:
    relative = _normalized_relative_path(raw, context=context)
    if relative.parts[:2] == ("docs", "_work"):
        raise CapabilityMapError(f"{context} cannot reference temporary _work evidence")
    root = repo_root.resolve()
    resolved = (root / pathlib.Path(*relative.parts)).resolve()
    if not resolved.is_relative_to(root):
        raise CapabilityMapError(f"{context} escapes the repository through a symlink")
    if resolved.relative_to(root).as_posix() != relative.as_posix():
        raise CapabilityMapError(f"{context} must not traverse a repository symlink")
    if not resolved.exists():
        raise CapabilityMapError(f"{context} does not exist: {relative.as_posix()}")
    return relative.as_posix(), resolved


def _python_symbol_nodes(path: pathlib.Path) -> list[tuple[str, ast.stmt]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise CapabilityMapError(f"cannot parse Python source {path}: {exc}") from exc

    symbols: list[tuple[str, ast.stmt]] = []

    def collect(nodes: Iterable[ast.stmt], prefix: str = "") -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append((f"{prefix}{node.name}", node))
            elif isinstance(node, ast.ClassDef):
                qualified = f"{prefix}{node.name}"
                symbols.append((qualified, node))
                collect(node.body, f"{qualified}.")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append((f"{prefix}{target.id}", node))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                symbols.append((f"{prefix}{node.target.id}", node))

    collect(tree.body)
    return symbols


def _without_script_comments_and_strings(source: str) -> str:
    """Blank JavaScript-like comments and strings while preserving line starts."""

    result: list[str] = []
    state = "code"
    index = 0
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if character == "/" and following == "/":
                result.extend((" ", " "))
                state = "line-comment"
                index += 2
                continue
            if character == "/" and following == "*":
                result.extend((" ", " "))
                state = "block-comment"
                index += 2
                continue
            if character in {"'", '"', "`"}:
                result.append(" ")
                state = {"'": "single", '"': "double", "`": "template"}[character]
                index += 1
                continue
            result.append(character)
            index += 1
            continue
        if character == "\n":
            result.append("\n")
            if state == "line-comment":
                state = "code"
            index += 1
            continue
        if state == "block-comment" and character == "*" and following == "/":
            result.extend((" ", " "))
            state = "code"
            index += 2
            continue
        closing = {"single": "'", "double": '"', "template": "`"}.get(state)
        if closing and character == "\\":
            result.append(" ")
            if following:
                result.append("\n" if following == "\n" else " ")
            index += 2
            continue
        if closing and character == closing:
            result.append(" ")
            state = "code"
            index += 1
            continue
        result.append(" ")
        index += 1
    if state not in {"code", "line-comment"}:
        raise CapabilityMapError(f"cannot prove script syntax: unterminated {state}")
    return "".join(result)


def _without_html_comments(source: str) -> str:
    """Blank HTML comments so they cannot manufacture Vue script elements."""

    result: list[str] = []
    in_comment = False
    index = 0
    while index < len(source):
        if not in_comment and source.startswith("-->", index):
            raise CapabilityMapError(
                "cannot prove Vue structure: unmatched HTML comment close"
            )
        marker = "-->" if in_comment else "<!--"
        if source.startswith(marker, index):
            result.extend(" " for _ in marker)
            in_comment = not in_comment
            index += len(marker)
            continue
        character = source[index]
        result.append(
            "\n"
            if in_comment and character == "\n"
            else " "
            if in_comment
            else character
        )
        index += 1
    if in_comment:
        raise CapabilityMapError(
            "cannot prove Vue structure: unterminated HTML comment"
        )
    return "".join(result)


def _script_declaration_source(path: pathlib.Path) -> tuple[str, int]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CapabilityMapError(f"cannot read source {path}: {exc}") from exc
    line_offset = 0
    if path.suffix == ".vue":
        source = _without_html_comments(source)
        script_block = re.match(
            r"\A[ \t\r\n]*<script\b(?P<attributes>[^>]*)>"
            r"(?P<body>.*?)</script\s*>",
            source,
            re.IGNORECASE | re.DOTALL,
        )
        if not script_block or not re.search(
            r"(?:^|[ \t])setup(?:[ \t]|=|$)", script_block.group("attributes")
        ):
            raise CapabilityMapError(
                f"reviewed Vue source must start with one script setup block: {path}"
            )
        if re.search(r"<script\b", source[script_block.end() :], re.IGNORECASE):
            raise CapabilityMapError(
                f"reviewed Vue source must contain only one script block: {path}"
            )
        line_offset = source[: script_block.start("body")].count("\n")
        source = script_block.group("body")
    return _without_script_comments_and_strings(source), line_offset


def _top_level_script_declaration_lines(
    source: str, symbol: str, *, vue_setup: bool
) -> tuple[int, ...]:
    declaration = (
        _VUE_SETUP_FUNCTION_DECLARATION if vue_setup else _EXPORTED_SCRIPT_DECLARATION
    )
    pattern = re.compile(declaration.format(symbol=re.escape(symbol)))
    if "/" in source:
        raise CapabilityMapError(
            "cannot prove script symbol scope in source containing regex or division"
        )
    lines: list[int] = []
    delimiter_stack: list[str] = []
    previous_significant = ""
    closing_delimiter = {")": "(", "]": "[", "}": "{"}
    for line_number, line in enumerate(source.splitlines(), start=1):
        at_statement_boundary = previous_significant in {"", ";", "}"}
        if not delimiter_stack and at_statement_boundary and pattern.match(line):
            lines.append(line_number)
        for character in line:
            if character in "([{":
                delimiter_stack.append(character)
            elif character in closing_delimiter:
                expected = closing_delimiter[character]
                if not delimiter_stack or delimiter_stack[-1] != expected:
                    raise CapabilityMapError(
                        "cannot prove script symbol scope: unmatched closing delimiter"
                    )
                delimiter_stack.pop()
            if not character.isspace():
                previous_significant = character
    if delimiter_stack:
        raise CapabilityMapError(
            "cannot prove script symbol scope: unmatched opening delimiter"
        )
    return tuple(lines)


def _node_line_range(node: ast.AST, *, context: str) -> tuple[int, int]:
    start_line = getattr(node, "lineno", None)
    end_line = getattr(node, "end_lineno", None)
    if (
        type(start_line) is not int
        or type(end_line) is not int
        or start_line < 1
        or end_line < start_line
    ):
        raise CapabilityMapError(f"{context} has no exact source line range")
    for decorator in getattr(node, "decorator_list", ()):
        decorator_line = getattr(decorator, "lineno", None)
        if type(decorator_line) is not int or decorator_line < 1:
            raise CapabilityMapError(f"{context} decorator has no exact line")
        start_line = min(start_line, decorator_line)
    return start_line, end_line


def _source_symbol_line_range(
    path: pathlib.Path, symbol: str, *, context: str
) -> tuple[int, int]:
    if not _SYMBOL.fullmatch(symbol):
        raise CapabilityMapError(f"{context}.symbol is not a canonical symbol name")
    if path.suffix == ".py":
        matches = [node for name, node in _python_symbol_nodes(path) if name == symbol]
        locations = tuple(_node_line_range(node, context=context) for node in matches)
    elif path.suffix in {".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx", ".vue"}:
        if "." in symbol:
            raise CapabilityMapError(
                f"{context}.symbol cannot be qualified for non-Python source"
            )
        source, line_offset = _script_declaration_source(path)
        declaration_lines = _top_level_script_declaration_lines(
            source, symbol, vue_setup=path.suffix == ".vue"
        )
        locations = tuple(
            (line_offset + line_number, line_offset + line_number)
            for line_number in declaration_lines
        )
    else:
        raise CapabilityMapError(f"{context}.path has unsupported source suffix")
    if len(locations) != 1:
        raise CapabilityMapError(
            f"{context}.symbol must resolve exactly once; "
            f"found {len(locations)}: {symbol!r}"
        )
    return locations[0]


def _source_reference(
    raw: Any, *, repo_root: pathlib.Path, context: str
) -> SourceReference:
    value = _require_object(raw, context=context)
    _require_exact_keys(value, expected=_SOURCE_KEYS, context=context)
    path_value, path = _resolve_repository_path(
        value["path"], repo_root=repo_root, context=f"{context}.path"
    )
    relative = pathlib.PurePosixPath(path_value)
    if relative.parts[0] == "bot":
        pass
    elif relative.parts[:2] == ("web", "src"):
        pass
    else:
        raise CapabilityMapError(f"{context}.path must be under bot/ or web/src/")
    if not path.is_file():
        raise CapabilityMapError(f"{context}.path must name a source file")
    symbol = _require_nonempty_string(value["symbol"], context=f"{context}.symbol")
    _source_symbol_line_range(path, symbol, context=context)
    return SourceReference(path=path_value, symbol=symbol)


def _markdown_headings(
    path: pathlib.Path,
) -> tuple[tuple[tuple[str, int, int], ...], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CapabilityMapError(f"cannot read contract {path}: {exc}") from exc
    headings: list[tuple[str, int, int]] = []
    fence_character = ""
    fence_length = 0
    html_end_marker = ""
    html_until_blank = False
    for line_number, line in enumerate(lines, start=1):
        if html_end_marker:
            if html_end_marker.lower() in line.lower():
                html_end_marker = ""
            continue
        if html_until_blank:
            if not line.strip():
                html_until_blank = False
            continue
        if fence_character:
            closing = re.fullmatch(
                rf"[ \t]{{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*",
                line,
            )
            if closing:
                fence_character = ""
                fence_length = 0
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if opening:
            marker = opening.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        if "<!--" in line:
            opening_index = line.index("<!--") + len("<!--")
            if "-->" not in line[opening_index:]:
                html_end_marker = "-->"
            continue
        stripped = line.lstrip(" \t")
        if len(line) - len(stripped) <= 3:
            persistent_html = (
                ("<?", "?>"),
                ("<![CDATA[", "]]>"),
            )
            matched_html = False
            for start, end in persistent_html:
                if stripped.startswith(start):
                    if end not in stripped[len(start) :]:
                        html_end_marker = end
                    matched_html = True
                    break
            if matched_html:
                continue
            if re.match(r"<![A-Z]", stripped):
                if ">" not in stripped[2:]:
                    html_end_marker = ">"
                continue
            if container := _RAW_HTML_CONTAINER.match(line):
                end = f"</{container.group('tag')}>"
                if end.lower() not in line[container.end() :].lower():
                    html_end_marker = end
                continue
            if _GENERIC_HTML_BLOCK.match(line):
                html_until_blank = True
                continue
        if match := _ATX_HEADING.fullmatch(line):
            level_match = re.match(r"#{1,6}", stripped)
            if level_match is None:  # The ATX matcher already proves this shape.
                raise CapabilityMapError(f"cannot parse Markdown heading in {path}")
            headings.append((match.group(1), len(level_match.group()), line_number))
    return tuple(headings), len(lines)


def _contract_heading_line_range(
    path: pathlib.Path, heading: str, *, context: str
) -> tuple[int, int]:
    headings, total_lines = _markdown_headings(path)
    matches = [item for item in headings if item[0] == heading]
    if len(matches) != 1:
        raise CapabilityMapError(
            f"{context}.heading must resolve exactly once; "
            f"found {len(matches)}: {heading!r}"
        )
    _, level, start_line = matches[0]
    end_line = total_lines
    for _, candidate_level, candidate_line in headings:
        if candidate_line > start_line and candidate_level <= level:
            end_line = candidate_line - 1
            break
    if end_line < start_line:
        raise CapabilityMapError(f"{context}.heading has no exact section range")
    return start_line, end_line


def _contract_reference(
    raw: Any, *, repo_root: pathlib.Path, context: str
) -> ContractReference:
    value = _require_object(raw, context=context)
    _require_exact_keys(value, expected=_CONTRACT_KEYS, context=context)
    path_value, path = _resolve_repository_path(
        value["path"], repo_root=repo_root, context=f"{context}.path"
    )
    relative = pathlib.PurePosixPath(path_value)
    if (
        relative.parts[:2]
        not in {
            ("docs", "architecture"),
            ("docs", "contracts"),
            ("docs", "decisions"),
        }
        or path.suffix != ".md"
    ):
        raise CapabilityMapError(
            f"{context}.path must name a Markdown architecture, contract, or decision"
        )
    heading = _require_nonempty_string(value["heading"], context=f"{context}.heading")
    _contract_heading_line_range(path, heading, context=context)
    return ContractReference(path=path_value, heading=heading)


def _pytest_node(
    path: pathlib.Path, node_parts: Sequence[str], *, context: str
) -> ast.stmt:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise CapabilityMapError(f"cannot parse pytest target {path}: {exc}") from exc
    nodes: Sequence[ast.stmt] = tree.body
    match: ast.stmt | None = None
    for index, raw_part in enumerate(node_parts):
        if not _PYTEST_NODE.fullmatch(raw_part):
            raise CapabilityMapError(f"{context} has an invalid pytest node segment")
        part = raw_part.partition("[")[0]
        matches = [
            node
            for node in nodes
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == part
        ]
        if len(matches) != 1:
            raise CapabilityMapError(
                f"{context} pytest node segment must resolve exactly once: {part!r}"
            )
        match = matches[0]
        if index + 1 < len(node_parts):
            if not isinstance(match, ast.ClassDef):
                raise CapabilityMapError(f"{context} descends through a test function")
            nodes = match.body
    if match is None:
        raise CapabilityMapError(f"{context} must include a pytest node")
    return match


def _verification_target(
    raw: Any,
    *,
    runner: str,
    repo_root: pathlib.Path,
    context: str,
) -> str:
    value = _require_nonempty_string(raw, context=context)
    if value.startswith("-"):
        raise CapabilityMapError(f"{context} cannot be a runner option")
    base, *node_parts = value.split("::")
    if any(character in base for character in "*?["):
        raise CapabilityMapError(f"{context} cannot contain a glob")
    path_value, path = _resolve_repository_path(
        base, repo_root=repo_root, context=context
    )
    relative = pathlib.PurePosixPath(path_value)
    if runner == "pytest":
        if relative.parts[0] != "tests":
            raise CapabilityMapError(f"{context} pytest target must be under tests/")
        if path.is_file() and path.suffix != ".py":
            raise CapabilityMapError(f"{context} pytest file must end in .py")
        if not path.is_file() and not path.is_dir():
            raise CapabilityMapError(
                f"{context} pytest target must be a file or directory"
            )
        if node_parts:
            if not path.is_file():
                raise CapabilityMapError(f"{context} pytest node requires a file")
            _pytest_node(path, node_parts, context=context)
    elif runner == "vitest":
        if node_parts:
            raise CapabilityMapError(f"{context} vitest target must be file-level")
        if relative.parts[:2] not in {("web", "src"), ("web", "test")}:
            raise CapabilityMapError(
                f"{context} vitest target must be under web/src/ or web/test/"
            )
        if not path.is_file() or not path.name.endswith((".test.ts", ".test.tsx")):
            raise CapabilityMapError(f"{context} vitest target must be a .test.ts file")
    else:  # Guarded by the runner whitelist before path inspection.
        raise CapabilityMapError(f"{context} has unknown runner {runner!r}")
    return value


def _verification_reference(
    raw: Any, *, repo_root: pathlib.Path, context: str
) -> VerificationReference:
    value = _require_object(raw, context=context)
    _require_exact_keys(value, expected=_VERIFICATION_KEYS, context=context)
    runner = _require_nonempty_string(value["runner"], context=f"{context}.runner")
    if runner not in ALLOWED_RUNNERS:
        raise CapabilityMapError(f"{context}.runner is not whitelisted: {runner!r}")
    raw_targets = value["targets"]
    if not isinstance(raw_targets, list) or not raw_targets:
        raise CapabilityMapError(f"{context}.targets must be a nonempty array")
    targets = tuple(
        _verification_target(
            item,
            runner=runner,
            repo_root=repo_root,
            context=f"{context}.targets[{index}]",
        )
        for index, item in enumerate(raw_targets)
    )
    if len(set(targets)) != len(targets):
        raise CapabilityMapError(f"{context}.targets contains duplicates")
    return VerificationReference(runner=runner, targets=targets)


def _require_unique_verification_targets(
    references: Sequence[VerificationReference], *, context: str
) -> None:
    seen: set[tuple[str, str]] = set()
    for reference in references:
        for target in reference.targets:
            key = (reference.runner, target)
            if key in seen:
                raise CapabilityMapError(
                    f"{context} contains duplicate runner target: {key!r}"
                )
            seen.add(key)


def _file_line_range(path: pathlib.Path, *, context: str) -> tuple[int, int]:
    try:
        line_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError) as exc:
        raise CapabilityMapError(f"cannot read {context} {path}: {exc}") from exc
    if line_count < 1:
        raise CapabilityMapError(f"{context} has no exact line range: {path}")
    return 1, line_count


def source_reference_location(
    reference: SourceReference, *, repo_root: pathlib.Path = REPO_ROOT
) -> ReferenceLocation:
    """Resolve a reviewed source selector to its current declaration lines."""

    context = f"source reference {reference.path}#{reference.symbol}"
    path_value, path = _resolve_repository_path(
        reference.path, repo_root=pathlib.Path(repo_root), context=f"{context}.path"
    )
    start_line, end_line = _source_symbol_line_range(
        path, reference.symbol, context=context
    )
    return ReferenceLocation(path_value, start_line, end_line)


def contract_reference_location(
    reference: ContractReference, *, repo_root: pathlib.Path = REPO_ROOT
) -> ReferenceLocation:
    """Resolve a reviewed Markdown heading to its current section lines."""

    context = f"contract reference {reference.path}#{reference.heading}"
    path_value, path = _resolve_repository_path(
        reference.path, repo_root=pathlib.Path(repo_root), context=f"{context}.path"
    )
    start_line, end_line = _contract_heading_line_range(
        path, reference.heading, context=context
    )
    return ReferenceLocation(path_value, start_line, end_line)


def verification_target_location(
    runner: str,
    target: str,
    *,
    repo_root: pathlib.Path = REPO_ROOT,
) -> ReferenceLocation:
    """Resolve one reviewed test target to its current file or pytest-node lines."""

    root = pathlib.Path(repo_root)
    context = f"{runner} target {target!r}"
    value = _verification_target(target, runner=runner, repo_root=root, context=context)
    base, *node_parts = value.split("::")
    path_value, path = _resolve_repository_path(
        base, repo_root=root, context=f"{context}.path"
    )
    if path.is_dir():
        raise CapabilityMapError(
            f"{context} is directory-level and has no exact line range"
        )
    if runner == "pytest" and node_parts:
        start_line, end_line = _node_line_range(
            _pytest_node(path, node_parts, context=context), context=context
        )
    else:
        start_line, end_line = _file_line_range(path, context=context)
    return ReferenceLocation(path_value, start_line, end_line)


def _reference_array(
    raw: Any,
    *,
    loader: Any,
    repo_root: pathlib.Path,
    context: str,
) -> tuple[Any, ...]:
    if not isinstance(raw, list) or not raw:
        raise CapabilityMapError(f"{context} must be a nonempty array")
    values = tuple(
        loader(item, repo_root=repo_root, context=f"{context}[{index}]")
        for index, item in enumerate(raw)
    )
    if len(set(values)) != len(values):
        raise CapabilityMapError(f"{context} contains duplicate references")
    return values


def _capability(name: str, raw: Any, *, repo_root: pathlib.Path) -> Capability:
    if not _CAPABILITY_ID.fullmatch(name):
        raise CapabilityMapError(f"invalid capability id: {name!r}")
    value = _require_object(raw, context=f"capability {name!r}")
    _require_exact_keys(
        value, expected=_CAPABILITY_KEYS, context=f"capability {name!r}"
    )
    entries = _reference_array(
        value["entry"],
        loader=_source_reference,
        repo_root=repo_root,
        context=f"capability {name!r}.entry",
    )
    owners = _reference_array(
        value["owner"],
        loader=_source_reference,
        repo_root=repo_root,
        context=f"capability {name!r}.owner",
    )
    contracts = _reference_array(
        value["contract"],
        loader=_contract_reference,
        repo_root=repo_root,
        context=f"capability {name!r}.contract",
    )
    focused_tests = _reference_array(
        value["focused-test"],
        loader=_verification_reference,
        repo_root=repo_root,
        context=f"capability {name!r}.focused-test",
    )
    _require_unique_verification_targets(
        focused_tests, context=f"capability {name!r}.focused-test"
    )
    sentinels = _reference_array(
        value["sentinel"],
        loader=_verification_reference,
        repo_root=repo_root,
        context=f"capability {name!r}.sentinel",
    )
    _require_unique_verification_targets(
        sentinels, context=f"capability {name!r}.sentinel"
    )
    raw_guards = value["guard"]
    if not isinstance(raw_guards, list) or not raw_guards:
        raise CapabilityMapError(f"capability {name!r}.guard must be a nonempty array")
    guards = tuple(
        _require_nonempty_string(item, context=f"capability {name!r}.guard[{index}]")
        for index, item in enumerate(raw_guards)
    )
    if len(set(guards)) != len(guards):
        raise CapabilityMapError(f"capability {name!r}.guard contains duplicates")
    unknown_guards = sorted(set(guards) - ALLOWED_GUARDS)
    if unknown_guards:
        raise CapabilityMapError(
            f"capability {name!r}.guard is not whitelisted: {unknown_guards}"
        )
    return Capability(
        name=name,
        entries=entries,
        owners=owners,
        contracts=contracts,
        focused_tests=focused_tests,
        sentinels=sentinels,
        guards=guards,
    )


def load_catalog(
    *,
    repo_root: pathlib.Path = REPO_ROOT,
    catalog_path: pathlib.Path = CATALOG_PATH,
) -> CapabilityCatalog:
    """Load and validate every reviewed reference in the canonical catalog."""

    root = pathlib.Path(repo_root).resolve()
    path = pathlib.Path(catalog_path)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise CapabilityMapError("capability catalog must live inside the repository")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except CapabilityMapError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapabilityMapError(
            f"cannot read capability catalog {path}: {exc}"
        ) from exc
    value = _require_object(payload, context="capability catalog")
    _require_exact_keys(
        value,
        expected=frozenset({"schema_version", "capabilities"}),
        context="capability catalog",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CapabilityMapError("capability catalog schema_version must be exactly 1")
    raw_capabilities = _require_object(
        value["capabilities"], context="capability catalog.capabilities"
    )
    if not raw_capabilities:
        raise CapabilityMapError("capability catalog.capabilities cannot be empty")
    capabilities = tuple(
        _capability(name, raw_capabilities[name], repo_root=root)
        for name in sorted(raw_capabilities)
    )
    return CapabilityCatalog(capabilities=capabilities)


def capability_payload(capability: Capability) -> dict[str, Any]:
    """Return a stable JSON-ready navigation view without inferred behavior."""

    def sources(values: tuple[SourceReference, ...]) -> list[dict[str, str]]:
        return [{"path": item.path, "symbol": item.symbol} for item in values]

    def contracts(values: tuple[ContractReference, ...]) -> list[dict[str, str]]:
        return [{"path": item.path, "heading": item.heading} for item in values]

    def verification(
        values: tuple[VerificationReference, ...],
    ) -> list[dict[str, Any]]:
        return [
            {"runner": item.runner, "targets": list(item.targets)} for item in values
        ]

    return {
        "name": capability.name,
        "entry": sources(capability.entries),
        "owner": sources(capability.owners),
        "contract": contracts(capability.contracts),
        "focused-test": verification(capability.focused_tests),
        "sentinel": verification(capability.sentinels),
        "guard": list(capability.guards),
    }


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Focus capability navigation refs."
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        catalog = load_catalog()
    except CapabilityMapError as exc:
        print(f"Focus capability map is invalid: {exc}", file=sys.stderr)
        return 2
    reference_count = sum(
        len(item.entries)
        + len(item.owners)
        + len(item.contracts)
        + sum(len(group.targets) for group in item.focused_tests)
        + sum(len(group.targets) for group in item.sentinels)
        + len(item.guards)
        for item in catalog.capabilities
    )
    print(
        "Focus capability map is valid "
        f"({len(catalog.capabilities)} capabilities, {reference_count} reviewed refs)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
