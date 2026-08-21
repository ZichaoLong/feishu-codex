"""Pure request-field decoders used by the Focus Web Gateway.

This module owns no session, document, routing, lock, or runtime state.  It
only converts one already-selected HTTP request/body field into the exact
shape required by the Gateway's existing handlers.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp import web

from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.turn_window import parse_turn_window_limit


_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_U32_MAX = (1 << 32) - 1
_CANONICAL_UNSIGNED_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")


def decode_turn_window_limit(query: object) -> int:
    if not hasattr(query, "getall") or "turn_limit" not in query:
        return parse_turn_window_limit(None)
    try:
        values = query.getall("turn_limit")
        if len(values) != 1:
            raise ValueError("turn_limit must appear exactly once")
        raw_value = values[0]
        return parse_turn_window_limit(raw_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise WebRuntimeError(
            "turn_limit must be exactly 5, 10, or 20.",
            code="invalid_turn_limit",
            status=400,
        ) from exc


def decode_tool_detail_query(query: object) -> tuple[str, int | None, str | None]:
    """Decode the explicit view plus exact locator/cursor query fields."""

    message = (
        "Tool detail requires view=preview or view=full, one canonical unsigned "
        "32-bit change_index at most, and at most one exact cursor."
    )
    values = _decode_exact_query(
        query,
        required=frozenset({"view"}),
        optional=frozenset({"change_index", "cursor"}),
        code="invalid_tool_detail_query",
        message=message,
    )
    view = values["view"]
    if view not in {"preview", "full"}:
        raise WebRuntimeError(message, code="invalid_tool_detail_query", status=400)
    raw_index = values.get("change_index")
    if raw_index is None:
        change_index = None
    elif not _CANONICAL_UNSIGNED_DECIMAL_RE.fullmatch(raw_index):
        raise WebRuntimeError(message, code="invalid_tool_detail_query", status=400)
    else:
        change_index = int(raw_index)
        if change_index > _U32_MAX:
            raise WebRuntimeError(message, code="invalid_tool_detail_query", status=400)
    cursor = values.get("cursor")
    if (
        cursor is not None
        and (not cursor or cursor != cursor.strip() or len(cursor) > 4096)
    ):
        raise WebRuntimeError(message, code="invalid_tool_detail_query", status=400)
    return view, change_index, cursor


def decode_conversation_search_query(query: object) -> tuple[str, str | None]:
    """Decode one bounded search term and optional exact opaque cursor."""

    message = (
        "Conversation search requires one query of 1..256 Unicode characters "
        "and at most one exact cursor."
    )
    values = _decode_exact_query(
        query,
        required=frozenset({"query"}),
        optional=frozenset({"cursor"}),
        code="invalid_conversation_search_query",
        message=message,
    )
    normalized_query = values["query"].strip()
    cursor = values.get("cursor")
    if not 1 <= len(normalized_query) <= 256:
        raise WebRuntimeError(
            message,
            code="invalid_conversation_search_query",
            status=400,
        )
    if (
        cursor is not None
        and (
            not cursor
            or cursor != cursor.strip()
            or len(cursor) > 4096
        )
    ):
        raise WebRuntimeError(
            message,
            code="invalid_conversation_search_query",
            status=400,
        )
    return normalized_query, cursor


def _decode_exact_query(
    query: object,
    *,
    required: frozenset[str],
    optional: frozenset[str],
    code: str,
    message: str,
) -> dict[str, str]:
    """Return one value per allowed key and reject every parallel shape."""

    try:
        keys = tuple(query.keys())  # type: ignore[union-attr]
        allowed = required | optional
        if set(keys) - allowed or not required.issubset(keys):
            raise ValueError("query keys do not match the closed contract")
        values: dict[str, str] = {}
        for key in allowed:
            if key not in keys:
                continue
            raw_values = query.getall(key)  # type: ignore[union-attr]
            if len(raw_values) != 1 or not isinstance(raw_values[0], str):
                raise ValueError(f"{key} must appear at most once")
            values[key] = raw_values[0]
        return values
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise WebRuntimeError(message, code=code, status=400) from exc


async def decode_json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise WebRuntimeError(
            "Request body must be a JSON object.",
            code="invalid_json",
        ) from exc
    if not isinstance(body, dict):
        raise WebRuntimeError(
            "Request body must be a JSON object.",
            code="invalid_json",
        )
    return body


def decode_attachment_ids(body: dict[str, Any]) -> list[str]:
    raw = body.get("attachment_ids", [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise WebRuntimeError(
            "attachment_ids must be an array of strings.",
            code="invalid_attachment",
        )
    return [value.strip() for value in raw]


def is_exact_text(value: object) -> bool:
    return bool(isinstance(value, str) and value and value == value.strip())


def decode_safe_integer_field(
    body: dict[str, Any],
    field: str,
    *,
    positive: bool,
) -> int:
    value = body.get(field)
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > _MAX_SAFE_INTEGER
    ):
        raise WebRuntimeError(
            f"{field} must be a {'positive' if positive else 'non-negative'} safe integer.",
            code="invalid_submission_scope",
            status=400,
        )
    return value


def decode_intent_generation(raw_value: object) -> int:
    raw = str(raw_value or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise WebRuntimeError(
            "Invalid browser intent generation.",
            code="invalid_intent",
        ) from exc
    if value < 0 or value > _MAX_SAFE_INTEGER:
        raise WebRuntimeError(
            "Invalid browser intent generation.",
            code="invalid_intent",
        )
    return value


def decode_request_connection_generation(body: dict[str, Any]) -> int:
    value = body.get("connection_generation")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_SAFE_INTEGER
    ):
        raise WebRuntimeError(
            "A positive Codex connection generation is required.",
            code="invalid_request_generation",
        )
    return value


def decode_backend_reset_request(body: dict[str, Any]) -> tuple[bool, int]:
    """Decode the complete destructive Web reset request without coercion."""

    force = body.get("force")
    generation = body.get("expected_connection_generation")
    if (
        set(body) != {"force", "expected_connection_generation"}
        or type(force) is not bool
        or type(generation) is not int
        or generation <= 0
        or generation > _MAX_SAFE_INTEGER
    ):
        raise WebRuntimeError(
            "Backend reset body must contain only an exact boolean force "
            "and a positive safe expected_connection_generation.",
            code="invalid_backend_reset_request",
            status=400,
        )
    return force, generation


def decode_request_response_capability(body: dict[str, Any]) -> str:
    value = body.get("response_capability")
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 256
    ):
        raise WebRuntimeError(
            "An exact response capability is required.",
            code="invalid_response_capability",
        )
    return value


def decode_client_id_hint(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if len(normalized) > 128 or any(char.isspace() for char in normalized):
        raise WebRuntimeError(
            "Invalid browser client id.",
            code="invalid_client",
        )
    return normalized


def decode_document_incarnation(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(char.isspace() for char in normalized)
    ):
        raise WebRuntimeError(
            "Invalid browser document incarnation.",
            code="invalid_document",
        )
    return normalized
