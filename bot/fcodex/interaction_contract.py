"""Typed fcodex server-request identity and action contracts.

This module owns no participant, operation, or request state.  It keeps the
pure identity/action vocabulary and the one outbound-response precondition
out of the mutable operation coordinator.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypeAlias

from bot.jsonrpc_id import jsonrpc_id_key


class FcodexServerRequestEnvelope(Protocol):
    """Read-only envelope facts used for typed replay comparison."""

    method: str
    thread_id: str
    turn_id: str
    params: dict[str, Any]


FcodexFailCloseOutcome: TypeAlias = Literal[
    "submitted",
    "superseded",
    "deferred",
    "not_sent",
    "unknown",
]


def fcodex_allow(*, root_thread_id: str) -> dict[str, Any]:
    return {
        "allowed": True,
        "root_thread_id": str(root_thread_id or ""),
    }


def fcodex_deny(reason: str) -> dict[str, Any]:
    return {"allowed": False, "reason": str(reason or "当前操作被 Focus 拒绝。")}


def fcodex_connection_id(connection_id: str) -> str:
    normalized = str(connection_id or "").strip()
    if not normalized:
        raise ValueError("operation connection_id 不能为空。")
    return normalized


def fcodex_server_request_key(request_id: Any) -> str:
    try:
        return jsonrpc_id_key(request_id)
    except ValueError as exc:
        raise ValueError("operation request id 必须是非空 string 或 number。") from exc


def fcodex_client_request_key(
    participant_id: str,
    connection_id: str,
    request_id: Any,
) -> str:
    participant = str(participant_id or "").strip()
    connection = fcodex_connection_id(connection_id)
    if not participant:
        raise ValueError("operation participant_id 不能为空。")
    return f"{participant}\x1f{connection}\x1f{fcodex_server_request_key(request_id)}"


def same_fcodex_server_request_identity(
    pending: FcodexServerRequestEnvelope,
    *,
    method: str,
    params: dict[str, Any],
) -> bool:
    normalized_params = dict(params or {})
    return (
        pending.method == str(method or "").strip()
        and pending.thread_id
        == str(normalized_params.get("threadId", "") or "").strip()
        and pending.turn_id
        == str(normalized_params.get("turnId", "") or "").strip()
        and pending.params == normalized_params
    )


def fcodex_proxy_fail_close_action(
    outcome: FcodexFailCloseOutcome,
) -> Literal["fail_closed", "deferred", "suppress"]:
    """Project the service-owned response attempt to a proxy route.

    The proxy never acquires response authority from a service pre-send
    failure. A proven pre-send failure remains hidden for an exact replay;
    an unknown outcome fences only the exact request instead of quarantining
    an otherwise healthy proxy socket.
    """

    if outcome == "submitted":
        return "fail_closed"
    if outcome in {"deferred", "not_sent"}:
        return "deferred"
    return "suppress"


def fcodex_service_fail_close_action(
    outcome: FcodexFailCloseOutcome,
) -> Literal["fail_closed", "suppress", "fail_close_unknown"]:
    if outcome == "submitted":
        return "fail_closed"
    if outcome == "superseded":
        return "suppress"
    return "fail_close_unknown"
