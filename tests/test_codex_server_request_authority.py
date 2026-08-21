"""Owner-level tests for generation-pinned server-request responses."""

from __future__ import annotations

import time
import unittest

from bot.codex_protocol.server_request_authority import (
    ServerRequestAuthorityError,
    ServerRequestAuthorityRegistry,
)


class ServerRequestAuthorityRegistryTests(unittest.TestCase):
    def test_claim_is_one_shot_but_pre_send_release_is_retryable(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember("request-1", 7)

        first = registry.claim(
            "request-1", connection_generation=7, deadline_monotonic=None
        )
        with self.assertRaises(ServerRequestAuthorityError):
            registry.claim(
                "request-1", connection_generation=7, deadline_monotonic=None
            )

        registry.release(first)

        self.assertEqual(
            registry.claim(
                "request-1", connection_generation=7, deadline_monotonic=None
            ),
            first,
        )

    def test_reused_id_across_generations_has_no_implicit_authority(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember("request-1", 7)
        registry.remember("request-1", 8)

        with self.assertRaises(TypeError):
            registry.claim("request-1", deadline_monotonic=None)

        exact = registry.claim(
            "request-1",
            connection_generation=8,
            deadline_monotonic=None,
        )
        self.assertEqual(exact[1], 8)

    def test_retired_claim_releases_memory_but_not_duplicate_authority(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember("request-1", 7)
        authority = registry.claim(
            "request-1", connection_generation=7, deadline_monotonic=None
        )

        registry.retire(authority)

        self.assertEqual(registry.remembered_request_count(), 0)
        with self.assertRaisesRegex(ServerRequestAuthorityError, "no recorded"):
            registry.claim(
                "request-1", connection_generation=7, deadline_monotonic=None
            )

    def test_rotation_rejects_old_capability_and_admits_replacement_id(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember(0, 1)
        old = registry.claim(0, connection_generation=1, deadline_monotonic=None)

        receipt = registry.rotate_after_backend_stop()
        registry.release(old)
        registry.remember(0, 2)
        replacement = registry.claim(
            0, connection_generation=2, deadline_monotonic=None
        )

        self.assertEqual(receipt.remembered_request_count, 1)
        self.assertEqual(replacement[1:], (2, receipt.active_epoch))
        with self.assertRaises(ServerRequestAuthorityError):
            registry.claim(0, connection_generation=1, deadline_monotonic=None)

    def test_resolved_request_retires_only_its_exact_generation(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember("request-1", 7)
        registry.remember("request-1", 8)

        self.assertTrue(registry.retire_request_generation("request-1", 7))
        self.assertFalse(registry.retire_request_generation("request-1", 7))
        self.assertEqual(registry.remembered_request_count(), 1)
        with self.assertRaises(ServerRequestAuthorityError):
            registry.claim(
                "request-1",
                connection_generation=7,
                deadline_monotonic=None,
            )
        self.assertEqual(
            registry.claim(
                "request-1",
                connection_generation=8,
                deadline_monotonic=None,
            )[1],
            8,
        )

    def test_disconnect_retires_every_request_from_one_generation(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember(1, 7)
        registry.remember("1", 7)
        registry.remember("replacement", 8)
        consumed = registry.claim(
            1,
            connection_generation=7,
            deadline_monotonic=None,
        )

        self.assertEqual(registry.retire_connection_generation(7), 2)
        registry.release(consumed)
        self.assertEqual(registry.remembered_request_count(), 1)
        with self.assertRaises(ServerRequestAuthorityError):
            registry.claim(1, connection_generation=7, deadline_monotonic=None)
        self.assertEqual(
            registry.claim(
                "replacement",
                connection_generation=8,
                deadline_monotonic=None,
            )[1],
            8,
        )

    def test_receiving_generation_must_be_positive_integer(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        for invalid in (0, -1, True, False):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    registry.remember("request-1", invalid)
                registry.remember("request-valid", 1)
                with self.assertRaises(ValueError):
                    registry.claim(
                        "request-valid",
                        connection_generation=invalid,
                        deadline_monotonic=None,
                    )

    def test_integer_and_string_ids_are_distinct(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember(1, 7)
        registry.remember("1", 8)

        self.assertEqual(
            registry.claim(1, connection_generation=7, deadline_monotonic=None)[1],
            7,
        )
        self.assertEqual(
            registry.claim("1", connection_generation=8, deadline_monotonic=None)[1],
            8,
        )

    def test_expired_deadline_never_waits_for_registry_lock(self) -> None:
        registry = ServerRequestAuthorityRegistry()
        registry.remember("request-1", 7)

        with self.assertRaises(TimeoutError):
            registry.claim(
                "request-1",
                connection_generation=7,
                deadline_monotonic=time.monotonic() - 1,
            )


if __name__ == "__main__":
    unittest.main()
