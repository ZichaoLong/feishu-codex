"""Strict decoders for Codex app-server thread-goal responses."""

from __future__ import annotations

from typing import Any

from bot.adapters.base import ThreadGoalSummary
from bot.codex_protocol.client import CodexRpcProtocolError


_REQUIRED_INTEGER_FIELDS = (
    "tokensUsed",
    "timeUsedSeconds",
    "createdAt",
    "updatedAt",
)


def decode_thread_goal_response(
    method: str,
    result: Any,
    *,
    expected_thread_id: str,
    allow_null: bool,
) -> ThreadGoalSummary | None:
    """Decode one required ``goal`` field without inventing safe defaults."""

    payload = _require_object(method, result)
    if "goal" not in payload:
        raise _protocol_error(method, "response is missing required goal")
    goal = payload["goal"]
    if goal is None:
        if allow_null:
            return None
        raise _protocol_error(method, "response goal must be an object")
    if not isinstance(goal, dict):
        raise _protocol_error(method, "response goal must be an object or null")

    thread_id = _required_string(method, goal, "threadId", allow_empty=False)
    normalized_expected = str(expected_thread_id or "").strip()
    if normalized_expected and thread_id != normalized_expected:
        raise _protocol_error(
            method,
            "response goal does not match the requested thread",
        )
    objective = _required_string(method, goal, "objective", allow_empty=True)
    status = _required_string(method, goal, "status", allow_empty=False)

    if "tokenBudget" not in goal:
        raise _protocol_error(method, "response goal is missing tokenBudget")
    token_budget = goal["tokenBudget"]
    if token_budget is not None and type(token_budget) is not int:
        raise _protocol_error(method, "response goal tokenBudget must be an integer or null")

    integers: dict[str, int] = {}
    for field in _REQUIRED_INTEGER_FIELDS:
        if field not in goal or type(goal[field]) is not int:
            raise _protocol_error(
                method,
                f"response goal {field} must be an integer",
            )
        integers[field] = goal[field]

    return ThreadGoalSummary(
        thread_id=thread_id,
        objective=objective,
        status=status,
        token_budget=token_budget,
        tokens_used=integers["tokensUsed"],
        time_used_seconds=integers["timeUsedSeconds"],
        created_at=integers["createdAt"],
        updated_at=integers["updatedAt"],
    )


def decode_thread_goal_clear_response(method: str, result: Any) -> bool:
    """Decode the required exact boolean clear result."""

    payload = _require_object(method, result)
    if "cleared" not in payload or type(payload["cleared"]) is not bool:
        raise _protocol_error(method, "response cleared must be a boolean")
    return payload["cleared"]


def _require_object(method: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise _protocol_error(method, "returned a non-object response")
    return result


def _required_string(
    method: str,
    payload: dict[str, Any],
    field: str,
    *,
    allow_empty: bool,
) -> str:
    if field not in payload or not isinstance(payload[field], str):
        raise _protocol_error(method, f"response goal {field} must be a string")
    value = payload[field]
    if not allow_empty and not value.strip():
        raise _protocol_error(method, f"response goal {field} must not be empty")
    return value.strip() if field in {"threadId", "status"} else value


def _protocol_error(method: str, detail: str) -> CodexRpcProtocolError:
    return CodexRpcProtocolError(method, f"Codex {method} {detail}")
