#!/usr/bin/env python3
"""Navigate reviewed Focus capabilities, changed paths, and Python imports."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence

try:
    from scripts import check_import_cycles, focus_capabilities
except ModuleNotFoundError:  # Direct execution from the scripts directory.
    import check_import_cycles  # type: ignore[no-redef]
    import focus_capabilities  # type: ignore[no-redef]


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PYTHON_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


class NavigationError(ValueError):
    """Raised when a requested navigation target is not current and exact."""


@dataclass(frozen=True, slots=True)
class ModuleNeighborhood:
    module: str
    imports: tuple[str, ...]
    importers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationTargetReference:
    runner: str
    target: str


ReviewedReference = (
    focus_capabilities.SourceReference
    | focus_capabilities.ContractReference
    | VerificationTargetReference
)


@dataclass(frozen=True, slots=True)
class CapabilityPathMatch:
    capability: str
    role: str
    reference: ReviewedReference
    location: focus_capabilities.ReferenceLocation | None = None


@dataclass(frozen=True, slots=True)
class PathImpact:
    path: str
    matches: tuple[CapabilityPathMatch, ...]


_ROLE_ORDER = {
    "entry": 0,
    "owner": 1,
    "contract": 2,
    "focused-test": 3,
    "sentinel": 4,
}


def module_name_from_query(
    query: str,
    *,
    graph: dict[str, frozenset[str]],
    package_name: str = "bot",
) -> str:
    """Resolve a dotted name or repository-relative source path in a live graph."""

    if not query or query.strip() != query or "\0" in query:
        raise NavigationError("module query must be a nonempty trimmed string")
    if "/" not in query and "\\" not in query and not query.endswith(".py"):
        if not _PYTHON_MODULE.fullmatch(query):
            raise NavigationError(f"invalid Python module name: {query!r}")
        module = query
    else:
        relative = pathlib.PurePosixPath(query)
        if (
            relative.is_absolute()
            or "\\" in query
            or ".." in relative.parts
            or relative.as_posix() != query
            or not relative.parts
            or relative.parts[0] != package_name
        ):
            raise NavigationError(
                "module path must be a normalized repository-relative path "
                f"under {package_name}/"
            )
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts.pop()
        elif parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        module = ".".join(parts)
    if module not in graph:
        raise NavigationError(
            f"module is not present in the live import graph: {module}"
        )
    return module


def module_neighborhood(
    query: str,
    *,
    package_root: pathlib.Path = REPO_ROOT / "bot",
    package_name: str = "bot",
) -> ModuleNeighborhood:
    """Compute one-hop imports and importers directly from current Python source."""

    graph = check_import_cycles.build_import_graph(
        pathlib.Path(package_root), package_name=package_name
    )
    module = module_name_from_query(query, graph=graph, package_name=package_name)
    return ModuleNeighborhood(
        module=module,
        imports=tuple(sorted(graph[module])),
        importers=tuple(
            sorted(
                source
                for source, dependencies in graph.items()
                if module in dependencies
            )
        ),
    )


def _module_payload(neighborhood: ModuleNeighborhood) -> dict[str, Any]:
    return {
        "module": neighborhood.module,
        "imports": list(neighborhood.imports),
        "importers": list(neighborhood.importers),
    }


def _normalized_changed_paths(paths: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in paths:
        if (
            not value
            or value.strip() != value
            or "\0" in value
            or "\n" in value
            or "\r" in value
        ):
            raise NavigationError("changed path must be a nonempty trimmed line")
        relative = pathlib.PurePosixPath(value)
        if (
            relative.is_absolute()
            or "\\" in value
            or "::" in value
            or any(character in value for character in "*?[")
            or not relative.parts
            or ".." in relative.parts
            or relative.as_posix() != value
        ):
            raise NavigationError(
                "changed path must be a normalized repository-relative POSIX path "
                "without selectors or globs"
            )
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise NavigationError("changed paths must not contain duplicates")
    return tuple(sorted(normalized))


def _reference_path(reference: ReviewedReference) -> str:
    if isinstance(reference, VerificationTargetReference):
        return reference.target.partition("::")[0]
    return reference.path


def _reference_sort_key(reference: ReviewedReference) -> tuple[str, ...]:
    if isinstance(reference, focus_capabilities.SourceReference):
        return (reference.path, "symbol", reference.symbol)
    if isinstance(reference, focus_capabilities.ContractReference):
        return (reference.path, "heading", reference.heading)
    return (reference.target, "runner", reference.runner)


def _match_sort_key(match: CapabilityPathMatch) -> tuple[Any, ...]:
    return (
        match.capability,
        _ROLE_ORDER[match.role],
        _reference_sort_key(match.reference),
    )


def _reference_location(
    reference: ReviewedReference, *, repo_root: pathlib.Path
) -> focus_capabilities.ReferenceLocation:
    if isinstance(reference, focus_capabilities.SourceReference):
        return focus_capabilities.source_reference_location(
            reference, repo_root=repo_root
        )
    if isinstance(reference, focus_capabilities.ContractReference):
        return focus_capabilities.contract_reference_location(
            reference, repo_root=repo_root
        )
    return focus_capabilities.verification_target_location(
        reference.runner, reference.target, repo_root=repo_root
    )


def _capability_references(
    capability: focus_capabilities.Capability,
) -> tuple[tuple[str, ReviewedReference], ...]:
    references: list[tuple[str, ReviewedReference]] = []
    references.extend(("entry", item) for item in capability.entries)
    references.extend(("owner", item) for item in capability.owners)
    references.extend(("contract", item) for item in capability.contracts)
    for role, groups in (
        ("focused-test", capability.focused_tests),
        ("sentinel", capability.sentinels),
    ):
        for group in groups:
            references.extend(
                (role, VerificationTargetReference(group.runner, target))
                for target in group.targets
            )
    return tuple(references)


def path_impacts(
    paths: Sequence[str],
    *,
    catalog: focus_capabilities.CapabilityCatalog,
    repo_root: pathlib.Path = REPO_ROOT,
    include_locations: bool = False,
) -> tuple[PathImpact, ...]:
    """Reverse-map explicit changed paths to reviewed refs without reading Git."""

    normalized_paths = _normalized_changed_paths(paths)
    root = pathlib.Path(repo_root)
    indexed: list[tuple[str, str, str, ReviewedReference]] = []
    for capability in catalog.capabilities:
        for role, reference in _capability_references(capability):
            reference_path = _reference_path(reference)
            indexed.append((capability.name, role, reference_path, reference))

    impacts: list[PathImpact] = []
    for changed_path in normalized_paths:
        matches: list[CapabilityPathMatch] = []
        seen_matches: set[tuple[str, str, ReviewedReference]] = set()
        for capability, role, reference_path, reference in indexed:
            if changed_path != reference_path:
                continue
            match_key = (capability, role, reference)
            if match_key in seen_matches:
                raise NavigationError(
                    "duplicate reviewed path ref: "
                    f"{capability} {role} {_reference_payload(reference)}"
                )
            seen_matches.add(match_key)
            location = (
                _reference_location(reference, repo_root=root)
                if include_locations
                else None
            )
            matches.append(
                CapabilityPathMatch(
                    capability=capability,
                    role=role,
                    reference=reference,
                    location=location,
                )
            )
        impacts.append(
            PathImpact(
                path=changed_path, matches=tuple(sorted(matches, key=_match_sort_key))
            )
        )
    return tuple(impacts)


def _reference_payload(reference: ReviewedReference) -> dict[str, str]:
    if isinstance(reference, focus_capabilities.SourceReference):
        return {"path": reference.path, "symbol": reference.symbol}
    if isinstance(reference, focus_capabilities.ContractReference):
        return {"path": reference.path, "heading": reference.heading}
    return {"runner": reference.runner, "target": reference.target}


def _path_match_payload(match: CapabilityPathMatch) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "capability": match.capability,
        "role": match.role,
        "ref": _reference_payload(match.reference),
    }
    if match.location is not None:
        payload["location"] = {
            "path": match.location.path,
            "start_line": match.location.start_line,
            "end_line": match.location.end_line,
        }
    return payload


def _paths_payload(impacts: Sequence[PathImpact]) -> dict[str, Any]:
    return {
        "paths": [
            {
                "path": impact.path,
                "matches": [_path_match_payload(item) for item in impact.matches],
                "unmapped": not impact.matches,
            }
            for impact in impacts
        ]
    }


def _render_paths(impacts: Sequence[PathImpact]) -> str:
    lines: list[str] = []
    for impact in impacts:
        if lines:
            lines.append("")
        lines.extend((f"path: {impact.path}", "matches:"))
        if not impact.matches:
            lines.append("- (none)")
        for match in impact.matches:
            reference = _reference_payload(match.reference)
            selector = reference.get("symbol") or reference.get("heading")
            if selector is None:
                rendered_ref = f"{reference['runner']}:{reference['target']}"
            else:
                rendered_ref = f"{reference['path']}#{selector}"
            location = ""
            if match.location is not None:
                location = (
                    f" lines {match.location.start_line}-{match.location.end_line}"
                )
            lines.append(f"- {match.capability} {match.role}: {rendered_ref}{location}")
        lines.append(f"unmapped: {'true' if not impact.matches else 'false'}")
    return "\n".join(lines)


def _render_capability(capability: focus_capabilities.Capability) -> str:
    payload = focus_capabilities.capability_payload(capability)
    lines = [f"capability: {capability.name}"]
    for key in ("entry", "owner", "contract", "focused-test", "sentinel", "guard"):
        lines.append(f"{key}:")
        values = payload[key]
        for value in values:
            if key in {"entry", "owner"}:
                lines.append(f"- {value['path']}#{value['symbol']}")
            elif key == "contract":
                lines.append(f"- {value['path']}#{value['heading']}")
            elif key in {"focused-test", "sentinel"}:
                targets = ", ".join(value["targets"])
                lines.append(f"- {value['runner']}: {targets}")
            else:
                lines.append(f"- {value}")
    return "\n".join(lines)


def _render_module(neighborhood: ModuleNeighborhood) -> str:
    lines = [f"module: {neighborhood.module}", "imports:"]
    lines.extend(f"- {item}" for item in neighborhood.imports)
    if not neighborhood.imports:
        lines.append("- (none)")
    lines.append("importers:")
    lines.extend(f"- {item}" for item in neighborhood.importers)
    if not neighborhood.importers:
        lines.append("- (none)")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Navigate reviewed Focus capabilities and direct Python imports."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List reviewed capability ids.")
    list_parser.add_argument("--json", action="store_true", help="Emit stable JSON.")

    show_parser = subparsers.add_parser("show", help="Show one reviewed capability.")
    show_parser.add_argument("capability")
    show_parser.add_argument("--json", action="store_true", help="Emit stable JSON.")

    module_parser = subparsers.add_parser(
        "module", help="Show live one-hop Python imports and importers."
    )
    module_parser.add_argument(
        "module", help="Dotted bot module or bot/... source path."
    )
    module_parser.add_argument("--json", action="store_true", help="Emit stable JSON.")

    paths_parser = subparsers.add_parser(
        "paths", help="Reverse-map explicit changed paths to reviewed capability refs."
    )
    paths_parser.add_argument(
        "paths", nargs="+", help="Repository-relative changed paths."
    )
    paths_parser.add_argument(
        "--locations",
        action="store_true",
        help="Resolve matched selectors to current source line ranges.",
    )
    paths_parser.add_argument("--json", action="store_true", help="Emit stable JSON.")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "module":
            neighborhood = module_neighborhood(args.module)
            if args.json:
                print(json.dumps(_module_payload(neighborhood), sort_keys=True))
            else:
                print(_render_module(neighborhood))
            return 0

        catalog = focus_capabilities.load_catalog()
        if args.command == "list":
            names = [item.name for item in catalog.capabilities]
            if args.json:
                print(json.dumps({"capabilities": names}, sort_keys=True))
            else:
                print("\n".join(names))
            return 0

        if args.command == "paths":
            impacts = path_impacts(
                args.paths,
                catalog=catalog,
                include_locations=args.locations,
            )
            if args.json:
                print(
                    json.dumps(
                        _paths_payload(impacts), ensure_ascii=False, sort_keys=True
                    )
                )
            else:
                print(_render_paths(impacts))
            return 0

        capability = catalog.require(args.capability)
        if args.json:
            print(
                json.dumps(
                    focus_capabilities.capability_payload(capability),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(_render_capability(capability))
        return 0
    except (
        check_import_cycles.ImportGraphError,
        focus_capabilities.CapabilityMapError,
        NavigationError,
    ) as exc:
        print(f"Focus navigation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
