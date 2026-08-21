#!/usr/bin/env python3
"""Reject strongly connected components in the Focus Python import graph."""

from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _REPO_ROOT / "bot"
_PACKAGE_NAME = "bot"


class ImportGraphError(ValueError):
    """Raised when the guarded package cannot be inspected reliably."""


@dataclass(frozen=True, slots=True)
class PythonModule:
    name: str
    path: pathlib.Path
    is_package: bool


@dataclass(frozen=True, slots=True)
class ImportCycle:
    modules: tuple[str, ...]

    def render(self) -> str:
        return "{" + ", ".join(self.modules) + "}"


@dataclass(frozen=True, slots=True)
class ImportGraphReport:
    module_count: int
    edge_count: int
    cycles: tuple[ImportCycle, ...]


def _discover_modules(
    package_root: pathlib.Path,
    *,
    package_name: str,
) -> dict[str, PythonModule]:
    root = pathlib.Path(package_root)
    if not root.is_dir():
        raise ImportGraphError(f"package root is missing: {root}")
    if not package_name or any(not part.isidentifier() for part in package_name.split(".")):
        raise ImportGraphError(f"invalid package name: {package_name!r}")

    modules: dict[str, PythonModule] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        is_package = relative.name == "__init__.py"
        suffix_parts = relative.parent.parts if is_package else relative.with_suffix("").parts
        name = ".".join((package_name, *suffix_parts))
        if name in modules:
            raise ImportGraphError(
                f"multiple source files resolve to Python module {name!r}"
            )
        modules[name] = PythonModule(
            name=name,
            path=path,
            is_package=is_package,
        )
    if package_name not in modules:
        raise ImportGraphError(
            f"package root has no __init__.py for {package_name!r}: {root}"
        )
    return modules


def _relative_import_base(source: PythonModule, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    package = source.name if source.is_package else source.name.rpartition(".")[0]
    package_parts = package.split(".") if package else []
    keep = len(package_parts) - (node.level - 1)
    if keep <= 0:
        raise ImportGraphError(
            f"relative import escapes guarded package in {source.path}: line {node.lineno}"
        )
    parts = package_parts[:keep]
    if node.module:
        parts.extend(node.module.split("."))
    return ".".join(parts)


def _known_module_dependencies(
    imported_name: str,
    *,
    modules: dict[str, PythonModule],
) -> set[str]:
    """Return the imported module and package initializers Python executes."""

    if imported_name not in modules:
        return set()
    parts = imported_name.split(".")
    return {
        candidate
        for index in range(1, len(parts) + 1)
        if (candidate := ".".join(parts[:index])) in modules
    }


def _module_imports(
    source: PythonModule,
    *,
    modules: dict[str, PythonModule],
) -> frozenset[str]:
    try:
        tree = ast.parse(
            source.path.read_text(encoding="utf-8"),
            filename=str(source.path),
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ImportGraphError(f"cannot parse {source.path}: {exc}") from exc

    dependencies: set[str] = set()
    # ast.walk is intentional: function-local lazy imports and imports guarded
    # by TYPE_CHECKING/try blocks still create architectural graph edges.
    for node in ast.walk(tree):
        imported_names: list[str] = []
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _relative_import_base(source, node)
            if base:
                imported_names.append(base)
            for alias in node.names:
                if alias.name != "*" and base:
                    imported_names.append(f"{base}.{alias.name}")
        else:
            continue
        for imported_name in imported_names:
            dependencies.update(
                _known_module_dependencies(imported_name, modules=modules)
            )
    dependencies.discard(source.name)
    return frozenset(dependencies)


def build_import_graph(
    package_root: pathlib.Path,
    *,
    package_name: str,
) -> dict[str, frozenset[str]]:
    modules = _discover_modules(package_root, package_name=package_name)
    return {
        name: _module_imports(source, modules=modules)
        for name, source in sorted(modules.items())
    }


def _strongly_connected_components(
    graph: dict[str, frozenset[str]],
) -> tuple[tuple[str, ...], ...]:
    next_index = 0
    indexes: dict[str, int] = {}
    low_links: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal next_index
        indexes[module] = next_index
        low_links[module] = next_index
        next_index += 1
        stack.append(module)
        on_stack.add(module)

        for dependency in sorted(graph[module]):
            if dependency not in indexes:
                visit(dependency)
                low_links[module] = min(low_links[module], low_links[dependency])
            elif dependency in on_stack:
                low_links[module] = min(low_links[module], indexes[dependency])

        if low_links[module] != indexes[module]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        components.append(tuple(sorted(component)))

    for module in sorted(graph):
        if module not in indexes:
            visit(module)
    return tuple(sorted(components))


def check_package(
    package_root: pathlib.Path,
    *,
    package_name: str,
) -> ImportGraphReport:
    graph = build_import_graph(package_root, package_name=package_name)
    cycles = tuple(
        ImportCycle(modules=component)
        for component in _strongly_connected_components(graph)
        if len(component) > 1
    )
    return ImportGraphReport(
        module_count=len(graph),
        edge_count=sum(len(dependencies) for dependencies in graph.values()),
        cycles=cycles,
    )


def check() -> ImportGraphReport:
    return check_package(_PACKAGE_ROOT, package_name=_PACKAGE_NAME)


def main() -> int:
    try:
        report = check()
    except ImportGraphError as exc:
        print(f"Python import-SCC guard could not inspect the package: {exc}", file=sys.stderr)
        return 2
    if report.cycles:
        print("Python import-SCC guard found cyclic module components:", file=sys.stderr)
        for cycle in report.cycles:
            print(f"- {cycle.render()}", file=sys.stderr)
        print(
            "Break every cycle at an ownership boundary; this guard has no cycle allowlist.",
            file=sys.stderr,
        )
        return 1
    print(
        "Python import graph is acyclic "
        f"({report.module_count} modules, {report.edge_count} internal edges)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
