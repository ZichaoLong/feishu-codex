"""Bounded, stateless inspection of one selected paginated Web thread."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

from bot.adapters.base import (
    ThreadItemsPage,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_writer_profile_store import WebWriterProfile
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
    require_web_direct_thread_snapshot,
)
from bot.web_runtime.document_registry import (
    WebDocumentOperationReceipt,
    WebDocumentRegistry,
)
from bot.web_runtime.projection import project_thread_inspection_tool
from bot.web_runtime.selection_coordinator import (
    WebSelectionCoordinator,
    WebSelectionNotReady,
)
from bot.web_runtime.thread_inspection_wire import encode_thread_inspection_json
from bot.web_runtime.thread_read_model import WebThreadReadObservationReceipt
from bot.web_runtime.tool_detail_source import project_tool_detail_source
from bot.web_runtime.writer_workspace_coordinator import (
    require_web_client_id,
    require_web_thread_id,
)


_INSPECTION_TIMEOUT_SECONDS = 2.0
_TOOL_PAGE_LIMIT = 100
_TOOL_CURSOR_MAX_CHARS = 4096
_TOOL_DETAIL_PREVIEW_MAX_BYTES = 1024 * 1024
_SEARCH_LIMIT = 20
_SEARCH_QUERY_MAX_CHARS = 256
_SEARCH_CURSOR_MAX_CHARS = 4096
_SEARCH_SNIPPET_MAX_CHARS = 1024
_SEARCH_RESPONSE_MAX_BYTES = 64 * 1024
_U32_MAX = (1 << 32) - 1
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "declined"})
_INSPECTABLE_TOOL_TYPES = frozenset({"commandExecution", "fileChange"})
ToolDetailView = Literal["preview", "full"]


@dataclass(frozen=True, slots=True)
class WebThreadInspectionPorts:
    """Read-only upstream and projection ports; none retain inspection state."""

    read_thread: Callable[..., ThreadSnapshot]
    list_thread_items: Callable[..., ThreadItemsPage]
    search_thread_occurrences: Callable[..., ThreadSearchOccurrencesPage]
    coordinates: Callable[[], dict[str, Any]]
    capture_observation: Callable[[str], WebThreadReadObservationReceipt]
    observation_is_current: Callable[[WebThreadReadObservationReceipt], bool]
    capture_connection_generation: Callable[[], int]
    run_if_connection_generation: Callable[[int, Callable[[], Any]], Any]


@dataclass(frozen=True, slots=True)
class WebThreadToolDetailPreparation:
    client_id: str
    thread_id: str
    turn_id: str
    item_id: str
    view: ToolDetailView
    change_index: int | None
    cursor: str | None
    document: WebDocumentOperationReceipt
    observation: WebThreadReadObservationReceipt
    connection_generation: int
    deadline: float
    runtime_epoch: str
    revision: int


@dataclass(frozen=True, slots=True)
class WebThreadConversationSearchPreparation:
    client_id: str
    thread_id: str
    query: str
    cursor: str | None
    document: WebDocumentOperationReceipt
    observation: WebThreadReadObservationReceipt
    connection_generation: int
    deadline: float
    runtime_epoch: str
    revision: int


WebThreadInspectionPreparation = (
    WebThreadToolDetailPreparation | WebThreadConversationSearchPreparation
)


@dataclass(frozen=True, slots=True)
class WebThreadInspectionEffect:
    snapshot: ThreadSnapshot | None = None
    payload: dict[str, Any] | None = None
    error: Exception | None = None
    profile: WebWriterProfile | None = None


def classify_thread_inspection_error(
    exc: Exception,
    *,
    operation: str,
) -> WebRuntimeError:
    """Map transport/protocol failures without creating a fallback read path."""

    if isinstance(exc, WebRuntimeError):
        return exc
    details = {"operation": operation}
    if isinstance(exc, TimeoutError):
        return WebRuntimeError(
            "Codex did not finish this conversation inspection before its deadline.",
            code="thread_inspection_timeout",
            status=504,
            details=details,
        )
    if isinstance(exc, CodexRpcPreSendError) and isinstance(exc.cause, TimeoutError):
        return WebRuntimeError(
            "Codex did not finish this conversation inspection before its deadline.",
            code="thread_inspection_timeout",
            status=504,
            details=details,
        )
    if isinstance(exc, CodexRpcProtocolError):
        return WebRuntimeError(
            "Codex returned malformed conversation inspection data.",
            code="thread_inspection_protocol_error",
            status=502,
            details=details,
        )
    if isinstance(exc, CodexRpcError) and exc.error.get("code") == -32601:
        return WebRuntimeError(
            "The connected Codex runtime does not provide this paginated inspection method.",
            code="thread_inspection_upstream_unsupported",
            status=503,
            details=details,
        )
    if isinstance(
        exc,
        (CodexRpcPreSendError, CodexRpcTransportError, CodexRpcError),
    ):
        return WebRuntimeError(
            "Codex conversation inspection is temporarily unavailable.",
            code="thread_inspection_upstream_unavailable",
            status=503,
            details=details,
        )
    return WebRuntimeError(
        "Focus could not validate the conversation inspection response.",
        code="thread_inspection_protocol_error",
        status=502,
        details=details,
    )


class WebThreadInspectionService:
    """Coordinate bounded point reads without caching transcript or tool data."""

    def __init__(
        self,
        *,
        documents: WebDocumentRegistry,
        selection: WebSelectionCoordinator,
        direct_targets: WebDirectThreadTargetCoordinator,
        ports: WebThreadInspectionPorts,
        runtime_context_guard: RuntimeContextGuard,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: float = _INSPECTION_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(ports, WebThreadInspectionPorts):
            raise TypeError("Web thread inspection requires typed ports")
        if not callable(monotonic):
            raise TypeError("Web thread inspection requires a monotonic clock")
        if not callable(runtime_context_guard):
            raise TypeError("Web thread inspection requires a RuntimeLoop guard")
        if float(timeout_seconds) <= 0:
            raise ValueError("Web thread inspection timeout must be positive")
        self._documents = documents
        self._selection = selection
        self._direct_targets = direct_targets
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._monotonic = monotonic
        self._timeout_seconds = float(timeout_seconds)

    def prepare_tool_detail(
        self,
        client_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        *,
        view: str,
        change_index: int | None = None,
        cursor: str | None = None,
    ) -> WebThreadToolDetailPreparation:
        """Freeze one exact tool-detail request without entering app-server I/O."""

        normalized_turn_id = self._require_locator(turn_id, field="turn_id")
        normalized_item_id = self._require_locator(item_id, field="item_id")
        normalized_view = self._require_tool_detail_view(view)
        normalized_change_index = self._require_change_index(change_index)
        normalized_cursor = self._require_tool_cursor(cursor)
        (
            normalized_client_id,
            normalized_thread_id,
            document,
            observation,
            generation,
            coordinates,
        ) = self._prepare_selected_thread(
            client_id,
            thread_id,
            operation="thread_tool_detail",
        )
        return WebThreadToolDetailPreparation(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            turn_id=normalized_turn_id,
            item_id=normalized_item_id,
            view=normalized_view,
            change_index=normalized_change_index,
            cursor=normalized_cursor,
            document=document,
            observation=observation,
            connection_generation=generation,
            deadline=self._monotonic() + self._timeout_seconds,
            runtime_epoch=str(coordinates["runtime_epoch"]),
            revision=int(coordinates["revision"]),
        )

    def execute_inspection(
        self,
        prepared: WebThreadInspectionPreparation,
    ) -> WebThreadInspectionEffect:
        """Perform only generation-pinned inspection reads and projection."""

        if not isinstance(
            prepared,
            (WebThreadToolDetailPreparation, WebThreadConversationSearchPreparation),
        ):
            raise TypeError("prepared Web thread inspection is required")
        profile: WebWriterProfile | None = None
        try:
            profile = self._selection.load_profile_snapshot(prepared.client_id)
            if profile is None or profile.selected_thread_id != prepared.thread_id:
                raise WebRuntimeError(
                    "Select this thread before inspecting its conversation.",
                    code="thread_not_selected",
                    status=409,
                )
            if isinstance(prepared, WebThreadToolDetailPreparation):
                effect = self._execute_tool_detail(prepared)
            else:
                effect = self._execute_conversation_search(prepared)
            return replace(effect, profile=profile)
        except Exception as exc:
            return WebThreadInspectionEffect(error=exc, profile=profile)

    def _execute_tool_detail(
        self,
        prepared: WebThreadToolDetailPreparation,
    ) -> WebThreadInspectionEffect:
        snapshot = self._read_paginated_thread(
            prepared,
            operation="inspect tool details",
        )
        page = self._ports.list_thread_items(
            prepared.thread_id,
            turn_id=prepared.turn_id,
            cursor=prepared.cursor,
            limit=_TOOL_PAGE_LIMIT,
            sort_direction="asc",
            timeout=self._remaining(
                prepared.deadline,
                operation="tool detail",
            ),
            expected_connection_generation=prepared.connection_generation,
        )
        self._remaining(prepared.deadline, operation="tool detail")
        if not isinstance(page, ThreadItemsPage):
            raise CodexRpcProtocolError(
                "thread/items/list",
                "Codex thread/items/list returned an invalid page",
            )
        if len(page.items) > _TOOL_PAGE_LIMIT:
            raise CodexRpcProtocolError(
                "thread/items/list",
                "Codex thread/items/list exceeded the requested page limit",
            )
        next_cursor = page.next_cursor
        if next_cursor is not None:
            if not page.items:
                raise CodexRpcProtocolError(
                    "thread/items/list",
                    "Codex thread/items/list returned an empty advancing page",
                )
            normalized_next_cursor = self._require_tool_cursor(
                next_cursor,
                field="next_cursor",
            )
            if normalized_next_cursor == prepared.cursor:
                raise CodexRpcProtocolError(
                    "thread/items/list",
                    "Codex thread/items/list returned a non-progressing cursor",
                )
            next_cursor = normalized_next_cursor

        for entry in page.items:
            if entry.turn_id != prepared.turn_id:
                raise CodexRpcProtocolError(
                    "thread/items/list",
                    "Codex thread/items/list returned an item from another turn",
                )
            item = entry.item
            if str(item.get("id", "") or "").strip() != prepared.item_id:
                continue
            detail = self._project_exact_tool_detail(
                item,
                turn_id=prepared.turn_id,
                change_index=prepared.change_index,
                view=prepared.view,
            )
            payload = {
                "runtime_epoch": prepared.runtime_epoch,
                "revision": prepared.revision,
                "thread_id": prepared.thread_id,
                "turn_id": prepared.turn_id,
                "item_id": prepared.item_id,
                "kind": str(item.get("type", "") or "").strip(),
                "change_index": prepared.change_index,
                "view": prepared.view,
                "status": "found",
                "cursor": prepared.cursor,
                "next_cursor": None,
                "scanned_items": len(page.items),
                "detail": detail,
            }
            if prepared.view == "preview":
                self._require_encoded_limit(
                    payload,
                    limit=_TOOL_DETAIL_PREVIEW_MAX_BYTES,
                    code="tool_detail_too_large",
                    message="This tool-detail preview exceeds the Focus Web response limit.",
                )
            return WebThreadInspectionEffect(snapshot=snapshot, payload=payload)

        status = "scanning" if next_cursor is not None else "not_found"
        payload = {
            "runtime_epoch": prepared.runtime_epoch,
            "revision": prepared.revision,
            "thread_id": prepared.thread_id,
            "turn_id": prepared.turn_id,
            "item_id": prepared.item_id,
            "kind": (
                "commandExecution"
                if prepared.change_index is None
                else "fileChange"
            ),
            "change_index": prepared.change_index,
            "view": prepared.view,
            "status": status,
            "cursor": prepared.cursor,
            "next_cursor": next_cursor,
            "scanned_items": len(page.items),
            "detail": None,
        }
        self._require_encoded_limit(
            payload,
            limit=_TOOL_DETAIL_PREVIEW_MAX_BYTES,
            code="tool_detail_scan_response_too_large",
            message="This tool-detail scan page exceeds the Focus Web response limit.",
        )
        return WebThreadInspectionEffect(snapshot=snapshot, payload=payload)

    def prepare_conversation_search(
        self,
        client_id: str,
        thread_id: str,
        *,
        query: str,
        cursor: str | None = None,
    ) -> WebThreadConversationSearchPreparation:
        normalized_query = self._require_query(query)
        normalized_cursor = self._require_search_cursor(cursor)
        (
            normalized_client_id,
            normalized_thread_id,
            document,
            observation,
            generation,
            coordinates,
        ) = self._prepare_selected_thread(
            client_id,
            thread_id,
            operation="thread_conversation_search",
        )
        return WebThreadConversationSearchPreparation(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            query=normalized_query,
            cursor=normalized_cursor,
            document=document,
            observation=observation,
            connection_generation=generation,
            deadline=self._monotonic() + self._timeout_seconds,
            runtime_epoch=str(coordinates["runtime_epoch"]),
            revision=int(coordinates["revision"]),
        )

    def _execute_conversation_search(
        self,
        prepared: WebThreadConversationSearchPreparation,
    ) -> WebThreadInspectionEffect:
        snapshot = self._read_paginated_thread(
            prepared,
            operation="search conversation",
        )
        page = self._ports.search_thread_occurrences(
            prepared.thread_id,
            search_term=prepared.query,
            cursor=prepared.cursor,
            limit=_SEARCH_LIMIT,
            timeout=self._remaining(
                prepared.deadline,
                operation="conversation search",
            ),
            expected_connection_generation=prepared.connection_generation,
        )
        self._remaining(prepared.deadline, operation="conversation search")
        if not isinstance(page, ThreadSearchOccurrencesPage):
            raise CodexRpcProtocolError(
                "thread/searchOccurrences",
                "Codex thread/searchOccurrences returned an invalid page",
            )
        if len(page.occurrences) > _SEARCH_LIMIT:
            raise CodexRpcProtocolError(
                "thread/searchOccurrences",
                "Codex thread/searchOccurrences exceeded the requested page limit",
            )
        occurrences: list[dict[str, Any]] = []
        for occurrence in page.occurrences:
            if len(occurrence.snippet) > _SEARCH_SNIPPET_MAX_CHARS:
                raise CodexRpcProtocolError(
                    "thread/searchOccurrences",
                    "Codex thread/searchOccurrences returned an oversized snippet",
                )
            turn_cursor = self._require_upstream_search_cursor(
                occurrence.turn_cursor,
                field="turnCursor",
            )
            start, end = occurrence.snippet_match_range
            occurrences.append(
                {
                    "turn_id": occurrence.turn_id,
                    "item_id": occurrence.item_id,
                    "snippet": occurrence.snippet,
                    "snippet_match_range": {"start": start, "end": end},
                    "turn_cursor": turn_cursor,
                }
            )
        next_cursor = (
            None
            if page.next_cursor is None
            else self._require_upstream_search_cursor(
                page.next_cursor,
                field="nextCursor",
            )
        )
        payload = {
            "runtime_epoch": prepared.runtime_epoch,
            "revision": prepared.revision,
            "thread_id": prepared.thread_id,
            "query": prepared.query,
            "cursor": prepared.cursor,
            "occurrences": occurrences,
            "next_cursor": next_cursor,
        }
        self._require_encoded_limit(
            payload,
            limit=_SEARCH_RESPONSE_MAX_BYTES,
            code="conversation_search_response_too_large",
            message="This conversation search page exceeds the Focus Web response limit.",
        )
        return WebThreadInspectionEffect(snapshot=snapshot, payload=payload)

    def settle_inspection(
        self,
        prepared: WebThreadInspectionPreparation,
        effect: WebThreadInspectionEffect,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._settle_inspection_current_generation(prepared, effect),
        )

    def _settle_inspection_current_generation(
        self,
        prepared: WebThreadInspectionPreparation,
        effect: WebThreadInspectionEffect,
    ) -> dict[str, Any]:
        """Validate, commit, and return one exact-generation inspection."""

        self._require_current_document(prepared)
        try:
            self._selection.require_history_ready_snapshot(
                effect.profile,
                prepared.client_id,
                prepared.thread_id,
            )
        except WebSelectionNotReady as exc:
            raise WebRuntimeError(
                "Select this thread before inspecting its conversation.",
                code="thread_not_selected",
                status=409,
            ) from exc
        self._require_current_observation(prepared)
        self._require_current_coordinates(prepared)
        if effect.error is None and (
            effect.snapshot is None or effect.payload is None
        ):
            raise RuntimeError("Web thread inspection returned no exact result")
        if effect.error is not None:
            if isinstance(effect.error, WebRuntimeError) and effect.error.code == (
                "subagent_detail_only"
            ):
                self._direct_targets.clear_rejected_direct_thread(
                    prepared.thread_id,
                    reason="web_direct_target_selection_cleared",
                )
            operation = (
                "tool_detail"
                if isinstance(prepared, WebThreadToolDetailPreparation)
                else "conversation_search"
            )
            classified = classify_thread_inspection_error(
                effect.error,
                operation=operation,
            )
            if classified is effect.error:
                raise effect.error
            raise classified from effect.error
        assert effect.snapshot is not None
        assert effect.payload is not None
        self._direct_targets.remember_verified_snapshot(effect.snapshot)
        return effect.payload

    def _require_current_coordinates(
        self,
        prepared: WebThreadInspectionPreparation,
    ) -> None:
        coordinates = self._coordinates()
        if (
            coordinates.get("runtime_epoch") == prepared.runtime_epoch
            and coordinates.get("revision") == prepared.revision
        ):
            return
        raise WebRuntimeError(
            "A newer Web runtime event replaced this conversation inspection.",
            code="stale_thread_read",
            status=409,
            details={"thread_id": prepared.thread_id},
        )

    def _require_current_observation(
        self,
        prepared: WebThreadInspectionPreparation,
    ) -> None:
        if self._ports.observation_is_current(prepared.observation):
            return
        raise WebRuntimeError(
            "A newer thread notification replaced this conversation inspection.",
            code="stale_thread_read",
            status=409,
            details={"thread_id": prepared.thread_id},
        )

    def _require_current_document(
        self,
        prepared: WebThreadInspectionPreparation,
    ) -> None:
        if self._documents.operation_is_current(prepared.document):
            return
        raise WebRuntimeError(
            "This browser inspection was replaced before it completed.",
            code="stale_document_read",
            status=409,
            details={"thread_id": prepared.thread_id},
        )

    def _read_paginated_thread(
        self,
        prepared: WebThreadInspectionPreparation,
        *,
        operation: str,
    ) -> ThreadSnapshot:
        snapshot = self._ports.read_thread(
            prepared.thread_id,
            False,
            timeout=self._remaining(prepared.deadline, operation=operation),
            expected_connection_generation=prepared.connection_generation,
        )
        self._remaining(prepared.deadline, operation=operation)
        require_web_direct_thread_snapshot(
            snapshot,
            thread_id=prepared.thread_id,
            operation=operation,
        )
        if snapshot.history_mode != "paginated":
            raise WebRuntimeError(
                "Tool details and conversation search are available only for new paginated threads.",
                code="thread_inspection_unavailable",
                status=409,
                details={"thread_id": prepared.thread_id},
            )
        return snapshot

    def _prepare_selected_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        operation: str,
    ) -> tuple[
        str,
        str,
        WebDocumentOperationReceipt,
        WebThreadReadObservationReceipt,
        int,
        dict[str, Any],
    ]:
        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        normalized_thread_id = require_web_thread_id(thread_id)
        self._require_materialized_thread(
            normalized_client_id,
            normalized_thread_id,
        )
        coordinates = self._coordinates()
        return (
            normalized_client_id,
            normalized_thread_id,
            self._documents.begin_operation(
                normalized_client_id,
                operation=operation,
                target_thread_id=normalized_thread_id,
            ),
            self._ports.capture_observation(normalized_thread_id),
            self._ports.capture_connection_generation(),
            coordinates,
        )

    def _require_materialized_thread(self, client_id: str, thread_id: str) -> None:
        try:
            self._selection.require_materialized_thread(client_id, thread_id)
        except WebSelectionNotReady as exc:
            raise WebRuntimeError(
                "Select this thread before inspecting its conversation.",
                code="thread_not_selected",
                status=409,
            ) from exc

    @staticmethod
    def _project_exact_tool_detail(
        item: dict[str, Any],
        *,
        turn_id: str,
        change_index: int | None,
        view: ToolDetailView,
    ) -> dict[str, Any]:
        item_type = str(item.get("type", "") or "").strip()
        if item_type not in _INSPECTABLE_TOOL_TYPES:
            raise WebRuntimeError(
                "This item does not provide inspectable command or file-change detail.",
                code="tool_detail_unsupported_item",
                status=409,
            )
        status = str(item.get("status", "") or "").strip()
        if status not in _TERMINAL_TOOL_STATUSES:
            raise WebRuntimeError(
                "Tool detail is available after the item reaches a terminal state.",
                code="tool_detail_not_terminal",
                status=409,
            )
        if (
            (item_type == "commandExecution" and change_index is not None)
            or (item_type == "fileChange" and change_index is None)
        ):
            raise WebRuntimeError(
                "The requested tool locator does not match this item.",
                code="tool_detail_unsupported_item",
                status=409,
            )
        if view == "full":
            try:
                return {
                    "view": "full",
                    "source": project_tool_detail_source(
                        item,
                        change_index=change_index,
                    ),
                }
            except ValueError as exc:
                raise CodexRpcProtocolError(
                    "thread/items/list",
                    "Codex thread/items/list returned malformed full tool detail",
                ) from exc
        try:
            return {
                "view": "preview",
                "tool": project_thread_inspection_tool(
                    item,
                    turn_id,
                    change_index,
                ),
            }
        except ValueError as exc:
            raise WebRuntimeError(
                "The requested tool locator does not match this item.",
                code="tool_detail_unsupported_item",
                status=409,
            ) from exc

    def _remaining(self, deadline: float, *, operation: str) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{operation} exceeded its inspection deadline")
        return remaining

    def _coordinates(self) -> dict[str, Any]:
        coordinates = self._ports.coordinates()
        if not isinstance(coordinates, dict):
            raise CodexRpcProtocolError(
                "focus/web/coordinates",
                "Focus Web projection returned invalid coordinates",
            )
        return dict(coordinates)

    @staticmethod
    def _require_locator(value: object, *, field: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
        ):
            raise WebRuntimeError(
                f"A valid {field} is required for tool detail.",
                code="invalid_tool_detail_locator",
                status=400,
            )
        return value

    @staticmethod
    def _require_change_index(value: object) -> int | None:
        if value is None:
            return None
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > _U32_MAX
        ):
            raise WebRuntimeError(
                "Tool detail change_index must be an unsigned 32-bit integer.",
                code="invalid_tool_detail_locator",
                status=400,
            )
        return value

    @staticmethod
    def _require_tool_detail_view(value: object) -> ToolDetailView:
        if value == "preview":
            return "preview"
        if value == "full":
            return "full"
        raise WebRuntimeError(
            "Tool detail view must be preview or full.",
            code="invalid_tool_detail_view",
            status=400,
        )

    @staticmethod
    def _require_tool_cursor(
        value: object,
        *,
        field: str = "cursor",
    ) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > _TOOL_CURSOR_MAX_CHARS
        ):
            raise WebRuntimeError(
                "Tool detail cursor is invalid.",
                code="invalid_inspection_cursor",
                status=400,
                details={"field": field},
            )
        return value

    @staticmethod
    def _require_query(value: object) -> str:
        if not isinstance(value, str):
            raise WebRuntimeError(
                "Conversation search requires a query of 1..256 Unicode characters.",
                code="invalid_conversation_search",
                status=400,
            )
        normalized = value.strip()
        if not 1 <= len(normalized) <= _SEARCH_QUERY_MAX_CHARS:
            raise WebRuntimeError(
                "Conversation search requires a query of 1..256 Unicode characters.",
                code="invalid_conversation_search",
                status=400,
            )
        return normalized

    @staticmethod
    def _require_search_cursor(value: object) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > _SEARCH_CURSOR_MAX_CHARS
        ):
            raise WebRuntimeError(
                "Conversation search cursor is invalid.",
                code="invalid_inspection_cursor",
                status=400,
            )
        return value

    @staticmethod
    def _require_upstream_search_cursor(value: object, *, field: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > _SEARCH_CURSOR_MAX_CHARS
        ):
            raise CodexRpcProtocolError(
                "thread/searchOccurrences",
                f"Codex thread/searchOccurrences returned an invalid {field}",
            )
        return value

    @staticmethod
    def _require_encoded_limit(
        payload: dict[str, Any],
        *,
        limit: int,
        code: str,
        message: str,
    ) -> None:
        encoded = encode_thread_inspection_json(payload)
        if len(encoded) > limit:
            raise WebRuntimeError(message, code=code, status=413)
