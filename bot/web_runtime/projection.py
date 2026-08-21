"""Focus-owned DTO projection for the browser frontend."""

from __future__ import annotations

import copy
import json
import logging
import pathlib
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict
from typing import Any

from bot.adapters.base import (
    RuntimeModelSummary,
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.focus_web_wire_catalog import require_focus_web_event_type
from bot.interaction_contract import normalize_interaction_request
from bot.stores.interaction_lease_store import InteractionLease
from bot.web_runtime.tool_output_presentation import (
    CachedToolOutputPresentation,
    INTERNAL_PRESENTATION_METADATA_KEY,
    MAX_VISIBLE,
    ToolOutputPresentation,
    ToolOutputPresentationBudget,
    file_change_fallback_output,
    generic_tool_output,
    present_tool_output,
    safe_inline_image_data_url,
)

WebEventListener = Callable[[dict[str, Any]], None]
logger = logging.getLogger(__name__)
_ATTACHMENT_ENVELOPE_START = "[[focus.attachments.v1]]\n"
_ATTACHMENT_ENVELOPE_END = "\n[[/focus.attachments.v1]]"
_ATTACHMENT_REQUEST_START = "\n[[focus.user_request]]\n"
_INSPECTION_TERMINAL_TOOL_STATUSES = frozenset(
    {"completed", "failed", "declined"}
)
_INSPECTION_CHANGE_INDEX_MAX = (1 << 32) - 1


class FocusWebProjection:
    """Process-local revision clock and invalidation event fan-out.

    Revisions are intentionally not durable. A browser that observes a gap or
    a different runtime epoch must reload the HTTP snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runtime_epoch = str(uuid.uuid4())
        self._revision = 0
        self._listeners: set[WebEventListener] = set()

    def coordinates(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_epoch": self._runtime_epoch,
                "revision": self._revision,
            }

    def subscribe(self, listener: WebEventListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def publish(
        self,
        event_type: str,
        *,
        thread_id: str = "",
        reason: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_type = require_focus_web_event_type(event_type)
        with self._lock:
            self._revision += 1
            event = {
                "type": event_type,
                "runtime_epoch": self._runtime_epoch,
                "revision": self._revision,
                "thread_id": str(thread_id or "").strip(),
                "reason": str(reason or "").strip(),
                "occurred_at": time.time(),
            }
            if detail:
                event["detail"] = dict(detail)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(dict(event))
            except Exception:
                # Projection fan-out happens after domain mutations in many
                # call paths.  A closed event loop or one faulty subscriber
                # must not turn a successful mutation into an HTTP 500, nor
                # prevent the remaining subscribers from seeing the revision.
                logger.exception(
                    "Focus Web projection listener failed: type=%s revision=%s",
                    event["type"],
                    event["revision"],
                )
        return event


def project_model(model: RuntimeModelSummary) -> dict[str, Any]:
    efforts = model.supported_reasoning_efforts
    return {
        "id": model.model,
        "catalog_id": model.catalog_id or model.model,
        "model": model.model,
        "display_name": model.display_name or model.model,
        "description": model.description,
        "is_default": model.is_default,
        "hidden": model.hidden,
        "default_reasoning_effort": model.default_reasoning_effort or "",
        "supported_reasoning_efforts": [
            {
                "effort": option.reasoning_effort,
                "description": option.description,
            }
            for option in (efforts or [])
        ],
        "input_modalities": (
            list(model.input_modalities)
            if model.input_modalities is not None
            else None
        ),
        "supports_personality": model.supports_personality,
        "service_tiers": [
            {
                "id": tier.id,
                "name": tier.name,
                "description": tier.description,
            }
            for tier in (model.service_tiers or [])
        ],
        "default_service_tier": model.default_service_tier or "",
        "upgrade": model.upgrade or "",
        "upgrade_info": (
            {
                "model": model.upgrade_info.model,
                "upgrade_copy": model.upgrade_info.upgrade_copy or "",
                "model_link": model.upgrade_info.model_link or "",
                "migration_markdown": model.upgrade_info.migration_markdown or "",
            }
            if model.upgrade_info is not None
            else None
        ),
    }


def project_owner(
    lease: InteractionLease | None,
    *,
    client_id: str = "",
) -> dict[str, Any]:
    if lease is None:
        return {
            "kind": "none",
            "holder_id": "",
            "relation": "none",
            "label": "No active writer",
        }
    holder = lease.holder
    expected_holder_id = f"web:{str(client_id or '').strip()}"
    relation = "self" if holder.kind == "web" and holder.holder_id == expected_holder_id else "other"
    if holder.kind == "feishu":
        label = "Feishu"
    elif holder.kind == "fcodex":
        label = "focus / fcodex"
    elif holder.kind == "web":
        label = "This browser" if relation == "self" else "Another browser"
    else:
        label = holder.kind or "Another frontend"
    return {
        "kind": holder.kind,
        # Internal holder ids can contain Feishu user/chat identifiers. The
        # browser needs only the relation and a human-readable label.
        "holder_id": "",
        "relation": relation,
        "label": label,
    }


def project_thread_summary(
    summary: ThreadSummary,
    *,
    owner: dict[str, Any] | None = None,
    pending_interaction: str = "none",
    loaded_instance: str = "",
    loaded_state_verified: bool = True,
    observed_here: bool = False,
    selectable: bool = True,
    unavailable_reason: str = "",
    action_capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    # This is a product DTO, not a hint for an authorization decision. A
    # missing projection must hide mutable browser controls rather than make a
    # best-effort guess that an action will be accepted.
    projected_action_capabilities = {
        "rename": False,
        "archive": False,
        "unarchive": False,
        "delete": False,
        "compact": False,
        "fork": False,
        "export": False,
        "review": False,
        "goal": False,
    }
    if action_capabilities is not None:
        for name in projected_action_capabilities:
            projected_action_capabilities[name] = bool(action_capabilities.get(name, False))
    return {
        "id": summary.thread_id,
        "title": summary.title,
        "name": summary.name,
        "preview": summary.preview,
        "cwd": summary.cwd,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "source": summary.source,
        "status": summary.status,
        "active_flags": list(summary.active_flags),
        "model_provider": summary.model_provider or "",
        "service_name": summary.service_name or "",
        "session_id": summary.session_id or "",
        "parent_thread_id": summary.parent_thread_id or "",
        "can_accept_direct_input": summary.can_accept_direct_input,
        "thread_source": summary.thread_source or "",
        "ephemeral": bool(summary.ephemeral),
        "agent_nickname": summary.agent_nickname or "",
        "agent_role": summary.agent_role or "",
        "subagent_kind": summary.subagent_kind or "",
        "history_mode": summary.history_mode or "unknown",
        "owner": owner or project_owner(None),
        "pending_interaction": pending_interaction,
        "loaded_instance": str(loaded_instance or "").strip(),
        "loaded_state_verified": bool(loaded_state_verified),
        "observed_here": bool(observed_here),
        "selectable": bool(selectable),
        "unavailable_reason": str(unavailable_reason or "").strip(),
        "action_capabilities": projected_action_capabilities,
    }


def project_goal(goal: ThreadGoalSummary | None) -> dict[str, Any] | None:
    if goal is None:
        return None
    token_budget = goal.token_budget
    remaining_tokens = None if token_budget is None else max(token_budget - goal.tokens_used, 0)
    return {
        "goal_id": goal.thread_id,
        "objective": goal.objective,
        "status": goal.status,
        "tokens_used": goal.tokens_used,
        "wall_clock_ms": max(goal.time_used_seconds, 0) * 1000,
        "budget": {
            "token_budget": token_budget,
            "remaining_tokens": remaining_tokens,
            "turn_budget": None,
            "remaining_turns": None,
            "wall_clock_budget_ms": None,
            "remaining_wall_clock_ms": None,
            "over_budget": bool(token_budget is not None and goal.tokens_used > token_budget),
        },
    }


def project_goal_payload(raw_goal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw_goal, dict):
        return None
    token_budget_raw = raw_goal.get("tokenBudget", raw_goal.get("token_budget"))
    return project_goal(
        ThreadGoalSummary(
            thread_id=str(
                raw_goal.get("threadId", raw_goal.get("thread_id", raw_goal.get("goal_id", "")))
                or ""
            ).strip(),
            objective=str(raw_goal.get("objective", "") or "").strip(),
            status=str(raw_goal.get("status", "") or "").strip(),
            token_budget=int(token_budget_raw) if token_budget_raw is not None else None,
            tokens_used=int(raw_goal.get("tokensUsed", raw_goal.get("tokens_used", 0)) or 0),
            time_used_seconds=int(
                raw_goal.get("timeUsedSeconds", raw_goal.get("time_used_seconds", 0)) or 0
            ),
        )
    )


def project_thread_snapshot(
    snapshot: ThreadSnapshot,
    *,
    owner: dict[str, Any],
    loaded_instance: str = "",
    observed_here: bool = False,
    pending_requests: Iterable[dict[str, Any]],
    coordinates: dict[str, Any],
    older_turn_cursor: str | None = None,
    goal: ThreadGoalSummary | None = None,
    token_usage: dict[str, Any] | None = None,
    token_usage_available: bool = False,
    active_turn_context: dict[str, Any] | None = None,
    attachment_url_for_path: Callable[[str], str] | None = None,
    attachment_url_for_id: Callable[[str], str] | None = None,
    action_capabilities: dict[str, bool] | None = None,
) -> dict[str, Any]:
    requests = [project_pending_request(item) for item in pending_requests]
    projected_turns = project_turns(
        snapshot.turns,
        attachment_url_for_path=attachment_url_for_path,
        attachment_url_for_id=attachment_url_for_id,
    )
    active_turn_id = ""
    active_turn_status = ""
    if snapshot.turns:
        latest = snapshot.turns[-1]
        if isinstance(latest, dict):
            latest_status = str(latest.get("status", "") or "").strip()
            if latest_status == "inProgress":
                active_turn_id = str(latest.get("id", "") or "").strip()
                active_turn_status = latest_status
    projected_active_turn_context = None
    if (
        active_turn_id
        and isinstance(active_turn_context, dict)
        and str(active_turn_context.get("turn_id", "") or "").strip()
        == active_turn_id
    ):
        projected_active_turn_context = copy.deepcopy(active_turn_context)
    return {
        **coordinates,
        "thread": project_thread_summary(
            snapshot.summary,
            owner=owner,
            pending_interaction=_pending_interaction_kind(requests),
            loaded_instance=loaded_instance,
            observed_here=observed_here,
            action_capabilities=action_capabilities,
        ),
        "turns": projected_turns,
        "active_turn_id": active_turn_id,
        "active_turn_status": active_turn_status,
        "active_turn_context": projected_active_turn_context,
        "pending_requests": requests,
        "tasks": project_subagent_tasks(snapshot.turns),
        "older_turn_cursor": older_turn_cursor or "",
        "has_more_turns": bool(older_turn_cursor),
        "goal": project_goal(goal),
        "token_usage": dict(token_usage) if isinstance(token_usage, dict) else None,
        "token_usage_available": bool(token_usage_available and isinstance(token_usage, dict)),
    }


def project_turn_page(
    turns: Iterable[dict[str, Any]],
    *,
    items_view: str,
    page_cursor: str | None,
    next_cursor: str | None,
    coordinates: dict[str, Any],
    attachment_url_for_path: Callable[[str], str] | None = None,
    attachment_url_for_id: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    if items_view == "summary":
        projected_turns = _project_summary_user_prompts(turns)
    elif items_view == "full":
        projected_turns = project_turns(
            turns,
            attachment_url_for_path=attachment_url_for_path,
            attachment_url_for_id=attachment_url_for_id,
        )
    else:
        raise ValueError("turn page items_view must be summary or full")
    return {
        **coordinates,
        "items_view": items_view,
        "page_cursor": page_cursor or "",
        "turns": projected_turns,
        "older_turn_cursor": next_cursor or "",
        "has_more_turns": bool(next_cursor),
    }


def project_thread_inspection_tool(
    item: dict[str, Any],
    turn_id: str,
    change_index: int | None,
) -> dict[str, Any]:
    """Project one exact inspectable upstream item through the tool boundary."""

    if not isinstance(item, dict):
        raise ValueError("thread inspection item must be an object")
    normalized_turn_id = str(turn_id or "").strip()
    item_id = str(item.get("id", "") or "").strip()
    item_type = str(item.get("type", "") or "").strip()
    status = str(item.get("status", "") or "").strip()
    if not normalized_turn_id or not item_id:
        raise ValueError("thread inspection tool locator is incomplete")
    if status not in _INSPECTION_TERMINAL_TOOL_STATUSES:
        raise ValueError("thread inspection tool must be terminal")

    projected_item = dict(item)
    if item_type == "commandExecution":
        if change_index is not None:
            raise ValueError("commandExecution detail cannot have a change index")
    elif item_type == "fileChange":
        if (
            not isinstance(change_index, int)
            or isinstance(change_index, bool)
            or change_index < 0
            or change_index > _INSPECTION_CHANGE_INDEX_MAX
        ):
            raise ValueError("fileChange detail requires a non-negative change index")
        changes = item.get("changes")
        if not isinstance(changes, list) or change_index >= len(changes):
            raise ValueError("fileChange detail change index is outside the item")
        change = changes[change_index]
        if not isinstance(change, dict):
            raise ValueError("fileChange detail target is not an object")
        projected_item["changes"] = [change]
        if len(changes) > 1:
            projected_item["id"] = f"{item_id}:{change_index + 1}"
    else:
        raise ValueError("thread inspection supports only commandExecution and fileChange")

    tools = _project_tools(
        projected_item,
        presentation_budget=ToolOutputPresentationBudget(),
    )
    if len(tools) != 1:
        raise ValueError("thread inspection item did not project to one semantic tool")
    tool = tools[0]
    tool["inspectionLocator"] = {
        "turn_id": normalized_turn_id,
        "item_id": item_id,
        "kind": item_type,
        "change_index": change_index,
    }
    return tool


def _project_summary_user_prompts(
    turns: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project only bounded first-user prompt locators from summary turns."""

    projected: list[dict[str, Any]] = []
    for raw_turn in turns:
        if not isinstance(raw_turn, dict):
            continue
        turn_id = str(raw_turn.get("id", "") or "").strip()
        if not turn_id:
            continue
        items = raw_turn.get("items") if isinstance(raw_turn.get("items"), list) else []
        first_user = next(
            (
                item
                for item in items
                if isinstance(item, dict)
                and str(item.get("type", "") or "").strip() == "userMessage"
            ),
            None,
        )
        if first_user is None:
            continue
        text_parts, _attachments = _project_user_content(first_user.get("content"))
        text, title_truncated = _bounded_summary_prompt_text("\n\n".join(text_parts))
        projected.append(
            {
                "id": f"{turn_id}:user",
                "role": "user",
                "no": len(projected) + 1,
                "text": text,
                "title_truncated": title_truncated,
            }
        )
    return projected


def _bounded_summary_prompt_text(
    text: str,
    *,
    limit: int = 160,
) -> tuple[str, bool]:
    """Return one display-ready title and whether visible content was omitted."""

    if limit <= 0:
        return "", bool(str(text or "").strip())
    result: list[str] = []
    pending_space = False
    for character in str(text or ""):
        if character.isspace():
            pending_space = bool(result)
            continue
        if pending_space:
            if len(result) >= limit:
                return "".join(result[: limit - 1]) + "…", True
            result.append(" ")
            pending_space = False
        if len(result) >= limit:
            return "".join(result[: limit - 1]) + "…", True
        result.append(character)
    return "".join(result), False


def project_pending_request(pending: dict[str, Any]) -> dict[str, Any]:
    method = str(pending.get("method", "") or "").strip()
    params = pending.get("params") if isinstance(pending.get("params"), dict) else {}
    normalized = normalize_interaction_request(method, params)
    projected_params = dict(normalized["params"])
    visible_at_ms = int(pending.get("auto_resolution_visible_at_ms", 0) or 0)
    due_at_ms = int(pending.get("auto_resolution_due_at_ms", 0) or 0)
    if visible_at_ms and due_at_ms:
        projected_params["autoResolutionVisibleAtMs"] = visible_at_ms
        projected_params["autoResolutionDueAtMs"] = due_at_ms
    return {
        "id": str(pending.get("request_key", "") or "").strip(),
        "connection_generation": int(pending.get("connection_generation", 0) or 0),
        "response_capability": str(
            pending.get("response_capability", "") or ""
        ).strip(),
        "kind": normalized["kind"],
        "method": method,
        "thread_id": str(pending.get("thread_id", "") or "").strip(),
        "owner_thread_id": str(
            pending.get("owner_thread_id", pending.get("thread_id", "")) or ""
        ).strip(),
        "turn_id": str(pending.get("turn_id", "") or "").strip(),
        "status": str(pending.get("status", "pending") or "pending"),
        "title": normalized["title"],
        "params": projected_params,
        "actions": [
            {key: value for key, value in action.items() if key != "response"}
            for action in normalized["actions"]
        ],
        "agent_name": str(pending.get("agent_name", "") or ""),
    }

def _pending_interaction_kind(requests: list[dict[str, Any]]) -> str:
    kinds = {str(item.get("kind", "") or "") for item in requests}
    if "question" in kinds or "elicitation" in kinds:
        return "question"
    if "approval" in kinds:
        return "approval"
    return "none"


def project_turns(
    turns: Iterable[dict[str, Any]],
    *,
    attachment_url_for_path: Callable[[str], str] | None = None,
    attachment_url_for_id: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    """Project app-server turn items without changing their causal order.

    A Codex ``turn`` is not necessarily one user bubble followed by one
    assistant bubble.  In particular, ``turn/steer`` appends a second
    ``userMessage`` *inside the existing turn*, after the assistant may have
    already emitted reasoning, text, or tool items.  The browser transcript
    therefore uses a small number of contiguous semantic segments rather than
    grouping every item by role for the whole turn.

    A user item is a hard boundary even when it has no presentable text (for
    example an empty steer or a hook item).  Keep an empty user segment as the
    stable Prompt/DOM anchor; the browser renders that segment as an invisible
    anchor rather than an empty bubble.  The following assistant item still
    receives a new segment.  This preserves what the user actually interrupted
    without inventing visible content.
    """
    projected: list[dict[str, Any]] = []
    presentation_budget = ToolOutputPresentationBudget()
    line_no = 0
    for raw_turn in turns:
        if not isinstance(raw_turn, dict):
            continue
        turn_id = str(raw_turn.get("id", "") or "").strip() or f"turn-{len(projected) + 1}"
        items = raw_turn.get("items") if isinstance(raw_turn.get("items"), list) else []
        started_at = raw_turn.get("startedAt")
        completed_at = raw_turn.get("completedAt")
        duration_ms = raw_turn.get("durationMs")
        turn_status = str(raw_turn.get("status", "") or "")
        created_at = _iso_timestamp(started_at)
        assistant_metadata = {
            "createdAt": created_at,
            "durationMs": _duration_ms(duration_ms, started_at, completed_at),
            "status": turn_status,
        }
        segment_counts = {"user": 0, "assistant": 0, "compaction": 0}
        used_ids: set[str] = set()
        current_user: dict[str, Any] | None = None
        current_assistant: dict[str, Any] | None = None
        last_semantic_kind = ""

        def _segment_id(role: str) -> str:
            segment_counts[role] += 1
            suffix = "" if segment_counts[role] == 1 else f":{segment_counts[role]}"
            return f"{turn_id}:{role}{suffix}"

        def _append_user_segment() -> dict[str, Any]:
            nonlocal line_no, current_user
            line_no += 1
            current_user = {
                "id": _segment_id("user"),
                "role": "user",
                "no": line_no,
                "text": "",
                "attachments": [],
                "createdAt": created_at,
            }
            used_ids.add(str(current_user["id"]))
            projected.append(current_user)
            return current_user

        def _append_assistant_segment() -> dict[str, Any]:
            nonlocal line_no, current_assistant
            line_no += 1
            current_assistant = {
                "id": _segment_id("assistant"),
                "role": "assistant",
                "no": line_no,
                "text": "",
                "blocks": [],
                "tools": [],
                **assistant_metadata,
            }
            used_ids.add(str(current_assistant["id"]))
            projected.append(current_assistant)
            return current_assistant

        def _ensure_user_segment() -> dict[str, Any]:
            nonlocal current_assistant
            if last_semantic_kind != "user" or current_user is None:
                _append_user_segment()
            current_assistant = None
            assert current_user is not None
            return current_user

        def _ensure_assistant_segment() -> dict[str, Any]:
            nonlocal current_user
            if last_semantic_kind != "assistant" or current_assistant is None:
                _append_assistant_segment()
            current_user = None
            assert current_assistant is not None
            return current_assistant

        def _sync_assistant_text(segment: dict[str, Any]) -> None:
            blocks = segment.get("blocks") if isinstance(segment.get("blocks"), list) else []
            segment["text"] = "\n\n".join(
                str(block.get("text", "") or "")
                for block in blocks
                if isinstance(block, dict)
                and block.get("kind") == "text"
                and str(block.get("text", "") or "")
            )

        def _append_assistant_block(block: dict[str, Any]) -> None:
            segment = _ensure_assistant_segment()
            blocks = segment.setdefault("blocks", [])
            if isinstance(blocks, list):
                blocks.append(block)
            _sync_assistant_text(segment)

        def _append_assistant_tools(projected_tools: list[dict[str, Any]]) -> None:
            if not projected_tools:
                return
            segment = _ensure_assistant_segment()
            tools = segment.setdefault("tools", [])
            blocks = segment.setdefault("blocks", [])
            if not isinstance(tools, list) or not isinstance(blocks, list):
                return
            for tool in projected_tools:
                tools.append(tool)
                blocks.append({"kind": "tool", "tool": tool})

        def _append_compaction(raw_item: dict[str, Any]) -> None:
            nonlocal line_no, current_user, current_assistant
            segment_counts["compaction"] += 1
            # Projection ids are also the browser's raw-turn grouping key.
            # Upstream compaction item ids carry no enclosing-turn identity, so
            # retaining them here would force the browser to guess ownership
            # from adjacent user/assistant segments.  Use the same canonical
            # segment shape as every other projected role instead.
            suffix = (
                ""
                if segment_counts["compaction"] == 1
                else f":{segment_counts['compaction']}"
            )
            item_id = f"{turn_id}:compaction{suffix}"
            line_no += 1
            projected.append(
                {
                    "id": item_id,
                    "role": "compaction",
                    "no": line_no,
                    "text": "",
                    "compaction": {},
                    "createdAt": created_at,
                }
            )
            used_ids.add(item_id)
            current_user = None
            current_assistant = None

        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item_type = str(raw_item.get("type", "") or "").strip()
            if item_type == "userMessage":
                text_parts, item_attachments = _project_user_content(
                    raw_item.get("content"),
                    attachment_url_for_path=attachment_url_for_path,
                    attachment_url_for_id=attachment_url_for_id,
                )
                user_segment = (
                    _ensure_user_segment()
                    if text_parts or item_attachments or segment_counts["user"] == 0
                    else None
                )
                if user_segment is not None and (text_parts or item_attachments):
                    user_text = "\n\n".join(part for part in text_parts if part).strip()
                    if user_text:
                        existing = str(user_segment.get("text", "") or "")
                        user_segment["text"] = (
                            f"{existing}\n\n{user_text}" if existing else user_text
                        )
                    attachments = user_segment.setdefault("attachments", [])
                    if isinstance(attachments, list):
                        for attachment in item_attachments:
                            _append_attachment(attachments, attachment)
                # An empty first user item needs the raw turn's stable summary
                # locator, so it keeps an anchor-only segment.  A later empty
                # steer still creates no bubble/anchor, but in both cases the
                # next assistant item must enter a new contiguous segment.
                last_semantic_kind = "user"
                current_assistant = None
                continue
            if item_type == "hookPrompt":
                fragments = raw_item.get("fragments") if isinstance(raw_item.get("fragments"), list) else []
                text_parts = [
                    str(fragment.get("text", "") or "")
                    for fragment in fragments
                    if isinstance(fragment, dict) and str(fragment.get("text", "") or "")
                ]
                if text_parts:
                    user_segment = _ensure_user_segment()
                    hook_text = "\n\n".join(text_parts).strip()
                    if hook_text:
                        existing = str(user_segment.get("text", "") or "")
                        user_segment["text"] = (
                            f"{existing}\n\n{hook_text}" if existing else hook_text
                        )
                last_semantic_kind = "user"
                current_assistant = None
                continue
            if item_type == "agentMessage":
                text = str(raw_item.get("text", "") or "")
                item_id = str(raw_item.get("id", "") or "")
                # A started agentMessage can initially be empty.  Preserve an
                # empty block while live so a later delta has its immutable
                # app-server item id as a target.
                if text or (turn_status == "inProgress" and item_id):
                    _append_assistant_block(
                        {"kind": "text", "itemId": item_id, "text": text}
                    )
                    last_semantic_kind = "assistant"
                continue
            if item_type == "reasoning":
                reasoning = "\n\n".join(
                    str(part or "").strip()
                    for part in [*(raw_item.get("summary") or []), *(raw_item.get("content") or [])]
                    if str(part or "").strip()
                )
                item_id = str(raw_item.get("id", "") or "")
                if reasoning or (turn_status == "inProgress" and item_id):
                    _append_assistant_block(
                        {
                            "kind": "thinking",
                            "itemId": item_id,
                            "thinking": reasoning,
                        }
                    )
                    last_semantic_kind = "assistant"
                continue
            if item_type == "plan":
                text = str(raw_item.get("text", "") or "").strip()
                if text or _has_cached_generic_output(raw_item):
                    tool = _generic_tool(
                        raw_item,
                        name="Plan",
                        arg="",
                        output=generic_tool_output(raw_item) or [],
                    )
                    _admit_projected_tool_output(tool, presentation_budget)
                    _append_assistant_tools([tool])
                    last_semantic_kind = "assistant"
                continue
            if item_type == "contextCompaction":
                _append_compaction(raw_item)
                last_semantic_kind = "compaction"
                continue
            projected_tools = _project_tools(
                raw_item,
                turn_id=turn_id,
                attachment_url_for_path=attachment_url_for_path,
                presentation_budget=presentation_budget,
            )
            if projected_tools:
                _append_assistant_tools(projected_tools)
                last_semantic_kind = "assistant"

        error = raw_turn.get("error") if isinstance(raw_turn.get("error"), dict) else {}
        error_message = str(error.get("message", "") or "").strip()
        if error_message:
            _append_assistant_block({"kind": "text", "text": f"**Error:** {error_message}"})
            last_semantic_kind = "assistant"
        # When a live turn currently ends at a user/steer or compaction
        # boundary, reserve the next assistant segment now.  A stream delta
        # can then target the response after that boundary rather than being
        # appended to the assistant segment that preceded it.
        if turn_status == "inProgress" and last_semantic_kind != "assistant":
            _append_assistant_segment()
            last_semantic_kind = "assistant"
    return projected


def project_subagent_tasks(
    turns: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for raw_turn in turns:
        if not isinstance(raw_turn, dict):
            continue
        items = raw_turn.get("items") if isinstance(raw_turn.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "") or "").strip()
            if item_type == "collabAgentToolCall":
                receiver_ids = list(
                    item.get("receiverThreadIds")
                    if isinstance(item.get("receiverThreadIds"), list)
                    else []
                )
                receiver_ids.extend(
                    value
                    for value in [item.get("newThreadId"), item.get("receiverThreadId")]
                    if value
                )
                states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
                collab_tool = str(item.get("tool", "") or "")
                for raw_thread_id in receiver_ids:
                    thread_id = str(raw_thread_id or "").strip()
                    if not thread_id:
                        continue
                    previous = tasks.get(thread_id, {})
                    state = states.get(thread_id) if isinstance(states, dict) else None
                    status = str(state.get("status", "") or "") if isinstance(state, dict) else ""
                    message = str(state.get("message", "") or "") if isinstance(state, dict) else ""
                    task_state = _subagent_task_state(status, item.get("status"))
                    tasks[thread_id] = {
                        "id": thread_id,
                        "name": str(item.get("model", "") or previous.get("name") or "Codex subagent"),
                        "kind": "subagent",
                        "state": task_state,
                        "timing": "",
                        "meta": str(previous.get("meta", "") or ""),
                        "progress": (
                            message.splitlines()
                            if message
                            else list(previous.get("progress") or previous.get("output") or [])
                        ),
                        "result": list(previous.get("result") or []),
                        "metadata": list(previous.get("metadata") or []),
                        "output": message.splitlines() if message else list(previous.get("output") or []),
                        "runInBackground": False,
                        "parentToolCallId": str(
                            previous.get("parentToolCallId", "")
                            or (
                                item.get("id", "")
                                if collab_tool == "spawnAgent"
                                else ""
                            )
                            or ""
                        ),
                        "prompt": str(item.get("prompt", "") or previous.get("prompt") or ""),
                        "executionState": _subagent_execution_state(status, item.get("status")),
                    }
                continue
            if item_type != "subAgentActivity":
                continue
            thread_id = str(item.get("agentThreadId", "") or "").strip()
            if not thread_id:
                continue
            previous = tasks.get(thread_id, {})
            kind = str(item.get("kind", "") or "").strip().lower()
            agent_path = pathlib.PurePath(str(item.get("agentPath", "") or "")).name
            tasks[thread_id] = {
                "id": thread_id,
                "name": str(previous.get("name") or agent_path or "Codex subagent"),
                "kind": "subagent",
                "state": "fail" if kind == "interrupted" else str(previous.get("state") or "run"),
                "timing": "",
                "meta": agent_path or str(previous.get("meta") or ""),
                "progress": list(previous.get("progress") or previous.get("output") or []),
                "result": list(previous.get("result") or []),
                "metadata": list(previous.get("metadata") or []),
                "output": list(previous.get("output") or []),
                "runInBackground": False,
                "parentToolCallId": str(previous.get("parentToolCallId") or ""),
                "prompt": str(previous.get("prompt") or ""),
                "executionState": (
                    "interrupted"
                    if kind == "interrupted"
                    else str(previous.get("executionState") or "active")
                ),
            }
    return list(tasks.values())


def _subagent_task_state(agent_status: str, call_status: Any) -> str:
    normalized = str(agent_status or "").strip().lower()
    if normalized in {"completed", "shutdown"}:
        return "done"
    if normalized in {"errored", "interrupted", "notfound"}:
        return "fail"
    if normalized in {"pendinginit", "running"}:
        return "run"
    return "fail" if _tool_status(call_status) == "error" else (
        "done" if _tool_status(call_status) == "ok" else "run"
    )


def _subagent_execution_state(agent_status: str, call_status: Any) -> str:
    normalized = str(agent_status or "").strip().lower()
    if normalized == "completed":
        return "completed"
    if normalized == "interrupted":
        return "interrupted"
    if normalized in {"errored", "notfound"}:
        return "failed"
    if normalized == "shutdown":
        return "not_loaded"
    if normalized in {"pendinginit", "running"}:
        return "active"
    tool_status = _tool_status(call_status)
    if tool_status == "ok":
        return "completed"
    if tool_status == "error":
        return "failed"
    return "active"


def _project_user_content(
    raw_content: Any,
    *,
    attachment_url_for_path: Callable[[str], str] | None = None,
    attachment_url_for_id: Callable[[str], str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    texts: list[str] = []
    attachments: list[dict[str, Any]] = []
    if not isinstance(raw_content, list):
        return texts, attachments
    for content in raw_content:
        if not isinstance(content, dict):
            continue
        content_type = str(content.get("type", "") or "").strip()
        if content_type == "text":
            text = str(content.get("text", "") or "")
            if text:
                projected_text, envelope_attachments = _project_attachment_envelope(
                    text,
                    attachment_url_for_id=attachment_url_for_id,
                )
                if projected_text:
                    texts.append(projected_text)
                for attachment in envelope_attachments:
                    _append_attachment(attachments, attachment)
            continue
        if content_type in {"image", "localImage"}:
            local_path = str(content.get("path", "") or "")
            _append_attachment(
                attachments,
                {
                    "kind": "image",
                    "url": (
                        attachment_url_for_path(local_path)
                        if local_path and attachment_url_for_path is not None
                        # A URL-bearing app-server image item can otherwise
                        # smuggle an arbitrary remote/data/file URL into an
                        # <img>. Only the same bounded, signature-checked
                        # inline image form used for tool results is eligible
                        # when there is no local path to copy into the private
                        # attachment cache.
                        else safe_inline_image_data_url(
                            str(content.get("url", "") or "")
                        )
                    ),
                    "name": pathlib.PurePath(local_path).name if local_path else None,
                },
            )
            continue
        if content_type in {"file", "localFile", "audio", "localAudio"}:
            local_path = str(content.get("path", "") or "")
            _append_attachment(
                attachments,
                {
                    "kind": "file",
                    # A Focus generic attachment is deliberately metadata,
                    # not a browser-readable file.  Do not turn an upstream
                    # local path (or an arbitrary URL) into a download
                    # capability merely while projecting transcript history.
                    "url": "",
                    "name": str(
                        content.get("name", "")
                        or (pathlib.PurePath(local_path).name if local_path else "")
                        or "file"
                    ),
                },
            )
    return texts, attachments


def _project_attachment_envelope(
    text: str,
    *,
    attachment_url_for_id: Callable[[str], str] | None,
) -> tuple[str, list[dict[str, Any]]]:
    if not text.startswith(_ATTACHMENT_ENVELOPE_START):
        return text, []
    manifest_end = text.find(_ATTACHMENT_ENVELOPE_END, len(_ATTACHMENT_ENVELOPE_START))
    if manifest_end < 0:
        return text, []
    request_start = text.find(
        _ATTACHMENT_REQUEST_START,
        manifest_end + len(_ATTACHMENT_ENVELOPE_END),
    )
    if request_start < 0:
        return text, []
    raw_manifest = text[len(_ATTACHMENT_ENVELOPE_START) : manifest_end]
    try:
        manifest = json.loads(raw_manifest)
    except (TypeError, ValueError):
        return text, []
    if not isinstance(manifest, list):
        return text, []
    attachments: list[dict[str, Any]] = []
    for raw in manifest:
        if not isinstance(raw, dict):
            continue
        attachment_id = str(raw.get("id", "") or "").strip()
        # Only signature-checked images have a controlled browser rendering
        # path.  Video, audio, and every other upload remain ordinary files
        # that Codex may inspect through its tools; Focus does not expose a
        # browser player, preview, or download for them.
        kind = "image" if str(raw.get("kind", "") or "").strip().lower() == "image" else "file"
        attachment = {
            "kind": kind,
            "url": (
                attachment_url_for_id(attachment_id)
                if kind == "image" and attachment_id and attachment_url_for_id is not None
                else ""
            ),
            "fileId": attachment_id or None,
            "name": str(raw.get("name", "") or "") or None,
            "mediaType": str(raw.get("media_type", "") or "") or None,
            "size": _non_negative_int(raw.get("size")),
        }
        _append_attachment(attachments, attachment)
    request_text = text[request_start + len(_ATTACHMENT_REQUEST_START) :]
    return request_text, attachments


def _append_attachment(
    attachments: list[dict[str, Any]],
    attachment: dict[str, Any],
) -> None:
    identities = {
        str(attachment.get(key, "") or "").strip()
        for key in ("fileId", "url")
        if str(attachment.get(key, "") or "").strip()
    }
    if identities and any(
        identities
        & {
            str(existing.get(key, "") or "").strip()
            for key in ("fileId", "url")
            if str(existing.get(key, "") or "").strip()
        }
        for existing in attachments
    ):
        return
    attachments.append({key: value for key, value in attachment.items() if value is not None})


def _non_negative_int(value: Any) -> int | None:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _project_tools(
    item: dict[str, Any],
    *,
    turn_id: str = "",
    attachment_url_for_path: Callable[[str], str] | None = None,
    presentation_budget: ToolOutputPresentationBudget | None = None,
) -> list[dict[str, Any]]:
    tools = _project_tools_unbudgeted(
        item,
        turn_id=turn_id,
        attachment_url_for_path=attachment_url_for_path,
    )
    if presentation_budget is not None:
        for tool in tools:
            _admit_projected_tool_output(tool, presentation_budget)
    return tools


def _attach_inspection_locator(
    tool: dict[str, Any],
    *,
    item: dict[str, Any],
    turn_id: str,
    change_index: int | None,
) -> None:
    """Attach source proof only for an exact terminal inspectable item."""

    normalized_turn_id = str(turn_id or "").strip()
    item_id = str(item.get("id", "") or "").strip()
    item_type = str(item.get("type", "") or "").strip()
    status = str(item.get("status", "") or "").strip()
    valid_change_index = (
        change_index is None
        if item_type == "commandExecution"
        else (
            isinstance(change_index, int)
            and not isinstance(change_index, bool)
            and 0 <= change_index <= _INSPECTION_CHANGE_INDEX_MAX
        )
    )
    if (
        not normalized_turn_id
        or not item_id
        or item_type not in {"commandExecution", "fileChange"}
        or status not in _INSPECTION_TERMINAL_TOOL_STATUSES
        or not valid_change_index
    ):
        return
    tool["inspectionLocator"] = {
        "turn_id": normalized_turn_id,
        "item_id": item_id,
        "kind": item_type,
        "change_index": change_index,
    }


def _project_tools_unbudgeted(
    item: dict[str, Any],
    *,
    turn_id: str = "",
    attachment_url_for_path: Callable[[str], str] | None = None,
) -> list[dict[str, Any]]:
    item_type = str(item.get("type", "") or "").strip()
    if item_type == "commandExecution":
        output = str(item.get("aggregatedOutput", "") or "")
        tool = _generic_tool(
            item,
            name="Shell",
            arg=str(item.get("command", "") or ""),
            output=output,
            prebounded_omitted_chars=_cached_aggregated_output_omitted_chars(item),
            prebounded_head_line_count=(
                _cached_aggregated_output_head_line_count(item)
            ),
            status=_tool_status(item.get("status")),
            timing=_timing(item.get("durationMs")),
        )
        # `arg`, `output`, `status`, and `timing` retain the familiar Kimi
        # renderer shape, but they are not a lossless commandExecution DTO.
        # Keep the remaining app-server facts in a namespaced field so a Web
        # card can explain *where* and *how* the command ran without turning a
        # server path into a browser file link or terminal capability.
        tool["commandExecution"] = _project_command_execution_facts(item)
        _attach_inspection_locator(
            tool,
            item=item,
            turn_id=turn_id,
            change_index=None,
        )
        return [tool]
    if item_type == "turnDiff":
        diff = str(item.get("diff", "") or "")
        tool = _generic_tool(
            item,
            name="Turn diff",
            arg="",
            output=diff,
            prebounded_omitted_chars=_cached_turn_diff_omitted_chars(item),
            prebounded_head_line_count=_cached_turn_diff_head_line_count(item),
            status=_tool_status(item.get("status")),
        )
        tool["diff"] = {
            "path": "",
            "lines": _unified_diff_lines(_projected_tool_output_text(tool)),
            **_projected_diff_omission(tool),
        }
        return [tool]
    if item_type == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        tools: list[dict[str, Any]] = []
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                continue
            path = str(change.get("path", "") or "")
            diff = str(change.get("diff", "") or "")
            kind, move_path = _file_change_kind(change.get("kind"))
            name = {"add": "Write", "delete": "Delete"}.get(kind, "Edit")
            tool_item = dict(item)
            if len(changes) > 1:
                tool_item["id"] = f"{str(item.get('id', '') or 'file-change')}:{index + 1}"
            arg: dict[str, Any] = {"path": path}
            if move_path:
                arg["move_path"] = move_path
            tool = _generic_tool(
                tool_item,
                name=name,
                arg=_json_text(arg),
                output=diff,
                prebounded_omitted_chars=_cached_change_diff_omitted_chars(
                    item,
                    index,
                ),
                prebounded_head_line_count=_cached_change_diff_head_line_count(
                    item,
                    index,
                ),
                status=_tool_status(item.get("status")),
            )
            tool["diff"] = {
                "path": path,
                "lines": _unified_diff_lines(_projected_tool_output_text(tool)),
                **_projected_diff_omission(tool),
            }
            _attach_inspection_locator(
                tool,
                item=item,
                turn_id=turn_id,
                change_index=index,
            )
            tools.append(tool)
        if tools:
            return tools
        return [
            _generic_tool(
                item,
                name="File change",
                arg="",
                output=file_change_fallback_output(item),
                status=_tool_status(item.get("status")),
            )
        ]
    if item_type == "mcpToolCall":
        return [
            _generic_tool(
                item,
                name=f"MCP · {str(item.get('server', '') or '')}/{str(item.get('tool', '') or '')}",
                arg=_json_text(item.get("arguments")),
                output=generic_tool_output(item) or [],
                status=_tool_status(item.get("status")),
                timing=_timing(item.get("durationMs")),
            )
        ]
    if item_type == "dynamicToolCall":
        content_items = item.get("contentItems") if isinstance(item.get("contentItems"), list) else []
        tools = [
            _generic_tool(
                item,
                name=str(item.get("tool", "") or "Tool"),
                arg=_json_text(item.get("arguments")),
                output=generic_tool_output(item) or [],
                status=_tool_status(item.get("status"), success=item.get("success")),
                timing=_timing(item.get("durationMs")),
            )
        ]
        for index, content in enumerate(content_items):
            if not isinstance(content, dict):
                continue
            content_type = str(content.get("type", "") or "")
            # Focus intentionally does not project dynamic audio/video (or an
            # arbitrary image URL) into a native browser media element. The
            # one supported shape is a safe inline image data URL; other tool
            # content remains ordinary tool text for Codex to handle.
            url = safe_inline_image_data_url(
                str(content.get("imageUrl", "") or "")
            )
            if content_type != "inputImage" or not url:
                continue
            media_item = dict(item)
            media_item["id"] = f"{str(item.get('id', '') or 'dynamic-tool')}:media:{index + 1}"
            media_item.pop(INTERNAL_PRESENTATION_METADATA_KEY, None)
            tools.append(
                _generic_tool(
                    media_item,
                    name="Tool image",
                    arg="",
                    output=[],
                    status=_tool_status(item.get("status"), success=item.get("success")),
                    media={"kind": "image", "url": url},
                )
            )
        return tools
    if item_type == "collabAgentToolCall":
        receiver_ids = item.get("receiverThreadIds") if isinstance(item.get("receiverThreadIds"), list) else []
        collab_tool = str(item.get("tool", "") or "collaboration")
        if collab_tool == "spawnAgent":
            arg = {
                "description": str(item.get("model", "") or "Subagent"),
                "subagent_type": str(item.get("model", "") or ""),
                "prompt": str(item.get("prompt", "") or ""),
            }
            name = "Agent"
        else:
            arg = {
                "prompt": str(item.get("prompt", "") or ""),
                "receiver_thread_ids": [str(value) for value in receiver_ids],
            }
            name = {
                "sendInput": "Agent input",
                "resumeAgent": "Agent resume",
                "wait": "Agent wait",
                "closeAgent": "Agent close",
            }.get(collab_tool, "Agent collaboration")
        return [
            _generic_tool(
                item,
                name=name,
                arg=_json_text(arg),
                output=generic_tool_output(item) or [],
                status=_tool_status(item.get("status")),
            )
        ]
    if item_type == "subAgentActivity":
        agent_path = pathlib.PurePath(str(item.get("agentPath", "") or "")).name
        return [
            _generic_tool(
                item,
                name="Agent activity",
                arg=str(item.get("agentThreadId", "") or ""),
                output=[
                    value
                    for value in [
                        str(item.get("kind", "") or ""),
                        f"path: {agent_path}" if agent_path else "",
                    ]
                    if value
                ],
            )
        ]
    if item_type == "webSearch":
        query = str(item.get("query", "") or item.get("searchQuery", "") or "")
        return [
            _generic_tool(
                item,
                name="Web search",
                arg=query,
                output=generic_tool_output(item) or [],
            )
        ]
    if item_type == "imageView":
        path = str(item.get("path", "") or "")
        url = attachment_url_for_path(path) if path and attachment_url_for_path is not None else ""
        return [
            _generic_tool(
                item,
                name="View image",
                arg=pathlib.PurePath(path).name,
                output=[],
                media={"kind": "image", "url": url, "path": pathlib.PurePath(path).name}
                if url
                else None,
            )
        ]
    if item_type == "imageGeneration":
        saved_path = str(item.get("savedPath", "") or "")
        result = str(item.get("result", "") or "")
        url = (
            attachment_url_for_path(saved_path)
            if saved_path and attachment_url_for_path is not None
            else safe_inline_image_data_url(result)
        )
        return [
            _generic_tool(
                item,
                name="Image generation",
                arg=str(item.get("revisedPrompt", "") or ""),
                output=generic_tool_output(
                    item,
                    image_result_is_media=bool(url),
                ) or [],
                status=_tool_status(item.get("status")),
                media={
                    "kind": "image",
                    "url": url,
                    "path": pathlib.PurePath(saved_path).name if saved_path else "generated image",
                }
                if url
                else None,
            )
        ]
    if item_type == "enteredReviewMode":
        return [
            _generic_tool(
                item,
                name="Review started",
                arg="",
                output=generic_tool_output(item) or [],
            )
        ]
    if item_type == "exitedReviewMode":
        return [
            _generic_tool(
                item,
                name="Review completed",
                arg="",
                output=generic_tool_output(item) or [],
            )
        ]
    if item_type == "sleep":
        return [
            _generic_tool(
                item,
                name="Wait",
                arg=_timing(item.get("durationMs")),
                output=[],
                timing=_timing(item.get("durationMs")),
            )
        ]
    if item_type:
        return [
            _generic_tool(
                item,
                name=f"Codex item · {item_type}",
                arg="",
                output=generic_tool_output(item) or [],
            )
        ]
    return []


def _file_change_kind(raw_kind: Any) -> tuple[str, str]:
    if isinstance(raw_kind, str):
        return raw_kind.strip().lower(), ""
    if isinstance(raw_kind, dict):
        kind = str(raw_kind.get("type", "update") or "update").strip().lower()
        move_path = str(raw_kind.get("movePath", "") or "").strip()
        return kind, move_path
    return "update", ""


def _projected_tool_output_text(tool: dict[str, Any]) -> str:
    """Return only the already-bounded output used by structured diff UI."""

    output = tool.get("output")
    if not isinstance(output, list):
        return ""
    return "\n".join(str(line) for line in output)


def _projected_diff_omission(tool: dict[str, Any]) -> dict[str, int]:
    """Carry the trusted output boundary into the structured diff renderer."""

    omitted_chars = _non_negative_int(tool.get("outputOmittedChars")) or 0
    head_line_count = _non_negative_int(tool.get("outputHeadLineCount")) or 0
    if omitted_chars <= 0:
        return {}
    return {
        "omittedChars": omitted_chars,
        "omissionLineIndex": head_line_count,
    }


_UNIFIED_HUNK_RE = re.compile(
    r"^@@\s+-(?P<old>\d+)(?:,(?P<old_count>\d+))?\s+\+(?P<new>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)


def _unified_diff_lines(diff: str) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    old_no = 0
    new_no = 0
    in_hunk = False
    for raw_line in str(diff or "").splitlines():
        match = _UNIFIED_HUNK_RE.match(raw_line)
        if match:
            old_no = int(match.group("old"))
            new_no = int(match.group("new"))
            in_hunk = True
            lines.append({"type": "hunk", "text": raw_line})
            continue
        if not in_hunk or raw_line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            lines.append({"type": "hunk", "text": raw_line})
            continue
        if raw_line.startswith("+"):
            lines.append({"type": "add", "text": raw_line[1:], "newNo": new_no})
            new_no += 1
            continue
        if raw_line.startswith("-"):
            lines.append({"type": "del", "text": raw_line[1:], "oldNo": old_no})
            old_no += 1
            continue
        if raw_line.startswith(" "):
            lines.append(
                {
                    "type": "context",
                    "text": raw_line[1:],
                    "oldNo": old_no,
                    "newNo": new_no,
                }
            )
            old_no += 1
            new_no += 1
            continue
        lines.append({"type": "hunk", "text": raw_line})
    return lines


def _admit_projected_tool_output(
    tool: dict[str, Any],
    budget: ToolOutputPresentationBudget,
) -> None:
    """Apply one page-wide budget after per-tool projection is complete."""

    raw_lines = tool.get("output")
    lines = list(raw_lines) if isinstance(raw_lines, list) else []
    omitted_chars = _non_negative_int(tool.get("outputOmittedChars")) or 0
    head_line_count = _non_negative_int(tool.get("outputHeadLineCount")) or 0
    if omitted_chars > 0:
        original_chars = omitted_chars if not lines else MAX_VISIBLE + omitted_chars
    else:
        original_chars = len("\n".join(str(line) for line in lines))
    admitted = budget.admit(
        ToolOutputPresentation(
            lines=[str(line) for line in lines],
            omitted_chars=omitted_chars,
            head_line_count=head_line_count,
            original_chars=original_chars,
        )
    )
    tool["output"] = admitted.lines
    if admitted.omitted_chars > 0:
        tool["outputTruncated"] = True
        tool["outputOmittedChars"] = admitted.omitted_chars
        tool["outputHeadLineCount"] = admitted.head_line_count
    else:
        tool.pop("outputTruncated", None)
        tool.pop("outputOmittedChars", None)
        tool.pop("outputHeadLineCount", None)
    diff = tool.get("diff")
    if (
        isinstance(diff, dict)
        and admitted.omitted_chars > 0
        and not admitted.lines
    ):
        diff["lines"] = []
        diff["omittedChars"] = admitted.omitted_chars
        diff["omissionLineIndex"] = 0


def _generic_tool(
    item: dict[str, Any],
    *,
    name: str,
    arg: str,
    output: str | list[str],
    prebounded_omitted_chars: int = 0,
    prebounded_head_line_count: int = 0,
    status: str = "ok",
    timing: str = "",
    media: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if (
        isinstance(metadata, CachedToolOutputPresentation)
        and metadata.generic_output_cached
    ):
        output = list(metadata.generic_output_lines)
        prebounded_omitted_chars = metadata.generic_output_omitted_chars
        prebounded_head_line_count = metadata.generic_output_head_line_count
    omitted_chars = max(int(prebounded_omitted_chars), 0)
    if omitted_chars:
        output_lines = output.split("\n") if isinstance(output, str) and output else (
            [] if isinstance(output, str) else list(output)
        )
        head_line_count = max(int(prebounded_head_line_count), 0)
    else:
        presented_output = present_tool_output(output)
        output_lines = presented_output.lines
        omitted_chars = presented_output.omitted_chars
        head_line_count = presented_output.head_line_count
    result = {
        "id": str(item.get("id", "") or "") or str(uuid.uuid4()),
        "name": name,
        "arg": arg,
        "status": status,
        "output": output_lines,
    }
    if omitted_chars:
        result["outputTruncated"] = True
        result["outputOmittedChars"] = omitted_chars
        result["outputHeadLineCount"] = head_line_count
    if timing:
        result["timing"] = timing
    if media:
        result["media"] = media
    return result


def _has_cached_generic_output(item: dict[str, Any]) -> bool:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    return (
        isinstance(metadata, CachedToolOutputPresentation)
        and metadata.generic_output_cached
    )


def _cached_aggregated_output_omitted_chars(item: dict[str, Any]) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    return _non_negative_int(metadata.aggregated_output_omitted_chars) or 0


def _cached_aggregated_output_head_line_count(item: dict[str, Any]) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    return _non_negative_int(metadata.aggregated_output_head_line_count) or 0


def _cached_turn_diff_omitted_chars(item: dict[str, Any]) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    return _non_negative_int(metadata.turn_diff_omitted_chars) or 0


def _cached_turn_diff_head_line_count(item: dict[str, Any]) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    return _non_negative_int(metadata.turn_diff_head_line_count) or 0


def _cached_change_diff_omitted_chars(
    item: dict[str, Any],
    index: int,
) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    omissions = metadata.change_diff_omitted_chars
    if index >= len(omissions):
        return 0
    return _non_negative_int(omissions[index]) or 0


def _cached_change_diff_head_line_count(
    item: dict[str, Any],
    index: int,
) -> int:
    metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
    if not isinstance(metadata, CachedToolOutputPresentation):
        return 0
    head_line_counts = metadata.change_diff_head_line_counts
    if index >= len(head_line_counts):
        return 0
    return _non_negative_int(head_line_counts[index]) or 0


def _project_command_execution_facts(item: dict[str, Any]) -> dict[str, Any]:
    """Project the non-rendered facts of an app-server commandExecution item.

    Keep this deliberately narrow and schema-shaped.  The command text,
    status, aggregate output, and duration already have first-class ToolCall
    fields; this preserves the current protocol fields that would otherwise
    disappear in the generic ``Shell`` adaptation.  Paths remain inert text:
    Focus does not manufacture a file URL, a file preview, or a terminal from
    these facts.
    """

    facts: dict[str, Any] = {}
    for source_key, target_key in (
        ("cwd", "cwd"),
        ("processId", "processId"),
        ("source", "source"),
    ):
        if source_key not in item:
            continue
        value = item.get(source_key)
        if value is None or isinstance(value, str):
            facts[target_key] = value

    # `exitCode` is nullable on the wire: null means that this item has not
    # reported an exit result, which is distinct from an omitted legacy field.
    if "exitCode" in item:
        exit_code = item.get("exitCode")
        facts["exitCode"] = (
            exit_code
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else None
        )

    # CommandAction is a small tagged union in the app-server schema.  Copy
    # only its documented scalar fields, rather than passing an open-ended
    # object through a browser DTO.  This also keeps the projection boundary
    # explicit when upstream adds a new action shape.
    if "commandActions" in item and isinstance(item.get("commandActions"), list):
        actions: list[dict[str, Any]] = []
        for raw_action in item["commandActions"]:
            if not isinstance(raw_action, dict):
                continue
            action: dict[str, Any] = {}
            for key in ("type", "command", "name", "path", "query"):
                value = raw_action.get(key)
                if value is None or isinstance(value, str):
                    if key in raw_action:
                        action[key] = value
            if action:
                actions.append(action)
        facts["commandActions"] = actions
    return facts


def _tool_status(raw_status: Any, *, success: Any = None) -> str:
    status = str(raw_status or "").strip().lower()
    if status in {"inprogress", "running", "pending"}:
        return "running"
    if status in {"failed", "declined", "error", "cancelled"} or success is False:
        return "error"
    return "ok"


def _timing(raw_duration_ms: Any) -> str:
    try:
        duration = int(raw_duration_ms)
    except (TypeError, ValueError):
        return ""
    if duration < 1000:
        return f"{duration}ms"
    return f"{duration / 1000:.1f}s"


def _json_text(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _json_lines(value: Any) -> list[str]:
    text = _json_text(value)
    return text.splitlines() if text else []


def _duration_ms(raw: Any, started_at: Any, completed_at: Any) -> int | None:
    try:
        if raw is not None:
            return max(int(raw), 0)
        if started_at is not None and completed_at is not None:
            return max((int(completed_at) - int(started_at)) * 1000, 0)
    except (TypeError, ValueError):
        return None
    return None


def _iso_timestamp(raw: Any) -> str | None:
    try:
        timestamp = int(raw)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def dataclass_payload(value: Any) -> dict[str, Any]:
    """Small public helper for diagnostics and tests."""

    return asdict(value)
