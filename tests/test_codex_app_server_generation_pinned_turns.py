import unittest
from contextlib import nullcontext
from unittest.mock import Mock, call

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.codex_protocol.connection import (
    CodexRpcConnectionGenerationMismatchError,
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)


_GENERATION = 7
_INPUT = [{"type": "text", "text": "hello"}]


class GenerationPinnedTurnTests(unittest.TestCase):
    def _adapter(self, *, permits=None):
        issued_permits = [object()] if permits is None else permits
        issue = Mock(side_effect=issued_permits)
        guard = Mock(side_effect=lambda _permit: nullcontext())
        confirm = Mock()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=guard,
            confirm_outbound_request=confirm,
        )
        rpc = Mock()
        adapter._rpc = rpc
        return adapter, rpc, issue, guard, confirm, issued_permits

    def _assert_exact_generation(self, rpc, *, method: str) -> None:
        args, kwargs = rpc.request.call_args
        self.assertEqual(args[0], method)
        self.assertTrue(kwargs["require_existing_connection"])
        self.assertEqual(kwargs["expected_connection_generation"], _GENERATION)

    def test_start_uses_admitted_exact_existing_connection(self) -> None:
        adapter, rpc, issue, guard, confirm, permits = self._adapter()

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            return {"turn": {"id": "turn-1"}}

        rpc.request.side_effect = request

        result = adapter.start_turn(
            thread_id="thread-1",
            input_items=_INPUT,
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        issue.assert_called_once_with("turn/start")
        guard.assert_called_once_with(permits[0])
        confirm.assert_called_once_with(permits[0])
        self._assert_exact_generation(rpc, method="turn/start")

    def test_steer_uses_admitted_exact_existing_connection(self) -> None:
        adapter, rpc, issue, guard, confirm, permits = self._adapter()

        def request(_method, params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            return {"turnId": params["expectedTurnId"]}

        rpc.request.side_effect = request

        result = adapter.steer_turn(
            thread_id="thread-1",
            expected_turn_id="turn-1",
            input_items=_INPUT,
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(result["turnId"], "turn-1")
        issue.assert_called_once_with("turn/steer")
        guard.assert_called_once_with(permits[0])
        confirm.assert_called_once_with(permits[0])
        self._assert_exact_generation(rpc, method="turn/steer")

    def test_generation_mismatch_remains_pre_send_without_confirmation(self) -> None:
        adapter, rpc, _issue, _guard, confirm, _permits = self._adapter()
        mismatch = CodexRpcConnectionGenerationMismatchError(
            "turn/start",
            expected_generation=_GENERATION,
            observed_generation=_GENERATION + 1,
        )
        rpc.request.side_effect = mismatch

        with self.assertRaises(CodexRpcPreSendError) as raised:
            adapter.start_turn(
                thread_id="thread-1",
                input_items=_INPUT,
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception, mismatch)
        confirm.assert_not_called()

    def test_transport_failure_after_send_guard_remains_unknown(self) -> None:
        adapter, rpc, _issue, guard, confirm, permits = self._adapter()
        transport_error = CodexRpcTransportError(
            "turn/steer",
            {"code": -32000, "message": "connection closed during send"},
        )

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            raise transport_error

        rpc.request.side_effect = request

        with self.assertRaises(CodexRpcTransportError) as raised:
            adapter.steer_turn(
                thread_id="thread-1",
                expected_turn_id="turn-1",
                input_items=_INPUT,
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception, transport_error)
        guard.assert_called_once_with(permits[0])
        confirm.assert_not_called()

    def test_decoded_rejection_confirms_exact_epoch(self) -> None:
        adapter, rpc, _issue, guard, confirm, permits = self._adapter()
        rejection = CodexRpcError(
            "turn/steer",
            {"code": -32602, "message": "expected turn is no longer active"},
        )

        def request(_method, _params, **kwargs):
            with kwargs["outbound_transport_guard"]():
                pass
            raise rejection

        rpc.request.side_effect = request

        with self.assertRaises(CodexRpcError) as raised:
            adapter.steer_turn(
                thread_id="thread-1",
                expected_turn_id="turn-1",
                input_items=_INPUT,
                expected_connection_generation=_GENERATION,
            )

        self.assertIs(raised.exception, rejection)
        guard.assert_called_once_with(permits[0])
        confirm.assert_called_once_with(permits[0])

    def test_permissions_fallback_renews_permit_and_retains_generation(self) -> None:
        permits = [object(), object()]
        adapter, rpc, issue, guard, confirm, _permits = self._adapter(permits=permits)

        def request(method, params, **kwargs):
            self.assertTrue(kwargs["require_existing_connection"])
            self.assertEqual(
                kwargs["expected_connection_generation"],
                _GENERATION,
            )
            with kwargs["outbound_transport_guard"]():
                pass
            if "permissions" in params:
                raise CodexRpcError(
                    method,
                    {"code": -32602, "message": "unknown field permissions"},
                )
            return {"turn": {"id": "turn-1"}}

        rpc.request.side_effect = request

        result = adapter.start_turn(
            thread_id="thread-1",
            input_items=_INPUT,
            expected_connection_generation=_GENERATION,
        )

        self.assertEqual(result["turn"]["id"], "turn-1")
        self.assertEqual(issue.call_args_list, [call("turn/start"), call("turn/start")])
        self.assertEqual(guard.call_args_list, [call(permits[0]), call(permits[1])])
        self.assertEqual(confirm.call_args_list, [call(permits[0]), call(permits[1])])
        self.assertIn("permissions", rpc.request.call_args_list[0].args[1])
        self.assertNotIn("permissions", rpc.request.call_args_list[1].args[1])
