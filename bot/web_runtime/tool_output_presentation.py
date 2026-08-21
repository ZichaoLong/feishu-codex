"""Bound tool output before it enters the Focus Web projection."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

from bot.input_media_contract import (
    is_native_input_media_type,
    validate_declared_native_media,
)

MAX_VISIBLE = 65_536
HEAD = 16_384
TAIL = 49_152

# A raw Codex turn or full-detail page can contain an arbitrary number of tool
# items. Per-tool truncation alone therefore bounds neither presentation
# window. These limits apply only to a Focus Web view; the upstream rollout
# remains authoritative and lossless.
MAX_TOOL_OUTPUT_WINDOW_CHARS = 262_144
MAX_TOOL_OUTPUT_WINDOW_OUTPUTS = 16

MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
_INLINE_IMAGE_DATA_URL_RE = re.compile(
    r"\Adata:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})\Z",
    re.IGNORECASE,
)

INTERNAL_PRESENTATION_METADATA_KEY = "_focusWebPresentation"


@dataclass(frozen=True, slots=True)
class CachedToolOutputPresentation:
    """Python-only proof that a read-cache payload was bounded by Focus."""

    aggregated_output_omitted_chars: int = 0
    aggregated_output_head_line_count: int = 0
    aggregated_output_original_chars: int = 0
    turn_diff_omitted_chars: int = 0
    turn_diff_head_line_count: int = 0
    turn_diff_original_chars: int = 0
    change_diff_omitted_chars: tuple[int, ...] = ()
    change_diff_head_line_counts: tuple[int, ...] = ()
    change_diff_original_chars: tuple[int, ...] = ()
    generic_output_cached: bool = False
    generic_output_lines: tuple[str, ...] = ()
    generic_output_omitted_chars: int = 0
    generic_output_head_line_count: int = 0
    generic_output_original_chars: int = 0


@dataclass(slots=True)
class ToolOutputPresentation:
    lines: list[str]
    omitted_chars: int = 0
    # Number of lines before the Focus-owned omission row.
    head_line_count: int = 0
    # Exact conceptual source length before presentation truncation.
    original_chars: int = 0


@dataclass(slots=True)
class ToolOutputPresentationBudget:
    """Stateless aggregate budget for one tool-output presentation window."""

    remaining_chars: int = MAX_TOOL_OUTPUT_WINDOW_CHARS
    remaining_outputs: int = MAX_TOOL_OUTPUT_WINDOW_OUTPUTS

    def admit(self, presentation: ToolOutputPresentation) -> ToolOutputPresentation:
        """Keep one bounded output or fully omit it when this turn is full."""

        if not presentation.lines:
            return presentation
        presented_chars = _conceptual_chars(presentation.lines)
        if (
            self.remaining_outputs > 0
            and presented_chars <= self.remaining_chars
        ):
            self.remaining_chars -= presented_chars
            self.remaining_outputs -= 1
            return presentation
        original_chars = max(
            int(presentation.original_chars),
            int(presentation.omitted_chars),
            0,
        )
        return ToolOutputPresentation(
            lines=[],
            omitted_chars=original_chars,
            head_line_count=0,
            original_chars=original_chars,
        )


def present_tool_output(output: str | list[str]) -> ToolOutputPresentation:
    """Preserve small output and retain a bounded head and tail when large."""

    if isinstance(output, str):
        if len(output) <= MAX_VISIBLE:
            return ToolOutputPresentation(
                lines=_split_output_text(output),
                original_chars=len(output),
            )
        return _truncated_text(output)

    total_chars = _conceptual_chars(output)
    if total_chars <= MAX_VISIBLE:
        return ToolOutputPresentation(lines=output, original_chars=total_chars)

    omitted_chars = total_chars - MAX_VISIBLE
    head_lines = _split_output_text(_take_head(output, HEAD))
    return ToolOutputPresentation(
        lines=[
            *head_lines,
            _omission_marker(omitted_chars),
            *_split_output_text(_take_tail(output, TAIL)),
        ],
        omitted_chars=omitted_chars,
        head_line_count=len(head_lines),
        original_chars=total_chars,
    )


def _truncated_text(text: str) -> ToolOutputPresentation:
    omitted_chars = len(text) - MAX_VISIBLE
    head_lines = _split_output_text(text[:HEAD])
    return ToolOutputPresentation(
        lines=[
            *head_lines,
            _omission_marker(omitted_chars),
            *_split_output_text(text[-TAIL:]),
        ],
        omitted_chars=omitted_chars,
        head_line_count=len(head_lines),
        original_chars=len(text),
    )


def _conceptual_chars(lines: list[str] | tuple[str, ...]) -> int:
    return sum(len(line) for line in lines) + max(len(lines) - 1, 0)


def _split_output_text(text: str) -> list[str]:
    """Split for line rendering while retaining exact LF chunk boundaries."""

    return [] if not text else text.split("\n")


def _take_head(lines: list[str], budget: int) -> str:
    parts: list[str] = []
    remaining = budget
    for index, line in enumerate(lines):
        if index:
            if remaining == 0:
                break
            parts.append("\n")
            remaining -= 1
        if remaining == 0:
            break
        part = line[:remaining]
        parts.append(part)
        remaining -= len(part)
    return "".join(parts)


def _take_tail(lines: list[str], budget: int) -> str:
    reverse_parts: list[str] = []
    remaining = budget
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        part = line[-remaining:] if remaining < len(line) else line
        reverse_parts.append(part)
        remaining -= len(part)
        if remaining == 0:
            break
        if index:
            reverse_parts.append("\n")
            remaining -= 1
            if remaining == 0:
                break
    return "".join(reversed(reverse_parts))


def _omission_marker(omitted_chars: int) -> str:
    return (
        f"[Focus Web omitted {omitted_chars} characters of tool output; "
        "showing a bounded head and tail.]"
    )


def generic_tool_output(
    item: dict[str, Any],
    *,
    image_result_is_media: bool = False,
) -> str | list[str] | None:
    """Build the generic-card output shared by cache and wire projection."""

    item_type = str(item.get("type", "") or "").strip()
    if item_type == "mcpToolCall":
        result = item.get("result")
        error = item.get("error")
        output = _json_lines(result if result is not None else error)
        progress = item.get("progressMessages")
        if isinstance(progress, list):
            output = [
                *[
                    str(message)
                    for message in progress
                    if str(message).strip()
                ],
                *output,
            ]
        return output
    if item_type == "dynamicToolCall":
        content_items = (
            item.get("contentItems")
            if isinstance(item.get("contentItems"), list)
            else []
        )
        return [
            str(content.get("text", "") or "")
            for content in content_items
            if isinstance(content, dict)
            and content.get("type") == "inputText"
            and str(content.get("text", "") or "")
        ]
    if item_type == "webSearch":
        return _json_lines(item.get("results"))
    if item_type == "collabAgentToolCall":
        receiver_ids = (
            item.get("receiverThreadIds")
            if isinstance(item.get("receiverThreadIds"), list)
            else []
        )
        states = (
            item.get("agentsStates")
            if isinstance(item.get("agentsStates"), dict)
            else {}
        )
        output: list[str] = []
        for raw_thread_id in receiver_ids:
            thread_id = str(raw_thread_id or "").strip()
            state = states.get(thread_id) if thread_id else None
            status = (
                str(state.get("status", "") or "").strip()
                if isinstance(state, dict)
                else ""
            )
            message = (
                str(state.get("message", "") or "").strip()
                if isinstance(state, dict)
                else ""
            )
            summary = f"thread: {thread_id}"
            if status:
                summary += f" · {status}"
            output.append(summary)
            if message:
                output.extend(message.splitlines())
        return output
    if item_type in {"enteredReviewMode", "exitedReviewMode"}:
        return str(item.get("review", "") or "")
    if item_type == "plan":
        return [str(item.get("text", "") or "")]
    if item_type == "imageGeneration":
        revised_prompt = str(item.get("revisedPrompt", "") or "")
        result = str(item.get("result", "") or "")
        if image_result_is_media:
            return [revised_prompt] if revised_prompt else []
        return [value for value in (revised_prompt, *result.splitlines()) if value]
    if item_type and item_type not in {
        "agentMessage",
        "commandExecution",
        "contextCompaction",
        "fileChange",
        "hookPrompt",
        "imageView",
        "reasoning",
        "sleep",
        "subAgentActivity",
        "turnDiff",
        "userMessage",
    }:
        return diagnostic_output_lines(
            {
                key: value
                for key, value in item.items()
                if key not in {"id", "type", INTERNAL_PRESENTATION_METADATA_KEY}
            }
        )
    return None


def file_change_fallback_output(item: dict[str, Any]) -> list[str]:
    """Build the one fallback card shown when no file-change row exists."""

    return _json_lines(
        {
            key: value
            for key, value in item.items()
            if key != INTERNAL_PRESENTATION_METADATA_KEY
        }
    )


def safe_inline_image_data_url(value: str) -> str:
    """Return a bounded, signature-checked inline image data URL.

    This is the single admission boundary used both before Web read-cache
    carrier stripping and before browser-media projection. A declared image
    MIME type alone never makes an app-server payload safe browser media.
    """

    match = _INLINE_IMAGE_DATA_URL_RE.fullmatch(str(value or "").strip())
    if match is None:
        return ""
    declared_media_type = match.group(1).lower()
    encoded = match.group(2)
    if (
        not declared_media_type.startswith("image/")
        or not is_native_input_media_type(declared_media_type)
    ):
        return ""
    if len(encoded) > ((MAX_INLINE_IMAGE_BYTES + 2) // 3) * 4:
        return ""
    try:
        payload = base64.b64decode(encoded, validate=True)
        canonical_media_type = validate_declared_native_media(
            declared_media_type,
            payload,
        )
    except (ValueError, binascii.Error):
        return ""
    if (
        not payload
        or len(payload) > MAX_INLINE_IMAGE_BYTES
        or not canonical_media_type.startswith("image/")
    ):
        return ""
    return (
        f"data:{canonical_media_type};base64,"
        f"{base64.b64encode(payload).decode('ascii')}"
    )


def _json_lines(value: Any) -> list[str]:
    if value in (None, "", {}, []):
        return []
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = str(value)
    return text.splitlines() if text else []


def diagnostic_output_lines(value: Any) -> list[str]:
    """Return the bounded, redacted fallback used for unknown app-server items."""

    redacted = _redact_diagnostic_value(value, depth=0)
    try:
        text = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(redacted)
    if len(text) > 12_000:
        text = f"{text[:12_000]}\n... diagnostics truncated ..."
    return text.splitlines() if text else []


def _redact_diagnostic_value(value: Any, *, depth: int) -> Any:
    if depth >= 8:
        return "<maximum depth reached>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:80]:
            key = str(raw_key)
            lowered = key.lower().replace("_", "").replace("-", "")
            if any(
                secret in lowered
                for secret in (
                    "authorization",
                    "password",
                    "secret",
                    "token",
                    "cookie",
                )
            ):
                result[key] = "<redacted>"
            else:
                result[key] = _redact_diagnostic_value(
                    raw_value,
                    depth=depth + 1,
                )
        if len(value) > 80:
            result["<truncated>"] = f"{len(value) - 80} additional fields"
        return result
    if isinstance(value, list):
        items = [
            _redact_diagnostic_value(item, depth=depth + 1)
            for item in value[:80]
        ]
        if len(value) > 80:
            items.append(f"<{len(value) - 80} additional items>")
        return items
    if isinstance(value, str) and len(value) > 4_000:
        return f"{value[:4_000]}... <truncated>"
    return value
