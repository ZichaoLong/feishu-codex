"""Rebuildable app-server facts used by the Focus Web projection.

This owner contains only backend-derived, process-local state.  It never
decides writer admission, interaction settlement, runtime retention, or
projection publication.  Those decisions remain in their existing runtime
owners and may treat these snapshots only as observation evidence.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

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
from bot.web_runtime.turn_window import (
    DEFAULT_TURN_WINDOW_LIMIT,
    MAX_TURN_WINDOW_LIMIT,
)

DEFAULT_RECENT_TURN_LIMIT = DEFAULT_TURN_WINDOW_LIMIT


@dataclass(frozen=True, slots=True)
class WebThreadReadSnapshot:
    thread_id: str
    turns: tuple[dict[str, Any], ...] = ()
    cwd: str = ""
    token_usage: dict[str, Any] | None = None
    token_usage_available: bool = False


@dataclass(frozen=True, slots=True)
class WebThreadNotificationUpdate:
    """One cache application result awaiting controller-owned projection."""

    method: str
    thread_id: str
    detail: dict[str, Any] = field(default_factory=dict)
    raw_turn: dict[str, Any] | None = None
    goal_changed: bool = False
    goal: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WebThreadReadObservationReceipt:
    """Exact cache-observation fence captured before one external read."""

    thread_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class PreparedWebThreadTurns:
    """Detached bounded turns whose cache ownership can move into RuntimeLoop.

    Expensive deepcopy and tool-output budgeting happen before this receipt is
    installed.  The cache and projection copies are deliberately separate so
    the caller can project the HTTP response after settlement without racing
    RuntimeLoop-owned notification mutations.
    """

    thread_id: str
    projection_turns: tuple[dict[str, Any], ...]
    _cache_turns: tuple[dict[str, Any], ...] = field(repr=False, compare=False)
    _authority_token: object = field(repr=False, compare=False)


class WebThreadReadModel:
    """Own the Web backend read cache and its deterministic merge rules."""

    def __init__(
        self,
        *,
        recent_turn_limit: int = DEFAULT_RECENT_TURN_LIMIT,
    ) -> None:
        self._recent_turn_limit = min(
            max(int(recent_turn_limit), 1),
            MAX_TURN_WINDOW_LIMIT,
        )
        self._turns_by_thread: dict[str, dict[str, dict[str, Any]]] = {}
        self._cwd_by_thread: dict[str, str] = {}
        self._token_usage_by_thread: dict[str, dict[str, Any]] = {}
        self._observation_revision_by_thread: dict[str, int] = {}
        self._next_observation_revision = 0
        self._prepared_turns_token = object()

    def capture_observation(
        self,
        thread_id: str,
    ) -> WebThreadReadObservationReceipt:
        normalized_thread_id = self._thread_id(thread_id)
        return WebThreadReadObservationReceipt(
            thread_id=normalized_thread_id,
            revision=self._observation_revision_by_thread.get(
                normalized_thread_id,
                0,
            ),
        )

    def observation_is_current(
        self,
        receipt: WebThreadReadObservationReceipt,
    ) -> bool:
        return bool(
            isinstance(receipt, WebThreadReadObservationReceipt)
            and self._observation_revision_by_thread.get(receipt.thread_id, 0)
            == receipt.revision
        )

    def claim_observation(
        self,
        receipt: WebThreadReadObservationReceipt,
    ) -> bool:
        """CAS one read result and retire every concurrent older observation."""

        if not self.observation_is_current(receipt):
            return False
        self._advance_observation(receipt.thread_id)
        return True

    def observe_notification(self, thread_id: str) -> int:
        """Advance before any notification may mutate the backend read cache."""

        return self._advance_observation(self._thread_id(thread_id))

    def snapshot(self, thread_id: str) -> WebThreadReadSnapshot:
        normalized_thread_id = self._thread_id(thread_id)
        turns = self._turns_by_thread.get(normalized_thread_id, {})
        token_usage_available = normalized_thread_id in self._token_usage_by_thread
        token_usage = self._token_usage_by_thread.get(normalized_thread_id)
        return WebThreadReadSnapshot(
            thread_id=normalized_thread_id,
            turns=tuple(copy.deepcopy(turn) for turn in turns.values()),
            cwd=self._cwd_by_thread.get(normalized_thread_id, ""),
            token_usage=copy.deepcopy(token_usage) if token_usage is not None else None,
            token_usage_available=token_usage_available,
        )

    def snapshots(self) -> tuple[WebThreadReadSnapshot, ...]:
        thread_ids = (
            set(self._turns_by_thread)
            | set(self._cwd_by_thread)
            | set(self._token_usage_by_thread)
        )
        return tuple(self.snapshot(thread_id) for thread_id in sorted(thread_ids))

    def turns(self, thread_id: str) -> tuple[dict[str, Any], ...]:
        return self.snapshot(thread_id).turns

    def turn_thread_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._turns_by_thread))

    def turn_ids(self, thread_id: str) -> tuple[str, ...]:
        normalized_thread_id = self._thread_id(thread_id)
        return tuple(self._turns_by_thread.get(normalized_thread_id, {}))

    def latest_turn(self, thread_id: str) -> dict[str, Any] | None:
        turns = self._turns_by_thread.get(self._thread_id(thread_id), {})
        latest = next(reversed(turns.values()), None)
        return copy.deepcopy(latest) if latest is not None else None

    def latest_turn_is_active(self, thread_id: str) -> bool:
        turns = self._turns_by_thread.get(self._thread_id(thread_id), {})
        latest = next(reversed(turns.values()), None)
        return bool(
            latest is not None
            and str(latest.get("status", "") or "") == "inProgress"
        )

    def collaboration_turns(
        self,
        thread_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Copy only items consumed by the subagent-task projection."""

        projected: list[dict[str, Any]] = []
        for turn in self._turns_by_thread.get(self._thread_id(thread_id), {}).values():
            items = [
                copy.deepcopy(item)
                for item in (turn.get("items") or [])
                if isinstance(item, dict)
                and str(item.get("type", "") or "")
                in {"collabAgentToolCall", "subAgentActivity"}
            ]
            if items:
                projected.append({"items": items})
        return tuple(projected)

    def token_usage(self, thread_id: str) -> tuple[dict[str, Any] | None, bool]:
        normalized_thread_id = self._thread_id(thread_id)
        available = normalized_thread_id in self._token_usage_by_thread
        usage = self._token_usage_by_thread.get(normalized_thread_id)
        return copy.deepcopy(usage) if usage is not None else None, available

    def cwd(self, thread_id: str) -> str:
        return self._cwd_by_thread.get(self._thread_id(thread_id), "")

    def remember_cwd(self, thread_id: str, cwd: str) -> None:
        normalized_thread_id = self._thread_id(thread_id)
        normalized_cwd = str(cwd or "").strip()
        if (
            normalized_cwd
            and self._cwd_by_thread.get(normalized_thread_id) != normalized_cwd
        ):
            self._advance_observation(normalized_thread_id)
            self._cwd_by_thread[normalized_thread_id] = normalized_cwd

    def replace_turns(self, thread_id: str, turns: Iterable[dict[str, Any]]) -> None:
        normalized_thread_id = self._thread_id(thread_id)
        self._advance_observation(normalized_thread_id)
        self._remember_turns(normalized_thread_id, turns, replace=True)

    def prepare_turn_replacement(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
    ) -> PreparedWebThreadTurns:
        """Bound one replacement outside RuntimeLoop without mutating the cache."""

        normalized_thread_id = self._thread_id(thread_id)
        remembered: dict[str, dict[str, Any]] = {}
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id", "") or "").strip()
            if turn_id:
                remembered[turn_id] = self._bounded_turn_copy(turn)
        self._bound_turns(remembered)
        cache_turns = tuple(remembered.values())
        return PreparedWebThreadTurns(
            thread_id=normalized_thread_id,
            projection_turns=tuple(copy.deepcopy(turn) for turn in cache_turns),
            _cache_turns=cache_turns,
            _authority_token=self._prepared_turns_token,
        )

    def install_prepared_turns(self, prepared: PreparedWebThreadTurns) -> None:
        """Install an already-bounded replacement with only an in-memory swap."""

        if not isinstance(prepared, PreparedWebThreadTurns):
            raise TypeError("prepared Web thread turns are required")
        if prepared._authority_token is not self._prepared_turns_token:
            raise ValueError("prepared Web thread turns belong to another read model")
        self._advance_observation(prepared.thread_id)
        self._turns_by_thread[prepared.thread_id] = {
            str(turn.get("id", "") or "").strip(): turn
            for turn in prepared._cache_turns
            if str(turn.get("id", "") or "").strip()
        }

    def merge_turns(self, thread_id: str, turns: Iterable[dict[str, Any]]) -> None:
        normalized_thread_id = self._thread_id(thread_id)
        self._advance_observation(normalized_thread_id)
        self._remember_turns(normalized_thread_id, turns, replace=False)

    def active_turn_id(
        self,
        thread_id: str,
        *,
        load_turns: Callable[[], Iterable[dict[str, Any]]],
    ) -> str:
        normalized_thread_id = self._thread_id(thread_id)
        active_turn_id = self.active_turn_id_from_turns(
            self._turns_by_thread.get(normalized_thread_id, {}).values()
        )
        if active_turn_id:
            return active_turn_id
        loaded_turns = list(load_turns())
        self.merge_turns(normalized_thread_id, loaded_turns)
        return self.active_turn_id_from_turns(loaded_turns)

    @staticmethod
    def active_turn_id_from_turns(turns: Iterable[dict[str, Any]]) -> str:
        values = tuple(turn for turn in turns if isinstance(turn, dict))
        for turn in reversed(values):
            if str(turn.get("status", "") or "") != "inProgress":
                continue
            return str(turn.get("id", "") or "").strip()
        return ""

    def forget_runtime(self, thread_id: str) -> None:
        """Forget only the turn stream after its runtime is unloaded."""

        normalized_thread_id = self._thread_id(thread_id)
        self._advance_observation(normalized_thread_id)
        self._turns_by_thread.pop(normalized_thread_id, None)

    def forget_closed_thread(self, thread_id: str) -> None:
        """Apply the existing thread/closed cache contract.

        Token usage deliberately survives until a full lifecycle forget or a
        backend epoch change, matching the controller behavior being moved.
        """

        normalized_thread_id = self._thread_id(thread_id)
        self._advance_observation(normalized_thread_id)
        self._turns_by_thread.pop(normalized_thread_id, None)
        self._cwd_by_thread.pop(normalized_thread_id, None)

    def forget_thread(self, thread_id: str) -> None:
        normalized_thread_id = self._thread_id(thread_id)
        self._advance_observation(normalized_thread_id)
        self._turns_by_thread.pop(normalized_thread_id, None)
        self._cwd_by_thread.pop(normalized_thread_id, None)
        self._token_usage_by_thread.pop(normalized_thread_id, None)

    def backend_disconnected(self) -> None:
        """Drop connection-epoch facts while retaining authoritative cwd hints."""

        for thread_id in tuple(
            set(self._turns_by_thread)
            | set(self._token_usage_by_thread)
            | set(self._observation_revision_by_thread)
        ):
            self._advance_observation(thread_id)
        self._turns_by_thread.clear()
        self._token_usage_by_thread.clear()

    def apply_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> WebThreadNotificationUpdate | None:
        """Apply one reviewed app-server notification to the read cache.

        The returned DTO contains projection inputs only.  It has no callbacks
        and cannot publish, mutate operation authority, or settle interactions.
        """

        normalized_method = str(method or "").strip()
        thread_id = str(params.get("threadId", "") or "").strip()
        if not thread_id:
            return None
        detail: dict[str, Any] = {"method": normalized_method}
        if normalized_method == "thread/status/changed":
            status = params.get("status")
            if isinstance(status, dict):
                detail["thread_status"] = copy.deepcopy(status)
            return self._update(normalized_method, thread_id, detail)
        if normalized_method == "thread/name/updated":
            detail["thread_name"] = str(params.get("threadName", "") or "")
            return self._update(normalized_method, thread_id, detail)
        if normalized_method in {"thread/goal/updated", "thread/goal/cleared"}:
            goal = params.get("goal")
            return self._update(
                normalized_method,
                thread_id,
                detail,
                goal_changed=True,
                goal=copy.deepcopy(goal) if isinstance(goal, dict) else None,
            )
        if normalized_method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage")
            if not isinstance(token_usage, dict):
                return None
            remembered = copy.deepcopy(token_usage)
            self._token_usage_by_thread[thread_id] = remembered
            detail["token_usage"] = copy.deepcopy(remembered)
            detail["token_usage_durable"] = False
            return self._update(normalized_method, thread_id, detail)

        turn_id = str(params.get("turnId", "") or "").strip()
        turns = self._turns_by_thread.setdefault(thread_id, {})
        if normalized_method in {"turn/started", "turn/completed"}:
            raw_turn = params.get("turn")
            if not isinstance(raw_turn, dict):
                return None
            turn_id = str(raw_turn.get("id", "") or "").strip()
            if not turn_id:
                return None
            turns[turn_id] = self._merge_turn(
                turns.get(turn_id),
                self._bounded_turn_copy(raw_turn),
            )
        elif normalized_method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not turn_id or not isinstance(item, dict):
                return None
            turn = self._live_turn(turns, turn_id)
            self._upsert_turn_item(turn, self._bounded_item_copy(item))
        elif normalized_method == "turn/diff/updated":
            if not turn_id:
                return None
            turn = self._live_turn(turns, turn_id)
            self._upsert_turn_item(
                turn,
                self._bounded_item_copy(
                    {
                        "id": f"{turn_id}:turn-diff",
                        "type": "turnDiff",
                        "diff": str(params.get("diff", "") or ""),
                        "status": "completed",
                    }
                ),
            )
        elif normalized_method == "turn/plan/updated":
            if not turn_id:
                return None
            raw_plan = params.get("plan") if isinstance(params.get("plan"), list) else []
            lines: list[str] = []
            explanation = str(params.get("explanation", "") or "").strip()
            if explanation:
                lines.append(explanation)
            for entry in raw_plan:
                if not isinstance(entry, dict):
                    continue
                step = str(entry.get("step", "") or "").strip()
                status = str(entry.get("status", "pending") or "pending").strip()
                if step:
                    lines.append(f"- [{status}] {step}")
            turn = self._live_turn(turns, turn_id)
            self._upsert_turn_item(
                turn,
                {
                    "id": f"{turn_id}:live-plan",
                    "type": "plan",
                    "text": "\n".join(lines),
                    "liveOnly": True,
                },
            )
            detail["plan_replay"] = "live_only"
        elif normalized_method == "item/mcpToolCall/progress":
            item_id = str(params.get("itemId", "") or "").strip()
            if not turn_id or not item_id:
                return None
            message = str(params.get("message", "") or "").strip()
            if not message:
                return None
            detail.update(
                {
                    "turn_id": turn_id,
                    "stream_delta": {
                        "turn_id": turn_id,
                        "item_id": item_id,
                        "kind": "tool_output",
                        "tool_name": "MCP",
                        "delta": f"{message}\n",
                    },
                    "active_turn_id": turn_id,
                    "active_turn_status": "inProgress",
                }
            )
            self._bound_turns(turns)
            return self._update(normalized_method, thread_id, detail)
        elif normalized_method in {
            "item/agentMessage/delta",
            "item/commandExecution/outputDelta",
            "item/fileChange/outputDelta",
            "item/plan/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryPartAdded",
        }:
            item_id = str(params.get("itemId", "") or "").strip()
            if not turn_id or not item_id:
                return None
            delta = str(params.get("delta", "") or "")
            kind = {
                "item/agentMessage/delta": "text",
                "item/commandExecution/outputDelta": "tool_output",
                "item/fileChange/outputDelta": "tool_output",
                "item/plan/delta": "plan",
                "item/reasoning/summaryTextDelta": "thinking",
                "item/reasoning/textDelta": "thinking",
                "item/reasoning/summaryPartAdded": "thinking_separator",
            }[normalized_method]
            turn = self._live_turn(turns, turn_id)
            if normalized_method not in {
                "item/commandExecution/outputDelta",
                "item/fileChange/outputDelta",
                "item/plan/delta",
            }:
                self._remember_live_item_delta(
                    turn,
                    normalized_method,
                    params,
                    item_id,
                    delta,
                )
            detail.update(
                {
                    "turn_id": turn_id,
                    "stream_delta": {
                        "turn_id": turn_id,
                        "item_id": item_id,
                        "kind": kind,
                        "delta": delta,
                    },
                    "active_turn_id": turn_id,
                    "active_turn_status": "inProgress",
                }
            )
            self._bound_turns(turns)
            return self._update(normalized_method, thread_id, detail)
        elif normalized_method == "item/fileChange/patchUpdated":
            item_id = str(params.get("itemId", "") or "").strip()
            if not turn_id or not item_id:
                return None
            turn = self._live_turn(turns, turn_id)
            item = self._find_turn_item(turn, item_id)
            if item is None:
                self._upsert_turn_item(turn, {"id": item_id, "type": "fileChange"})
                item = self._find_turn_item(turn, item_id)
            changes = params.get("changes")
            if item is not None and isinstance(changes, list):
                self._replace_file_changes(item, changes)
        else:
            return None

        if turn_id and turn_id in turns:
            self._rebudget_turn(turns[turn_id])
        self._bound_turns(turns)
        raw_turn = turns.get(turn_id)
        if raw_turn is None:
            return None
        detail["turn_id"] = turn_id
        return self._update(
            normalized_method,
            thread_id,
            detail,
            raw_turn=copy.deepcopy(raw_turn),
        )

    def _remember_turns(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
        *,
        replace: bool,
    ) -> None:
        normalized_thread_id = self._thread_id(thread_id)
        remembered = (
            {}
            if replace
            else dict(self._turns_by_thread.get(normalized_thread_id, {}))
        )
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id", "") or "").strip()
            if turn_id:
                remembered[turn_id] = self._bounded_turn_copy(turn)
        self._bound_turns(remembered)
        self._turns_by_thread[normalized_thread_id] = remembered

    def _bound_turns(self, turns: dict[str, dict[str, Any]]) -> None:
        """Keep one chronological recent window without evicting active work."""

        if len(turns) <= self._recent_turn_limit:
            return
        ordered_ids = list(turns)
        kept_ids = ordered_ids[-self._recent_turn_limit :]
        active_turn_id = self.active_turn_id_from_turns(turns.values())
        if active_turn_id and active_turn_id not in kept_ids:
            if self._recent_turn_limit == 1:
                kept_ids = [active_turn_id]
            else:
                kept_ids = [
                    active_turn_id,
                    *kept_ids[-(self._recent_turn_limit - 1) :],
                ]
        kept = set(kept_ids)
        for turn_id in ordered_ids:
            if turn_id not in kept:
                turns.pop(turn_id, None)

    @classmethod
    def _bounded_turn_copy(cls, turn: dict[str, Any]) -> dict[str, Any]:
        remembered = {
            key: copy.deepcopy(value)
            for key, value in turn.items()
            if key != "items"
        }
        items = turn.get("items")
        if isinstance(items, list):
            presentation_budget = ToolOutputPresentationBudget()
            remembered["items"] = [
                cls._bounded_item_copy(item, presentation_budget)
                for item in items
                if isinstance(item, dict)
            ]
        elif "items" in turn:
            remembered["items"] = copy.deepcopy(items)
        return remembered

    @classmethod
    def _bounded_item_copy(
        cls,
        item: dict[str, Any],
        presentation_budget: ToolOutputPresentationBudget | None = None,
    ) -> dict[str, Any]:
        """Strip untrusted cache metadata and bound retained tool payloads."""

        remembered = copy.deepcopy(item)
        trusted_metadata = remembered.pop(INTERNAL_PRESENTATION_METADATA_KEY, None)
        if not isinstance(trusted_metadata, CachedToolOutputPresentation):
            trusted_metadata = None
        budget = presentation_budget or ToolOutputPresentationBudget()
        item_type = str(remembered.get("type", "") or "").strip()
        aggregated_output_omitted_chars = 0
        aggregated_output_head_line_count = 0
        aggregated_output_original_chars = 0
        turn_diff_omitted_chars = 0
        turn_diff_head_line_count = 0
        turn_diff_original_chars = 0
        change_diff_omitted_chars: tuple[int, ...] = ()
        change_diff_head_line_counts: tuple[int, ...] = ()
        change_diff_original_chars: tuple[int, ...] = ()
        generic_output_cached = False
        generic_output_lines: tuple[str, ...] = ()
        generic_output_omitted_chars = 0
        generic_output_head_line_count = 0
        generic_output_original_chars = 0
        generic_output: str | list[str] | None = None
        if item_type == "commandExecution":
            (
                aggregated_output_omitted_chars,
                aggregated_output_head_line_count,
                aggregated_output_original_chars,
            ) = cls._bound_aggregated_output(
                remembered,
                budget,
                trusted_metadata,
            )
        elif item_type == "fileChange":
            changes = (
                remembered.get("changes")
                if isinstance(remembered.get("changes"), list)
                else []
            )
            if any(isinstance(change, dict) for change in changes):
                # `aggregatedOutput` is not rendered when concrete change cards
                # exist.  Do not retain or charge the hidden carrier.
                remembered.pop("aggregatedOutput", None)
                (
                    change_diff_omitted_chars,
                    change_diff_head_line_counts,
                    change_diff_original_chars,
                ) = cls._bound_file_change_diffs(
                    remembered,
                    budget,
                    trusted_metadata,
                )
            else:
                generic_output = file_change_fallback_output(remembered)
        elif item_type == "turnDiff":
            (
                turn_diff_omitted_chars,
                turn_diff_head_line_count,
                turn_diff_original_chars,
            ) = cls._bound_turn_diff(
                remembered,
                budget,
                trusted_metadata,
            )
        else:
            generic_output = cls._generic_tool_output_for_cache(remembered)
        if generic_output is not None:
            generic_output_cached = True
            if trusted_metadata is not None and trusted_metadata.generic_output_cached:
                presentation = cls._cached_text_presentation(
                    "\n".join(trusted_metadata.generic_output_lines),
                    omitted_chars=trusted_metadata.generic_output_omitted_chars,
                    head_line_count=trusted_metadata.generic_output_head_line_count,
                    original_chars=trusted_metadata.generic_output_original_chars,
                )
            else:
                presentation = present_tool_output(generic_output)
            presentation = budget.admit(presentation)
            generic_output_lines = tuple(presentation.lines)
            generic_output_omitted_chars = presentation.omitted_chars
            generic_output_head_line_count = presentation.head_line_count
            generic_output_original_chars = presentation.original_chars
            cls._strip_generic_tool_output_carriers(remembered)
        if (
            aggregated_output_omitted_chars
            or turn_diff_omitted_chars
            or any(change_diff_omitted_chars)
            or generic_output_cached
        ):
            remembered[INTERNAL_PRESENTATION_METADATA_KEY] = (
                CachedToolOutputPresentation(
                    aggregated_output_omitted_chars=(
                        aggregated_output_omitted_chars
                    ),
                    aggregated_output_head_line_count=(
                        aggregated_output_head_line_count
                    ),
                    aggregated_output_original_chars=(
                        aggregated_output_original_chars
                    ),
                    turn_diff_omitted_chars=turn_diff_omitted_chars,
                    turn_diff_head_line_count=turn_diff_head_line_count,
                    turn_diff_original_chars=turn_diff_original_chars,
                    change_diff_omitted_chars=change_diff_omitted_chars,
                    change_diff_head_line_counts=change_diff_head_line_counts,
                    change_diff_original_chars=change_diff_original_chars,
                    generic_output_cached=generic_output_cached,
                    generic_output_lines=generic_output_lines,
                    generic_output_omitted_chars=generic_output_omitted_chars,
                    generic_output_head_line_count=(
                        generic_output_head_line_count
                    ),
                    generic_output_original_chars=generic_output_original_chars,
                )
            )
        return remembered

    @classmethod
    def _generic_tool_output_for_cache(
        cls,
        item: dict[str, Any],
    ) -> str | list[str] | None:
        """Build the exact generic-card output before raw carriers are dropped."""

        item_type = str(item.get("type", "") or "").strip()
        if item_type == "imageGeneration":
            saved_path = str(item.get("savedPath", "") or "")
            result = str(item.get("result", "") or "")
            safe_result = safe_inline_image_data_url(result)
            output = generic_tool_output(
                item,
                image_result_is_media=bool(saved_path) or bool(safe_result),
            )
            if safe_result:
                item["result"] = safe_result
            else:
                item.pop("result", None)
            return output
        return generic_tool_output(item)

    @classmethod
    def _strip_generic_tool_output_carriers(
        cls,
        item: dict[str, Any],
    ) -> None:
        item_type = str(item.get("type", "") or "").strip()
        if item_type == "mcpToolCall":
            for key in ("result", "error", "progressMessages"):
                item.pop(key, None)
            return
        if item_type == "dynamicToolCall":
            content_items = item.get("contentItems")
            if isinstance(content_items, list):
                for content in content_items:
                    if isinstance(content, dict) and content.get("type") == "inputText":
                        content["text"] = ""
            return
        if item_type == "webSearch":
            item.pop("results", None)
            return
        if item_type == "collabAgentToolCall":
            states = item.get("agentsStates")
            if isinstance(states, dict):
                for state in states.values():
                    if isinstance(state, dict) and "message" in state:
                        message = str(state.get("message", "") or "")
                        # Tasks metadata is a separate presentation surface.
                        # Bound each retained message without charging the
                        # tool-card aggregate a second time.
                        presented = present_tool_output(message)
                        state["message"] = "\n".join(presented.lines)
            return
        if item_type in {"enteredReviewMode", "exitedReviewMode"}:
            item["review"] = ""
            return
        if item_type == "plan":
            item["text"] = ""
            return
        if item_type == "imageGeneration":
            return
        preserved = {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key in {"id", "type", "status", "durationMs"}
        }
        item.clear()
        item.update(preserved)

    @classmethod
    def _bound_aggregated_output(
        cls,
        item: dict[str, Any],
        budget: ToolOutputPresentationBudget,
        metadata: CachedToolOutputPresentation | None,
    ) -> tuple[int, int, int]:
        if "aggregatedOutput" not in item:
            return 0, 0, 0
        output = str(item.get("aggregatedOutput", "") or "")
        presentation = cls._cached_text_presentation(
            output,
            omitted_chars=(
                metadata.aggregated_output_omitted_chars if metadata else 0
            ),
            head_line_count=(
                metadata.aggregated_output_head_line_count if metadata else 0
            ),
            original_chars=(
                metadata.aggregated_output_original_chars if metadata else 0
            ),
        )
        admitted = budget.admit(presentation)
        item["aggregatedOutput"] = "\n".join(admitted.lines)
        return (
            admitted.omitted_chars,
            admitted.head_line_count,
            admitted.original_chars,
        )

    @classmethod
    def _bound_turn_diff(
        cls,
        item: dict[str, Any],
        budget: ToolOutputPresentationBudget,
        metadata: CachedToolOutputPresentation | None,
    ) -> tuple[int, int, int]:
        if "diff" not in item:
            return 0, 0, 0
        diff = str(item.get("diff", "") or "")
        presentation = cls._cached_text_presentation(
            diff,
            omitted_chars=metadata.turn_diff_omitted_chars if metadata else 0,
            head_line_count=metadata.turn_diff_head_line_count if metadata else 0,
            original_chars=metadata.turn_diff_original_chars if metadata else 0,
        )
        admitted = budget.admit(presentation)
        item["diff"] = "\n".join(admitted.lines)
        return (
            admitted.omitted_chars,
            admitted.head_line_count,
            admitted.original_chars,
        )

    @staticmethod
    def _bound_file_change_diffs(
        item: dict[str, Any],
        budget: ToolOutputPresentationBudget,
        metadata: CachedToolOutputPresentation | None,
    ) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
        changes = item.get("changes")
        if not isinstance(changes, list):
            return (), (), ()
        omitted_by_change = [0] * len(changes)
        head_lines_by_change = [0] * len(changes)
        original_chars_by_change = [0] * len(changes)
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or "diff" not in change:
                continue
            diff = str(change.get("diff", "") or "")
            presentation = WebThreadReadModel._cached_text_presentation(
                diff,
                omitted_chars=WebThreadReadModel._tuple_value(
                    metadata.change_diff_omitted_chars if metadata else (),
                    index,
                ),
                head_line_count=WebThreadReadModel._tuple_value(
                    metadata.change_diff_head_line_counts if metadata else (),
                    index,
                ),
                original_chars=WebThreadReadModel._tuple_value(
                    metadata.change_diff_original_chars if metadata else (),
                    index,
                ),
            )
            admitted = budget.admit(presentation)
            change["diff"] = "\n".join(admitted.lines)
            omitted_by_change[index] = admitted.omitted_chars
            head_lines_by_change[index] = admitted.head_line_count
            original_chars_by_change[index] = admitted.original_chars
        return (
            tuple(omitted_by_change),
            tuple(head_lines_by_change),
            tuple(original_chars_by_change),
        )

    @staticmethod
    def _cached_text_presentation(
        text: str,
        *,
        omitted_chars: int,
        head_line_count: int,
        original_chars: int,
    ) -> ToolOutputPresentation:
        if omitted_chars <= 0:
            return present_tool_output(text)
        lines = text.split("\n") if text else []
        exact_original_chars = max(int(original_chars), 0)
        if exact_original_chars <= 0:
            exact_original_chars = (
                omitted_chars
                if not lines
                else MAX_VISIBLE + omitted_chars
            )
        return ToolOutputPresentation(
            lines=lines,
            omitted_chars=max(int(omitted_chars), 0),
            head_line_count=max(int(head_line_count), 0),
            original_chars=exact_original_chars,
        )

    @staticmethod
    def _tuple_value(values: tuple[int, ...], index: int) -> int:
        if index < 0 or index >= len(values):
            return 0
        return max(int(values[index]), 0)

    @classmethod
    def _replace_file_changes(
        cls,
        item: dict[str, Any],
        changes: list[Any],
    ) -> None:
        metadata = item.get(INTERNAL_PRESENTATION_METADATA_KEY)
        aggregated_output_omitted_chars = (
            metadata.aggregated_output_omitted_chars
            if isinstance(metadata, CachedToolOutputPresentation)
            else 0
        )
        aggregated_output_head_line_count = (
            metadata.aggregated_output_head_line_count
            if isinstance(metadata, CachedToolOutputPresentation)
            else 0
        )
        aggregated_output_original_chars = (
            metadata.aggregated_output_original_chars
            if isinstance(metadata, CachedToolOutputPresentation)
            else 0
        )
        item["changes"] = [
            copy.deepcopy(change) for change in changes if isinstance(change, dict)
        ]
        (
            change_diff_omitted_chars,
            change_diff_head_line_counts,
            change_diff_original_chars,
        ) = cls._bound_file_change_diffs(
            item,
            ToolOutputPresentationBudget(),
            None,
        )
        if aggregated_output_omitted_chars or any(change_diff_omitted_chars):
            item[INTERNAL_PRESENTATION_METADATA_KEY] = CachedToolOutputPresentation(
                aggregated_output_omitted_chars=aggregated_output_omitted_chars,
                aggregated_output_head_line_count=(
                    aggregated_output_head_line_count
                ),
                aggregated_output_original_chars=(
                    aggregated_output_original_chars
                ),
                change_diff_omitted_chars=change_diff_omitted_chars,
                change_diff_head_line_counts=change_diff_head_line_counts,
                change_diff_original_chars=change_diff_original_chars,
            )
        else:
            item.pop(INTERNAL_PRESENTATION_METADATA_KEY, None)

    @staticmethod
    def _live_turn(
        turns: dict[str, dict[str, Any]],
        turn_id: str,
    ) -> dict[str, Any]:
        turn = turns.get(turn_id)
        if turn is None:
            turn = {"id": turn_id, "status": "inProgress", "items": []}
            turns[turn_id] = turn
        if not isinstance(turn.get("items"), list):
            turn["items"] = []
        return turn

    @classmethod
    def _merge_turn(
        cls,
        current: dict[str, Any] | None,
        incoming: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(current, dict):
            return copy.deepcopy(incoming)
        merged = copy.deepcopy(current)
        for key, value in incoming.items():
            if key != "items":
                merged[key] = copy.deepcopy(value)
        incoming_items = incoming.get("items")
        if isinstance(incoming_items, list):
            if not isinstance(merged.get("items"), list):
                merged["items"] = []
            for item in incoming_items:
                if isinstance(item, dict):
                    cls._upsert_turn_item(merged, item)
        cls._rebudget_turn(merged)
        return merged

    @classmethod
    def _rebudget_turn(cls, turn: dict[str, Any]) -> None:
        items = turn.get("items")
        if not isinstance(items, list):
            return
        budget = ToolOutputPresentationBudget()
        turn["items"] = [
            cls._bounded_item_copy(item, budget)
            for item in items
            if isinstance(item, dict)
        ]

    @staticmethod
    def _find_turn_item(
        turn: dict[str, Any],
        item_id: str,
    ) -> dict[str, Any] | None:
        items = turn.get("items") if isinstance(turn.get("items"), list) else []
        for item in items:
            if isinstance(item, dict) and str(item.get("id", "") or "").strip() == item_id:
                return item
        return None

    @classmethod
    def _upsert_turn_item(cls, turn: dict[str, Any], item: dict[str, Any]) -> None:
        item_id = str(item.get("id", "") or "").strip()
        items = turn.setdefault("items", [])
        if not isinstance(items, list):
            items = []
            turn["items"] = items
        if item_id:
            for index, current in enumerate(items):
                if (
                    isinstance(current, dict)
                    and str(current.get("id", "") or "").strip() == item_id
                ):
                    items[index] = copy.deepcopy(item)
                    return
        items.append(copy.deepcopy(item))

    @classmethod
    def _remember_live_item_delta(
        cls,
        turn: dict[str, Any],
        method: str,
        params: dict[str, Any],
        item_id: str,
        delta: str,
    ) -> None:
        item = cls._find_turn_item(turn, item_id)
        if item is None:
            cls._upsert_turn_item(
                turn,
                {
                    "id": item_id,
                    "type": cls._item_type_for_delta(method),
                    "status": "inProgress",
                },
            )
            item = cls._find_turn_item(turn, item_id)
        if item is None:
            return
        if method == "item/agentMessage/delta":
            item["text"] = f"{str(item.get('text', '') or '')}{delta}"
            return
        if method == "item/reasoning/summaryTextDelta":
            cls._append_indexed_delta(item, "summary", params.get("summaryIndex"), delta)
            return
        if method == "item/reasoning/summaryPartAdded":
            cls._append_indexed_delta(item, "summary", params.get("summaryIndex"), "")
            return
        if method == "item/reasoning/textDelta":
            cls._append_indexed_delta(item, "content", params.get("contentIndex"), delta)

    @staticmethod
    def _append_indexed_delta(
        item: dict[str, Any],
        field: str,
        raw_index: Any,
        delta: str,
    ) -> None:
        try:
            index = max(int(raw_index), 0)
        except (TypeError, ValueError):
            index = 0
        values = item.setdefault(field, [])
        if not isinstance(values, list):
            values = []
            item[field] = values
        while len(values) <= index:
            values.append("")
        values[index] = f"{str(values[index] or '')}{delta}"

    @staticmethod
    def _item_type_for_delta(method: str) -> str:
        return {
            "item/agentMessage/delta": "agentMessage",
            "item/fileChange/patchUpdated": "fileChange",
            "item/reasoning/summaryTextDelta": "reasoning",
            "item/reasoning/summaryPartAdded": "reasoning",
            "item/reasoning/textDelta": "reasoning",
        }.get(method, "unknown")

    @staticmethod
    def _update(
        method: str,
        thread_id: str,
        detail: dict[str, Any],
        *,
        raw_turn: dict[str, Any] | None = None,
        goal_changed: bool = False,
        goal: dict[str, Any] | None = None,
    ) -> WebThreadNotificationUpdate:
        return WebThreadNotificationUpdate(
            method=method,
            thread_id=thread_id,
            detail=copy.deepcopy(detail),
            raw_turn=copy.deepcopy(raw_turn) if raw_turn is not None else None,
            goal_changed=goal_changed,
            goal=copy.deepcopy(goal) if goal is not None else None,
        )

    @staticmethod
    def _thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("Web thread read model requires a thread id.")
        return normalized

    def _advance_observation(self, thread_id: str) -> int:
        self._next_observation_revision += 1
        self._observation_revision_by_thread[thread_id] = (
            self._next_observation_revision
        )
        return self._next_observation_revision
