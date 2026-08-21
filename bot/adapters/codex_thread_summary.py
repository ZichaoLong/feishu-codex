"""Pure projection from Codex app-server thread payloads."""

from __future__ import annotations

from typing import Any, cast

from bot.adapters.base import ThreadHistoryMode, ThreadSourceStatus, ThreadSummary


def read_optional_string(data: dict[str, Any], *keys: str) -> str | None:
    """Read the first present string-compatible field from an upstream object."""

    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def thread_summary_from_app_server_thread(thread: dict[str, Any]) -> ThreadSummary:
    """Project one upstream thread object without constructing a transport."""

    status = thread.get("status") or {}
    source, subagent_kind, source_status = _session_source(thread.get("source"))
    history_mode = _required_history_mode(thread)
    return ThreadSummary(
        thread_id=thread.get("id", ""),
        cwd=thread.get("cwd", ""),
        name=thread.get("name") or "",
        preview=thread.get("preview") or "",
        created_at=int(thread.get("createdAt") or 0),
        updated_at=int(thread.get("updatedAt") or 0),
        source=source,
        status=status.get("type", "unknown"),
        active_flags=list(status.get("activeFlags") or []),
        path=thread.get("path"),
        model_provider=thread.get("modelProvider"),
        service_name=thread.get("serviceName"),
        session_id=read_optional_string(thread, "sessionId", "session_id"),
        parent_thread_id=read_optional_string(
            thread,
            "parentThreadId",
            "parent_thread_id",
        ),
        can_accept_direct_input=(
            thread.get("canAcceptDirectInput")
            if isinstance(thread.get("canAcceptDirectInput"), bool)
            else None
        ),
        thread_source=read_optional_string(thread, "threadSource", "thread_source"),
        ephemeral=bool(thread.get("ephemeral", False)),
        agent_nickname=read_optional_string(thread, "agentNickname", "agent_nickname"),
        agent_role=read_optional_string(thread, "agentRole", "agent_role"),
        subagent_kind=subagent_kind,
        history_mode=history_mode,
        source_status=source_status,
    )


def _required_history_mode(thread: dict[str, Any]) -> ThreadHistoryMode:
    """Decode the experimental persisted fact without inventing a default."""

    value = thread.get("historyMode")
    if not isinstance(value, str) or value not in {"legacy", "paginated"}:
        raise ValueError("upstream Thread.historyMode must be legacy or paginated")
    return cast(ThreadHistoryMode, value)


_DIRECT_ROOT_SOURCES = frozenset({"cli", "vscode", "exec", "appServer"})


def _normalize_source_key(value: object) -> str:
    return str(value or "").replace("_", "").replace("-", "").lower()


def _session_source(
    value: Any,
) -> tuple[str, str | None, ThreadSourceStatus]:
    """Project the upstream source while retaining proof of its shape.

    ``Thread.source`` is display data, but direct-target admission also needs
    to know whether the source was a valid upstream enum/object.  Unknown and
    malformed values remain readable as ``unknown`` and never silently become
    a root proof.
    """

    if isinstance(value, str):
        source = value.strip()
        if source in _DIRECT_ROOT_SOURCES:
            return source, None, "known"
        if source == "unknown":
            return "unknown", None, "unknown"
        return "unknown", None, "unknown"
    if not isinstance(value, dict):
        return "unknown", None, "malformed"
    if set(value) == {"custom"}:
        custom = value.get("custom")
        if isinstance(custom, str) and custom.strip():
            return "custom", None, "known"
        return "unknown", None, "malformed"
    if set(value) != {"subAgent"}:
        return "unknown", None, "malformed"

    raw_subagent = value.get("subAgent")
    if isinstance(raw_subagent, str):
        normalized = _normalize_source_key(raw_subagent)
        subagent_kind = {
            "review": "review",
            "compact": "compact",
            "memoryconsolidation": "memoryConsolidation",
        }.get(normalized)
        if subagent_kind is None:
            return "unknown", None, "unknown"
        return "subAgent", subagent_kind, "known"

    if not isinstance(raw_subagent, dict) or len(raw_subagent) != 1:
        return "unknown", None, "malformed"
    raw_kind, raw_detail = next(iter(raw_subagent.items()))
    normalized_kind = _normalize_source_key(raw_kind)
    if normalized_kind == "threadspawn":
        if not isinstance(raw_detail, dict):
            return "subAgent", "threadSpawn", "malformed"
        parent_thread_id = _first_alias(
            raw_detail,
            "parentThreadId",
            "parent_thread_id",
        )
        depth = raw_detail.get("depth")
        if (
            not isinstance(parent_thread_id, str)
            or not parent_thread_id.strip()
            or type(depth) is not int
            or depth < 0
        ):
            return "subAgent", "threadSpawn", "malformed"
        return "subAgent", "threadSpawn", "known"
    if normalized_kind == "other":
        if not isinstance(raw_detail, str) or not raw_detail.strip():
            return "subAgent", "other", "malformed"
        return "subAgent", raw_detail.strip(), "known"
    return "unknown", None, "unknown"


def _first_alias(data: dict[str, Any], *keys: str) -> object:
    present = [key for key in keys if key in data]
    if len(present) != 1:
        return None
    return data[present[0]]
