import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.fcodex.operation_contract import THREAD_READ_METHODS
from bot.fcodex.proxy import _ProxyInteractionGate
from bot.service_control_plane import ServiceControlKnownNotCommittedError


class ProxyInteractionGateTests(unittest.TestCase):
    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[str | bytes] = []
            self.events: list[str] = []
            self.closed = False

        def send(self, payload: str | bytes) -> None:
            if self.closed:
                raise RuntimeError("websocket is closed")
            self.sent.append(payload)
            self.events.append("send")

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self.events.append("close")

    class _FakeOperationControl:
        """A small stand-in for the RuntimeLoop-owned control plane.

        The proxy deliberately owns no lease or operation state any more.  Its
        tests must therefore assert the protocol it speaks to the service,
        rather than recreate the removed local ownership implementation.
        """

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.admission: dict = {"allowed": True, "root_thread_id": "thread-1"}
            self.server_route: dict = {
                "action": "deliver",
                "root_thread_id": "thread-1",
                "response_token": "response-token-1",
            }
            self.response_submission: dict = {
                "allowed": True,
                "root_thread_id": "thread-1",
                "response_disposition": "submitted",
            }
            self.invalid_response_outcome: dict = {"action": "fail_closed"}
            self.participant_connected: dict = {"connected": True, "state": "connected"}
            self.client_response_receipt: dict | BaseException | None = None
            self.fenced = False
            self._next_request_token = 0

        def __call__(self, data_dir: Path, method: str, params: dict) -> dict:
            self.calls.append((method, {"data_dir": data_dir, **dict(params)}))
            if self.fenced and method != "operation/participant-disconnected":
                raise RuntimeError("backend reset fenced")
            if method == "operation/participant-connected":
                return dict(self.participant_connected)
            if method == "operation/transport-admit":
                return {"allowed": True}
            if method == "operation/admit":
                admission = dict(self.admission)
                if admission.get("allowed") and "tracks_response" not in admission:
                    if (
                        not str(params.get("thread_id", "") or "").strip()
                        and params.get("rpc_method") != "thread/start"
                    ) or params.get("rpc_method") in THREAD_READ_METHODS:
                        admission.update(tracks_response=False, request_token=None)
                    else:
                        self._next_request_token += 1
                        admission.update(
                            tracks_response=True,
                            request_token=self._next_request_token,
                        )
                return admission
            if method == "operation/client-response":
                receipt = self.client_response_receipt
                if isinstance(receipt, BaseException):
                    raise receipt
                if receipt is not None:
                    return dict(receipt)
                return {
                    "known": True,
                    "settled": True,
                    "request_token": params["request_token"],
                }
            if method == "operation/server-request":
                return dict(self.server_route)
            if method == "operation/request-response-submit":
                return dict(self.response_submission)
            if method == "operation/request-response-invalid":
                return dict(self.invalid_response_outcome)
            return {"ok": True}

        def calls_for(self, method: str) -> list[dict]:
            return [params for call_method, params in self.calls if call_method == method]

    @staticmethod
    def _decode_payload(payload: str | bytes) -> dict:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        return json.loads(payload)

    def _gate(
        self,
        data_dir: Path,
        control: _FakeOperationControl,
        *,
        participant_id: str = "fcodex:test:incarnation",
        connection_id: str = "connection-1",
    ) -> _ProxyInteractionGate:
        return _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=data_dir,
            participant_id=participant_id,
            connection_id=connection_id,
            control_request_fn=control,
        )

    @staticmethod
    def _request(request_id: int | str, method: str, params: dict | None) -> str:
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

    def _admit_resume(
        self,
        gate: _ProxyInteractionGate,
        *,
        client_ws: _FakeWs,
        backend_ws: _FakeWs,
        request_id: int | str = 1,
        thread_id: str = "thread-1",
    ) -> None:
        gate.handle_client_message(
            self._request(request_id, "thread/resume", {"threadId": thread_id}),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

    def test_thread_resume_uses_service_admission_before_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.admission = {
                "allowed": False,
                "reason": "当前 thread 仍由运行中的实例 `default` 保持为 loaded；当前按 fail-close 拒绝跨实例继续。",
            }
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(1, "thread/resume", {"threadId": "thread-1"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            error = self._decode_payload(client_ws.sent[-1])
            self.assertEqual(error["id"], 1)
            self.assertIn("拒绝跨实例继续", error["error"]["message"])
            admission = control.calls_for("operation/admit")[-1]
            self.assertEqual(admission["rpc_method"], "thread/resume")
            self.assertEqual(admission["thread_id"], "thread-1")

    def test_thread_resume_rejects_history_and_path_aliases_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.response_submission = {
                "allowed": True,
                "root_thread_id": "thread-1",
                "response_disposition": "deferred",
            }
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id, params, rejected_field in (
                (1, {"threadId": "thread-1", "history": []}, "history"),
                (2, {"threadId": "thread-1", "path": "/tmp/other-rollout.jsonl"}, "path"),
                (3, {"threadId": " thread-1 "}, "threadId"),
            ):
                gate.handle_client_message(
                    self._request(request_id, "thread/resume", params),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                error = self._decode_payload(client_ws.sent[-1])
                self.assertEqual(error["id"], request_id)
                self.assertIn(rejected_field, error["error"]["message"])

            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/admit"), [])

    def test_thread_resume_forwards_tui_settings_instructions_and_reviewer_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            params = {
                "threadId": "thread-1",
                "history": None,
                "path": None,
                "model": "gpt-5.4",
                "modelProvider": "openai",
                "serviceTier": "priority",
                "cwd": "/workspace/other",
                "runtimeWorkspaceRoots": ["/workspace/other"],
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
                "sandbox": "danger-full-access",
                "permissions": ":full",
                "config": {"model_reasoning_effort": "high"},
                "baseInstructions": "base override",
                "developerInstructions": "developer override",
                "personality": "pragmatic",
                "excludeTurns": True,
                "initialTurnsPage": {"limit": 40, "sortDirection": "desc"},
            }

            gate.handle_client_message(
                self._request(1, "thread/resume", params),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(len(backend_ws.sent), 1)
            forwarded = self._decode_payload(backend_ws.sent[0])
            self.assertEqual(forwarded["params"], params)
            admission = control.calls_for("operation/admit")[-1]
            self.assertEqual(admission["thread_id"], "thread-1")

    def test_thread_resume_forwards_future_parameter_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(
                    1,
                    "thread/resume",
                    {"threadId": "thread-1", "futureOverride": True},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(
                self._decode_payload(backend_ws.sent[-1])["params"],
                {"threadId": "thread-1", "futureOverride": True},
            )
            self.assertEqual(
                control.calls_for("operation/admit")[-1]["thread_id"],
                "thread-1",
            )

    def test_review_start_rejects_detached_before_owner_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(
                    1,
                    "review/start",
                    {
                        "threadId": "thread-1",
                        "target": {"type": "uncommittedChanges"},
                        "delivery": "detached",
                    },
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/admit"), [])
            error = self._decode_payload(client_ws.sent[-1])
            self.assertEqual(error["id"], 1)
            self.assertIn("detached review", error["error"]["message"])

    def test_review_start_makes_omitted_or_null_delivery_inline_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id, delivery in ((1, "omit"), (2, None), (3, "inline")):
                params = {
                    "threadId": " thread-1 ",
                    "target": {"type": "uncommittedChanges"},
                }
                if delivery != "omit":
                    params["delivery"] = delivery
                gate.handle_client_message(
                    self._request(request_id, "review/start", params),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(
                [self._decode_payload(message)["params"] for message in backend_ws.sent],
                [
                    {
                        "threadId": "thread-1",
                        "target": {"type": "uncommittedChanges"},
                        "delivery": "inline",
                    },
                    {
                        "threadId": "thread-1",
                        "target": {"type": "uncommittedChanges"},
                        "delivery": "inline",
                    },
                    {
                        "threadId": "thread-1",
                        "target": {"type": "uncommittedChanges"},
                        "delivery": "inline",
                    },
                ],
            )
            self.assertEqual(
                [call["thread_id"] for call in control.calls_for("operation/admit")],
                ["thread-1", "thread-1", "thread-1"],
            )

    def test_local_error_response_requires_request_id(self) -> None:
        from bot.fcodex.proxy import _send_local_error_response

        with self.assertRaisesRegex(ValueError, "requires a request id"):
            _send_local_error_response(self._FakeWs(), "", "boom")

    def test_reviewed_unscoped_requests_forward_without_owner_admission(self) -> None:
        allowed_requests = (
            ("initialize", {}),
            ("account/read", {"refreshToken": False}),
            ("config/read", {}),
            ("configRequirements/read", None),
            ("model/list", {}),
            # These CWD-bearing discovery reads are intentionally allowed in
            # Focus's explicitly shared, fully trusted deployment.  They are
            # exact method permissions, not a general host-operation grant.
            ("hooks/list", {"cwds": ["/workspace/project"]}),
            (
                "skills/list",
                {"cwds": ["/workspace/project"], "forceReload": True},
            ),
            ("account/rateLimits/read", None),
            ("thread/list", {}),
            ("thread/loaded/list", {}),
            ("app/list", {}),
            ("app/installed", {}),
            ("experimentalFeature/list", {}),
            ("mcpServerStatus/list", {}),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id, (method, params) in enumerate(allowed_requests, start=1):
                gate.handle_client_message(
                    self._request(request_id, method, params),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(
                [
                    (
                        self._decode_payload(message)["method"],
                        self._decode_payload(message)["params"],
                    )
                    for message in backend_ws.sent
                ],
                list(allowed_requests),
            )
            self.assertEqual(
                len(control.calls_for("operation/admit")),
                len(allowed_requests),
            )

    def test_unscoped_global_and_unknown_requests_fail_closed_locally(self) -> None:
        denied_requests = (
            ("account/login/start", {"type": "chatgpt"}),
            ("account/logout", {}),
            ("config/value/write", {"key": "model", "value": "other"}),
            ("skills/extraRoots/set", {"roots": ["/workspace/other"]}),
            ("mcpServer/oauth/login", {"name": "server-a"}),
            ("mcpServer/resource/read", {"server": "server-a", "uri": "file:///tmp/value"}),
            ("mcpServer/tool/call", {"server": "server-a", "tool": "read"}),
            ("feedback/upload", {"classification": "bug"}),
            ("fs/readFile", {"path": "/workspace/project/.env"}),
            ("fs/writeFile", {"path": "/workspace/project/file", "content": "x"}),
            ("command/exec", {"command": "id"}),
            ("process/spawn", {"command": "id"}),
            ("future/globalMutation", {}),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id, (method, params) in enumerate(denied_requests, start=1):
                gate.handle_client_message(
                    self._request(request_id, method, params),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/admit"), [])
            self.assertEqual(len(client_ws.sent), len(denied_requests))
            for request_id, ((method, _params), payload) in enumerate(
                zip(denied_requests, client_ws.sent), start=1
            ):
                error = self._decode_payload(payload)
                self.assertEqual(error["id"], request_id)
                self.assertEqual(error["error"]["code"], -32002)
                self.assertIn(method, error["error"]["message"])
                self.assertIn("未转发", error["error"]["message"])

    def test_reset_fence_blocks_unscoped_read_after_proxy_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            control.fenced = True

            gate.handle_client_message(
                self._request(1, "thread/list", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            error = self._decode_payload(client_ws.sent[-1])
            self.assertEqual(error["id"], 1)
            self.assertIn("backend reset fenced", error["error"]["message"])

    def test_reset_fence_quarantines_initialized_notification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            control.fenced = True

            gate.handle_client_message(
                json.dumps({"jsonrpc": "2.0", "method": "initialized"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)

    def test_unscoped_optional_thread_target_uses_owner_admission_when_targeted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.admission = {
                "allowed": False,
                "reason": "thread-scoped policy rejected this query",
            }
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(1, "mcpServerStatus/list", {"threadId": "thread-1"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            self.assertIn("thread-scoped policy", self._decode_payload(client_ws.sent[-1])["error"]["message"])
            admission = control.calls_for("operation/admit")[-1]
            self.assertEqual(admission["rpc_method"], "mcpServerStatus/list")
            self.assertEqual(admission["thread_id"], "thread-1")

    def test_only_reviewed_connection_local_client_notification_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                json.dumps({"jsonrpc": "2.0", "method": "initialized"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            gate.handle_client_message(
                json.dumps({"jsonrpc": "2.0", "method": "future/globalNotification"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            for invalid_id in (None, ""):
                gate.handle_client_message(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": invalid_id,
                            "method": "initialized",
                        }
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(len(backend_ws.sent), 1)
            self.assertEqual(self._decode_payload(backend_ws.sent[0])["method"], "initialized")
            self.assertEqual(control.calls_for("operation/admit"), [])
            self.assertEqual(len(control.calls_for("operation/transport-admit")), 1)

    def test_malformed_or_batched_client_frames_cannot_bypass_unscoped_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            unsafe_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "process/spawn",
                "params": {"command": "id"},
            }
            for frame in (
                json.dumps([unsafe_request]),
                "{not valid json",
                json.dumps(["not a JSON-RPC object"]),
            ):
                gate.handle_client_message(
                    frame,
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/admit"), [])

    def test_observer_copy_of_interactive_request_is_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.server_route = {"action": "suppress", "root_thread_id": "thread-1"}
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "ls"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/server-request")[-1]["request_id"], "req-1")

    def test_fcodex_answers_current_time_without_tui_or_owner_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            with patch("bot.interaction_contract.time.time", return_value=1_781_717_655.9):
                gate.handle_backend_message(
                    self._request(
                        "clock-1",
                        "currentTime/read",
                        {"threadId": "thread-1"},
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(
                self._decode_payload(backend_ws.sent[-1]),
                {
                    "jsonrpc": "2.0",
                    "id": "clock-1",
                    "result": {"currentTimeAt": 1_781_717_655},
                },
            )
            self.assertEqual(control.calls_for("operation/server-request"), [])

    def test_headerless_interactive_response_is_submitted_by_service_not_proxy_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "ls"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            forwarded = self._decode_payload(client_ws.sent[-1])
            self.assertEqual(forwarded["method"], "item/commandExecution/requestApproval")

            gate.handle_client_message(
                json.dumps({"id": "req-1", "result": {"decision": "cancel"}}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(backend_ws.sent, [])
            submission = control.calls_for("operation/request-response-submit")[-1]
            self.assertEqual(submission["request_id"], "req-1")
            self.assertEqual(submission["response_token"], "response-token-1")
            self.assertEqual(submission["response_result"], {"decision": "cancel"})
            self.assertIsNone(submission["response_error"])
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_superseded_tui_response_retires_only_exact_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            control.response_submission = {
                "allowed": True,
                "root_thread_id": "thread-1",
                "response_disposition": "superseded",
            }
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    "req-1",
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "command": "ls"},
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

            self.assertEqual(gate._pending_server_request_ids, {})
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(
                len(control.calls_for("operation/request-response-submit")),
                1,
            )

    def test_accepted_tui_response_without_typed_disposition_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            control.response_submission = {
                "allowed": True,
                "root_thread_id": "thread-1",
            }
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    "req-1",
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "command": "ls"},
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

            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)
            self.assertEqual(backend_ws.sent, [])

    def test_exact_unknown_response_does_not_quarantine_unrelated_proxy_wire(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            control.response_submission = {
                "allowed": False,
                "response_disposition": "unknown",
            }
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    "req-1",
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "command": "ls"},
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

            self.assertEqual(gate._pending_server_request_ids, {})
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(
                len(control.calls_for("operation/request-response-submit")),
                1,
            )

    def test_invalid_tui_response_accepts_service_deferred_fail_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            control.invalid_response_outcome = {
                "action": "deferred",
                "response_disposition": "deferred",
            }
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_backend_message(
                self._request(
                    "req-1",
                    "item/commandExecution/requestApproval",
                    {"threadId": "thread-1", "command": "ls"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_client_message(
                json.dumps({"jsonrpc": "2.0", "id": "req-1", "result": "bad"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            invalid = control.calls_for("operation/request-response-invalid")[-1]
            self.assertEqual(invalid["response_token"], "response-token-1")
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_uncorrelated_tui_response_candidates_quarantine_both_sockets(self) -> None:
        malformed_candidates = (
            {"jsonrpc": "2.0", "result": {"decision": "cancel"}},
            {"jsonrpc": "2.0", "id": None, "result": {"decision": "cancel"}},
            {"jsonrpc": "2.0", "id": "", "result": {"decision": "cancel"}},
            {"jsonrpc": "2.0", "id": True, "result": {"decision": "cancel"}},
            {"jsonrpc": "2.0", "id": "req-2", "result": {"decision": "cancel"}},
        )
        for candidate in malformed_candidates:
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as tmpdir:
                control = self._FakeOperationControl()
                gate = self._gate(Path(tmpdir), control)
                client_ws = self._FakeWs()
                backend_ws = self._FakeWs()
                gate.handle_backend_message(
                    self._request(
                        "req-1",
                        "item/commandExecution/requestApproval",
                        {"threadId": "thread-1", "command": "ls"},
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                gate.handle_client_message(
                    json.dumps(candidate),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                self.assertTrue(client_ws.closed)
                self.assertTrue(backend_ws.closed)
                self.assertEqual(
                    control.calls_for("operation/request-response-submit"),
                    [],
                )

    def test_exact_malformed_tui_response_uses_service_fail_close_receipt(self) -> None:
        malformed_envelopes = (
            {"jsonrpc": "2.0", "id": "req-1", "method": None, "result": {}},
            {"jsonrpc": "2.0", "id": "req-1", "result": {}, "error": {}},
            {"jsonrpc": "2.0", "id": "req-1", "result": "cancel"},
        )
        for envelope in malformed_envelopes:
            with self.subTest(envelope=envelope), tempfile.TemporaryDirectory() as tmpdir:
                control = self._FakeOperationControl()
                gate = self._gate(Path(tmpdir), control)
                client_ws = self._FakeWs()
                backend_ws = self._FakeWs()
                gate.handle_backend_message(
                    self._request(
                        "req-1",
                        "item/commandExecution/requestApproval",
                        {"threadId": "thread-1", "command": "ls"},
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                gate.handle_client_message(
                    json.dumps(envelope),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                invalid = control.calls_for("operation/request-response-invalid")
                self.assertEqual(invalid[-1]["request_id"], "req-1")
                self.assertFalse(client_ws.closed)
                self.assertFalse(backend_ws.closed)
                self.assertEqual(
                    control.calls_for("operation/request-response-submit"),
                    [],
                )

    def test_numeric_and_string_server_request_ids_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id in (1, "1"):
                gate.handle_backend_message(
                    self._request(
                        request_id,
                        "item/commandExecution/requestApproval",
                        {"threadId": "thread-1", "command": "ls"},
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
            self.assertEqual(len(client_ws.sent), 2)

            for request_id in ("1", 1):
                gate.handle_client_message(
                    json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"decision": "cancel"}}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
            self.assertEqual(backend_ws.sent, [])
            submissions = control.calls_for("operation/request-response-submit")
            self.assertEqual([submission["request_id"] for submission in submissions], ["1", 1])

    def test_service_failure_closed_route_does_not_send_a_second_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.server_route = {"action": "fail_closed", "root_thread_id": "thread-1"}
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "ls"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(backend_ws.sent, [])

    def test_service_pre_send_route_quarantines_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.server_route = {"action": "quarantine", "root_thread_id": "thread-1"}
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "ls"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(client_ws.sent, [])

    def test_interactive_request_without_thread_id_is_service_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.server_route = {"action": "fail_closed", "root_thread_id": ""}
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "item/tool/requestUserInput",
                        "params": {"questions": []},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(backend_ws.sent, [])
            route = control.calls_for("operation/server-request")[-1]
            self.assertEqual(route["request_id"], "req-1")
            self.assertEqual(route["request_params"], {"questions": []})

    def test_dynamic_tool_call_is_never_passed_directly_to_an_observer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            control.server_route = {"action": "fail_closed", "root_thread_id": "thread-1"}
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_backend_message(
                self._request(
                    "dynamic-1",
                    "item/tool/call",
                    {"threadId": "thread-1", "callId": "call-1", "tool": "tool"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(client_ws.sent, [])
            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(control.calls_for("operation/server-request")[-1]["rpc_method"], "item/tool/call")

    def test_headerless_thread_start_response_is_settled_once_with_raw_result_and_exact_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            control = self._FakeOperationControl()
            gate = self._gate(data_dir, control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            params = {
                "model": "gpt-5.4",
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
                "permissions": ":full",
                "historyMode": "legacy",
                "baseInstructions": "base override",
                "developerInstructions": "developer override",
                "futureStartOption": {"enabled": True},
            }
            gate.handle_client_message(
                self._request(1, "thread/start", params),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            forwarded = self._decode_payload(backend_ws.sent[-1])
            self.assertEqual(forwarded["params"], {**params, "cwd": "/tmp/project"})

            response = {
                "id": 1,
                "result": {
                    "approvalsReviewer": "auto_review",
                    "thread": {
                        "id": "thread-1",
                        "historyMode": "legacy",
                        "status": {"type": "idle"},
                    },
                },
            }
            gate.handle_backend_message(
                json.dumps(response),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["request_id"], 1)
            self.assertEqual(outcomes[0]["request_token"], 1)
            self.assertEqual(outcomes[0]["outcome"], "success")
            self.assertEqual(outcomes[0]["response_result"], response["result"])
            self.assertEqual(self._decode_payload(client_ws.sent[-1]), response)
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_thread_start_auto_review_response_succeeds_without_closing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(
                    1,
                    "thread/start",
                    {"approvalsReviewer": "user"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            response = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "approvalsReviewer": "auto_review",
                    "thread": {"id": "thread-1", "status": {"type": "idle"}},
                },
            }
            gate.handle_backend_message(
                json.dumps(response),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcome = control.calls_for("operation/client-response")[-1]
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["response_result"], response["result"])
            self.assertEqual(self._decode_payload(client_ws.sent[-1]), response)
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_nonexclusive_resume_response_is_unknown_and_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws)

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"thread": {"id": "thread-1"}},
                        "error": {"code": -32000, "message": "ambiguous"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual(outcomes[-1]["request_id"], 1)
            self.assertEqual(outcomes[-1]["outcome"], "unknown")
            self.assertNotIn("response_result", outcomes[-1])
            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)

    def test_non_object_resume_error_is_unknown_and_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws)

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "error": "not-an-object"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(control.calls_for("operation/client-response")[-1]["outcome"], "unknown")
            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)

    def test_resume_result_for_different_thread_is_unknown_and_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws)

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"thread": {"id": "other-thread"}},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(control.calls_for("operation/client-response")[-1]["outcome"], "unknown")
            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)

    def test_typed_mismatched_resume_response_id_is_unknown_for_original_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws, request_id=1)

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "1",
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual(outcomes[-1]["request_id"], 1)
            self.assertEqual(outcomes[-1]["outcome"], "unknown")

    def test_headerless_typed_resume_error_is_known_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws)

            gate.handle_backend_message(
                json.dumps(
                    {
                        "id": 1,
                        "error": {"code": -32602, "message": "rejected"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcome = control.calls_for("operation/client-response")[-1]
            self.assertEqual(outcome["request_id"], 1)
            self.assertEqual(outcome["outcome"], "error")

    def test_headerless_resume_result_is_passed_once_raw_with_exact_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            self._admit_resume(gate, client_ws=client_ws, backend_ws=backend_ws)
            result = {
                "approvalsReviewer": "user",
                "thread": {"id": "thread-1", "status": {"type": "idle"}},
            }

            gate.handle_backend_message(
                json.dumps({"id": 1, "result": result}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual(len(outcomes), 1)
            outcome = outcomes[0]
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["response_result"], result)
            self.assertEqual(outcome["request_token"], 1)

    def test_loaded_resume_reviewer_mismatch_succeeds_without_closing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            params = {
                "threadId": "thread-1",
                "approvalsReviewer": "user",
            }
            gate.handle_client_message(
                self._request(1, "thread/resume", params),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            self.assertEqual(
                self._decode_payload(backend_ws.sent[-1])["params"],
                params,
            )

            response = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "approvalsReviewer": "auto_review",
                    "thread": {"id": "thread-1", "status": {"type": "idle"}},
                },
            }
            gate.handle_backend_message(
                json.dumps(response),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcome = control.calls_for("operation/client-response")[-1]
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["response_result"], response["result"])
            self.assertEqual(self._decode_payload(client_ws.sent[-1]), response)
            self.assertFalse(client_ws.closed)
            self.assertFalse(backend_ws.closed)

    def test_raw_goal_set_marks_continuation_risk_before_backend_forward(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            gate.handle_client_message(
                self._request(
                    1,
                    "thread/goal/set",
                    {"threadId": "thread-1", "objective": "continue the task"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            admission = control.calls_for("operation/admit")[-1]
            self.assertEqual(admission["rpc_method"], "thread/goal/set")
            self.assertEqual(admission["thread_id"], "thread-1")
            self.assertTrue(admission["continuation_risk"])
            self.assertEqual(len(backend_ws.sent), 1)

    def test_goal_set_response_requires_matching_typed_goal_before_known_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(
                    1,
                    "thread/goal/set",
                    {"threadId": "thread-1", "objective": "continue the task"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            # `ThreadGoalSetResponse.goal` must identify both its root and a
            # resulting status. A partial/mismatched payload cannot settle a
            # pre-start fence as a normal success.
            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"goal": {"status": "paused"}}}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            self.assertEqual(control.calls_for("operation/client-response")[-1]["outcome"], "unknown")

    def test_goal_set_typed_result_is_forwarded_for_causal_fence_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(
                    1,
                    "thread/goal/set",
                    {"threadId": "thread-1", "objective": "pause safely", "status": "paused"},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            result = {"goal": {"threadId": "thread-1", "status": "paused"}}

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcome = control.calls_for("operation/client-response")[-1]
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["response_result"], result)

    def test_goal_clear_response_requires_boolean_cleared_before_known_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(1, "thread/goal/clear", {"threadId": "thread-1"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"cleared": "true"}}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(control.calls_for("operation/client-response")[-1]["outcome"], "unknown")

    def test_goal_clear_false_is_forwarded_as_a_known_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(1, "thread/goal/clear", {"threadId": "thread-1"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            result = {"cleared": False}

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcome = control.calls_for("operation/client-response")[-1]
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["response_result"], result)

    def test_inline_review_response_cannot_report_a_detached_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(
                    1,
                    "review/start",
                    {"threadId": "thread-1", "target": {"type": "uncommittedChanges"}},
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_backend_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "turn": {"id": "review-turn", "status": "inProgress", "items": []},
                            "reviewThreadId": "detached-review-thread",
                        },
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(control.calls_for("operation/client-response")[-1]["outcome"], "unknown")

    def test_uncommitted_unknown_or_invalid_receipt_forwards_response_then_closes(self) -> None:
        receipts = (
            ("known-not-committed", ServiceControlKnownNotCommittedError("not committed")),
            ("outcome-unknown", RuntimeError("connection lost")),
            (
                "invalid-exact-receipt",
                {"known": True, "settled": False, "request_token": 1},
            ),
        )
        for label, receipt in receipts:
            with self.subTest(receipt=label), tempfile.TemporaryDirectory() as tmpdir:
                control = self._FakeOperationControl()
                control.client_response_receipt = receipt
                gate = self._gate(Path(tmpdir), control)
                client_ws = self._FakeWs()
                backend_ws = self._FakeWs()
                response = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "approvalsReviewer": "user",
                        "thread": {"id": "thread-1", "status": {"type": "idle"}},
                    },
                }

                gate.handle_client_message(
                    self._request(1, "thread/start", {}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                gate.handle_backend_message(
                    json.dumps(response),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                self.assertEqual(self._decode_payload(client_ws.sent[-1]), response)
                self.assertEqual(client_ws.events, ["send", "close"])
                self.assertEqual(backend_ws.events, ["send", "close"])
                self.assertTrue(client_ws.closed)
                self.assertTrue(backend_ws.closed)

    def test_reused_jsonrpc_id_stale_request_token_quarantines_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for thread_id in ("thread-1", "thread-2"):
                gate.handle_client_message(
                    self._request(7, "thread/start", {}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                if thread_id == "thread-2":
                    control.client_response_receipt = {
                        "known": True,
                        "settled": True,
                        "request_token": 1,
                    }
                gate.handle_backend_message(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 7,
                            "result": {
                                "approvalsReviewer": "user",
                                "thread": {"id": thread_id, "status": {"type": "idle"}},
                            },
                        }
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual([item["request_token"] for item in outcomes], [1, 2])
            self.assertEqual(client_ws.events, ["send", "send", "close"])
            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)
            forwarded_count = len(backend_ws.sent)
            gate.handle_client_message(
                self._request(8, "thread/start", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            self.assertEqual(len(backend_ws.sent), forwarded_count)

    def test_close_notifies_service_once_instead_of_releasing_local_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)

            gate.close()
            gate.close()

            self.assertEqual(len(control.calls_for("operation/participant-disconnected")), 1)

    def test_every_forwarded_request_reserves_its_exact_wire_id(self) -> None:
        scenarios = (
            (("model/list", {}), ("turn/start", {"threadId": "thread-1"}), 1),
            (("turn/start", {"threadId": "thread-1"}), ("model/list", {}), 1),
            (("model/list", {}), ("thread/list", {}), 1),
        )
        for first, second, expected_admissions in scenarios:
            with self.subTest(first=first[0], second=second[0]), tempfile.TemporaryDirectory() as tmpdir:
                control = self._FakeOperationControl()
                gate = self._gate(Path(tmpdir), control)
                client_ws = self._FakeWs()
                backend_ws = self._FakeWs()

                gate.handle_client_message(
                    self._request(1, first[0], first[1]),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                gate.handle_client_message(
                    self._request(1, second[0], second[1]),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                self.assertEqual(len(backend_ws.sent), 1)
                self.assertEqual(
                    len(control.calls_for("operation/admit")),
                    expected_admissions,
                )
                self.assertIn(
                    "复用了尚未完成",
                    self._decode_payload(client_ws.sent[-1])["error"]["message"],
                )

    def test_typed_distinct_passthrough_and_tracked_ids_settle_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(1, "model/list", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            gate.handle_client_message(
                self._request("1", "turn/start", {"threadId": "thread-1"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"data": []}}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
            self.assertEqual(control.calls_for("operation/client-response"), [])
            self.assertFalse(client_ws.closed)
            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"turn": {"id": "t1"}}}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            outcomes = control.calls_for("operation/client-response")
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["request_id"], "1")
            self.assertEqual(outcomes[0]["outcome"], "success")
            self.assertFalse(client_ws.closed)

    def test_headerless_bootstrap_responses_pass_through_and_release_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for method, result in (
                ("initialize", {"userAgent": "codex-cli/0.147.0"}),
                ("account/read", {"account": None, "requiresOpenaiAuth": False}),
            ):
                gate.handle_client_message(
                    self._request(1, method, {}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                gate.handle_backend_message(
                    json.dumps({"id": 1, "result": result}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            gate.handle_client_message(
                self._request(1, "thread/list", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(len(backend_ws.sent), 3)
            self.assertEqual(
                [self._decode_payload(message) for message in client_ws.sent],
                [
                    {"id": 1, "result": {"userAgent": "codex-cli/0.147.0"}},
                    {
                        "id": 1,
                        "result": {"account": None, "requiresOpenaiAuth": False},
                    },
                ],
            )
            self.assertEqual(control.calls_for("operation/client-response"), [])
            self.assertFalse(client_ws.closed)

    def test_unscoped_invalid_jsonrpc_ids_never_reach_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()

            for request_id in (True, {"id": 1}, [1]):
                gate.handle_client_message(
                    self._request(request_id, "model/list", {}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

            self.assertEqual(backend_ws.sent, [])
            self.assertEqual(len(client_ws.sent), 3)
            self.assertTrue(
                all(
                    "无效的 JSON-RPC id"
                    in self._decode_payload(item)["error"]["message"]
                    for item in client_ws.sent
                )
            )

    def test_any_uncorrelated_backend_response_closes_the_wire_epoch(self) -> None:
        invalid_responses = (
            "not-json",
            json.dumps({"jsonrpc": "2.0", "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hybrid", "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": "1", "result": {}}),
        )
        for response in invalid_responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmpdir:
                control = self._FakeOperationControl()
                gate = self._gate(Path(tmpdir), control)
                client_ws = self._FakeWs()
                backend_ws = self._FakeWs()
                gate.handle_client_message(
                    self._request(1, "turn/start", {"threadId": "thread-1"}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                gate.handle_backend_message(
                    response,
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

                self.assertTrue(client_ws.closed)
                self.assertTrue(backend_ws.closed)
                outcomes = control.calls_for("operation/client-response")
                self.assertEqual(outcomes[-1]["outcome"], "unknown")

    def test_unusable_exact_passthrough_response_closes_wire_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = self._FakeWs()
            gate.handle_client_message(
                self._request(1, "model/list", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            gate.handle_backend_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "error": "invalid"}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertTrue(client_ws.closed)
            self.assertTrue(backend_ws.closed)
            self.assertEqual(control.calls_for("operation/client-response"), [])

    def test_backend_send_failure_releases_passthrough_reservation(self) -> None:
        class _FailOnceWs(self._FakeWs):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = True

            def send(self, payload: str | bytes) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("send failed")
                super().send(payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            control = self._FakeOperationControl()
            gate = self._gate(Path(tmpdir), control)
            client_ws = self._FakeWs()
            backend_ws = _FailOnceWs()

            with self.assertRaisesRegex(RuntimeError, "send failed"):
                gate.handle_client_message(
                    self._request(1, "model/list", {}),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
            gate.handle_client_message(
                self._request(1, "model/list", {}),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

            self.assertEqual(len(backend_ws.sent), 1)



if __name__ == "__main__":
    unittest.main()
