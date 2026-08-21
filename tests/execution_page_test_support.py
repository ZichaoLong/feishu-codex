from __future__ import annotations

from typing import Any

from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionPageStatus,
    ExecutionPresentationPage,
    ExecutionTranscriptCursor,
)


def execution_page_ledger(
    *,
    current_message_id: str = "",
    last_message_id: str = "",
) -> ExecutionPageLedger:
    pages: list[ExecutionPresentationPage] = []
    zero = ExecutionTranscriptCursor()
    if last_message_id:
        pages.append(
            ExecutionPresentationPage(
                generation=1,
                outbound_attempt_id="test-attempt-sealed",
                message_id=last_message_id,
                cursor_start=zero,
                cursor_end=zero,
                status=ExecutionPageStatus.SEALED,
            )
        )
    if current_message_id:
        pages.append(
            ExecutionPresentationPage(
                generation=len(pages) + 1,
                outbound_attempt_id="test-attempt-active",
                message_id=current_message_id,
                cursor_start=zero,
                cursor_end=None,
                status=ExecutionPageStatus.ACTIVE,
            )
        )
    return ExecutionPageLedger(tuple(pages))


def set_execution_page_state(
    state: dict[str, Any],
    *,
    current_message_id: str = "",
    last_message_id: str = "",
) -> None:
    state["execution_pages"] = execution_page_ledger(
        current_message_id=current_message_id,
        last_message_id=last_message_id,
    )
