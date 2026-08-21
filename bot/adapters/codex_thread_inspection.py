"""Strict projection for paginated Codex thread inspection responses."""

from __future__ import annotations

from typing import Any

from bot.adapters.base import (
    ThreadItemEntry,
    ThreadItemsPage,
    ThreadSearchOccurrence,
    ThreadSearchOccurrencesPage,
)
from bot.codex_protocol.client import CodexRpcProtocolError


_U32_MAX = (1 << 32) - 1


def require_request_identity(value: object, *, field: str) -> str:
    """Require one exact, non-whitespace request identity."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(f"{field} must be a non-empty string without surrounding whitespace")
    return value


def require_optional_request_cursor(value: object, *, field: str = "cursor") -> str | None:
    if value is None:
        return None
    return require_request_identity(value, field=field)


def require_optional_u32(value: object, *, field: str = "limit") -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > _U32_MAX:
        raise ValueError(f"{field} must be an unsigned 32-bit integer")
    return value


def thread_items_page_from_result(result: Any) -> ThreadItemsPage:
    method = "thread/items/list"
    payload = _require_object(method, result)
    raw_data = payload.get("data")
    if not isinstance(raw_data, list):
        raise _protocol_error(method, "response is missing data")

    entries: list[ThreadItemEntry] = []
    for index, raw_entry in enumerate(raw_data):
        if not isinstance(raw_entry, dict):
            raise _protocol_error(method, f"data[{index}] is not an object")
        turn_id = _require_response_identity(
            method,
            raw_entry,
            "turnId",
            location=f"data[{index}]",
        )
        raw_item = raw_entry.get("item")
        if not isinstance(raw_item, dict):
            raise _protocol_error(method, f"data[{index}].item is not an object")
        _require_response_identity(
            method,
            raw_item,
            "id",
            location=f"data[{index}].item",
        )
        _require_response_identity(
            method,
            raw_item,
            "type",
            location=f"data[{index}].item",
        )
        entries.append(ThreadItemEntry(turn_id=turn_id, item=dict(raw_item)))

    return ThreadItemsPage(
        items=entries,
        next_cursor=_require_response_cursor(method, payload, "nextCursor"),
        backwards_cursor=_require_response_cursor(
            method,
            payload,
            "backwardsCursor",
        ),
    )


def thread_search_occurrences_page_from_result(
    result: Any,
) -> ThreadSearchOccurrencesPage:
    method = "thread/searchOccurrences"
    payload = _require_object(method, result)
    raw_data = payload.get("data")
    if not isinstance(raw_data, list):
        raise _protocol_error(method, "response is missing data")

    occurrences: list[ThreadSearchOccurrence] = []
    for index, raw_occurrence in enumerate(raw_data):
        location = f"data[{index}]"
        if not isinstance(raw_occurrence, dict):
            raise _protocol_error(method, f"{location} is not an object")
        snippet = raw_occurrence.get("snippet")
        if not isinstance(snippet, str):
            raise _protocol_error(method, f"{location}.snippet is not a string")
        match_range = _require_match_range(
            method,
            snippet,
            raw_occurrence.get("snippetMatchRange"),
            location=f"{location}.snippetMatchRange",
        )
        occurrences.append(
            ThreadSearchOccurrence(
                turn_id=_require_response_identity(
                    method,
                    raw_occurrence,
                    "turnId",
                    location=location,
                ),
                item_id=_require_response_identity(
                    method,
                    raw_occurrence,
                    "itemId",
                    location=location,
                ),
                snippet=snippet,
                snippet_match_range=match_range,
                turn_cursor=_require_response_identity(
                    method,
                    raw_occurrence,
                    "turnCursor",
                    location=location,
                ),
            )
        )

    return ThreadSearchOccurrencesPage(
        occurrences=occurrences,
        next_cursor=_require_response_cursor(method, payload, "nextCursor"),
    )


def _require_object(method: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise _protocol_error(method, "returned a non-object response")
    return result


def _require_response_identity(
    method: str,
    payload: dict[str, Any],
    field: str,
    *,
    location: str,
) -> str:
    value = payload.get(field)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise _protocol_error(method, f"{location}.{field} is invalid")
    return value


def _require_response_cursor(
    method: str,
    payload: dict[str, Any],
    field: str,
) -> str | None:
    if field not in payload:
        raise _protocol_error(method, f"response is missing {field}")
    value = payload[field]
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise _protocol_error(method, f"response has an invalid {field}")
    return value


def _require_match_range(
    method: str,
    snippet: str,
    raw_range: object,
    *,
    location: str,
) -> tuple[int, int]:
    if not isinstance(raw_range, dict):
        raise _protocol_error(method, f"{location} is not an object")
    start = raw_range.get("start")
    end = raw_range.get("end")
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end < 0
        or start > _U32_MAX
        or end > _U32_MAX
        or start >= end
    ):
        raise _protocol_error(method, f"{location} is not a valid non-empty u32 range")

    utf16_boundaries = {0}
    offset = 0
    for character in snippet:
        offset += 2 if ord(character) > 0xFFFF else 1
        utf16_boundaries.add(offset)
    if start not in utf16_boundaries or end not in utf16_boundaries:
        raise _protocol_error(
            method,
            f"{location} is outside snippet UTF-16 character boundaries",
        )
    return start, end


def _protocol_error(method: str, message: str) -> CodexRpcProtocolError:
    return CodexRpcProtocolError(method, f"Codex {method} {message}")
