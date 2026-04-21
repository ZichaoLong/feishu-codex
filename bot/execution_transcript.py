"""
Structured execution transcript state for Feishu execution cards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


_SNAPSHOT_WORK_ITEM_TYPES = {
    "commandExecution",
    "fileChange",
    "imageGeneration",
    "mcpToolCall",
    "patchApply",
    "viewImageToolCall",
    "webSearch",
}

_CARD_REPLY_TRUNCATION_NOTICE = "\n\n**[回复过长，执行卡仅显示部分内容]**"
_CARD_REPLY_COMPACT_TRUNCATION_NOTICE = "[回复过长]"


@dataclass(frozen=True)
class ExecutionReplySegment:
    kind: Literal["assistant", "divider"]
    text: str = ""


@dataclass
class ExecutionTranscript:
    reply_segments: list[ExecutionReplySegment] = field(default_factory=list)
    process_blocks: list[str] = field(default_factory=list)
    _active_reply_index: int | None = None
    _active_process_index: int | None = None
    _pending_reply_divider: bool = False
    _had_assistant_output: bool = False

    def clone(self) -> ExecutionTranscript:
        return ExecutionTranscript(
            reply_segments=list(self.reply_segments),
            process_blocks=list(self.process_blocks),
            _active_reply_index=self._active_reply_index,
            _active_process_index=self._active_process_index,
            _pending_reply_divider=self._pending_reply_divider,
            _had_assistant_output=self._had_assistant_output,
        )

    def reset(self) -> None:
        self.reply_segments = []
        self.process_blocks = []
        self._active_reply_index = None
        self._active_process_index = None
        self._pending_reply_divider = False
        self._had_assistant_output = False

    def reply_text(self) -> str:
        return "\n\n".join(
            segment.text
            for segment in self.reply_segments
            if segment.kind == "assistant" and segment.text
        )

    def process_text(self) -> str:
        return "".join(block for block in self.process_blocks if block)

    def has_reply_output(self) -> bool:
        return any(segment.kind == "assistant" and bool(segment.text) for segment in self.reply_segments)

    def has_process_output(self) -> bool:
        return any(bool(block) for block in self.process_blocks)

    def set_reply_text(self, text: str) -> None:
        normalized = str(text or "")
        self.reply_segments = [ExecutionReplySegment("assistant", normalized)] if normalized else []
        self._active_reply_index = None
        self._pending_reply_divider = False
        self._had_assistant_output = bool(normalized)

    def rebuild_reply_from_snapshot_items(
        self,
        items: list[dict[str, Any]] | None,
        *,
        fallback_text: str = "",
        drop_last_text_message: bool = False,
    ) -> bool:
        last_text_index = None
        if drop_last_text_message:
            for idx, item in enumerate(items or []):
                if str(item.get("type", "") or "").strip() != "agentMessage":
                    continue
                if str(item.get("text", "") or "").strip():
                    last_text_index = idx
        rebuilt: list[ExecutionReplySegment] = []
        saw_assistant = False
        saw_work_since_assistant = False
        for idx, item in enumerate(items or []):
            item_type = str(item.get("type", "") or "").strip()
            if item_type == "agentMessage":
                text = str(item.get("text", "") or "").strip()
                if not text:
                    continue
                if drop_last_text_message and idx == last_text_index:
                    continue
                if saw_assistant and saw_work_since_assistant:
                    rebuilt.append(ExecutionReplySegment("divider"))
                rebuilt.append(ExecutionReplySegment("assistant", text))
                saw_assistant = True
                saw_work_since_assistant = False
                continue
            if saw_assistant and item_type in _SNAPSHOT_WORK_ITEM_TYPES:
                saw_work_since_assistant = True
        if rebuilt:
            self.reply_segments = rebuilt
            self._active_reply_index = None
            self._pending_reply_divider = False
            self._had_assistant_output = any(
                segment.kind == "assistant" and bool(segment.text)
                for segment in rebuilt
            )
            return True
        if fallback_text:
            self.set_reply_text(fallback_text)
            return True
        return False

    def append_assistant_delta(self, delta: str) -> None:
        if not delta:
            return
        self._active_process_index = None
        if self._active_reply_index is None:
            if self._pending_reply_divider and self._had_assistant_output:
                self.reply_segments.append(ExecutionReplySegment("divider"))
            self._pending_reply_divider = False
            self.reply_segments.append(ExecutionReplySegment("assistant", ""))
            self._active_reply_index = len(self.reply_segments) - 1
        current = self.reply_segments[self._active_reply_index]
        self.reply_segments[self._active_reply_index] = ExecutionReplySegment(
            "assistant",
            current.text + delta,
        )
        self._had_assistant_output = True

    def reconcile_current_assistant_text(self, text: str) -> None:
        normalized = str(text or "")
        if not normalized:
            return
        target_index = self._active_reply_index
        if target_index is None and (
            self._pending_reply_divider
            or not self.reply_segments
            or self.reply_segments[-1].kind != "assistant"
        ):
            if self._pending_reply_divider and self._had_assistant_output:
                self.reply_segments.append(ExecutionReplySegment("divider"))
            self._pending_reply_divider = False
            self.reply_segments.append(ExecutionReplySegment("assistant", normalized))
            self._had_assistant_output = True
            return
        if target_index is None:
            for idx in range(len(self.reply_segments) - 1, -1, -1):
                if self.reply_segments[idx].kind == "assistant":
                    target_index = idx
                    break
        if target_index is None:
            self.set_reply_text(normalized)
            return
        self.reply_segments[target_index] = ExecutionReplySegment("assistant", normalized)
        self._had_assistant_output = True

    def start_process_block(self, text: str, *, marks_work: bool) -> None:
        self._active_reply_index = None
        self._active_process_index = None
        self.process_blocks.append(str(text or ""))
        self._active_process_index = len(self.process_blocks) - 1
        if marks_work and self._had_assistant_output:
            self._pending_reply_divider = True

    def append_process_delta(self, text: str) -> None:
        if not text:
            return
        if self._active_process_index is None:
            self.start_process_block("", marks_work=False)
        assert self._active_process_index is not None
        self.process_blocks[self._active_process_index] += text

    def finish_process_block(self, suffix: str = "") -> None:
        if suffix:
            self.append_process_delta(suffix)
        self._active_process_index = None

    def append_process_note(self, text: str, *, marks_work: bool = False) -> None:
        if not text:
            return
        self._active_reply_index = None
        self._active_process_index = None
        self.process_blocks.append(str(text))
        if marks_work and self._had_assistant_output:
            self._pending_reply_divider = True

    def reply_segments_for_card(self, char_limit: int) -> list[ExecutionReplySegment]:
        remaining = max(int(char_limit), 0)
        rendered: list[ExecutionReplySegment] = []
        pending_divider = False

        for segment in self.reply_segments:
            if segment.kind == "divider":
                if rendered:
                    pending_divider = True
                continue
            if not segment.text:
                continue
            if remaining <= 0:
                break
            text = segment.text
            if len(text) > remaining:
                text = self._truncate_card_reply_text(text, remaining)
                remaining = 0
            else:
                remaining -= len(text)
            if pending_divider and rendered:
                rendered.append(ExecutionReplySegment("divider"))
                pending_divider = False
            rendered.append(ExecutionReplySegment("assistant", text))
            if remaining <= 0:
                break
        return rendered

    @staticmethod
    def _truncate_card_reply_text(text: str, char_limit: int) -> str:
        limit = max(int(char_limit), 0)
        if limit <= 0:
            return ""
        full_notice = _CARD_REPLY_TRUNCATION_NOTICE
        compact_notice = _CARD_REPLY_COMPACT_TRUNCATION_NOTICE
        notice = full_notice if limit > len(compact_notice) + 16 else compact_notice
        if len(notice) >= limit:
            return notice[:limit]
        return text[: limit - len(notice)] + notice
