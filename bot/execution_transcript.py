"""
Structured execution transcript state for Feishu execution cards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal


_SNAPSHOT_WORK_ITEM_TYPES = {
    "collabAgentToolCall",
    "commandExecution",
    "contextCompaction",
    "dynamicToolCall",
    "enteredReviewMode",
    "exitedReviewMode",
    "fileChange",
    "imageView",
    "imageGeneration",
    "mcpToolCall",
    "patchApply",
    "plan",
    "reasoning",
    "sleep",
    "webSearch",
}
_TERMINAL_INVALIDATING_WORK_ITEM_TYPES = (
    _SNAPSHOT_WORK_ITEM_TYPES - {"imageGeneration"}
)
TerminalAgentReplyMatchReason = Literal[
    "matched",
    "coordinate_unavailable",
    "raw_text_mismatch",
    "not_trailing",
]


def is_execution_work_item_type(item_type: object) -> bool:
    """Return whether an app-server item separates assistant reply phases."""

    return str(item_type or "").strip() in _SNAPSHOT_WORK_ITEM_TYPES


def is_terminal_invalidating_work_item_type(item_type: object) -> bool:
    """Return whether later work proves an earlier agent item was not final."""

    return str(item_type or "").strip() in _TERMINAL_INVALIDATING_WORK_ITEM_TYPES


def agent_message_can_be_terminal_candidate(phase: object) -> bool:
    """Honor an explicit upstream message phase while retaining legacy fallback."""

    if phase is None:
        return True
    if type(phase) is not str:
        return False
    return phase.strip().lower() == "final_answer"


@dataclass(frozen=True, slots=True)
class TerminalAgentReplyCoordinate:
    """Exact raw reply interval owned by one completed agent item."""

    item_id: str
    raw_text: str
    start_reply_chars: int
    end_reply_chars: int

    def __post_init__(self) -> None:
        if type(self.item_id) is not str or type(self.raw_text) is not str:
            raise TypeError("terminal agent reply identity and text must be exact strings")
        if (
            type(self.start_reply_chars) is not int
            or type(self.end_reply_chars) is not int
        ):
            raise TypeError("terminal agent reply coordinates must be exact ints")
        if self.start_reply_chars < 0 or self.end_reply_chars <= self.start_reply_chars:
            raise ValueError("terminal agent reply interval must be positive and ordered")
        if self.end_reply_chars - self.start_reply_chars != len(self.raw_text):
            raise ValueError("terminal agent reply interval must match its raw text width")


@dataclass(frozen=True, slots=True)
class ExecutionReplySegment:
    kind: Literal["assistant", "divider"]
    text: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {"assistant", "divider"}:
            raise ValueError("unknown execution reply segment kind")
        if type(self.text) is not str:
            raise TypeError("execution reply segment text must be an exact string")


@dataclass(frozen=True, slots=True)
class ExecutionReplySegmentSnapshot:
    """Deeply immutable read vocabulary for one reply segment."""

    kind: Literal["assistant", "divider"]
    text: str = ""

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {"assistant", "divider"}:
            raise ValueError("unknown execution reply segment kind")
        if type(self.text) is not str:
            raise TypeError(
                "execution reply segment snapshot text must be an exact string"
            )


@dataclass(frozen=True, slots=True)
class ExecutionTranscriptSnapshot:
    """Deeply immutable value snapshot of one mutable transcript owner.

    Snapshot segments are copied into their own frozen vocabulary instead of
    retaining the owner's segment objects.  The tuple-only containers make
    the whole reachable value graph safe to pass across read-side boundaries.
    """

    reply_segments: tuple[ExecutionReplySegmentSnapshot, ...] = ()
    process_blocks: tuple[str, ...] = ()
    active_reply_index: int | None = None
    active_process_index: int | None = None
    pending_reply_divider: bool = False
    had_assistant_output: bool = False
    last_completed_assistant_text: str | None = None
    terminal_error_text: str | None = None
    terminal_agent_reply_coordinate: TerminalAgentReplyCoordinate | None = None

    def __post_init__(self) -> None:
        if type(self.reply_segments) is not tuple or any(
            type(segment) is not ExecutionReplySegmentSnapshot
            for segment in self.reply_segments
        ):
            raise TypeError(
                "execution transcript snapshot reply_segments must be an exact typed tuple"
            )
        if type(self.process_blocks) is not tuple or any(
            type(block) is not str for block in self.process_blocks
        ):
            raise TypeError(
                "execution transcript snapshot process_blocks must be an exact string tuple"
            )
        self._validate_index(
            "active_reply_index",
            self.active_reply_index,
            len(self.reply_segments),
        )
        self._validate_index(
            "active_process_index",
            self.active_process_index,
            len(self.process_blocks),
        )
        if self.active_reply_index is not None:
            segment = self.reply_segments[self.active_reply_index]
            if segment.kind != "assistant":
                raise ValueError(
                    "execution transcript active_reply_index must target an assistant segment"
                )
        if (
            self.active_reply_index is not None
            and self.active_process_index is not None
        ):
            raise ValueError(
                "execution transcript cannot have active reply and process cursors together"
            )
        for name, value in (
            ("pending_reply_divider", self.pending_reply_divider),
            ("had_assistant_output", self.had_assistant_output),
        ):
            if type(value) is not bool:
                raise TypeError(f"execution transcript snapshot {name} must be bool")
        for name, value in (
            ("last_completed_assistant_text", self.last_completed_assistant_text),
            ("terminal_error_text", self.terminal_error_text),
        ):
            if value is not None and type(value) is not str:
                raise TypeError(
                    f"execution transcript snapshot {name} must be str or None"
                )
        if self.terminal_agent_reply_coordinate is not None and type(
            self.terminal_agent_reply_coordinate
        ) is not TerminalAgentReplyCoordinate:
            raise TypeError(
                "execution transcript snapshot terminal coordinate must be typed or None"
            )

    @staticmethod
    def _validate_index(name: str, value: int | None, length: int) -> None:
        if value is None:
            return
        if type(value) is not int:
            raise TypeError(f"execution transcript snapshot {name} must be int or None")
        if value < 0 or value >= length:
            raise ValueError(f"execution transcript snapshot {name} is out of bounds")

    @classmethod
    def from_transcript(
        cls,
        transcript: ExecutionTranscript,
    ) -> ExecutionTranscriptSnapshot:
        """Capture a detached value graph without mutating the owner."""

        if type(transcript) is not ExecutionTranscript:
            raise TypeError("execution transcript snapshot requires a transcript owner")
        if type(transcript.reply_segments) is not list or any(
            type(segment) is not ExecutionReplySegment
            for segment in transcript.reply_segments
        ):
            raise TypeError(
                "execution transcript owner reply_segments must be an exact typed list"
            )
        if type(transcript.process_blocks) is not list or any(
            type(block) is not str for block in transcript.process_blocks
        ):
            raise TypeError(
                "execution transcript owner process_blocks must be an exact string list"
            )
        return cls(
            reply_segments=tuple(
                ExecutionReplySegmentSnapshot(segment.kind, segment.text)
                for segment in transcript.reply_segments
            ),
            process_blocks=tuple(transcript.process_blocks),
            active_reply_index=transcript._active_reply_index,
            active_process_index=transcript._active_process_index,
            pending_reply_divider=transcript._pending_reply_divider,
            had_assistant_output=transcript._had_assistant_output,
            last_completed_assistant_text=transcript._last_completed_assistant_text,
            terminal_error_text=transcript._terminal_error_text,
            terminal_agent_reply_coordinate=(
                transcript._terminal_agent_reply_coordinate
            ),
        )

    def to_transcript(self) -> ExecutionTranscript:
        """Create a fresh mutable owner with the exact captured state."""

        return ExecutionTranscript(
            reply_segments=[
                ExecutionReplySegment(segment.kind, segment.text)
                for segment in self.reply_segments
            ],
            process_blocks=list(self.process_blocks),
            _active_reply_index=self.active_reply_index,
            _active_process_index=self.active_process_index,
            _pending_reply_divider=self.pending_reply_divider,
            _had_assistant_output=self.had_assistant_output,
            _last_completed_assistant_text=self.last_completed_assistant_text,
            _terminal_error_text=self.terminal_error_text,
            _terminal_agent_reply_coordinate=self.terminal_agent_reply_coordinate,
        )

    def reply_text(self) -> str:
        return "\n\n".join(
            segment.text
            for segment in self.reply_segments
            if segment.kind == "assistant" and segment.text
        )

    def reply_content_chars(self) -> int:
        """Return the stable cursor width of assistant-owned text.

        Synthetic spacing and divider elements are presentation details and do
        not consume cursor coordinates.
        """

        return sum(
            len(segment.text)
            for segment in self.reply_segments
            if segment.kind == "assistant"
        )

    def process_text(self) -> str:
        return "".join(block for block in self.process_blocks if block)

    def has_reply_output(self) -> bool:
        return any(
            segment.kind == "assistant" and bool(segment.text)
            for segment in self.reply_segments
        )

    def has_process_output(self) -> bool:
        return any(bool(block) for block in self.process_blocks)

    def reply_segments_between(
        self,
        start_chars: int,
        end_chars: int,
    ) -> tuple[ExecutionReplySegment, ...]:
        return tuple(
            _reply_segments_between(
                (
                    ExecutionReplySegment(segment.kind, segment.text)
                    for segment in self.reply_segments
                ),
                start_chars,
                end_chars,
            )
        )


@dataclass
class ExecutionTranscript:
    reply_segments: list[ExecutionReplySegment] = field(default_factory=list)
    process_blocks: list[str] = field(default_factory=list)
    _active_reply_index: int | None = None
    _active_process_index: int | None = None
    _pending_reply_divider: bool = False
    _had_assistant_output: bool = False
    _last_completed_assistant_text: str | None = None
    _terminal_error_text: str | None = None
    _terminal_agent_reply_coordinate: TerminalAgentReplyCoordinate | None = None

    def clone(self) -> ExecutionTranscript:
        return ExecutionTranscript(
            reply_segments=list(self.reply_segments),
            process_blocks=list(self.process_blocks),
            _active_reply_index=self._active_reply_index,
            _active_process_index=self._active_process_index,
            _pending_reply_divider=self._pending_reply_divider,
            _had_assistant_output=self._had_assistant_output,
            _last_completed_assistant_text=self._last_completed_assistant_text,
            _terminal_error_text=self._terminal_error_text,
            _terminal_agent_reply_coordinate=self._terminal_agent_reply_coordinate,
        )

    def snapshot(self) -> ExecutionTranscriptSnapshot:
        """Capture a deeply immutable read-side value."""

        return ExecutionTranscriptSnapshot.from_transcript(self)

    def reset(self) -> None:
        self.reply_segments = []
        self.process_blocks = []
        self._active_reply_index = None
        self._active_process_index = None
        self._pending_reply_divider = False
        self._had_assistant_output = False
        self._last_completed_assistant_text = None
        self._terminal_error_text = None
        self._terminal_agent_reply_coordinate = None

    def reply_text(self) -> str:
        return "\n\n".join(
            segment.text
            for segment in self.reply_segments
            if segment.kind == "assistant" and segment.text
        )

    def reply_content_chars(self) -> int:
        return sum(
            len(segment.text)
            for segment in self.reply_segments
            if segment.kind == "assistant"
        )

    def process_text(self) -> str:
        return "".join(block for block in self.process_blocks if block)

    def has_reply_output(self) -> bool:
        return any(
            segment.kind == "assistant" and bool(segment.text)
            for segment in self.reply_segments
        )

    def has_process_output(self) -> bool:
        return any(bool(block) for block in self.process_blocks)

    def terminal_reply_evidence(
        self,
    ) -> tuple[Literal["agent", "error"], str] | None:
        """Return exact completed text evidence, preserving an empty agent final."""

        if (
            self._last_completed_assistant_text is not None
            and self._last_completed_assistant_text.strip()
        ):
            return "agent", self._last_completed_assistant_text
        if self._terminal_error_text is not None:
            return "error", self._terminal_error_text
        if self._last_completed_assistant_text is not None:
            return "agent", ""
        return None

    def terminal_agent_reply_coordinate(
        self,
    ) -> TerminalAgentReplyCoordinate | None:
        """Return the immutable local coordinate for a completed agent item."""

        return self._terminal_agent_reply_coordinate

    def trailing_reply_interval(self, expected_text: str) -> tuple[int, int] | None:
        """Locate one exact final assistant segment without searching its text."""

        if type(expected_text) is not str or not expected_text:
            return None
        last_assistant = next(
            (
                segment
                for segment in reversed(self.reply_segments)
                if segment.kind == "assistant"
            ),
            None,
        )
        if last_assistant is None or last_assistant.text != expected_text:
            return None
        end_reply_chars = self.reply_content_chars()
        return end_reply_chars - len(expected_text), end_reply_chars

    def terminal_agent_reply_interval(
        self,
        expected_text: str,
    ) -> tuple[tuple[int, int] | None, TerminalAgentReplyMatchReason]:
        """Match the recorded completion against the current trailing segment."""

        coordinate = self._terminal_agent_reply_coordinate
        if coordinate is None:
            return None, "coordinate_unavailable"
        if coordinate.raw_text != expected_text:
            return None, "raw_text_mismatch"
        interval = coordinate.start_reply_chars, coordinate.end_reply_chars
        if self.trailing_reply_interval(coordinate.raw_text) != interval:
            return None, "not_trailing"
        return interval, "matched"

    def record_terminal_error(self, text: str) -> None:
        normalized = str(text or "").strip()
        if normalized:
            self._terminal_error_text = normalized

    def record_unavailable_assistant_completion(self) -> None:
        """Invalidate an older agent candidate when completion text is unusable."""

        self._last_completed_assistant_text = None
        self._active_reply_index = None
        self._terminal_agent_reply_coordinate = None

    def set_reply_text(self, text: str) -> None:
        normalized = str(text or "")
        self.reply_segments = (
            [ExecutionReplySegment("assistant", normalized)] if normalized else []
        )
        self._active_reply_index = None
        self._pending_reply_divider = False
        self._had_assistant_output = bool(normalized)
        self._terminal_agent_reply_coordinate = None

    def rebuild_reply_from_snapshot_items(
        self,
        items: list[dict[str, Any]] | None,
        *,
        fallback_text: str = "",
        drop_last_text_message: bool = False,
    ) -> bool:
        # Snapshot items rebuild display only. Their completion status is
        # classified by the recovery owner before this method is called.
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
                raw_text = item.get("text")
                text = raw_text if type(raw_text) is str else ""
                if not text.strip():
                    continue
                if drop_last_text_message and idx == last_text_index:
                    continue
                if saw_assistant and saw_work_since_assistant:
                    rebuilt.append(ExecutionReplySegment("divider"))
                rebuilt.append(ExecutionReplySegment("assistant", text))
                saw_assistant = True
                saw_work_since_assistant = False
                continue
            if saw_assistant and is_execution_work_item_type(item_type):
                saw_work_since_assistant = True
        if rebuilt:
            self.reply_segments = rebuilt
            self._active_reply_index = None
            self._pending_reply_divider = bool(
                saw_assistant and saw_work_since_assistant
            )
            self._had_assistant_output = any(
                segment.kind == "assistant" and bool(segment.text)
                for segment in rebuilt
            )
            self._terminal_agent_reply_coordinate = None
            return True
        if fallback_text:
            self.set_reply_text(fallback_text)
            return True
        return False

    def append_assistant_delta(self, delta: str) -> None:
        if not delta:
            return
        self._terminal_agent_reply_coordinate = None
        self._active_process_index = None
        if self._active_reply_index is None:
            self._last_completed_assistant_text = None
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

    def reconcile_current_assistant_text(
        self,
        text: str,
        *,
        terminal_candidate: bool = True,
        item_id: str = "",
    ) -> bool:
        if type(terminal_candidate) is not bool:
            raise TypeError("assistant terminal_candidate must be an exact bool")
        if type(item_id) is not str:
            raise TypeError("assistant item_id must be an exact string")
        normalized = str(text or "")
        normalized_item_id = item_id.strip()
        prior_coordinate = self._terminal_agent_reply_coordinate
        if (
            terminal_candidate
            and normalized_item_id
            and prior_coordinate is not None
            and prior_coordinate.item_id == normalized_item_id
            and prior_coordinate.raw_text == normalized
        ):
            return True
        if not terminal_candidate:
            self._last_completed_assistant_text = None
            self._terminal_agent_reply_coordinate = None
        if not normalized.strip():
            if terminal_candidate:
                self._last_completed_assistant_text = ""
            self._terminal_agent_reply_coordinate = None
            self._active_reply_index = None
            return True
        target_index = self._active_reply_index
        if target_index is None:
            if self._pending_reply_divider and self._had_assistant_output:
                self.reply_segments.append(ExecutionReplySegment("divider"))
            self._pending_reply_divider = False
            self.reply_segments.append(ExecutionReplySegment("assistant", normalized))
        else:
            self.reply_segments[target_index] = ExecutionReplySegment(
                "assistant", normalized
            )
        self._active_reply_index = None
        self._had_assistant_output = True
        if terminal_candidate:
            self._last_completed_assistant_text = normalized
            self._terminal_error_text = None
            end_reply_chars = self.reply_content_chars()
            self._terminal_agent_reply_coordinate = TerminalAgentReplyCoordinate(
                item_id=normalized_item_id,
                raw_text=normalized,
                start_reply_chars=end_reply_chars - len(normalized),
                end_reply_chars=end_reply_chars,
            )
        return True

    def append_display_only_reply(self, text: str) -> None:
        """Append local presentation text without creating terminal evidence."""

        normalized = str(text or "").strip()
        if not normalized:
            return
        if self.has_reply_output():
            self.reply_segments.append(ExecutionReplySegment("divider"))
        self.reply_segments.append(ExecutionReplySegment("assistant", normalized))
        self._active_reply_index = None
        self._pending_reply_divider = False
        self._had_assistant_output = True
        self._terminal_agent_reply_coordinate = None

    def start_process_block(self, text: str, *, marks_work: bool) -> None:
        self._active_reply_index = None
        self._active_process_index = None
        if marks_work:
            self._last_completed_assistant_text = None
            self._terminal_agent_reply_coordinate = None
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

    def mark_process_work(self) -> None:
        """Invalidate a prior terminal candidate without projecting tool output.

        Unlike a new process note, a streaming tool delta belongs to the
        currently active process item.  Keep that cursor intact so the later
        completion summary closes the same block.
        """

        self._active_reply_index = None
        self._last_completed_assistant_text = None
        self._terminal_agent_reply_coordinate = None
        if self._had_assistant_output:
            self._pending_reply_divider = True

    def finish_process_block(
        self,
        suffix: str = "",
        *,
        marks_work: bool = False,
    ) -> None:
        if type(marks_work) is not bool:
            raise TypeError("process completion marks_work must be an exact bool")
        if marks_work:
            self._active_reply_index = None
            self._last_completed_assistant_text = None
            self._terminal_agent_reply_coordinate = None
            if self._had_assistant_output:
                self._pending_reply_divider = True
        if suffix:
            self.append_process_delta(suffix)
        self._active_process_index = None

    def append_process_note(self, text: str, *, marks_work: bool = False) -> None:
        if not text:
            if marks_work:
                self._active_reply_index = None
                self._active_process_index = None
                self._last_completed_assistant_text = None
                self._terminal_agent_reply_coordinate = None
                if self._had_assistant_output:
                    self._pending_reply_divider = True
            return
        self._active_reply_index = None
        self._active_process_index = None
        if marks_work:
            self._last_completed_assistant_text = None
            self._terminal_agent_reply_coordinate = None
        self.process_blocks.append(str(text))
        if marks_work and self._had_assistant_output:
            self._pending_reply_divider = True

    def reply_segments_between(
        self,
        start_chars: int,
        end_chars: int,
    ) -> list[ExecutionReplySegment]:
        return _reply_segments_between(
            iter(self.reply_segments),
            start_chars,
            end_chars,
        )


@dataclass(frozen=True, slots=True)
class ExecutionTranscriptTrailingReplyProjection:
    """Hide one proven trailing reply interval without shifting page cursors."""

    transcript: ExecutionTranscript
    retained_reply_chars: int

    def __post_init__(self) -> None:
        if type(self.transcript) is not ExecutionTranscript:
            raise TypeError("trailing reply projection requires an exact transcript")
        if type(self.retained_reply_chars) is not int:
            raise TypeError("retained reply cursor must be an exact int")
        if not 0 <= self.retained_reply_chars <= self.transcript.reply_content_chars():
            raise ValueError("retained reply cursor is outside the transcript")

    def process_text(self) -> str:
        return self.transcript.process_text()

    def reply_content_chars(self) -> int:
        """Preserve original coordinates even though the trailing text is hidden."""

        return self.transcript.reply_content_chars()

    def reply_text(self) -> str:
        return "\n\n".join(
            segment.text
            for segment in self.reply_segments_between(
                0,
                self.transcript.reply_content_chars(),
            )
            if segment.kind == "assistant" and segment.text
        )

    def reply_segments_between(
        self,
        start_chars: int,
        end_chars: int,
    ) -> list[ExecutionReplySegment]:
        if type(start_chars) is not int or type(end_chars) is not int:
            raise TypeError("execution reply range coordinates must be exact ints")
        total_chars = self.transcript.reply_content_chars()
        if start_chars < 0 or end_chars < start_chars:
            raise ValueError("execution reply range must be ordered and non-negative")
        if end_chars > total_chars:
            raise ValueError("execution reply range exceeds transcript content")
        visible_end = min(end_chars, self.retained_reply_chars)
        if visible_end <= start_chars:
            return []
        return self.transcript.reply_segments_between(start_chars, visible_end)


def _reply_segments_between(
    segments: Iterable[ExecutionReplySegment],
    start_chars: int,
    end_chars: int,
) -> list[ExecutionReplySegment]:
    """Project one half-open assistant-text range without copying transcript state."""

    if type(start_chars) is not int or type(end_chars) is not int:
        raise TypeError("execution reply range coordinates must be exact ints")
    if start_chars < 0 or end_chars < start_chars:
        raise ValueError("execution reply range must be ordered and non-negative")

    rendered: list[ExecutionReplySegment] = []
    offset = 0
    pending_divider = False
    for segment in segments:
        if segment.kind == "divider":
            if rendered:
                pending_divider = True
            continue
        segment_end = offset + len(segment.text)
        overlap_start = max(start_chars, offset)
        overlap_end = min(end_chars, segment_end)
        if overlap_start < overlap_end:
            if pending_divider and rendered:
                rendered.append(ExecutionReplySegment("divider"))
            pending_divider = False
            rendered.append(
                ExecutionReplySegment(
                    "assistant",
                    segment.text[
                        overlap_start - offset : overlap_end - offset
                    ],
                )
            )
        offset = segment_end
        if offset >= end_chars:
            break
    if end_chars > offset:
        raise ValueError("execution reply range exceeds transcript content")
    return rendered
