"""Typed source projection for an explicit full Web tool-detail read.

The formal contract is in ``docs/contracts/focus-web-wire.zh-CN.md``.  This is
deliberately not a raw-item passthrough: it retains every published field of the
two currently inspectable app-server variants and rejects an unknown/malformed
variant before it crosses the Focus Web boundary.
"""

from __future__ import annotations

from typing import Any


_COMMAND_ACTION_TYPES = frozenset({"read", "listFiles", "search", "unknown"})
_PATCH_CHANGE_TYPES = frozenset({"add", "delete", "update"})


def project_tool_detail_source(
    item: dict[str, Any],
    *,
    change_index: int | None,
) -> dict[str, Any]:
    """Return the complete stable source shape for one exact inspectable item.

    The caller already owns item identity and terminal-state admission.  This
    function owns only stable source-shape validation and deliberately leaves
    all textual fields untouched.
    """

    if not isinstance(item, dict):
        raise ValueError("tool-detail source item must be an object")
    item_type = _require_string(item.get("type"), field="item.type")
    if item_type == "commandExecution":
        if change_index is not None:
            raise ValueError("commandExecution source cannot have a change index")
        return _project_command_execution(item)
    if item_type == "fileChange":
        if (
            not isinstance(change_index, int)
            or isinstance(change_index, bool)
            or change_index < 0
        ):
            raise ValueError("fileChange source requires a non-negative change index")
        return _project_file_change(item, change_index=change_index)
    raise ValueError("tool-detail source supports only known inspectable variants")


def _project_command_execution(item: dict[str, Any]) -> dict[str, Any]:
    raw_actions = item.get("commandActions")
    if not isinstance(raw_actions, list):
        raise ValueError("commandExecution.commandActions must be an array")
    return {
        "type": "commandExecution",
        "id": _require_string(item.get("id"), field="commandExecution.id"),
        "pluginId": _nullable_string(item.get("pluginId"), field="commandExecution.pluginId"),
        "scriptPath": _nullable_string(item.get("scriptPath"), field="commandExecution.scriptPath"),
        "command": _require_string(item.get("command"), field="commandExecution.command"),
        "cwd": _require_string(item.get("cwd"), field="commandExecution.cwd"),
        "processId": _nullable_string(item.get("processId"), field="commandExecution.processId"),
        "source": _require_string(item.get("source"), field="commandExecution.source"),
        "status": _require_string(item.get("status"), field="commandExecution.status"),
        "commandActions": [
            _project_command_action(raw_action, index=index)
            for index, raw_action in enumerate(raw_actions)
        ],
        "aggregatedOutput": _nullable_string(
            item.get("aggregatedOutput"),
            field="commandExecution.aggregatedOutput",
        ),
        "exitCode": _nullable_int(item.get("exitCode"), field="commandExecution.exitCode"),
        "durationMs": _nullable_int(
            item.get("durationMs"),
            field="commandExecution.durationMs",
        ),
    }


def _project_command_action(raw_action: object, *, index: int) -> dict[str, Any]:
    if not isinstance(raw_action, dict):
        raise ValueError(f"commandExecution.commandActions[{index}] must be an object")
    action_type = _require_string(
        raw_action.get("type"),
        field=f"commandExecution.commandActions[{index}].type",
    )
    if action_type not in _COMMAND_ACTION_TYPES:
        raise ValueError("commandExecution source has an unknown CommandAction variant")
    action: dict[str, Any] = {
        "type": action_type,
        "command": _require_string(
            raw_action.get("command"),
            field=f"commandExecution.commandActions[{index}].command",
        ),
    }
    if action_type == "read":
        action["name"] = _require_string(
            raw_action.get("name"),
            field=f"commandExecution.commandActions[{index}].name",
        )
        action["path"] = _require_string(
            raw_action.get("path"),
            field=f"commandExecution.commandActions[{index}].path",
        )
        _require_exact_keys(raw_action, {"type", "command", "name", "path"})
    elif action_type == "listFiles":
        action["path"] = _nullable_string(
            raw_action.get("path"),
            field=f"commandExecution.commandActions[{index}].path",
        )
        _require_exact_keys(raw_action, {"type", "command", "path"})
    elif action_type == "search":
        action["query"] = _nullable_string(
            raw_action.get("query"),
            field=f"commandExecution.commandActions[{index}].query",
        )
        action["path"] = _nullable_string(
            raw_action.get("path"),
            field=f"commandExecution.commandActions[{index}].path",
        )
        _require_exact_keys(raw_action, {"type", "command", "query", "path"})
    else:
        _require_exact_keys(raw_action, {"type", "command"})
    return action


def _project_file_change(
    item: dict[str, Any],
    *,
    change_index: int,
) -> dict[str, Any]:
    raw_changes = item.get("changes")
    if not isinstance(raw_changes, list) or change_index >= len(raw_changes):
        raise ValueError("fileChange source change index is outside the item")
    changes = [
        _project_file_change_entry(raw_change, index=index)
        for index, raw_change in enumerate(raw_changes)
    ]
    return {
        "type": "fileChange",
        "id": _require_string(item.get("id"), field="fileChange.id"),
        "changes": changes,
        "status": _require_string(item.get("status"), field="fileChange.status"),
    }


def _project_file_change_entry(raw_change: object, *, index: int) -> dict[str, Any]:
    if not isinstance(raw_change, dict):
        raise ValueError(f"fileChange.changes[{index}] must be an object")
    _require_exact_keys(raw_change, {"path", "kind", "diff"})
    return {
        "path": _require_string(
            raw_change.get("path"),
            field=f"fileChange.changes[{index}].path",
        ),
        "kind": _project_patch_change_kind(raw_change.get("kind"), index=index),
        "diff": _require_string(
            raw_change.get("diff"),
            field=f"fileChange.changes[{index}].diff",
        ),
    }


def _project_patch_change_kind(raw_kind: object, *, index: int) -> dict[str, Any]:
    if not isinstance(raw_kind, dict):
        raise ValueError(f"fileChange.changes[{index}].kind must be an object")
    change_type = _require_string(
        raw_kind.get("type"),
        field=f"fileChange.changes[{index}].kind.type",
    )
    if change_type not in _PATCH_CHANGE_TYPES:
        raise ValueError("fileChange source has an unknown PatchChangeKind variant")
    if change_type == "update":
        _require_exact_keys(raw_kind, {"type", "move_path"})
        return {
            "type": "update",
            "movePath": _nullable_string(
                raw_kind.get("move_path"),
                field=f"fileChange.changes[{index}].kind.move_path",
            ),
        }
    _require_exact_keys(raw_kind, {"type"})
    return {"type": change_type}


def _require_string(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _nullable_string(value: object, *, field: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{field} must be a string or null")


def _nullable_int(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be an integer or null")


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("tool-detail source variant has an unexpected field shape")
