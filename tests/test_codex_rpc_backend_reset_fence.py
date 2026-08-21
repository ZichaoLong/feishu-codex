from __future__ import annotations

import threading
import unittest

from bot.codex_protocol.connection import (
    _CONNECTION_DISCONNECTED,
    AppServerEndpointMode,
    CodexRpcConnection,
    CodexRpcConnectionGenerationMismatchError,
    CodexRpcPreSendError,
)


class CodexRpcBackendResetFenceTest(unittest.TestCase):
    def test_stale_physical_generation_never_calls_gate(self) -> None:
        connection = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT
        )
        connection._connection_generation = 8
        callbacks: list[str] = []

        with self.assertRaises(CodexRpcConnectionGenerationMismatchError):
            connection.fence_backend_reset_generation(
                expected_connection_generation=7,
                fence_ingress=lambda: callbacks.append("fenced"),
                timeout=0.1,
            )

        self.assertEqual(callbacks, [])
        self.assertEqual(connection._connection_generation, 8)

    def test_same_disconnected_generation_can_fence_under_identity_lock(self) -> None:
        connection = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT
        )
        connection._connection_generation = 7
        connection._connection_state = _CONNECTION_DISCONNECTED
        callbacks: list[tuple[int, bool]] = []

        connection.fence_backend_reset_generation(
            expected_connection_generation=7,
            fence_ingress=lambda: callbacks.append(
                (connection._connection_generation, connection._lock._is_owned())
            ),
            timeout=0.1,
        )

        self.assertEqual(callbacks, [(7, True)])

    def test_invalid_generation_is_rejected_before_lock_or_callback(self) -> None:
        connection = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT
        )
        callbacks: list[str] = []

        for expected in (None, True, False, 0, -1, 1.0, "1"):
            with self.subTest(expected=expected):
                with self.assertRaises((TypeError, ValueError)):
                    connection.fence_backend_reset_generation(
                        expected_connection_generation=expected,  # type: ignore[arg-type]
                        fence_ingress=lambda: callbacks.append("fenced"),
                        timeout=0.1,
                    )

        self.assertEqual(callbacks, [])

    def test_identity_lock_timeout_is_known_pre_callback(self) -> None:
        connection = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT
        )
        connection._connection_generation = 7
        entered = threading.Event()
        release = threading.Event()
        callbacks: list[str] = []

        def hold_identity_lock() -> None:
            with connection._lock:
                entered.set()
                release.wait(timeout=1.0)

        holder = threading.Thread(target=hold_identity_lock)
        holder.start()
        self.assertTrue(entered.wait(timeout=1.0))
        try:
            with self.assertRaises(CodexRpcPreSendError):
                connection.fence_backend_reset_generation(
                    expected_connection_generation=7,
                    fence_ingress=lambda: callbacks.append("fenced"),
                    timeout=0.01,
                )
        finally:
            release.set()
            holder.join(timeout=1.0)

        self.assertEqual(callbacks, [])
        self.assertFalse(holder.is_alive())

    def test_gate_callback_timeout_is_not_reclassified_pre_send(self) -> None:
        connection = CodexRpcConnection(
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT
        )
        connection._connection_generation = 7

        with self.assertRaisesRegex(TimeoutError, "gate drain timed out"):
            connection.fence_backend_reset_generation(
                expected_connection_generation=7,
                fence_ingress=lambda: (_ for _ in ()).throw(
                    TimeoutError("gate drain timed out")
                ),
                timeout=0.1,
            )


if __name__ == "__main__":
    unittest.main()
