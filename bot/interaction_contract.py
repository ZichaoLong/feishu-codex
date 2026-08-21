"""Shared Codex server-request projection and response contract."""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from typing import Any
from urllib.parse import urlparse


COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
SHARED_APPROVAL_METHODS = frozenset(
    {COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL}
)
USER_INPUT = "item/tool/requestUserInput"
MCP_ELICITATION = "mcpServer/elicitation/request"
INTERACTIVE_SERVER_REQUEST_METHODS = frozenset(
    {
        *SHARED_APPROVAL_METHODS,
        USER_INPUT,
        MCP_ELICITATION,
        "item/tool/call",
    }
)
CURRENT_TIME_READ = "currentTime/read"


def automatic_server_request_response(
    method: str,
    params: Any,
    *,
    current_time_at: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None] | None:
    """Answer reviewed protocol utilities which require no frontend owner.

    ``currentTime/read`` is an app-server protocol callback, not a user
    interaction and not authority to mutate a main turn. Keeping it out
    of the approval/question router avoids manufacturing a writer fence while
    still supporting upstream's external-clock mode.  ``None`` means the
    method is not an automatic utility and must continue through the ordinary
    fail-closed server-request classifier.
    """

    normalized_method = str(method or "").strip()
    if normalized_method != CURRENT_TIME_READ:
        return None
    if (
        not isinstance(params, dict)
        or set(params) != {"threadId"}
        or not isinstance(params.get("threadId"), str)
        or params["threadId"] != params["threadId"].strip()
        or not params["threadId"]
    ):
        return None, {
            "code": -32602,
            "message": "currentTime/read requires exactly one non-empty threadId",
        }
    timestamp = int(time.time()) if current_time_at is None else current_time_at
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        return None, {
            "code": -32603,
            "message": "Focus could not read a valid Unix timestamp",
        }
    return {"currentTimeAt": timestamp}, None


def normalize_interaction_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    normalized_method = str(method or "").strip()
    raw = dict(params) if isinstance(params, dict) else {}
    if normalized_method == COMMAND_APPROVAL:
        actions = _command_actions(raw.get("availableDecisions"))
        return {
            "kind": "approval",
            "title": "Command approval",
            "params": {
                key: value
                for key, value in raw.items()
                if key not in {"threadId", "turnId"}
            },
            "actions": actions,
            "presentable": bool(actions),
        }
    if normalized_method == FILE_APPROVAL:
        return {
            "kind": "approval",
            "title": "File change approval",
            "params": {
                key: value
                for key, value in raw.items()
                if key not in {"threadId", "turnId"}
            },
            "actions": [
                _action("approve_once", "Allow once", {"decision": "accept"}, "primary"),
                _action(
                    "approve_session",
                    "Allow for session",
                    {"decision": "acceptForSession"},
                    "secondary",
                ),
                _action("reject", "Decline", {"decision": "decline"}, "danger"),
                _action("cancel", "Cancel turn", {"decision": "cancel"}, "danger"),
            ],
            "presentable": True,
        }
    if normalized_method == PERMISSIONS_APPROVAL:
        permissions = raw.get("permissions") if isinstance(raw.get("permissions"), dict) else {}
        return {
            "kind": "approval",
            "title": "Additional permissions",
            "params": {
                key: value
                for key, value in raw.items()
                if key not in {"threadId", "turnId"}
            },
            "actions": [
                _action(
                    "approve_once",
                    "Allow once",
                    {"permissions": permissions, "scope": "turn"},
                    "primary",
                ),
                _action(
                    "approve_strict_auto_review",
                    "Allow for turn with strict auto review",
                    {
                        "permissions": permissions,
                        "scope": "turn",
                        "strictAutoReview": True,
                    },
                    "secondary",
                ),
                _action(
                    "approve_session",
                    "Allow for session",
                    {"permissions": permissions, "scope": "session"},
                    "secondary",
                ),
                _action(
                    "reject",
                    "Decline",
                    {"permissions": {}, "scope": "turn"},
                    "danger",
                ),
            ],
            "presentable": True,
        }
    if normalized_method == USER_INPUT:
        questions = raw.get("questions") if isinstance(raw.get("questions"), list) else []
        normalized_questions = [
            _normalize_user_question(question)
            for question in questions
            if isinstance(question, dict)
        ]
        normalized_questions = [question for question in normalized_questions if question["id"]]
        return {
            "kind": "question",
            "title": "User input required",
            "params": {
                "questions": normalized_questions,
                "autoResolutionMs": _optional_non_negative_int(raw.get("autoResolutionMs")),
            },
            "actions": [],
            "presentable": bool(normalized_questions),
        }
    if normalized_method == MCP_ELICITATION:
        mode = str(raw.get("mode", "") or "").strip()
        return {
            "kind": "elicitation",
            "title": "MCP input required",
            "params": {
                key: value
                for key, value in raw.items()
                if key not in {"threadId", "turnId", "_meta"}
            },
            "actions": [],
            "presentable": mode == "form" and _valid_mcp_form_schema(raw.get("requestedSchema")),
            "unsupported_reason": (
                "Only MCP form elicitation is supported by this Focus frontend."
                if mode != "form"
                else "The MCP form schema is not supported by this Focus frontend."
            ),
        }
    return {
        "kind": "unsupported",
        "title": "Codex request",
        "params": {},
        "actions": [],
        "presentable": False,
        "unsupported_reason": f"Unsupported request: {normalized_method}",
    }


def interaction_response_payload(
    method: str,
    params: dict[str, Any],
    *,
    action: str,
    answers: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized = normalize_interaction_request(method, params)
    normalized_action = str(action or "").strip()
    if method in SHARED_APPROVAL_METHODS:
        for candidate in normalized["actions"]:
            if candidate["id"] == normalized_action:
                return dict(candidate["response"]), None
        raise ValueError("Unsupported approval action.")
    if method == USER_INPUT:
        if normalized_action == "cancel":
            return None, {"code": -32002, "message": "Focus user cancelled the request"}
        if normalized_action == "auto_resolve":
            return {"answers": {}}, None
        if normalized_action != "answer":
            raise ValueError("Unsupported user-input action.")
        return {"answers": _normalize_user_answers(normalized["params"], answers or {})}, None
    if method == MCP_ELICITATION:
        if normalized_action == "cancel":
            return {"action": "cancel", "content": None, "_meta": None}, None
        if normalized_action != "accept" or not normalized.get("presentable"):
            raise ValueError("Unsupported elicitation action.")
        content = _normalize_mcp_form_answers(
            normalized["params"].get("requestedSchema"),
            answers or {},
        )
        return {"action": "accept", "content": content, "_meta": None}, None
    raise ValueError("Unsupported Codex request.")


def fail_closed_interaction_response(
    method: str,
    params: dict[str, Any],
    *,
    message: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if method == COMMAND_APPROVAL:
        return {"decision": "cancel"}, None
    if method == FILE_APPROVAL:
        return {"decision": "cancel"}, None
    if method == PERMISSIONS_APPROVAL:
        return {"permissions": {}, "scope": "turn"}, None
    if method == USER_INPUT:
        return None, {"code": -32002, "message": message}
    if method == MCP_ELICITATION:
        return {"action": "cancel", "content": None, "_meta": None}, None
    return None, {"code": -32001, "message": f"Unsupported request: {method}"}


def _command_actions(raw_decisions: Any) -> list[dict[str, Any]]:
    declared = isinstance(raw_decisions, list)
    decisions = raw_decisions if declared else ["accept", "acceptForSession", "decline", "cancel"]
    actions: list[dict[str, Any]] = []
    for decision in decisions:
        if decision == "accept":
            actions.append(_action("approve_once", "Allow once", {"decision": "accept"}, "primary"))
        elif decision == "acceptForSession":
            actions.append(
                _action(
                    "approve_session",
                    "Allow for session",
                    {"decision": "acceptForSession"},
                    "secondary",
                )
            )
        elif decision == "decline":
            actions.append(_action("reject", "Decline", {"decision": "decline"}, "danger"))
        elif decision == "cancel":
            actions.append(_action("cancel", "Cancel turn", {"decision": "cancel"}, "danger"))
        elif isinstance(decision, dict) and len(decision) == 1:
            variant, payload = next(iter(decision.items()))
            if variant == "acceptWithExecpolicyAmendment":
                actions.append(
                    _action(
                        "approve_execpolicy_amendment",
                        "Allow and remember command policy",
                        {"decision": {variant: payload}},
                        "secondary",
                    )
                )
            elif variant == "applyNetworkPolicyAmendment":
                amendment = (
                    payload.get("network_policy_amendment")
                    if isinstance(payload, dict)
                    else {}
                )
                host = str(amendment.get("host", "") or "") if isinstance(amendment, dict) else ""
                policy_action = str(amendment.get("action", "") or "") if isinstance(amendment, dict) else ""
                label = "Apply network policy"
                if host:
                    label = (
                        f"{(policy_action or 'Apply').capitalize()} "
                        f"network policy for {host}"
                    )
                actions.append(
                    _action(
                        f"network_policy_{len(actions)}",
                        label,
                        {"decision": {variant: payload}},
                        "secondary",
                    )
                )
    if declared:
        return actions
    return actions or [_action("cancel", "Cancel turn", {"decision": "cancel"}, "danger")]


def _action(
    action_id: str,
    label: str,
    response: dict[str, Any],
    style: str,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "style": style,
        "response": response,
    }


def _normalize_user_question(question: dict[str, Any]) -> dict[str, Any]:
    options = question.get("options") if isinstance(question.get("options"), list) else []
    return {
        "id": str(question.get("id", "") or "").strip(),
        "header": str(question.get("header", "") or ""),
        "question": str(question.get("question", "") or ""),
        "isOther": bool(question.get("isOther", False)),
        "isSecret": bool(question.get("isSecret", False)),
        "options": [
            {
                "label": str(option.get("label", "") or ""),
                "description": str(option.get("description", "") or ""),
            }
            for option in options
            if isinstance(option, dict) and str(option.get("label", "") or "").strip()
        ],
    }


def _normalize_user_answers(
    params: dict[str, Any],
    answers: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    questions = params.get("questions") if isinstance(params.get("questions"), list) else []
    normalized: dict[str, dict[str, list[str]]] = {}
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id", "") or "").strip()
        if not question_id:
            continue
        raw_answer = answers.get(question_id)
        if isinstance(raw_answer, list):
            values = [str(value or "").strip() for value in raw_answer if str(value or "").strip()]
        else:
            value = str(raw_answer or "").strip()
            values = [value] if value else []
        if not values:
            raise ValueError(f"Missing answer for question {question_id}.")
        options = {
            str(option.get("label", "") or "").strip()
            for option in (question.get("options") or [])
            if isinstance(option, dict) and str(option.get("label", "") or "").strip()
        }
        if options and not question.get("isOther") and any(value not in options for value in values):
            raise ValueError(f"Question {question_id} only accepts one of its declared options.")
        normalized[question_id] = {"answers": values}
    return normalized


def _valid_mcp_form_schema(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("type") != "object":
        return False
    if not set(value).issubset(
        {
            "$schema",
            "type",
            "title",
            "description",
            "properties",
            "required",
            "additionalProperties",
        }
    ):
        return False
    properties = value.get("properties")
    if not isinstance(properties, dict) or not properties:
        return False
    required = value.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(name, str) and name in properties
        for name in required
    ):
        return False
    if not all(
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(schema, dict)
        and _valid_mcp_form_field(schema)
        for name, schema in properties.items()
    ):
        return False
    additional_properties = value.get("additionalProperties")
    return additional_properties is None or isinstance(additional_properties, bool)


def _valid_mcp_form_field(schema: dict[str, Any]) -> bool:
    field_type = schema.get("type")
    annotations = {"type", "title", "description", "default", "examples"}
    if field_type == "string":
        if not set(schema).issubset(
            annotations
            | {"enum", "enumNames", "oneOf", "format", "minLength", "maxLength"}
        ):
            return False
        if not _valid_non_negative_integer_range(schema, "minLength", "maxLength"):
            return False
        if str(schema.get("format", "") or "") not in {
            "",
            "password",
            "email",
            "uri",
            "date",
            "date-time",
        }:
            return False
        if "enum" in schema:
            values = schema.get("enum")
            if not _valid_string_enum(values):
                return False
            return _valid_enum_names(schema.get("enumNames"), len(values))
        if "enumNames" in schema:
            return False
        if "oneOf" in schema:
            return _valid_mcp_enum_options(schema.get("oneOf"))
        return True
    if field_type in {"number", "integer"}:
        if not set(schema).issubset(annotations | {"minimum", "maximum"}):
            return False
        return _valid_finite_number_range(schema, "minimum", "maximum")
    if field_type == "boolean":
        return set(schema).issubset(annotations)
    if field_type != "array":
        return False
    if not set(schema).issubset(
        annotations | {"items", "minItems", "maxItems"}
    ):
        return False
    if not _valid_non_negative_integer_range(schema, "minItems", "maxItems"):
        return False
    items = schema.get("items")
    if not isinstance(items, dict):
        return False
    if not set(items).issubset(
        {"type", "enum", "enumNames", "anyOf", "oneOf"}
    ):
        return False
    if items.get("type", "string") != "string":
        return False
    if "enum" in items:
        values = items.get("enum")
        if not _valid_string_enum(values):
            return False
        return _valid_enum_names(items.get("enumNames"), len(values))
    if "enumNames" in items:
        return False
    return _valid_mcp_enum_options(items.get("anyOf", items.get("oneOf")))


def _valid_mcp_enum_options(value: Any) -> bool:
    return bool(value) and isinstance(value, list) and all(
        isinstance(option, dict)
        and set(option).issubset({"const", "title", "description"})
        and isinstance(option.get("const"), str)
        and bool(option.get("const"))
        and isinstance(option.get("title"), str)
        and bool(option.get("title"))
        for option in value
    )


def _valid_string_enum(value: Any) -> bool:
    return bool(value) and isinstance(value, list) and all(
        isinstance(item, str) and bool(item)
        for item in value
    )


def _valid_enum_names(value: Any, expected_length: int) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, list)
        and len(value) == expected_length
        and all(isinstance(item, str) and bool(item) for item in value)
    )


def _valid_non_negative_integer_range(
    schema: dict[str, Any],
    minimum_key: str,
    maximum_key: str,
) -> bool:
    minimum = schema.get(minimum_key)
    maximum = schema.get(maximum_key)
    for value in (minimum, maximum):
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            return False
    return minimum is None or maximum is None or minimum <= maximum


def _valid_finite_number_range(
    schema: dict[str, Any],
    minimum_key: str,
    maximum_key: str,
) -> bool:
    minimum = schema.get(minimum_key)
    maximum = schema.get(maximum_key)
    for value in (minimum, maximum):
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            return False
    return minimum is None or maximum is None or minimum <= maximum


def _normalize_mcp_form_answers(schema: Any, answers: dict[str, Any]) -> dict[str, Any]:
    if not _valid_mcp_form_schema(schema):
        raise ValueError("Unsupported MCP form schema.")
    properties = schema["properties"]
    required = {str(value) for value in (schema.get("required") or [])}
    result: dict[str, Any] = {}
    for key, field in properties.items():
        raw = answers.get(key)
        if raw in (None, "", []):
            if key in required:
                raise ValueError(f"Missing required MCP field {key}.")
            continue
        field_type = field.get("type")
        if field_type == "boolean":
            if isinstance(raw, bool):
                value: Any = raw
            elif str(raw).strip().lower() in {"true", "yes", "1", "on"}:
                value = True
            elif str(raw).strip().lower() in {"false", "no", "0", "off"}:
                value = False
            else:
                raise ValueError(f"MCP field {key} must be boolean.")
        elif field_type in {"number", "integer"}:
            try:
                value = int(raw) if field_type == "integer" else float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"MCP field {key} must be {field_type}.") from exc
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"MCP field {key} must be finite.")
            minimum = field.get("minimum")
            maximum = field.get("maximum")
            if minimum is not None and value < minimum:
                raise ValueError(f"MCP field {key} is below its minimum.")
            if maximum is not None and value > maximum:
                raise ValueError(f"MCP field {key} is above its maximum.")
        elif field_type == "array":
            value = raw if isinstance(raw, list) else [str(raw)]
            value = [str(item) for item in value]
            minimum = field.get("minItems")
            maximum = field.get("maxItems")
            if minimum is not None and len(value) < int(minimum):
                raise ValueError(f"MCP field {key} has too few selections.")
            if maximum is not None and len(value) > int(maximum):
                raise ValueError(f"MCP field {key} has too many selections.")
            allowed = _mcp_field_options(field.get("items"))
            if allowed is not None and any(item not in allowed for item in value):
                raise ValueError(f"MCP field {key} contains an unsupported option.")
        else:
            value = str(raw)
            allowed = _mcp_field_options(field)
            if allowed is not None and value not in allowed:
                raise ValueError(f"MCP field {key} contains an unsupported option.")
            minimum = field.get("minLength")
            maximum = field.get("maxLength")
            if minimum is not None and len(value) < int(minimum):
                raise ValueError(f"MCP field {key} is shorter than its minimum length.")
            if maximum is not None and len(value) > int(maximum):
                raise ValueError(f"MCP field {key} exceeds its maximum length.")
            if not _valid_mcp_string_format(value, field.get("format")):
                raise ValueError(f"MCP field {key} does not match its declared format.")
        result[key] = value
    return result


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_mcp_string_format(value: str, raw_format: Any) -> bool:
    string_format = str(raw_format or "").strip()
    if string_format in {"", "password"}:
        return True
    if string_format == "email":
        return bool(_EMAIL_PATTERN.fullmatch(value))
    if string_format == "uri":
        parsed = urlparse(value)
        return bool(parsed.scheme and not any(character.isspace() for character in value))
    if string_format == "date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    if string_format == "date-time":
        try:
            dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return "T" in value or "t" in value
    return False


def _mcp_field_options(schema: Any) -> set[str] | None:
    if not isinstance(schema, dict):
        return None
    raw_options = schema.get("enum")
    if isinstance(raw_options, list):
        return {str(value) for value in raw_options}
    raw_options = schema.get("anyOf", schema.get("oneOf"))
    if isinstance(raw_options, list):
        return {
            str(option.get("const"))
            for option in raw_options
            if isinstance(option, dict) and isinstance(option.get("const"), str)
        }
    return None


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None
