"""Pure, bounded process-log projection for Feishu execution cards.

The app-server completion item is the display source for command output and
file changes.  Streaming tool deltas are runtime evidence only and therefore
never need a second buffer in this owner.
"""

from __future__ import annotations

from typing import Any, Protocol

from bot.constants import display_path


NORMAL_PROCESS_LOG_LIMIT_BYTES = 10 * 1024
DIAGNOSTIC_PROCESS_LOG_LIMIT_BYTES = 12 * 1024

_COMMAND_FIELD_LIMIT_BYTES = 1024
_OUTPUT_CANDIDATE_LIMIT_BYTES = 2 * 1024
_FILE_PATH_LIMIT_BYTES = 512
_ELLIPSIS = "…"


class _ProcessTranscript(Protocol):
    def process_text(self) -> str: ...


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _safe_text(value: object) -> str:
    raw = "" if value is None else value if type(value) is str else str(value)
    return raw.encode("utf-8", errors="replace").decode("utf-8")


def _truncate_utf8_head(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    safe = _safe_text(text)
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit:
        return safe
    marker = _ELLIPSIS.encode("utf-8")
    if limit < len(marker):
        return encoded[:limit].decode("utf-8", errors="ignore")
    retained = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
    return f"{retained}{_ELLIPSIS}"


def _truncate_utf8_tail(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    safe = _safe_text(text)
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit:
        return safe
    marker = _ELLIPSIS.encode("utf-8")
    if limit < len(marker):
        return encoded[-limit:].decode("utf-8", errors="ignore")
    retained = encoded[-(limit - len(marker)) :].decode(
        "utf-8",
        errors="ignore",
    )
    return f"{_ELLIPSIS}{retained}"


def _bounded_one_line(value: object, *, limit: int) -> str:
    raw = "" if value is None else value if type(value) is str else str(value)
    cropped = len(raw) > limit
    candidate = raw[:limit]
    normalized = _safe_text(candidate).replace("\r\n", "\n").replace("\r", "\n")
    flattened = " ".join(normalized.splitlines()).strip()
    if cropped and flattened:
        flattened = f"{flattened}{_ELLIPSIS}"
    return _truncate_utf8_head(flattened, limit)


def _meaningful_tail_lines(value: object, *, count: int) -> str:
    raw = "" if value is None else value if type(value) is str else str(value)
    end = len(raw)
    while end and raw[end - 1].isspace():
        end -= 1
    if not end:
        return ""

    start = 0
    cursor = end
    for _ in range(count):
        line_feed = raw.rfind("\n", 0, cursor)
        carriage_return = raw.rfind("\r", 0, cursor)
        delimiter_end = max(line_feed, carriage_return)
        if delimiter_end < 0:
            start = 0
            break
        start = delimiter_end + 1
        cursor = delimiter_end
        if (
            raw[delimiter_end] == "\n"
            and delimiter_end > 0
            and raw[delimiter_end - 1] == "\r"
        ):
            cursor -= 1

    candidate_start = max(start, end - _OUTPUT_CANDIDATE_LIMIT_BYTES)
    candidate = raw[candidate_start:end]
    normalized = candidate.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    projected = "\n".join(lines[first:])
    if not projected:
        return ""
    return f"{_ELLIPSIS}{projected}" if candidate_start > start else projected


def _remaining_bytes(transcript: _ProcessTranscript, *, limit: int) -> int:
    return max(limit - _utf8_size(transcript.process_text()), 0)


def _bounded_completion(
    header: str,
    detail: str,
    *,
    transcript: _ProcessTranscript,
    limit: int,
) -> str:
    remaining = _remaining_bytes(transcript, limit=limit)
    if remaining <= 0:
        return ""
    bounded_header = _truncate_utf8_head(header, remaining)
    available = remaining - _utf8_size(bounded_header)
    if available <= 0 or not detail:
        return bounded_header
    bounded_detail = _truncate_utf8_tail(
        detail,
        min(available, _OUTPUT_CANDIDATE_LIMIT_BYTES),
    )
    return f"{bounded_header}{bounded_detail}"


def _command_status(item: dict[str, Any]) -> str:
    return _bounded_one_line(item.get("status"), limit=64) or "unknown"


def _command_exit(item: dict[str, Any]) -> str:
    value = item.get("exitCode")
    return str(value) if type(value) is int else "-"


def _command_duration(item: dict[str, Any]) -> str:
    value = item.get("durationMs")
    return f"{value}ms" if type(value) in {int, float} and value >= 0 else "-"


def _command_is_success(item: dict[str, Any]) -> bool:
    status = _command_status(item).lower()
    exit_code = item.get("exitCode")
    return status == "completed" and (
        exit_code is None or (type(exit_code) is int and exit_code == 0)
    )


class FeishuExecutionProcessProjection:
    """Own the display-only, bounded projection of completed tool facts."""

    @staticmethod
    def command_started(
        item: dict[str, Any],
        *,
        transcript: _ProcessTranscript,
    ) -> str:
        raw_cwd = _bounded_one_line(
            item.get("cwd"),
            limit=_COMMAND_FIELD_LIMIT_BYTES,
        )
        cwd = display_path(raw_cwd) if raw_cwd else "-"
        cwd = _truncate_utf8_head(cwd, _COMMAND_FIELD_LIMIT_BYTES)
        command = _bounded_one_line(
            item.get("command"),
            limit=_COMMAND_FIELD_LIMIT_BYTES,
        )
        command = command or "-"
        candidate = f"\n$ ({cwd}) {command}\n"
        return _truncate_utf8_head(
            candidate,
            _remaining_bytes(
                transcript,
                limit=NORMAL_PROCESS_LOG_LIMIT_BYTES,
            ),
        )

    @staticmethod
    def command_completed(
        item: dict[str, Any],
        *,
        transcript: _ProcessTranscript,
    ) -> str:
        status = _command_status(item)
        header = (
            f"\n[命令结束 status={status} exit={_command_exit(item)} "
            f"duration={_command_duration(item)}]\n"
        )
        success = _command_is_success(item)
        output = _meaningful_tail_lines(
            item.get("aggregatedOutput"),
            count=1 if success else 4,
        )
        detail = ""
        if output:
            detail = f"[{'输出' if success else '诊断输出'}]\n{output}\n"
        return _bounded_completion(
            header,
            detail,
            transcript=transcript,
            limit=(
                NORMAL_PROCESS_LOG_LIMIT_BYTES
                if success
                else DIAGNOSTIC_PROCESS_LOG_LIMIT_BYTES
            ),
        )

    @staticmethod
    def file_change_started(
        *,
        transcript: _ProcessTranscript,
    ) -> str:
        return _truncate_utf8_head(
            "\n[文件修改]\n",
            _remaining_bytes(
                transcript,
                limit=NORMAL_PROCESS_LOG_LIMIT_BYTES,
            ),
        )

    @staticmethod
    def file_change_completed(
        item: dict[str, Any],
        *,
        transcript: _ProcessTranscript,
    ) -> str:
        raw_changes = item.get("changes")
        changes = raw_changes if isinstance(raw_changes, list) else []
        lines = [f"\n[文件变更 count={len(changes)}]\n"]
        for change in changes[:3]:
            raw_path = change.get("path") if isinstance(change, dict) else ""
            path = _bounded_one_line(
                raw_path,
                limit=_FILE_PATH_LIMIT_BYTES,
            )
            path = path or "?"
            lines.append(f"- {path}\n")
        remaining_count = max(len(changes) - 3, 0)
        if remaining_count:
            lines.append(f"- 另有 {remaining_count} 项\n")
        return _truncate_utf8_head(
            "".join(lines),
            _remaining_bytes(
                transcript,
                limit=NORMAL_PROCESS_LOG_LIMIT_BYTES,
            ),
        )
