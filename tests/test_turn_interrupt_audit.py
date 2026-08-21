from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from bot.turn_interrupt_audit import (
    TurnInterruptSource,
    record_turn_interrupt_dispatch_attempt,
)


def _short_reference(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()[
        :16
    ]


class TurnInterruptAuditTests(unittest.TestCase):
    def test_records_only_trusted_source_and_hashed_exact_references(self) -> None:
        thread_id = "thread-secret\nwith-control"
        turn_id = "turn-secret\ud800"

        with patch("bot.turn_interrupt_audit.logger.info") as info:
            record_turn_interrupt_dispatch_attempt(
                source=TurnInterruptSource.WEB_DOCUMENT,
                thread_id=thread_id,
                turn_id=turn_id,
            )

        info.assert_called_once()
        template, *values = info.call_args.args
        rendered = template % tuple(values)
        self.assertIn("turn_interrupt_dispatch_attempt", rendered)
        self.assertIn("phase=attempt", rendered)
        self.assertIn("source=web_document", rendered)
        self.assertIn(f"thread_ref={_short_reference(thread_id)}", rendered)
        self.assertIn(f"turn_ref={_short_reference(turn_id)}", rendered)
        self.assertNotIn(thread_id, rendered)
        self.assertNotIn(turn_id, rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("\n", rendered)

    def test_plain_string_cannot_impersonate_a_trusted_source(self) -> None:
        with patch("bot.turn_interrupt_audit.logger.info") as info:
            record_turn_interrupt_dispatch_attempt(
                source="web_document",  # type: ignore[arg-type]
                thread_id="thread-1",
                turn_id="turn-1",
            )

        template, *values = info.call_args.args
        rendered = template % tuple(values)
        self.assertIn("source=invalid_internal_source", rendered)
        self.assertNotIn("source=web_document", rendered)

    def test_empty_startup_target_keeps_one_redacted_turn_reference(self) -> None:
        with patch("bot.turn_interrupt_audit.logger.info") as info:
            record_turn_interrupt_dispatch_attempt(
                source=TurnInterruptSource.WEB_DOCUMENT,
                thread_id="thread-1",
                turn_id="",
            )

        template, *values = info.call_args.args
        rendered = template % tuple(values)
        self.assertIn(f"turn_ref={_short_reference('')}", rendered)

    def test_logging_failure_never_blocks_the_effect_caller(self) -> None:
        with patch(
            "bot.turn_interrupt_audit.logger.info",
            side_effect=RuntimeError("log sink failed"),
        ):
            record_turn_interrupt_dispatch_attempt(
                source=TurnInterruptSource.FEISHU_BINDING,
                thread_id="thread-1",
                turn_id="turn-1",
            )


if __name__ == "__main__":
    unittest.main()
