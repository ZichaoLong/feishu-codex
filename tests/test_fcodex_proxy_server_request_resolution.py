import json
import tempfile
import unittest
from pathlib import Path

from bot.fcodex.proxy import _RESOLVED_SERVER_REQUEST_RECEIPT_LIMIT
from bot.jsonrpc_id import jsonrpc_id_key
from tests import test_fcodex_proxy_operation_receipts as _operation_receipts


class ProxyServerRequestResolutionTests(unittest.TestCase):
    """Resolved receipt and exact retry coverage for the proxy wire owner."""

    _FakeWs = _operation_receipts.ProxyInteractionGateTests._FakeWs
    _FakeOperationControl = (
        _operation_receipts.ProxyInteractionGateTests._FakeOperationControl
    )
    _decode_payload = staticmethod(
        _operation_receipts.ProxyInteractionGateTests._decode_payload
    )
    _gate = _operation_receipts.ProxyInteractionGateTests._gate
    _request = staticmethod(_operation_receipts.ProxyInteractionGateTests._request)

    def test_known_not_sent_response_reprojects_the_exact_server_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            control.response_submission = {
                "allowed": False,
                "response_disposition": "not_sent",
            }
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            request = self._request(
                7,
                "item/permissions/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "permissions": {"network": {"enabled": True}},
                },
            )
            gate.handle_backend_message(
                request,
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "result": {
                            "permissions": {"network": {"enabled": True}},
                            "scope": "session",
                        },
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(len(client_ws.sent), 2)
            self.assertEqual(
                self._decode_payload(client_ws.sent[-1]),
                self._decode_payload(request),
            )
            self.assertIn(jsonrpc_id_key(7), gate._pending_server_request_ids)
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_resolved_first_silently_retires_a_late_tui_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    "req-1",
                    "item/commandExecution/requestApproval",
                    {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "command": "ls",
                    },
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "serverRequest/resolved",
                        "params": {
                            "requestId": "req-1",
                            "threadId": "thread-1",
                        },
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "result": {"decision": "accept"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(
                control.calls_for("operation/request-response-submit"),
                [],
            )
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_resolved_receipt_preserves_jsonrpc_id_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    1,
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "turnId": "turn-1"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "serverRequest/resolved",
                        "params": {"requestId": 1, "threadId": "thread-1"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_backend_message(
                self._request(
                    "1",
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "turnId": "turn-1"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertIn(jsonrpc_id_key(1), gate._resolved_server_request_ids)
            self.assertIn(jsonrpc_id_key("1"), gate._pending_server_request_ids)
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_resolved_receipts_are_bounded_and_cleared_with_the_wire(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            gate = self._gate(Path(tmpdir), self._FakeOperationControl())
            with gate._lock:
                for request_id in range(
                    _RESOLVED_SERVER_REQUEST_RECEIPT_LIMIT + 1
                ):
                    gate._remember_resolved_server_request_locked(
                        jsonrpc_id_key(request_id)
                    )

            self.assertEqual(
                len(gate._resolved_server_request_ids),
                _RESOLVED_SERVER_REQUEST_RECEIPT_LIMIT,
            )
            self.assertNotIn(
                jsonrpc_id_key(0),
                gate._resolved_server_request_ids,
            )
            gate.close()
            self.assertEqual(gate._resolved_server_request_ids, {})


if __name__ == "__main__":
    unittest.main()
