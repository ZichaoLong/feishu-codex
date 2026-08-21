from __future__ import annotations

import unittest
from dataclasses import replace

from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionPageStatus,
    ExecutionPresentationPage,
    ExecutionTranscriptCursor,
)


def _page(
    generation: int,
    *,
    attempt_id: str,
    message_id: str,
    status: ExecutionPageStatus,
    cursor_start: ExecutionTranscriptCursor | None = None,
    cursor_end: ExecutionTranscriptCursor | None = None,
) -> ExecutionPresentationPage:
    return ExecutionPresentationPage(
        generation=generation,
        outbound_attempt_id=attempt_id,
        message_id=message_id,
        cursor_start=cursor_start or ExecutionTranscriptCursor(),
        cursor_end=cursor_end,
        status=status,
    )


class ExecutionPageLedgerTests(unittest.TestCase):
    def test_cursor_requires_exact_non_negative_coordinates(self) -> None:
        for kwargs in (
            {"process_chars": -1},
            {"reply_chars": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                ValueError,
                "non-negative",
            ):
                ExecutionTranscriptCursor(**kwargs)
        with self.assertRaisesRegex(TypeError, "must be int"):
            ExecutionTranscriptCursor(process_chars=True)

    def test_page_status_invariants_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "active.*message_id"):
            _page(
                1,
                attempt_id="attempt-1",
                message_id="",
                status=ExecutionPageStatus.ACTIVE,
            )
        with self.assertRaisesRegex(ValueError, "sealed.*message_id"):
            _page(
                1,
                attempt_id="attempt-1",
                message_id="",
                status=ExecutionPageStatus.SEALED,
                cursor_end=ExecutionTranscriptCursor(),
            )
        with self.assertRaisesRegex(ValueError, "valid cursor range"):
            _page(
                1,
                attempt_id="attempt-1",
                message_id="card-1",
                status=ExecutionPageStatus.SEALED,
            )
        with self.assertRaisesRegex(ValueError, "unsealed.*cursor_end"):
            _page(
                1,
                attempt_id="attempt-1",
                message_id="",
                status=ExecutionPageStatus.OPENING,
                cursor_end=ExecutionTranscriptCursor(),
            )

    def test_ledger_rejects_non_contiguous_or_duplicate_page_facts(self) -> None:
        cursor_1 = ExecutionTranscriptCursor(process_chars=3)
        sealed = _page(
            1,
            attempt_id="attempt-1",
            message_id="card-1",
            status=ExecutionPageStatus.SEALED,
            cursor_end=cursor_1,
        )
        cases = (
            (
                "generations",
                (
                    _page(
                        2,
                        attempt_id="attempt-2",
                        message_id="card-2",
                        status=ExecutionPageStatus.ACTIVE,
                    ),
                ),
            ),
            (
                "live suffix",
                (
                    _page(
                        1,
                        attempt_id="attempt-1",
                        message_id="card-1",
                        status=ExecutionPageStatus.ACTIVE,
                    ),
                    _page(
                        2,
                        attempt_id="attempt-2",
                        message_id="card-2",
                        status=ExecutionPageStatus.ACTIVE,
                    ),
                ),
            ),
            (
                "cursor ranges",
                (
                    sealed,
                    _page(
                        2,
                        attempt_id="attempt-2",
                        message_id="card-2",
                        status=ExecutionPageStatus.ACTIVE,
                    ),
                ),
            ),
            (
                "outbound attempts",
                (
                    sealed,
                    _page(
                        2,
                        attempt_id="attempt-1",
                        message_id="card-2",
                        status=ExecutionPageStatus.ACTIVE,
                        cursor_start=cursor_1,
                    ),
                ),
            ),
            (
                "message ids",
                (
                    sealed,
                    _page(
                        2,
                        attempt_id="attempt-2",
                        message_id="card-1",
                        status=ExecutionPageStatus.ACTIVE,
                        cursor_start=cursor_1,
                    ),
                ),
            ),
        )
        for message, pages in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError,
                message,
            ):
                ExecutionPageLedger(pages)

    def test_opening_transition_requires_the_exact_page_capability(self) -> None:
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="attempt-1",
            known_message_id="reserved-card",
        )
        page = opening.current_page
        assert page is not None

        with self.assertRaisesRegex(RuntimeError, "capability is stale"):
            opening.activate_opening(
                expected_page=replace(page),
                message_id="reserved-card",
            )

        active = opening.activate_opening(
            expected_page=page,
            message_id="reserved-card",
        )
        self.assertEqual(active.current_message_id, "reserved-card")
        self.assertIsNot(active, opening)

    def test_rollover_pending_page_is_a_unique_ledger_capability(self) -> None:
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="attempt-1",
        )
        initial_page = opening.current_page
        assert initial_page is not None
        active = opening.activate_opening(
            expected_page=initial_page,
            message_id="card-1",
        )
        pending = active.prepare_rollover(
            outbound_attempt_id="attempt-2",
            cursor_start=ExecutionTranscriptCursor(process_chars=7),
        )

        self.assertIs(pending.active_page, active.active_page)
        self.assertEqual(pending.pending_page.status, ExecutionPageStatus.OPENING)
        with self.assertRaisesRegex(RuntimeError, "unblocked active page"):
            pending.prepare_rollover(
                outbound_attempt_id="attempt-3",
                cursor_start=ExecutionTranscriptCursor(process_chars=8),
            )

    def test_confirmed_rollover_seals_old_page_and_preserves_cursor_continuity(
        self,
    ) -> None:
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="attempt-1",
        )
        initial_page = opening.current_page
        assert initial_page is not None
        active = opening.activate_opening(
            expected_page=initial_page,
            message_id="card-1",
        )
        pending = active.prepare_rollover(
            outbound_attempt_id="attempt-2",
            cursor_start=ExecutionTranscriptCursor(
                process_chars=7,
                reply_chars=5,
            ),
        )
        old_page = pending.active_page
        next_page = pending.pending_page
        assert old_page is not None and next_page is not None

        committed = pending.activate_rollover(
            expected_active=old_page,
            expected_opening=next_page,
            message_id="card-2",
        )

        self.assertIs(committed.pages[0].status, ExecutionPageStatus.SEALED)
        self.assertEqual(
            committed.pages[0].cursor_end,
            committed.pages[1].cursor_start,
        )
        self.assertEqual(committed.current_message_id, "card-2")

    def test_unknown_rollover_keeps_old_page_active_and_blocks_another_effect(
        self,
    ) -> None:
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="attempt-1",
        )
        initial_page = opening.current_page
        assert initial_page is not None
        active = opening.activate_opening(
            expected_page=initial_page,
            message_id="card-1",
        )
        pending = active.prepare_rollover(
            outbound_attempt_id="attempt-2",
            cursor_start=ExecutionTranscriptCursor(reply_chars=9),
        )
        old_page = pending.active_page
        next_page = pending.pending_page
        assert old_page is not None and next_page is not None

        unknown = pending.mark_rollover_send_unknown(
            expected_active=old_page,
            expected_opening=next_page,
        )

        self.assertIs(unknown.active_page, old_page)
        self.assertIs(
            unknown.pending_page.status,
            ExecutionPageStatus.SEND_UNKNOWN,
        )
        self.assertTrue(unknown.has_unresolved_send)


if __name__ == "__main__":
    unittest.main()
