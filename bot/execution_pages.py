"""Typed process-local ledger for Feishu execution presentation pages."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal, Protocol


class _TranscriptProjection(Protocol):
    def process_text(self) -> str: ...

    def reply_content_chars(self) -> int: ...


def _require_text(value: object, *, field: str, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field} cannot be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class ExecutionTranscriptCursor:
    """A position in the canonical transcript without copying its content."""

    process_chars: int = 0
    reply_chars: int = 0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("process_chars", self.process_chars),
            ("reply_chars", self.reply_chars),
        ):
            if type(value) is not int:
                raise TypeError(
                    f"execution transcript cursor {field_name} must be int"
                )
            if value < 0:
                raise ValueError(
                    "execution transcript cursor "
                    f"{field_name} must be non-negative"
                )

    @classmethod
    def from_transcript(
        cls,
        transcript: _TranscriptProjection,
    ) -> ExecutionTranscriptCursor:
        return cls(
            process_chars=len(transcript.process_text()),
            reply_chars=transcript.reply_content_chars(),
        )

    def follows(self, other: ExecutionTranscriptCursor) -> bool:
        if type(other) is not ExecutionTranscriptCursor:
            return False
        return bool(
            self.process_chars >= other.process_chars
            and self.reply_chars >= other.reply_chars
        )

    def strictly_follows(self, other: ExecutionTranscriptCursor) -> bool:
        return bool(self.follows(other) and self != other)

    def contains(self, other: ExecutionTranscriptCursor) -> bool:
        """Return whether ``other`` is no later than this endpoint."""

        return bool(type(other) is ExecutionTranscriptCursor and self.follows(other))


@dataclass(frozen=True, slots=True)
class TerminalExecutionPageReceipt:
    """Confirmed message identity plus its immutable transcript range."""

    message_id: str
    cursor_start: ExecutionTranscriptCursor
    cursor_end: ExecutionTranscriptCursor

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "message_id",
            _require_text(
                self.message_id,
                field="terminal execution page receipt message_id",
            ),
        )
        if (
            type(self.cursor_start) is not ExecutionTranscriptCursor
            or type(self.cursor_end) is not ExecutionTranscriptCursor
            or not self.cursor_end.follows(self.cursor_start)
        ):
            raise TypeError(
                "terminal execution page receipt requires an ordered cursor range"
            )


@dataclass(frozen=True, slots=True)
class TerminalReplyIntervalCoverage:
    """Confirmed pages intersecting one half-open reply interval."""

    receipts: tuple[TerminalExecutionPageReceipt, ...]
    fully_covered: bool

    def __post_init__(self) -> None:
        if type(self.receipts) is not tuple or any(
            type(receipt) is not TerminalExecutionPageReceipt
            for receipt in self.receipts
        ):
            raise TypeError("terminal reply interval receipts must be typed")
        if len({receipt.message_id for receipt in self.receipts}) != len(
            self.receipts
        ):
            raise ValueError("terminal reply interval receipt ids must be unique")
        if type(self.fully_covered) is not bool:
            raise TypeError("terminal reply interval coverage must be bool")


TerminalPageCleanupStatus = Literal["scheduled", "partial", "retained"]
TerminalPageCleanupReason = Literal[
    "carrier_unavailable",
    "non_agent_terminal",
    "local_coordinate_unavailable",
    "item_identity_mismatch",
    "raw_text_mismatch",
    "snapshot_projection_mismatch",
    "terminal_not_trailing",
    "confirmed_receipts_missing",
    "interval_not_fully_covered",
    "patch_dispatch_partial",
    "cleanup_scheduled",
]


@dataclass(frozen=True, slots=True)
class TerminalPageCleanupOutcome:
    """Dispatch outcome for exact terminal-reply execution-page cleanup."""

    status: TerminalPageCleanupStatus
    reason: TerminalPageCleanupReason
    attempted_receipts: int = 0
    scheduled_patches: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"scheduled", "partial", "retained"}:
            raise ValueError("unknown terminal page cleanup status")
        if type(self.attempted_receipts) is not int or type(
            self.scheduled_patches
        ) is not int:
            raise TypeError("terminal page cleanup counts must be exact ints")
        if (
            self.attempted_receipts < 0
            or self.scheduled_patches < 0
            or self.scheduled_patches > self.attempted_receipts
        ):
            raise ValueError("terminal page cleanup counts are inconsistent")

    @classmethod
    def retained(
        cls,
        reason: TerminalPageCleanupReason,
    ) -> TerminalPageCleanupOutcome:
        return cls(status="retained", reason=reason)


def require_terminal_execution_page_receipts(
    value: object,
    *,
    field: str,
) -> tuple[TerminalExecutionPageReceipt, ...]:
    """Require one contiguous, unique sequence of confirmed page receipts."""

    if type(value) is not tuple or any(
        type(receipt) is not TerminalExecutionPageReceipt for receipt in value
    ):
        raise TypeError(f"{field} must be an exact terminal page receipt tuple")
    message_ids: set[str] = set()
    previous_end: ExecutionTranscriptCursor | None = None
    for receipt in value:
        if receipt.message_id in message_ids:
            raise ValueError(f"{field} message ids must be unique")
        message_ids.add(receipt.message_id)
        if previous_end is not None and receipt.cursor_start != previous_end:
            raise ValueError(f"{field} cursor ranges must be contiguous")
        previous_end = receipt.cursor_end
    return value


def terminal_reply_interval_coverage(
    receipts: tuple[TerminalExecutionPageReceipt, ...],
    *,
    start_reply_chars: int,
    end_reply_chars: int,
) -> TerminalReplyIntervalCoverage:
    """Project confirmed pages and prove whether they cover the whole interval."""

    validated = require_terminal_execution_page_receipts(
        receipts,
        field="terminal reply interval source receipts",
    )
    if type(start_reply_chars) is not int or type(end_reply_chars) is not int:
        raise TypeError("terminal reply interval coordinates must be exact ints")
    if start_reply_chars < 0 or end_reply_chars <= start_reply_chars:
        raise ValueError("terminal reply interval must be positive and ordered")
    intersecting = tuple(
        receipt
        for receipt in validated
        if receipt.cursor_end.reply_chars > start_reply_chars
        and receipt.cursor_start.reply_chars < end_reply_chars
    )
    covered_until = start_reply_chars
    for receipt in intersecting:
        if receipt.cursor_start.reply_chars > covered_until:
            break
        covered_until = max(covered_until, receipt.cursor_end.reply_chars)
        if covered_until >= end_reply_chars:
            break
    return TerminalReplyIntervalCoverage(
        receipts=intersecting,
        fully_covered=covered_until >= end_reply_chars,
    )


class ExecutionPageStatus(StrEnum):
    OPENING = "opening"
    ACTIVE = "active"
    SEALED = "sealed"
    SEND_UNKNOWN = "send_unknown"


class ExecutionPageSendOutcome(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, eq=False)
class ExecutionPresentationPage:
    """One immutable page record owned by a single execution ledger."""

    generation: int
    outbound_attempt_id: str
    message_id: str
    cursor_start: ExecutionTranscriptCursor
    cursor_end: ExecutionTranscriptCursor | None
    status: ExecutionPageStatus
    reconciliation_attempted: bool = False
    _identity_token: object = field(default_factory=object, repr=False)

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("execution page generation must be a positive int")
        object.__setattr__(
            self,
            "outbound_attempt_id",
            _require_text(
                self.outbound_attempt_id,
                field="execution page outbound_attempt_id",
            ),
        )
        object.__setattr__(
            self,
            "message_id",
            _require_text(
                self.message_id,
                field="execution page message_id",
                allow_empty=True,
            ),
        )
        if type(self.cursor_start) is not ExecutionTranscriptCursor:
            raise TypeError("execution page cursor_start must be typed")
        if self.cursor_end is not None and type(
            self.cursor_end
        ) is not ExecutionTranscriptCursor:
            raise TypeError("execution page cursor_end must be typed or None")
        if type(self.status) is not ExecutionPageStatus:
            raise TypeError("execution page status must be typed")
        if type(self.reconciliation_attempted) is not bool:
            raise TypeError("execution page reconciliation_attempted must be bool")
        if (
            self.reconciliation_attempted
            and self.status is not ExecutionPageStatus.SEND_UNKNOWN
        ):
            raise ValueError(
                "only a send_unknown page can record a reconciliation attempt"
            )
        if type(self._identity_token) is not object:
            raise TypeError("execution page identity token must be exact")
        if self.status is ExecutionPageStatus.SEALED:
            if not self.message_id:
                raise ValueError("sealed execution page requires message_id")
            if self.cursor_end is None or not self.cursor_end.follows(
                self.cursor_start
            ):
                raise ValueError("sealed execution page requires a valid cursor range")
            return
        if self.cursor_end is not None:
            raise ValueError("an unsealed execution page cannot have cursor_end")
        if self.status is ExecutionPageStatus.ACTIVE and not self.message_id:
            raise ValueError("active execution page requires message_id")


@dataclass(frozen=True, slots=True, eq=False)
class ExecutionPageLedger:
    """Canonical ordered page state for one process-local execution."""

    pages: tuple[ExecutionPresentationPage, ...] = ()

    def __post_init__(self) -> None:
        if type(self.pages) is not tuple or any(
            type(page) is not ExecutionPresentationPage for page in self.pages
        ):
            raise TypeError("execution page ledger requires an exact page tuple")
        expected_cursor = ExecutionTranscriptCursor()
        attempt_ids: set[str] = set()
        message_ids: set[str] = set()
        live_pages: list[ExecutionPresentationPage] = []
        for index, page in enumerate(self.pages, start=1):
            if page.generation != index:
                raise ValueError("execution page generations must be contiguous")
            if page.outbound_attempt_id in attempt_ids:
                raise ValueError("execution page outbound attempts must be unique")
            attempt_ids.add(page.outbound_attempt_id)
            if page.message_id:
                if page.message_id in message_ids:
                    raise ValueError("execution page message ids must be unique")
                message_ids.add(page.message_id)
            if page.status is ExecutionPageStatus.SEALED:
                if live_pages:
                    raise ValueError("a sealed execution page cannot follow live pages")
                if page.cursor_start != expected_cursor:
                    raise ValueError("execution page cursor ranges must be contiguous")
                assert page.cursor_end is not None
                expected_cursor = page.cursor_end
                continue
            live_pages.append(page)

        live_statuses = tuple(page.status for page in live_pages)
        allowed_live_statuses = {
            (ExecutionPageStatus.OPENING,),
            (ExecutionPageStatus.ACTIVE,),
            (ExecutionPageStatus.SEND_UNKNOWN,),
            (ExecutionPageStatus.ACTIVE, ExecutionPageStatus.OPENING),
            (ExecutionPageStatus.ACTIVE, ExecutionPageStatus.SEND_UNKNOWN),
        }
        if live_statuses and live_statuses not in allowed_live_statuses:
            raise ValueError("execution page live suffix is invalid")
        if live_pages:
            first = live_pages[0]
            if first.cursor_start != expected_cursor:
                raise ValueError("execution page cursor ranges must be contiguous")
        if len(live_pages) == 2:
            active, pending = live_pages
            if not pending.cursor_start.strictly_follows(active.cursor_start):
                raise ValueError(
                    "execution rollover cursor must advance beyond the active page"
                )

    @classmethod
    def empty(cls) -> ExecutionPageLedger:
        return cls()

    @property
    def current_page(self) -> ExecutionPresentationPage | None:
        if not self.pages:
            return None
        page = self.pages[-1]
        if page.status is ExecutionPageStatus.SEALED:
            return None
        return page

    @property
    def active_page(self) -> ExecutionPresentationPage | None:
        for page in reversed(self.pages):
            if page.status is ExecutionPageStatus.ACTIVE:
                return page
            if page.status is ExecutionPageStatus.SEALED:
                return None
        return None

    @property
    def pending_page(self) -> ExecutionPresentationPage | None:
        page = self.current_page
        if page is None or page.status not in {
            ExecutionPageStatus.OPENING,
            ExecutionPageStatus.SEND_UNKNOWN,
        }:
            return None
        return page

    @property
    def current_message_id(self) -> str:
        page = self.active_page
        return page.message_id if page is not None else ""

    @property
    def last_message_id(self) -> str:
        for page in reversed(self.pages):
            if page.status is ExecutionPageStatus.SEALED:
                return page.message_id
        return ""

    @property
    def effective_message_id(self) -> str:
        return self.current_message_id or self.last_message_id

    @property
    def has_execution_anchor(self) -> bool:
        return self.current_page is not None

    @property
    def has_unresolved_send(self) -> bool:
        return self.pending_page is not None

    @property
    def send_outcome_unknown(self) -> bool:
        page = self.pending_page
        return bool(
            page is not None and page.status is ExecutionPageStatus.SEND_UNKNOWN
        )

    def active_projection_end(
        self,
        transcript: _TranscriptProjection,
    ) -> ExecutionTranscriptCursor | None:
        active = self.active_page
        if active is None:
            return None
        pending = self.pending_page
        endpoint = (
            pending.cursor_start
            if pending is not None
            else ExecutionTranscriptCursor.from_transcript(transcript)
        )
        if not endpoint.follows(active.cursor_start):
            raise RuntimeError("execution transcript regressed behind its active page")
        return endpoint

    def page_for_message_id(
        self,
        message_id: str,
    ) -> ExecutionPresentationPage | None:
        normalized = _require_text(
            message_id,
            field="execution page lookup message_id",
        )
        for page in reversed(self.pages):
            if page.message_id == normalized:
                return page
        return None

    def prepare_initial(
        self,
        *,
        outbound_attempt_id: str,
        known_message_id: str = "",
    ) -> ExecutionPageLedger:
        if self.pages:
            raise RuntimeError("initial execution page requires an empty ledger")
        page = ExecutionPresentationPage(
            generation=1,
            outbound_attempt_id=outbound_attempt_id,
            message_id=known_message_id,
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=None,
            status=ExecutionPageStatus.OPENING,
        )
        return ExecutionPageLedger((page,))

    def activate_opening(
        self,
        *,
        expected_page: ExecutionPresentationPage,
        message_id: str,
    ) -> ExecutionPageLedger:
        page = self._require_initial_opening(expected_page)
        active = replace(
            page,
            message_id=_require_text(message_id, field="active page message_id"),
            status=ExecutionPageStatus.ACTIVE,
        )
        return ExecutionPageLedger(self.pages[:-1] + (active,))

    def mark_send_unknown(
        self,
        *,
        expected_page: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        page = self._require_initial_opening(expected_page)
        unknown = replace(page, status=ExecutionPageStatus.SEND_UNKNOWN)
        return ExecutionPageLedger(self.pages[:-1] + (unknown,))

    def discard_opening(
        self,
        *,
        expected_page: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        self._require_initial_opening(expected_page)
        return ExecutionPageLedger(self.pages[:-1])

    def prepare_rollover(
        self,
        *,
        outbound_attempt_id: str,
        cursor_start: ExecutionTranscriptCursor,
    ) -> ExecutionPageLedger:
        active = self.active_page
        if active is None or self.pending_page is not None:
            raise RuntimeError("execution rollover requires one unblocked active page")
        if type(cursor_start) is not ExecutionTranscriptCursor:
            raise TypeError("execution rollover cursor must be typed")
        if not cursor_start.strictly_follows(active.cursor_start):
            raise ValueError("execution rollover cursor must advance")
        opening = ExecutionPresentationPage(
            generation=len(self.pages) + 1,
            outbound_attempt_id=outbound_attempt_id,
            message_id="",
            cursor_start=cursor_start,
            cursor_end=None,
            status=ExecutionPageStatus.OPENING,
        )
        return ExecutionPageLedger(self.pages + (opening,))

    def activate_rollover(
        self,
        *,
        expected_active: ExecutionPresentationPage,
        expected_opening: ExecutionPresentationPage,
        message_id: str,
    ) -> ExecutionPageLedger:
        active, opening = self._require_rollover_opening(
            expected_active,
            expected_opening,
        )
        sealed = replace(
            active,
            cursor_end=opening.cursor_start,
            status=ExecutionPageStatus.SEALED,
        )
        next_active = replace(
            opening,
            message_id=_require_text(
                message_id,
                field="rollover active page message_id",
            ),
            status=ExecutionPageStatus.ACTIVE,
        )
        return ExecutionPageLedger(self.pages[:-2] + (sealed, next_active))

    def mark_rollover_send_unknown(
        self,
        *,
        expected_active: ExecutionPresentationPage,
        expected_opening: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        _active, opening = self._require_rollover_opening(
            expected_active,
            expected_opening,
        )
        unknown = replace(opening, status=ExecutionPageStatus.SEND_UNKNOWN)
        return ExecutionPageLedger(self.pages[:-1] + (unknown,))

    def discard_rollover_opening(
        self,
        *,
        expected_active: ExecutionPresentationPage,
        expected_opening: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        self._require_rollover_opening(expected_active, expected_opening)
        return ExecutionPageLedger(self.pages[:-1])

    def confirm_send_unknown(
        self,
        *,
        expected_page: ExecutionPresentationPage,
        message_id: str,
    ) -> ExecutionPageLedger:
        """Resolve one exact unknown effect without changing its page identity."""

        active, pending = self._require_send_unknown(expected_page)
        resolved_message_id = _require_text(
            message_id,
            field="reconciled execution page message_id",
        )
        if pending.message_id and pending.message_id != resolved_message_id:
            raise ValueError(
                "reconciled execution page cannot replace its known message id"
            )
        resolved = replace(
            pending,
            message_id=resolved_message_id,
            status=ExecutionPageStatus.ACTIVE,
            reconciliation_attempted=False,
        )
        if active is None:
            return ExecutionPageLedger(self.pages[:-1] + (resolved,))
        sealed = replace(
            active,
            cursor_end=pending.cursor_start,
            status=ExecutionPageStatus.SEALED,
        )
        return ExecutionPageLedger(self.pages[:-2] + (sealed, resolved))

    def reject_send_unknown(
        self,
        *,
        expected_page: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        """Discard one exact effect now proven to have had no effect."""

        self._require_send_unknown(expected_page)
        return ExecutionPageLedger(self.pages[:-1])

    def retain_send_unknown(
        self,
        *,
        expected_page: ExecutionPresentationPage,
    ) -> ExecutionPageLedger:
        """Consume the sole reconciliation attempt while preserving uncertainty."""

        _active, pending = self._require_send_unknown(expected_page)
        if pending.reconciliation_attempted:
            raise RuntimeError("execution page reconciliation was already attempted")
        retained = replace(pending, reconciliation_attempted=True)
        return ExecutionPageLedger(self.pages[:-1] + (retained,))

    def seal_active(
        self,
        *,
        cursor_end: ExecutionTranscriptCursor,
    ) -> ExecutionPageLedger:
        page = self.active_page
        if page is None:
            if self.current_page is not None:
                raise RuntimeError("unresolved execution page cannot be sealed")
            return self
        if self.pending_page is not None:
            raise RuntimeError("pending execution rollover cannot be sealed")
        sealed = replace(
            page,
            cursor_end=cursor_end,
            status=ExecutionPageStatus.SEALED,
        )
        return ExecutionPageLedger(self.pages[:-1] + (sealed,))

    def retire_for_turn_completion(
        self,
        *,
        cursor_end: ExecutionTranscriptCursor,
    ) -> ExecutionPageLedger:
        """Close known pages without making an uncertain send a turn gate.

        An opening or ``send_unknown`` page represents only that exact outbound
        presentation effect. Turn completion removes it from the resident
        runtime rather than keeping the execution active. An immutable
        pre-retirement snapshot may still reconcile one ``send_unknown`` UUID
        once, without writing back to this ledger or delaying turn release. A
        known active page is sealed over the complete terminal transcript so it
        can be projected later from that snapshot.
        """

        if type(cursor_end) is not ExecutionTranscriptCursor:
            raise TypeError("execution retirement cursor must be typed")
        pages = list(self.pages)
        if pages and pages[-1].status in {
            ExecutionPageStatus.OPENING,
            ExecutionPageStatus.SEND_UNKNOWN,
        }:
            pages.pop()
        if pages and pages[-1].status is ExecutionPageStatus.ACTIVE:
            active = pages[-1]
            if not cursor_end.follows(active.cursor_start):
                raise ValueError(
                    "execution retirement cursor cannot precede the active page"
                )
            pages[-1] = replace(
                active,
                cursor_end=cursor_end,
                status=ExecutionPageStatus.SEALED,
            )
        return ExecutionPageLedger(tuple(pages))

    def clear_known_pages(self) -> ExecutionPageLedger:
        page = self.current_page
        if page is not None and page.status in {
            ExecutionPageStatus.OPENING,
            ExecutionPageStatus.SEND_UNKNOWN,
        }:
            raise RuntimeError("unresolved execution page requires explicit settlement")
        return ExecutionPageLedger.empty()

    def _require_initial_opening(
        self,
        expected_page: ExecutionPresentationPage,
    ) -> ExecutionPresentationPage:
        if type(expected_page) is not ExecutionPresentationPage:
            raise TypeError("execution page transition requires a typed page")
        page = self.current_page
        if (
            page is not expected_page
            or page.status is not ExecutionPageStatus.OPENING
            or self.active_page is not None
        ):
            raise RuntimeError("execution page opening capability is stale")
        return page

    def _require_rollover_opening(
        self,
        expected_active: ExecutionPresentationPage,
        expected_opening: ExecutionPresentationPage,
    ) -> tuple[ExecutionPresentationPage, ExecutionPresentationPage]:
        if (
            type(expected_active) is not ExecutionPresentationPage
            or type(expected_opening) is not ExecutionPresentationPage
        ):
            raise TypeError("execution rollover transition requires typed pages")
        if len(self.pages) < 2:
            raise RuntimeError("execution rollover capability is stale")
        active, opening = self.pages[-2:]
        if (
            active is not expected_active
            or active.status is not ExecutionPageStatus.ACTIVE
            or opening is not expected_opening
            or opening.status is not ExecutionPageStatus.OPENING
        ):
            raise RuntimeError("execution rollover capability is stale")
        return active, opening

    def _require_send_unknown(
        self,
        expected_page: ExecutionPresentationPage,
    ) -> tuple[
        ExecutionPresentationPage | None,
        ExecutionPresentationPage,
    ]:
        if type(expected_page) is not ExecutionPresentationPage:
            raise TypeError("execution page reconciliation requires a typed page")
        pending = self.pending_page
        if (
            pending is not expected_page
            or pending.status is not ExecutionPageStatus.SEND_UNKNOWN
        ):
            raise RuntimeError("execution page reconciliation capability is stale")
        return self.active_page, pending
