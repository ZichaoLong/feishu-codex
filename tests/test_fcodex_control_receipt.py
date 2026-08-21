import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable

from bot.fcodex.proxy import _ProxyInteractionGate
from bot.service_control_plane import (
    ServiceControlError,
    ServiceControlKnownNotCommittedError,
    ServiceControlOutcomeUnknownError,
    ServiceControlResponseTimeoutError,
)


class FcodexControlReceiptTests(unittest.TestCase):
    class FakeWebsocket:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.closed = False
            self.close_error: BaseException | None = None

        def send(self, payload: str | bytes) -> None:
            self.sent.append(payload)

        def close(self) -> None:
            self.closed = True
            if self.close_error is not None:
                raise self.close_error

    class FakeControl:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.behavior: dict[str, Callable[[dict[str, Any]], Any]] = {}

        def __call__(self, _data_dir: Path, method: str, params: dict[str, Any]) -> Any:
            copied_params = dict(params)
            self.calls.append((method, copied_params))
            callback = self.behavior.get(method)
            if callback is not None:
                return callback(copied_params)
            if method == "operation/participant-connected":
                return {"connected": True, "state": "connected"}
            if method == "operation/server-request":
                return {
                    "action": "deliver",
                    "root_thread_id": "thread-1",
                    "response_token": "response-token-1",
                }
            if method == "operation/request-response-submit":
                return {"allowed": True, "root_thread_id": "thread-1"}
            return {"ok": True}

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.control = self.FakeControl()
        self.client = self.FakeWebsocket()
        self.backend = self.FakeWebsocket()
        self.gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=Path(self.temporary_directory.name),
            participant_id="fcodex:test:control-receipt",
            connection_id="connection-1",
            control_request_fn=self.control,
        )

    def tearDown(self) -> None:
        self.gate.close()
        self.temporary_directory.cleanup()

    def reset_gate(self) -> None:
        self.gate.close()
        self.client = self.FakeWebsocket()
        self.backend = self.FakeWebsocket()
        self.gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=Path(self.temporary_directory.name),
            participant_id="fcodex:test:control-receipt",
            connection_id="connection-1",
            control_request_fn=self.control,
        )

    @staticmethod
    def request(request_id: str = "request-1") -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thread-1", "command": "pwd"},
            }
        )

    @staticmethod
    def response(request_id: str = "request-1", *, valid: bool) -> str:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        payload["result"] = {"decision": "accept"} if valid else "invalid"
        return json.dumps(payload)

    @staticmethod
    def decode(payload: str | bytes) -> dict[str, Any]:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    def service_response_then(self, outcome: Any) -> Callable[[dict[str, Any]], Any]:
        def respond(_params: dict[str, Any]) -> Any:
            self.backend.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "request-1",
                        "result": {"decision": "accept", "responder": "service"},
                    }
                )
            )
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return respond

    def deliver_request_to_tui(self) -> None:
        self.gate.handle_backend_message(
            self.request(),
            client_ws=self.client,
            backend_ws=self.backend,
        )
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.backend.sent, [])

    def test_server_request_uncertain_or_invalid_ack_never_adds_proxy_response(self) -> None:
        outcomes = (
            ServiceControlOutcomeUnknownError("ACK lost"),
            ServiceControlResponseTimeoutError("ACK timeout"),
            None,
            ServiceControlError("service returned an error"),
        )
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                self.control.behavior.clear()
                self.reset_gate()
                self.control.behavior["operation/server-request"] = (
                    self.service_response_then(outcome)
                )

                self.gate.handle_backend_message(
                    self.request(),
                    client_ws=self.client,
                    backend_ws=self.backend,
                )

                self.assertEqual(len(self.backend.sent), 1)
                self.assertEqual(
                    self.decode(self.backend.sent[0])["result"]["responder"],
                    "service",
                )
                self.assertTrue(self.client.closed)
                self.assertTrue(self.backend.closed)

    def test_valid_tui_response_ack_loss_never_adds_proxy_response(self) -> None:
        self.deliver_request_to_tui()
        self.control.behavior["operation/request-response-submit"] = (
            self.service_response_then(ServiceControlOutcomeUnknownError("ACK lost"))
        )

        self.gate.handle_client_message(
            self.response(valid=True),
            client_ws=self.client,
            backend_ws=self.backend,
        )

        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(self.decode(self.backend.sent[0])["result"]["responder"], "service")
        self.assertEqual(len(self.gate._pending_server_request_ids), 0)
        self.assertTrue(self.client.closed)
        self.assertTrue(self.backend.closed)

    def test_invalid_tui_response_ack_loss_never_adds_proxy_response(self) -> None:
        self.deliver_request_to_tui()
        self.control.behavior["operation/request-response-invalid"] = (
            self.service_response_then(ServiceControlResponseTimeoutError("ACK timeout"))
        )

        self.gate.handle_client_message(
            self.response(valid=False),
            client_ws=self.client,
            backend_ws=self.backend,
        )

        self.assertEqual(len(self.backend.sent), 1)
        self.assertEqual(self.decode(self.backend.sent[0])["result"]["responder"], "service")
        self.assertEqual(len(self.gate._pending_server_request_ids), 0)
        self.assertTrue(self.client.closed)
        self.assertTrue(self.backend.closed)

    def test_known_not_committed_route_quarantines_without_proxy_response(self) -> None:
        def not_committed(_params: dict[str, Any]) -> None:
            raise ServiceControlKnownNotCommittedError("connection refused before send")

        self.control.behavior["operation/server-request"] = not_committed
        self.client.close_error = RuntimeError("client close failed")
        self.backend.close_error = RuntimeError("backend close failed")

        self.gate.handle_backend_message(
            self.request(),
            client_ws=self.client,
            backend_ws=self.backend,
        )
        self.gate.handle_backend_message(
            self.request(),
            client_ws=self.client,
            backend_ws=self.backend,
        )

        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.client.sent, [])
        self.assertTrue(self.client.closed)
        self.assertTrue(self.backend.closed)
        self.assertEqual(
            sum(method == "operation/server-request" for method, _ in self.control.calls),
            1,
        )

    def test_bad_receipt_and_quarantine_are_one_linearized_gate_transition(self) -> None:
        control_entered = threading.Event()
        release_control = threading.Event()
        second_started = threading.Event()

        def not_committed(_params: dict[str, Any]) -> None:
            control_entered.set()
            self.assertTrue(release_control.wait(1.0))
            raise ServiceControlKnownNotCommittedError("connection refused before send")

        self.control.behavior["operation/server-request"] = not_committed
        self.client.close_error = RuntimeError("client close failed")
        self.backend.close_error = RuntimeError("backend close failed")

        def deliver() -> None:
            self.gate.handle_backend_message(
                self.request(),
                client_ws=self.client,
                backend_ws=self.backend,
            )

        first = threading.Thread(target=deliver)

        def deliver_second() -> None:
            second_started.set()
            deliver()

        second = threading.Thread(target=deliver_second)
        first.start()
        self.assertTrue(control_entered.wait(1.0))
        second.start()
        self.assertTrue(second_started.wait(1.0))
        release_control.set()
        first.join(1.0)
        second.join(1.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(
            sum(method == "operation/server-request" for method, _ in self.control.calls),
            1,
        )

    def test_known_not_committed_tui_submission_quarantines_without_proxy_response(self) -> None:
        for valid, method in (
            (True, "operation/request-response-submit"),
            (False, "operation/request-response-invalid"),
        ):
            with self.subTest(valid=valid):
                self.control.behavior.clear()
                self.reset_gate()
                self.deliver_request_to_tui()

                def not_committed(_params: dict[str, Any]) -> None:
                    raise ServiceControlKnownNotCommittedError(
                        "connection refused before send"
                    )

                self.control.behavior[method] = not_committed
                self.client.close_error = RuntimeError("client close failed")
                self.backend.close_error = RuntimeError("backend close failed")
                self.gate.handle_client_message(
                    self.response(valid=valid),
                    client_ws=self.client,
                    backend_ws=self.backend,
                )
                self.gate.handle_client_message(
                    self.response(valid=valid),
                    client_ws=self.client,
                    backend_ws=self.backend,
                )

                self.assertEqual(self.backend.sent, [])
                self.assertTrue(self.client.closed)
                self.assertTrue(self.backend.closed)
                self.assertEqual(
                    sum(call_method == method for call_method, _ in self.control.calls),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
