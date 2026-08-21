#!/usr/bin/env python3
"""Fail-closed upgrade guard for Focus's Codex app-server dependencies.

This is deliberately a maintenance-time guard, not a runtime JSON Schema
validator.  Focus owns its browser DTOs; this tool compares the upstream
app-server surface that Focus consciously depends on with a reviewed compact
baseline.

Generate the input with the same Codex binary that will be upgraded, including
experimental APIs, then run this tool.  See
``docs/contracts/codex-app-server-schema-drift.md`` for the supported workflow.
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import hashlib
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "docs" / "contracts" / "codex-app-server-schema-baseline.json"

_ROOT_SCHEMA_FILES = {
    "client_request": "ClientRequest.json",
    "server_request": "ServerRequest.json",
    "server_notification": "ServerNotification.json",
}
_V2_BUNDLE_FILE = "codex_app_server_protocol.v2.schemas.json"
_GENERAL_BUNDLE_FILE = "codex_app_server_protocol.schemas.json"
_REQUIRED_CLIENT_CLASSES = {
    "shared_operation_mutation",
    "observer_read",
    "connection_local_request",
    "explicit_admin_control_plane",
}
_EXPERIMENTAL_SENTINELS = {
    "client_request": "thread/turns/list",
    "server_request": "currentTime/read",
}
_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY = "fcodex_unscoped_client_request_policy"
_FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME = "_FCODEX_UNSCOPED_ALLOWED_CLIENT_REQUEST_METHODS"
_CODEX_APP_SERVER_ADAPTER_PATH = Path("bot/adapters/codex_app_server.py")
_CODEX_APP_SERVER_ADAPTER_CLASS = "CodexAppServerAdapter"
_CODEX_APP_SERVER_ADAPTER_REQUEST_CALLS = frozenset(
    {
        "_request_turn_start",
        "_request_with_permissions_fallback",
        "_rpc.request",
        "_rpc_request",
    }
)
_CODEX_APP_SERVER_ADAPTER_REVIEWED_FORWARDERS = frozenset(
    {
        ("_request_turn_start", "_request_with_permissions_fallback", "method"),
        ("_request_with_permissions_fallback", "_rpc_request", "method"),
        ("_rpc_request", "_rpc.request", "method"),
    }
)
_NON_SEMANTIC_SCHEMA_KEYS = {
    "$schema",
    "default",
    "description",
    "examples",
    "format",
    "title",
}
_UNORDERED_SCHEMA_ARRAY_KEYS = {"allOf", "anyOf", "enum", "oneOf", "required", "type"}


class GuardInputError(ValueError):
    """The generated schema or reviewed baseline cannot be interpreted safely."""


@dataclass(frozen=True)
class SchemaDocument:
    name: str
    value: dict[str, Any]

    @property
    def definitions(self) -> dict[str, Any]:
        definitions = self.value.get("definitions", {})
        if not isinstance(definitions, dict):
            raise GuardInputError(f"{self.name}: definitions must be an object")
        return definitions


@dataclass(frozen=True)
class SchemaInput:
    root_documents: dict[str, SchemaDocument]
    auxiliary_documents: tuple[SchemaDocument, ...]

    @property
    def all_documents(self) -> tuple[SchemaDocument, ...]:
        return (*self.root_documents.values(), *self.auxiliary_documents)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuardInputError(f"missing generated schema: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GuardInputError(f"invalid JSON schema {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardInputError(f"schema root must be an object: {path}")
    return value


def load_schema_input(schema_dir: Path) -> SchemaInput:
    schema_dir = schema_dir.expanduser().resolve()
    if not schema_dir.is_dir():
        raise GuardInputError(f"schema directory does not exist: {schema_dir}")
    root_documents = {
        direction: SchemaDocument(filename, _read_json(schema_dir / filename))
        for direction, filename in _ROOT_SCHEMA_FILES.items()
    }
    auxiliary_documents = (
        SchemaDocument(_V2_BUNDLE_FILE, _read_json(schema_dir / _V2_BUNDLE_FILE)),
        SchemaDocument(_GENERAL_BUNDLE_FILE, _read_json(schema_dir / _GENERAL_BUNDLE_FILE)),
    )
    return SchemaInput(root_documents=root_documents, auxiliary_documents=auxiliary_documents)


def load_baseline(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if value.get("format_version") != 1:
        raise GuardInputError(
            f"{path}: unsupported or missing format_version; expected 1"
        )
    return value


def _method_branches(document: SchemaDocument) -> dict[str, dict[str, Any]]:
    branches = document.value.get("oneOf")
    if not isinstance(branches, list):
        raise GuardInputError(f"{document.name}: expected top-level oneOf")
    result: dict[str, dict[str, Any]] = {}
    for branch in branches:
        if not isinstance(branch, dict):
            raise GuardInputError(f"{document.name}: request union contains non-object branch")
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            raise GuardInputError(f"{document.name}: request branch has no properties")
        method_schema = properties.get("method")
        if not isinstance(method_schema, dict):
            raise GuardInputError(f"{document.name}: request branch has no method schema")
        methods = method_schema.get("enum")
        if not isinstance(methods, list) or len(methods) != 1 or not isinstance(methods[0], str):
            raise GuardInputError(
                f"{document.name}: expected every request branch to carry one string method enum"
            )
        method = methods[0]
        if method in result:
            raise GuardInputError(f"{document.name}: duplicate method {method!r}")
        result[method] = branch
    return result


def _thread_item_branches(document: SchemaDocument) -> dict[str, dict[str, Any]]:
    thread_item = document.definitions.get("ThreadItem")
    if not isinstance(thread_item, dict):
        raise GuardInputError(f"{document.name}: missing definitions.ThreadItem")
    branches = thread_item.get("oneOf")
    if not isinstance(branches, list):
        raise GuardInputError(f"{document.name}: ThreadItem must be a oneOf union")
    result: dict[str, dict[str, Any]] = {}
    for branch in branches:
        if not isinstance(branch, dict):
            raise GuardInputError(f"{document.name}: ThreadItem branch is not an object")
        properties = branch.get("properties")
        if not isinstance(properties, dict):
            raise GuardInputError(f"{document.name}: ThreadItem branch has no properties")
        type_schema = properties.get("type")
        if not isinstance(type_schema, dict):
            raise GuardInputError(f"{document.name}: ThreadItem branch has no type schema")
        variants = type_schema.get("enum")
        if not isinstance(variants, list) or len(variants) != 1 or not isinstance(variants[0], str):
            raise GuardInputError(
                f"{document.name}: expected every ThreadItem branch to carry one string type enum"
            )
        item_type = variants[0]
        if item_type in result:
            raise GuardInputError(f"{document.name}: duplicate ThreadItem type {item_type!r}")
        result[item_type] = branch
    return result


def _normalize_schema(value: Any, *, parent_key: str | None = None) -> Any:
    """Keep only wire-semantic JSON Schema fields and make ordering stable."""

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            if key in _NON_SEMANTIC_SCHEMA_KEYS:
                continue
            normalized[key] = _normalize_schema(value[key], parent_key=key)
        return normalized
    if isinstance(value, list):
        normalized_items = [_normalize_schema(item, parent_key=parent_key) for item in value]
        if parent_key in _UNORDERED_SCHEMA_ARRAY_KEYS:
            return sorted(normalized_items, key=_canonical_json)
        return normalized_items
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _ref_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    ref = value.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/definitions/"):
        return None
    name = ref.removeprefix("#/definitions/")
    return name or None


def _definition_at(document: SchemaDocument, name: str) -> dict[str, Any] | None:
    """Resolve the JSON Pointer tail after ``#/definitions/``.

    The regular root files use flat definitions.  The generated aggregate
    bundle additionally has namespaced definitions such as
    ``#/definitions/v2/ThreadStartResponse``; treating that tail as a single
    dictionary key would silently miss the response half of the contract.
    """

    current: Any = document.definitions
    for raw_segment in name.split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
    return current if isinstance(current, dict) else None


def _resolve_definition(schema_input: SchemaInput, document: SchemaDocument, name: str) -> tuple[SchemaDocument, dict[str, Any]]:
    local = _definition_at(document, name)
    if local is not None:
        return document, local
    candidates: list[tuple[SchemaDocument, dict[str, Any]]] = []
    for candidate_document in schema_input.all_documents:
        candidate = _definition_at(candidate_document, name)
        if candidate is not None:
            candidates.append((candidate_document, candidate))
    if not candidates:
        raise GuardInputError(f"{document.name}: unresolved schema definition {name!r}")
    normalized = _normalize_schema(candidates[0][1])
    if any(_normalize_schema(candidate) != normalized for _, candidate in candidates[1:]):
        locations = ", ".join(source.name for source, _ in candidates)
        raise GuardInputError(
            f"ambiguous schema definition {name!r} differs across generated documents: {locations}"
        )
    return candidates[0]


def _collect_local_refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        refs = set()
        name = _ref_name(value)
        if name:
            refs.add(name)
        for nested in value.values():
            refs.update(_collect_local_refs(nested))
        return refs
    if isinstance(value, list):
        refs: set[str] = set()
        for nested in value:
            refs.update(_collect_local_refs(nested))
        return refs
    return set()


def _closure(schema_input: SchemaInput, document: SchemaDocument, root: Any) -> dict[str, Any]:
    """Return a canonical root plus every referenced definition reachable from it."""

    pending: list[tuple[SchemaDocument, str]] = [(document, name) for name in _collect_local_refs(root)]
    visited: set[str] = set()
    definitions: dict[str, Any] = {}
    while pending:
        source, name = pending.pop()
        if name in visited:
            continue
        resolved_source, definition = _resolve_definition(schema_input, source, name)
        visited.add(name)
        definitions[name] = _normalize_schema(definition)
        pending.extend((resolved_source, child) for child in _collect_local_refs(definition))
    return {
        "root": _normalize_schema(root),
        "definitions": {name: definitions[name] for name in sorted(definitions)},
    }


def _resolve_top_level_schema(schema_input: SchemaInput, document: SchemaDocument, value: Any) -> tuple[SchemaDocument, Any, str | None]:
    name = _ref_name(value)
    if name is None:
        return document, value, None
    source, definition = _resolve_definition(schema_input, document, name)
    return source, definition, name


def _method_signature(schema_input: SchemaInput, document: SchemaDocument, branch: dict[str, Any]) -> dict[str, Any]:
    properties = branch.get("properties")
    if not isinstance(properties, dict):
        raise GuardInputError(f"{document.name}: method branch has no properties")
    params = properties.get("params", {})
    source, resolved_params, params_ref = _resolve_top_level_schema(schema_input, document, params)
    if not isinstance(resolved_params, dict):
        raise GuardInputError(f"{document.name}: params schema must be an object")
    closure = _closure(schema_input, source, resolved_params)
    return {
        "params_definition": params_ref,
        "params": _normalize_schema(resolved_params),
        "closure_sha256": _digest(closure),
    }


def _definition_signature(schema_input: SchemaInput, document: SchemaDocument, root_name: str) -> dict[str, Any]:
    source, definition = _resolve_definition(schema_input, document, root_name)
    closure = _closure(schema_input, source, definition)
    return {
        "root_definition": root_name,
        "schema": _normalize_schema(definition),
        "closure_sha256": _digest(closure),
    }


def _method_classification(baseline: Mapping[str, Any], direction: str) -> dict[str, list[str]]:
    all_classifications = baseline.get("method_classification")
    if not isinstance(all_classifications, dict):
        raise GuardInputError("baseline method_classification must be an object")
    classifications = all_classifications.get(direction, {})
    if not isinstance(classifications, dict):
        raise GuardInputError(f"baseline method_classification.{direction} must be an object")
    result: dict[str, list[str]] = {}
    for category, methods in classifications.items():
        if not isinstance(category, str) or not isinstance(methods, list) or not all(
            isinstance(method, str) for method in methods
        ):
            raise GuardInputError(
                f"baseline method_classification.{direction}.{category} must be a list of strings"
            )
        result[category] = list(methods)
    return result


def _classified_methods(baseline: Mapping[str, Any], direction: str) -> set[str]:
    classifications = _method_classification(baseline, direction)
    methods: set[str] = set()
    duplicates: set[str] = set()
    for category_methods in classifications.values():
        for method in category_methods:
            if method in methods:
                duplicates.add(method)
            methods.add(method)
    if duplicates:
        raise GuardInputError(
            f"baseline method_classification.{direction} classifies methods more than once: "
            + ", ".join(sorted(duplicates))
        )
    return methods


def _item_classification(baseline: Mapping[str, Any]) -> dict[str, list[str]]:
    classifications = baseline.get("thread_item_classification")
    if not isinstance(classifications, dict):
        raise GuardInputError("baseline thread_item_classification must be an object")
    result: dict[str, list[str]] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    for category, items in classifications.items():
        if not isinstance(category, str) or not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise GuardInputError(
                f"baseline thread_item_classification.{category} must be a list of strings"
            )
        result[category] = list(items)
        for item in items:
            if item in seen:
                duplicates.add(item)
            seen.add(item)
    if duplicates:
        raise GuardInputError(
            "baseline thread_item_classification classifies item types more than once: "
            + ", ".join(sorted(duplicates))
        )
    return result


def _manual_required_methods(baseline: Mapping[str, Any], direction: str) -> set[str]:
    declared = baseline.get("manual_required_methods", {})
    if not isinstance(declared, dict):
        raise GuardInputError("baseline manual_required_methods must be an object")
    methods = declared.get(direction, [])
    if not isinstance(methods, list) or not all(isinstance(method, str) for method in methods):
        raise GuardInputError(f"baseline manual_required_methods.{direction} must be a list of strings")
    return set(methods)


def _literal_strings_under(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise GuardInputError(f"cannot statically inspect {path}: {exc}") from exc
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _focus_source_literals(source_root: Path) -> set[str]:
    bot_root = source_root / "bot"
    if not bot_root.is_dir():
        raise GuardInputError(f"Focus source root has no bot directory: {source_root}")
    literals: set[str] = set()
    for path in sorted(bot_root.rglob("*.py")):
        literals.update(_literal_strings_under(path))
    return literals


def _assignment_target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {
            name
            for item in target.elts
            for name in _assignment_target_names(item)
        }
    return set()


def _function_local_assignments(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[ast.AST | None]]:
    """Collect assignments in one function without crossing nested scopes."""

    assignments: dict[str, list[ast.AST | None]] = {}

    def record(target: ast.AST, value: ast.AST | None) -> None:
        for name in _assignment_target_names(target):
            assignments.setdefault(name, []).append(value)

    class AssignmentVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return None

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return None

        def visit_ListComp(self, node: ast.ListComp) -> None:
            return None

        def visit_SetComp(self, node: ast.SetComp) -> None:
            return None

        def visit_DictComp(self, node: ast.DictComp) -> None:
            return None

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            return None

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                record(target, node.value)
            self.visit(node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            record(node.target, node.value)
            if node.value is not None:
                self.visit(node.value)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            record(node.target, node.value)
            self.visit(node.value)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                assignments.setdefault(node.id, []).append(None)

    visitor = AssignmentVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return assignments


def _function_parameter_names(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    arguments = function.args
    names = {
        argument.arg
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _function_calls(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    """Collect calls in one function without attributing nested scopes to it."""

    calls: list[ast.Call] = []

    class CallVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return None

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return None

        def visit_ListComp(self, node: ast.ListComp) -> None:
            return None

        def visit_SetComp(self, node: ast.SetComp) -> None:
            return None

        def visit_DictComp(self, node: ast.DictComp) -> None:
            return None

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
            return None

        def visit_Call(self, node: ast.Call) -> None:
            calls.append(node)
            self.generic_visit(node)

    visitor = CallVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return calls


def _adapter_request_call_name(call: ast.Call) -> str | None:
    function = call.func
    if not isinstance(function, ast.Attribute):
        return None
    if isinstance(function.value, ast.Name) and function.value.id == "self":
        return function.attr if function.attr in _CODEX_APP_SERVER_ADAPTER_REQUEST_CALLS else None
    if (
        function.attr == "request"
        and isinstance(function.value, ast.Attribute)
        and isinstance(function.value.value, ast.Name)
        and function.value.value.id == "self"
        and function.value.attr == "_rpc"
    ):
        return "_rpc.request"
    return None


def _adapter_request_attribute_name(attribute: ast.Attribute) -> str | None:
    if isinstance(attribute.value, ast.Name) and attribute.value.id == "self":
        return (
            attribute.attr
            if attribute.attr in _CODEX_APP_SERVER_ADAPTER_REQUEST_CALLS
            else None
        )
    if (
        attribute.attr == "request"
        and isinstance(attribute.value, ast.Attribute)
        and isinstance(attribute.value.value, ast.Name)
        and attribute.value.value.id == "self"
        and attribute.value.attr == "_rpc"
    ):
        return "_rpc.request"
    return None


def _adapter_getattr_request_sink(call: ast.Call) -> str | None:
    if (
        not isinstance(call.func, ast.Name)
        or call.func.id != "getattr"
        or len(call.args) < 2
    ):
        return None
    target = call.args[0]
    attribute = call.args[1]
    if isinstance(target, ast.Name) and target.id == "self":
        allowed_names = _CODEX_APP_SERVER_ADAPTER_REQUEST_CALLS - {"_rpc.request"}
        target_name = "self"
    elif (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
        and target.attr == "_rpc"
    ):
        allowed_names = frozenset({"request"})
        target_name = "self._rpc"
    else:
        return None
    if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
        return (
            f"{target_name}.{attribute.value}"
            if attribute.value in allowed_names
            else None
        )
    return f"{target_name}.<dynamic>"


def _reject_indirect_adapter_request_sinks(
    path: Path,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> None:
    nodes = tuple(ast.walk(function))
    parents = {
        id(child): parent
        for parent in nodes
        for child in ast.iter_child_nodes(parent)
    }
    for node in nodes:
        if isinstance(node, ast.Attribute):
            sink_name = _adapter_request_attribute_name(node)
            if sink_name is None:
                continue
            parent = parents.get(id(node))
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            raise GuardInputError(
                f"{path}:{node.lineno}: {sink_name} cannot be read or bound "
                "indirectly; call the reviewed request sink directly"
            )
        if isinstance(node, ast.Call):
            sink_name = _adapter_getattr_request_sink(node)
            if sink_name is not None:
                raise GuardInputError(
                    f"{path}:{node.lineno}: {sink_name} cannot be obtained through "
                    "getattr; call a reviewed request sink directly"
                )


def _codex_app_server_adapter_outbound_methods(source_root: Path) -> set[str]:
    """Return statically proven ClientRequest methods emitted by the adapter."""

    path = source_root / _CODEX_APP_SERVER_ADAPTER_PATH
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise GuardInputError(f"cannot statically inspect {path}: {exc}") from exc
    adapter_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == _CODEX_APP_SERVER_ADAPTER_CLASS
    ]
    if len(adapter_classes) != 1:
        raise GuardInputError(
            f"{path}: expected exactly one {_CODEX_APP_SERVER_ADAPTER_CLASS} class"
        )

    methods: set[str] = set()
    for function in adapter_classes[0].body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        _reject_indirect_adapter_request_sinks(path, function)
        assignments = _function_local_assignments(function)
        parameter_names = _function_parameter_names(function)
        direct_calls = _function_calls(function)
        direct_call_ids = {id(call) for call in direct_calls}
        for nested_call in (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and id(node) not in direct_call_ids
        ):
            nested_call_name = _adapter_request_call_name(nested_call)
            if nested_call_name is not None:
                raise GuardInputError(
                    f"{path}:{nested_call.lineno}: {nested_call_name} in a nested "
                    "scope cannot prove one adapter outbound method"
                )
        for call in direct_calls:
            call_name = _adapter_request_call_name(call)
            if call_name is None:
                continue
            if not call.args:
                raise GuardInputError(
                    f"{path}:{call.lineno}: {call_name} must receive a positional literal method"
                )
            argument = call.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                methods.add(argument.value)
                continue
            if isinstance(argument, ast.Name):
                values = assignments.get(argument.id, [])
                if (
                    argument.id not in parameter_names
                    and len(values) == 1
                    and isinstance(values[0], ast.Constant)
                    and isinstance(values[0].value, str)
                ):
                    methods.add(values[0].value)
                    continue
                if (
                    (
                        function.name,
                        call_name,
                        argument.id,
                    )
                    in _CODEX_APP_SERVER_ADAPTER_REVIEWED_FORWARDERS
                    and argument.id in parameter_names
                    and not values
                ):
                    continue
            raise GuardInputError(
                f"{path}:{call.lineno}: cannot prove {call_name} method as one literal or "
                "a reviewed helper parameter forwarding"
            )
    return methods


def _pinned_client_request_methods(baseline: Mapping[str, Any]) -> set[str]:
    inventory = baseline.get("method_inventory")
    if not isinstance(inventory, dict):
        raise GuardInputError("baseline method_inventory must be an object")
    methods = inventory.get("client_request")
    if not isinstance(methods, list) or not all(isinstance(method, str) for method in methods):
        raise GuardInputError("baseline method_inventory.client_request must be a list of strings")
    duplicates = {method for method in methods if methods.count(method) > 1}
    if duplicates:
        raise GuardInputError(
            "baseline method_inventory.client_request repeats methods: "
            + ", ".join(sorted(duplicates))
        )
    return set(methods)


def _thread_targeted_client_methods(
    schema_input: SchemaInput, branches: Mapping[str, dict[str, Any]]
) -> set[str]:
    document = schema_input.root_documents["client_request"]
    thread_targeted: set[str] = set()
    for method, branch in branches.items():
        properties = branch.get("properties", {})
        if not isinstance(properties, dict):
            continue
        params = properties.get("params")
        source, resolved_params, _ = _resolve_top_level_schema(schema_input, document, params)
        if not isinstance(resolved_params, dict):
            continue
        # The current app-server method convention puts threadId directly in
        # params.  Do not recursively search arbitrary nested payloads: that
        # would accidentally classify unrelated global methods whose optional
        # filters happen to include a thread id.
        if "threadId" in (resolved_params.get("properties") or {}):
            thread_targeted.add(method)
    return thread_targeted


def _required_thread_targeted_client_methods(
    schema_input: SchemaInput, branches: Mapping[str, dict[str, Any]]
) -> set[str]:
    """Return methods whose direct ``threadId`` is required by the schema."""

    document = schema_input.root_documents["client_request"]
    required_thread_targets: set[str] = set()
    for method, branch in branches.items():
        properties = branch.get("properties", {})
        if not isinstance(properties, dict):
            continue
        params = properties.get("params")
        _source, resolved_params, _ = _resolve_top_level_schema(schema_input, document, params)
        if not isinstance(resolved_params, dict):
            continue
        parameter_properties = resolved_params.get("properties")
        required = resolved_params.get("required")
        if (
            isinstance(parameter_properties, dict)
            and "threadId" in parameter_properties
            and isinstance(required, list)
            and "threadId" in required
        ):
            required_thread_targets.add(method)
    return required_thread_targets


def _fcodex_unscoped_client_request_policy(baseline: Mapping[str, Any]) -> set[str]:
    """Read the reviewed default-deny fcodex no-thread target allowlist."""

    policy = baseline.get(_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY)
    if not isinstance(policy, dict):
        raise GuardInputError(
            f"baseline {_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY} must be an object"
        )
    if policy.get("default_action") != "deny":
        raise GuardInputError(
            f"baseline {_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.default_action must be 'deny'"
        )
    allowed = policy.get("allowed")
    if not isinstance(allowed, list) or not all(isinstance(method, str) for method in allowed):
        raise GuardInputError(
            f"baseline {_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed must be a list of strings"
        )
    duplicates = {method for method in allowed if allowed.count(method) > 1}
    if duplicates:
        raise GuardInputError(
            f"baseline {_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed repeats methods: "
            + ", ".join(sorted(duplicates))
        )
    return set(allowed)


def _literal_string_collection(value: ast.AST, *, path: Path) -> set[str]:
    """Read a literal set/list/tuple, or ``frozenset`` around one, from Python."""

    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and not value.keywords
    ):
        value = value.args[0]
    if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        raise GuardInputError(
            f"{path}: {_FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME} must be a literal set/list/tuple"
        )
    methods: list[str] = []
    for item in value.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise GuardInputError(
                f"{path}: {_FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME} must contain only string literals"
            )
        methods.append(item.value)
    duplicates = {method for method in methods if methods.count(method) > 1}
    if duplicates:
        raise GuardInputError(
            f"{path}: {_FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME} repeats methods: "
            + ", ".join(sorted(duplicates))
        )
    return set(methods)


def _fcodex_proxy_unscoped_allowlist(source_root: Path) -> set[str] | None:
    """Return the proxy constant when the inspected source tree contains it.

    Unit fixtures intentionally only include a tiny synthetic ``bot`` tree, so
    a missing proxy file means this optional implementation-consistency check
    is not applicable there.  In the real repository a missing/ambiguous
    constant is itself a guard input error.
    """

    path = source_root / "bot" / "fcodex" / "proxy.py"
    if not path.is_file():
        return None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise GuardInputError(f"cannot statically inspect {path}: {exc}") from exc

    values: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name)
                and target.id == _FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME
                for target in node.targets
            ):
                values.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == _FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME
                and node.value is not None
            ):
                values.append(node.value)
    if len(values) != 1:
        raise GuardInputError(
            f"{path}: expected exactly one {_FCODEX_PROXY_UNSCOPED_ALLOWLIST_NAME} assignment"
        )
    return _literal_string_collection(values[0], path=path)


def _validate_policy(
    schema_input: SchemaInput,
    baseline: Mapping[str, Any],
    *,
    source_root: Path,
) -> list[str]:
    errors: list[str] = []
    branches = {
        direction: _method_branches(document)
        for direction, document in schema_input.root_documents.items()
    }
    for direction, sentinel in _EXPERIMENTAL_SENTINELS.items():
        if sentinel not in branches[direction]:
            errors.append(
                "schema input is not the required experimental surface: "
                f"{direction} is missing {sentinel!r}; regenerate with "
                "`codex app-server generate-json-schema --experimental`"
            )

    client_categories = _method_classification(baseline, "client_request")
    missing_categories = _REQUIRED_CLIENT_CLASSES - set(client_categories)
    if missing_categories:
        errors.append(
            "client request classification is missing required categories: "
            + ", ".join(sorted(missing_categories))
        )

    adapter_outbound_methods: set[str] = set()
    try:
        adapter_outbound_methods = _codex_app_server_adapter_outbound_methods(source_root)
        pinned_client_methods = _pinned_client_request_methods(baseline)
    except GuardInputError as exc:
        errors.append(str(exc))
    else:
        unsupported_adapter_methods = adapter_outbound_methods - pinned_client_methods
        if unsupported_adapter_methods:
            errors.append(
                "CodexAppServerAdapter emits client request methods absent from the pinned "
                "official ClientRequest inventory: "
                + ", ".join(sorted(unsupported_adapter_methods))
            )

    source_literals = _focus_source_literals(source_root)
    for direction, direction_branches in branches.items():
        try:
            classified = _classified_methods(baseline, direction)
        except GuardInputError as exc:
            errors.append(str(exc))
            continue
        unknown_classified = classified - set(direction_branches)
        if unknown_classified:
            errors.append(
                f"{direction} classification names methods absent from generated schema: "
                + ", ".join(sorted(unknown_classified))
            )
        used_by_focus = {
            method
            for method in source_literals & set(direction_branches)
            # Bare values such as `error` and `warning` are common UI states.
            # They are listed explicitly in manual_required_methods instead.
            if "/" in method
        } | _manual_required_methods(baseline, direction)
        if direction == "client_request":
            used_by_focus.update(adapter_outbound_methods)
        unclassified = used_by_focus - classified
        if unclassified:
            errors.append(
                f"Focus uses {direction} methods without a reviewed classification: "
                + ", ".join(sorted(unclassified))
            )

    client_classified = _classified_methods(baseline, "client_request")
    unclassified_thread_targets = _thread_targeted_client_methods(
        schema_input, branches["client_request"]
    ) - client_classified
    if unclassified_thread_targets:
        errors.append(
            "generated client methods with direct threadId lack an explicit classification "
            "(new thread-scoped mutations must remain fail-closed): "
            + ", ".join(sorted(unclassified_thread_targets))
        )

    try:
        fcodex_unscoped_allowed = _fcodex_unscoped_client_request_policy(baseline)
    except GuardInputError as exc:
        errors.append(str(exc))
        fcodex_unscoped_allowed = set()
    else:
        client_methods = set(branches["client_request"])
        unknown_allowed = fcodex_unscoped_allowed - client_methods
        if unknown_allowed:
            errors.append(
                f"{_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed names methods absent "
                "from generated schema: "
                + ", ".join(sorted(unknown_allowed))
            )
        required_thread_targets = _required_thread_targeted_client_methods(
            schema_input, branches["client_request"]
        )
        invalid_allowed = fcodex_unscoped_allowed & required_thread_targets
        if invalid_allowed:
            errors.append(
                f"{_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed includes methods "
                "whose direct threadId is required: "
                + ", ".join(sorted(invalid_allowed))
            )
        unreviewed_allowed = fcodex_unscoped_allowed - (
            client_classified | _manual_required_methods(baseline, "client_request")
        )
        if unreviewed_allowed:
            errors.append(
                f"{_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed includes methods "
                "without a reviewed client classification: "
                + ", ".join(sorted(unreviewed_allowed))
            )
        try:
            proxy_allowed = _fcodex_proxy_unscoped_allowlist(source_root)
        except GuardInputError as exc:
            errors.append(str(exc))
        else:
            if proxy_allowed is not None and proxy_allowed != fcodex_unscoped_allowed:
                errors.append(
                    "fcodex proxy unscoped client-request allowlist differs from reviewed "
                    f"{_FCODEX_UNSCOPED_CLIENT_REQUEST_POLICY_KEY}.allowed; "
                    "new global methods must default to local deny until explicitly reviewed "
                    f"(baseline: {', '.join(sorted(fcodex_unscoped_allowed)) or '<empty>'}; "
                    f"proxy: {', '.join(sorted(proxy_allowed)) or '<empty>'})"
                )

    item_branches = _thread_item_branches(schema_input.auxiliary_documents[0])
    try:
        item_categories = _item_classification(baseline)
    except GuardInputError as exc:
        errors.append(str(exc))
        item_categories = {}
    classified_items = {item for items in item_categories.values() for item in items}
    unknown_items = classified_items - set(item_branches)
    if unknown_items:
        errors.append(
            "thread item classification names types absent from generated schema: "
            + ", ".join(sorted(unknown_items))
        )
    source_item_types = source_literals & set(item_branches)
    unclassified_items = source_item_types - classified_items
    if unclassified_items:
        errors.append(
            "Focus references ThreadItem types without a reviewed classification: "
            + ", ".join(sorted(unclassified_items))
        )
    return errors


def _response_root_mapping(baseline: Mapping[str, Any], direction: str) -> dict[str, str]:
    all_mappings = baseline.get("response_roots", {})
    if not isinstance(all_mappings, dict):
        raise GuardInputError("baseline response_roots must be an object")
    mapping = all_mappings.get(direction, {})
    if not isinstance(mapping, dict) or not all(
        isinstance(method, str) and isinstance(root, str) for method, root in mapping.items()
    ):
        raise GuardInputError(f"baseline response_roots.{direction} must map strings to strings")
    return dict(mapping)


def build_generated_baseline_fields(
    schema_input: SchemaInput,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the generated portion of a reviewed baseline.

    The result intentionally stores an inventory and compact semantic
    fingerprints, rather than vendoring several megabytes of raw generated
    files.  A fingerprint covers the complete reachable definition closure;
    its adjacent direct schema is retained to make a failure actionable.
    """

    branches = {
        direction: _method_branches(document)
        for direction, document in schema_input.root_documents.items()
    }
    item_branches = _thread_item_branches(schema_input.auxiliary_documents[0])
    focused_methods = {
        direction: sorted(_classified_methods(baseline, direction))
        for direction in _ROOT_SCHEMA_FILES
    }
    focused_schema: dict[str, Any] = {}
    for direction, methods in focused_methods.items():
        document = schema_input.root_documents[direction]
        missing = set(methods) - set(branches[direction])
        if missing:
            raise GuardInputError(
                f"cannot snapshot {direction}; classified methods missing from generated schema: "
                + ", ".join(sorted(missing))
            )
        focused_schema[direction] = {
            method: _method_signature(schema_input, document, branches[direction][method])
            for method in methods
        }

    response_signatures: dict[str, dict[str, Any]] = {}
    for direction in ("client_request", "server_request"):
        mapping = _response_root_mapping(baseline, direction)
        unknown_methods = set(mapping) - set(branches[direction])
        if unknown_methods:
            raise GuardInputError(
                f"response_roots.{direction} references unavailable methods: "
                + ", ".join(sorted(unknown_methods))
            )
        unclassified = set(mapping) - set(focused_methods[direction])
        if unclassified:
            raise GuardInputError(
                f"response_roots.{direction} requires a method classification first: "
                + ", ".join(sorted(unclassified))
            )
        response_signatures[direction] = {
            method: _definition_signature(schema_input, schema_input.auxiliary_documents[0], root)
            for method, root in sorted(mapping.items())
        }

    focused_items = sorted(
        item for items in _item_classification(baseline).values() for item in items
    )
    missing_items = set(focused_items) - set(item_branches)
    if missing_items:
        raise GuardInputError(
            "cannot snapshot ThreadItem types missing from generated schema: "
            + ", ".join(sorted(missing_items))
        )
    item_document = schema_input.auxiliary_documents[0]
    focused_schema["thread_item"] = {
        item: {
            "schema": _normalize_schema(item_branches[item]),
            "closure_sha256": _digest(_closure(schema_input, item_document, item_branches[item])),
        }
        for item in focused_items
    }
    focused_schema["responses"] = response_signatures

    return {
        "method_inventory": {
            direction: sorted(direction_branches)
            for direction, direction_branches in branches.items()
        },
        "thread_item_inventory": sorted(item_branches),
        "focused_schema": focused_schema,
    }


def build_updated_baseline(
    schema_input: SchemaInput,
    baseline: Mapping[str, Any],
    *,
    upstream_commit: str | None = None,
    generator: str | None = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(dict(baseline))
    updated.update(build_generated_baseline_fields(schema_input, updated))
    upstream = updated.get("upstream")
    if not isinstance(upstream, dict):
        raise GuardInputError("baseline upstream must be an object")
    if upstream_commit:
        upstream["commit"] = upstream_commit
    if generator:
        upstream["generator"] = generator
    return updated


def _unified_json_diff(expected: Any, actual: Any, *, label: str, max_lines: int) -> str:
    expected_lines = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    actual_lines = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    lines = list(
        difflib.unified_diff(
            expected_lines,
            actual_lines,
            fromfile=f"baseline/{label}",
            tofile=f"generated/{label}",
            lineterm="",
        )
    )
    if len(lines) > max_lines:
        lines = [
            *lines[:max_lines],
            f"... diff truncated after {max_lines} lines; regenerate and inspect raw schema artifacts ...",
        ]
    return "\n".join(lines)


def check(
    schema_dir: Path,
    baseline_path: Path = DEFAULT_BASELINE,
    *,
    source_root: Path = REPO_ROOT,
    max_diff_lines: int = 180,
) -> tuple[list[str], dict[str, Any]]:
    schema_input = load_schema_input(schema_dir)
    baseline = load_baseline(baseline_path)
    errors = _validate_policy(schema_input, baseline, source_root=source_root)
    try:
        generated = build_generated_baseline_fields(schema_input, baseline)
    except GuardInputError as exc:
        errors.append(str(exc))
        return errors, {}
    for field in ("method_inventory", "thread_item_inventory", "focused_schema"):
        expected = baseline.get(field)
        actual = generated[field]
        if expected != actual:
            errors.append(
                f"upstream app-server drift in {field}:\n"
                + _unified_json_diff(
                    expected,
                    actual,
                    label=field,
                    max_lines=max_diff_lines,
                )
            )
    return errors, generated


def _parse_args(argv: Iterable[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-dir",
        type=Path,
        required=True,
        help="directory created by `codex app-server generate-json-schema --experimental`",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"reviewed baseline (default: {DEFAULT_BASELINE})",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=REPO_ROOT,
        help=f"Focus repository root for literal coverage scan (default: {REPO_ROOT})",
    )
    parser.add_argument(
        "--max-diff-lines",
        type=int,
        default=180,
        help="maximum rendered lines per generated diff (default: 180)",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="explicitly replace generated baseline fields after policy validation",
    )
    parser.add_argument(
        "--upstream-commit",
        help="required with --write-baseline; commit reviewed for this update",
    )
    parser.add_argument(
        "--generator",
        help="optional exact generator label recorded with --write-baseline",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.max_diff_lines <= 0:
        print("error: --max-diff-lines must be positive", file=sys.stderr)
        return 2
    if args.write_baseline and not args.upstream_commit:
        print("error: --write-baseline requires --upstream-commit", file=sys.stderr)
        return 2
    try:
        schema_input = load_schema_input(args.schema_dir)
        baseline = load_baseline(args.baseline)
        policy_errors = _validate_policy(schema_input, baseline, source_root=args.source_root)
        if policy_errors:
            print("Codex app-server drift guard FAILED:", file=sys.stderr)
            for error in policy_errors:
                print(f"\n- {error}", file=sys.stderr)
            return 1
        if args.write_baseline:
            updated = build_updated_baseline(
                schema_input,
                baseline,
                upstream_commit=args.upstream_commit,
                generator=args.generator,
            )
            target = args.baseline.expanduser().resolve()
            target.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"Wrote reviewed-candidate baseline to {target}. Inspect the diff before committing."
            )
            return 0
        errors, _generated = check(
            args.schema_dir,
            args.baseline,
            source_root=args.source_root,
            max_diff_lines=args.max_diff_lines,
        )
    except GuardInputError as exc:
        print(f"Codex app-server drift guard input error: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("Codex app-server drift guard FAILED:", file=sys.stderr)
        for error in errors:
            print(f"\n- {error}", file=sys.stderr)
        return 1
    print("Codex app-server drift guard passed.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
