"""Immutable vocabulary for fcodex proxy request coordination.

This module contains no mutable runtime state.  It is shared by the
operation owner and its composition facade so method classification and
release evidence have one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.goal_continuation_policy import is_reviewed_non_continuing_goal_status


THREAD_READ_METHODS = frozenset(
    {
        "thread/read",
        "thread/goal/get",
        "thread/backgroundTerminals/list",
        "thread/turns/list",
        "thread/items/list",
    }
)

THREAD_RUNTIME_METHODS = frozenset({"thread/unsubscribe"})

EXCLUSIVE_MAIN_TURN_START_METHODS = frozenset(
    {"review/start", "thread/compact/start"}
)

EXPLICITLY_DENIED_THREAD_MUTATION_METHODS = frozenset({"thread/fork"})

UNSUPPORTED_ASYNC_THREAD_MUTATION_METHODS = frozenset(
    {
        "thread/shellCommand",
        "thread/approveGuardianDeniedAction",
        "thread/backgroundTerminals/clean",
        "thread/rollback",
        "thread/realtime/start",
        "thread/realtime/appendAudio",
        "thread/realtime/appendText",
        "thread/realtime/appendSpeech",
        "thread/realtime/stop",
    }
)

THREAD_MUTATION_METHODS = frozenset(
    {
        "thread/archive",
        "thread/delete",
        "thread/unarchive",
        "thread/increment_elicitation",
        "thread/decrement_elicitation",
        "thread/name/set",
        "thread/goal/set",
        "thread/goal/clear",
        "thread/metadata/update",
        "thread/settings/update",
        "thread/memoryMode/set",
        "thread/compact/start",
        "thread/backgroundTerminals/terminate",
        "thread/inject_items",
        "turn/start",
        "turn/steer",
        "turn/interrupt",
        "review/start",
    }
)

ROOT_TERMINAL_NOTIFICATIONS = frozenset(
    {"turn/completed", "thread/closed", "thread/archived", "thread/deleted"}
)

THREAD_GONE_NOTIFICATIONS = frozenset(
    {"thread/closed", "thread/archived", "thread/deleted"}
)

_CHILD_METADATA_READ_METHOD = "thread/read"
_CHILD_METADATA_READ_PARAMS = frozenset({"threadId", "includeTurns"})
_INTERRUPT_PARAMS = frozenset({"threadId", "turnId"})
_STEER_REQUIRED_PARAMS = frozenset({"threadId", "input", "expectedTurnId"})
_STEER_ALLOWED_PARAMS = _STEER_REQUIRED_PARAMS | frozenset(
    {
        "clientUserMessageId",
        "responsesapiClientMetadata",
        "additionalContext",
    }
)
_STEER_EXPERIMENTAL_CONTEXT_PARAMS = frozenset(
    {"responsesapiClientMetadata", "additionalContext"}
)


def fcodex_method_requires_thread_mutation_admission(
    method: object,
    thread_id: object,
) -> bool:
    """Classify the service-side pre-owner recovery gate."""

    normalized_method = str(method or "").strip()
    normalized_thread_id = str(thread_id or "").strip()
    return bool(
        normalized_thread_id
        and normalized_method != "thread/start"
        and normalized_method not in THREAD_READ_METHODS
    )


def is_strict_fcodex_child_metadata_read(
    rpc_method: str,
    thread_id: str,
    request_params: object,
) -> bool:
    """Recognize only upstream TUI's reviewed metadata-only child read."""

    if rpc_method != _CHILD_METADATA_READ_METHOD:
        return False
    if not isinstance(request_params, dict):
        return False
    if set(request_params) - _CHILD_METADATA_READ_PARAMS:
        return False
    requested_thread_id = request_params.get("threadId")
    if not isinstance(requested_thread_id, str) or requested_thread_id != thread_id:
        return False
    include_turns = request_params.get("includeTurns", False)
    return isinstance(include_turns, bool) and not include_turns


def strict_fcodex_interrupt_target(
    request_params: object,
) -> tuple[str, str] | None:
    """Decode the exact-or-current raw interrupt shape safe to forward unchanged."""

    if not isinstance(request_params, dict):
        return None
    if set(request_params) != _INTERRUPT_PARAMS:
        return None
    thread_id = request_params.get("threadId")
    turn_id = request_params.get("turnId")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
        or not isinstance(turn_id, str)
        or turn_id != turn_id.strip()
    ):
        return None
    return thread_id, turn_id


def strict_fcodex_steer_target(
    request_params: object,
) -> tuple[str, str] | None:
    """Decode the exact raw steer identity safe for shared admission.

    Input and the stable optional client-message id remain upstream-owned
    schema. Focus only freezes the admitted parameter surface and refuses
    experimental context injection; the raw request is forwarded unchanged.
    """

    if not isinstance(request_params, dict):
        return None
    keys = set(request_params)
    if not _STEER_REQUIRED_PARAMS.issubset(keys) or keys - _STEER_ALLOWED_PARAMS:
        return None
    if any(
        request_params.get(key) is not None
        for key in _STEER_EXPERIMENTAL_CONTEXT_PARAMS
    ):
        return None
    thread_id = request_params.get("threadId")
    expected_turn_id = request_params.get("expectedTurnId")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or thread_id != thread_id.strip()
        or not isinstance(expected_turn_id, str)
        or not expected_turn_id
        or expected_turn_id != expected_turn_id.strip()
    ):
        return None
    return thread_id, expected_turn_id


def fcodex_notification_proves_runtime_unloaded(
    method: str,
    params: dict[str, Any],
) -> bool:
    """Return whether one upstream frame authoritatively unloads a thread."""

    if method in THREAD_GONE_NOTIFICATIONS:
        return True
    if method != "thread/status/changed":
        return False
    status = params.get("status") if isinstance(params.get("status"), dict) else {}
    return str(status.get("type", "") or "").strip() == "notLoaded"


def fcodex_request_can_have_unknown_root_mutation(
    method: str,
    *,
    continuation_risk: bool,
) -> bool:
    return method in THREAD_MUTATION_METHODS or bool(continuation_risk)


def fcodex_known_noncontinuing_goal_mutation_result(
    method: str,
    response_result: dict[str, Any] | None,
) -> bool:
    """Accept only the typed causal result which permanently disarms a goal."""

    if not isinstance(response_result, dict):
        return False
    normalized_method = str(method or "").strip()
    if normalized_method == "thread/goal/clear":
        return response_result.get("cleared") is True
    if normalized_method != "thread/goal/set":
        return False
    goal = response_result.get("goal")
    return bool(
        isinstance(goal, dict)
        and is_reviewed_non_continuing_goal_status(goal.get("status", ""))
    )


def fcodex_successful_response_thread_identity(
    method: str,
    *,
    admitted_thread_id: str,
    admitted_root_thread_id: str,
    observed_thread_id: str,
    observed_root_thread_id: str,
) -> tuple[str, str] | None:
    """Validate the exact identity carried by a successful start/resume."""

    observed_thread = str(observed_thread_id or "").strip()
    observed_root = str(observed_root_thread_id or "").strip()
    if method == "thread/start":
        return (
            (observed_thread, observed_root)
            if observed_thread and observed_root == observed_thread
            else None
        )
    admitted_thread = str(admitted_thread_id or "").strip()
    admitted_root = str(admitted_root_thread_id or "").strip()
    return (
        (observed_thread, observed_root)
        if observed_thread == admitted_thread and observed_root == admitted_root
        else None
    )


def fcodex_client_response_receipt(
    request_token: int,
    *,
    settled: bool,
    **extra: Any,
) -> dict[str, Any]:
    """Build the only positive response capability accepted by the proxy."""

    return {
        "known": True,
        "settled": bool(settled),
        "request_token": request_token,
        **extra,
    }


@dataclass(frozen=True, slots=True)
class FcodexRequestEpochCloseReceipt:
    """Process-local proxy request facts retired after the backend stopped."""

    client_request_keys: tuple[str, ...]
    routed_thread_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FcodexBackendEpochSettlementReceipt:
    """Cross-owner local settlement completed before machine-holder replace."""

    requests: FcodexRequestEpochCloseReceipt
    interaction_request_keys: tuple[str, ...]
