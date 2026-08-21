import json
import os
import pathlib
from unittest.mock import patch

from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.fcodex.proxy import _ProxyInteractionGate
from bot.service_control_plane import (
    ServiceControlKnownNotCommittedError,
)
from bot.stores.interaction_lease_store import make_fcodex_interaction_holder
from bot.thread_runtime_authority import (
    ThreadResumeLocalFailurePolicy,
)
from bot.turn_interrupt_audit import record_turn_interrupt_dispatch_attempt

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerFcodexTests(CodexHandlerHarness):
    def test_fcodex_empty_interrupt_preserves_wire_audit_and_transport_order(
        self,
    ) -> None:
        handler, _ = self._make_handler()
        thread_id = "root-1"
        participant_id = "fcodex:test:interrupt-wire"
        connection_id = "connection-1"
        order: list[str] = []
        self._seed_authoritative_thread(handler, thread_id, status="active")

        class _Ws:
            def __init__(self, *, order_label: str | None = None) -> None:
                self.sent: list[str | bytes] = []
                self.order_label = order_label

            def send(self, payload: str | bytes) -> None:
                if self.order_label is not None:
                    order.append(self.order_label)
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            if method == "operation/admit":
                order.append("admission")
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id=participant_id,
            connection_id=connection_id,
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        handler._runtime_call(
            handler._fcodex_participant_runtime.retain_connection_source,
            participant_id,
            connection_id,
            thread_id,
        )
        client_ws = _Ws()
        backend_ws = _Ws(order_label="transport")
        exact_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn/interrupt",
            "params": {"threadId": thread_id, "turnId": ""},
        }

        def _record_audit(**kwargs: object) -> None:
            order.append("audit")
            record_turn_interrupt_dispatch_attempt(**kwargs)  # type: ignore[arg-type]

        with patch(
            "bot.fcodex.proxy.record_turn_interrupt_dispatch_attempt",
            side_effect=_record_audit,
        ):
            with self.assertLogs("bot.turn_interrupt_audit", level="INFO") as audit:
                gate.handle_client_message(
                    json.dumps(exact_request),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )
                gate.handle_client_message(
                    json.dumps(
                        {
                            **exact_request,
                            "id": 2,
                            "params": {
                                "threadId": thread_id,
                                "turnId": "",
                                "extra": True,
                            },
                        }
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

        self.assertEqual(
            order,
            ["admission", "audit", "transport", "admission"],
        )
        self.assertEqual(
            [json.loads(str(payload)) for payload in backend_ws.sent],
            [exact_request],
        )
        self.assertEqual(len(client_ws.sent), 1)
        denied = json.loads(str(client_ws.sent[0]))
        self.assertEqual(denied["id"], 2)
        self.assertIn("threadId", denied["error"]["message"])
        self.assertIn("turnId", denied["error"]["message"])
        self.assertEqual(len(audit.output), 1)
        self.assertIn("source=fcodex_endpoint", audit.output[0])
        self.assertNotIn(thread_id, audit.output[0])

    def test_fcodex_steer_preserves_exact_wire_shape_and_settles(self) -> None:
        handler, _ = self._make_handler()
        thread_id = "root-1"
        participant_id = "fcodex:test:steer-wire"
        connection_id = "connection-1"
        self._seed_authoritative_thread(handler, thread_id, status="active")

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        control_calls: list[tuple[str, dict]] = []

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            control_calls.append((method, dict(params)))
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id=participant_id,
            connection_id=connection_id,
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        holder = make_fcodex_interaction_holder(
            participant_id,
            connection_id=connection_id,
            owner_pid=os.getpid(),
        )
        blank = handler._interaction_lease_store.force_acquire(thread_id, holder)
        active = handler._interaction_lease_store.activate_turn(blank, "turn-1")
        self.assertIsNotNone(active)
        client_ws = _Ws()
        backend_ws = _Ws()
        exact_params = {
            "threadId": thread_id,
            "clientUserMessageId": "client-message-1",
            "input": [{"type": "text", "text": "shared wire steer"}],
            "responsesapiClientMetadata": None,
            "additionalContext": None,
            "expectedTurnId": "turn-1",
        }
        exact_request = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "turn/steer",
            "params": exact_params,
        }

        gate.handle_client_message(
            json.dumps(exact_request),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )
        gate.handle_client_message(
            json.dumps(
                {
                    **exact_request,
                    "id": 12,
                    "params": {**exact_params, "futureOverride": True},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(
            [json.loads(str(payload)) for payload in backend_ws.sent],
            [exact_request],
        )
        admissions = [
            params for method, params in control_calls if method == "operation/admit"
        ]
        self.assertEqual(admissions[0]["request_params"], exact_params)
        self.assertEqual(admissions[1]["request_params"]["futureOverride"], True)
        self.assertEqual(len(client_ws.sent), 1)
        denied = json.loads(str(client_ws.sent[0]))
        self.assertEqual(denied["id"], 12)
        self.assertIn("expectedTurnId", denied["error"]["message"])

        response = {
            "jsonrpc": "2.0",
            "id": 11,
            "result": {"turnId": "turn-1"},
        }
        gate.handle_backend_message(
            json.dumps(response),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(json.loads(str(client_ws.sent[-1])), response)
        self.assertEqual(
            self._fcodex_operation_service(handler)._client_requests,
            {},
        )
        self.assertEqual(handler._interaction_lease_store.load(thread_id), active)

    def test_fcodex_resume_local_failure_does_not_quarantine_other_mutations(self) -> None:
        handler, _ = self._make_handler()
        thread_id = "root-1"
        self._seed_authoritative_thread(handler, thread_id, status="idle")
        pending = handler._thread_runtime_authority.begin_resume_thread(thread_id)

        def _fail_local_commit() -> None:
            raise RuntimeError("owner commit failed")

        with self.assertRaises(RuntimeError):
            pending.commit_local_state(
                _fail_local_commit,
                failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            )

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:resume-recovery",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()

        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "mutation-after-local-failure",
                    "method": "thread/settings/update",
                    "params": {"threadId": thread_id, "model": "gpt-5.5"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(client_ws.sent, [])
        self.assertEqual(len(backend_ws.sent), 1)
        forwarded = json.loads(str(backend_ws.sent[0]))
        self.assertEqual(forwarded["method"], "thread/settings/update")

    def test_fcodex_admission_rejects_thread_spawn_before_proxy_forwards(self) -> None:
        """Authority rejects uncached ThreadSpawn targets before proxy send."""

        handler, _ = self._make_handler()

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:child-guard",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        client_ws = _Ws()
        backend_ws = _Ws()
        for request_id, (thread_id, parent_thread_id) in enumerate(
            (("child-with-parent", "root-1"), ("child-without-parent", None)),
            start=1,
        ):
            handler._adapter.thread_snapshots[(thread_id, False)] = ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id=thread_id,
                    cwd="/tmp/project",
                    name="child",
                    preview="",
                    created_at=1,
                    updated_at=1,
                    source="subAgent",
                    status="active",
                    parent_thread_id=parent_thread_id,
                    subagent_kind="threadSpawn",
                )
            )
            gate.handle_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "turn/start",
                        "params": {"threadId": thread_id, "input": []},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

        self.assertEqual(backend_ws.sent, [])
        self.assertEqual(len(client_ws.sent), 2)
        for request_id, payload in enumerate(client_ws.sent, start=1):
            response = json.loads(str(payload))
            self.assertEqual(response["id"], request_id)
            self.assertIn("ThreadSpawn", response["error"]["message"])
        self.assertEqual(
            handler._adapter.read_thread_calls,
            [
                {"thread_id": "child-with-parent", "include_turns": False},
                {"thread_id": "child-without-parent", "include_turns": False},
            ],
        )
        self.assertIsNone(handler._interaction_lease_store.load("child-with-parent"))
        self.assertIsNone(handler._interaction_lease_store.load("child-without-parent"))
        roots = self._fcodex_operation_service(handler)._direct_root_ids
        self.assertNotIn("child-with-parent", roots)
        self.assertNotIn("child-without-parent", roots)

    def test_fcodex_control_outage_quarantines_without_proxy_response(self) -> None:
        """A surviving proxy socket never becomes a second response owner."""

        handler, _ = self._make_handler()

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []
                self.closed = False

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

            def close(self) -> None:
                self.closed = True

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            if method == "operation/server-request":
                raise ServiceControlKnownNotCommittedError("service unavailable before dispatch")
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:durable-fallback",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()
        request_id = "proxy-fallback-request"

        gate.handle_backend_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "root-1", "command": "pwd"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(client_ws.sent, [])
        self.assertEqual(backend_ws.sent, [])
        self.assertTrue(client_ws.closed)
        self.assertTrue(backend_ws.closed)

    def test_fcodex_loaded_subagent_metadata_read_is_non_owning(self) -> None:
        """TUI child metadata backfill creates no ownership or runtime route."""

        handler, _ = self._make_handler()
        child = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=1,
            updated_at=1,
            source="subAgent",
            status="active",
            parent_thread_id="root-1",
            can_accept_direct_input=False,
            subagent_kind="threadSpawn",
        )
        handler._adapter.thread_snapshots[("child-1", False)] = ThreadSnapshot(summary=child)

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:loaded-child-read",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()

        # The unscoped inventory is already a reviewed discovery call.  The
        # next wire request is the actual upstream TUI backfill pattern.
        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "thread/loaded/list",
                    "params": {},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )
        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "thread/read",
                    # Upstream's serde omits a false `includeTurns`, so this
                    # is the real remote-TUI wire shape for the no-turn read.
                    "params": {"threadId": "child-1"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(client_ws.sent, [])
        self.assertEqual(
            [json.loads(str(payload))["method"] for payload in backend_ws.sent],
            ["thread/loaded/list", "thread/read"],
        )
        self.assertEqual(
            handler._adapter.read_thread_calls,
            [{"thread_id": "child-1", "include_turns": False}],
        )
        self.assertNotIn(
            "child-1",
            self._fcodex_operation_service(handler)._direct_root_ids,
        )
        self.assertIsNone(handler._interaction_lease_store.load("child-1"))
        self.assertFalse(handler._direct_thread_targets.is_known("child-1"))

        # A read-only child response goes straight back to the TUI.  It has no
        # operation/client-response outcome to settle and therefore cannot
        # alter the root's writer state after the fact.
        gate.handle_backend_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"thread": {"id": "child-1"}},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )
        self.assertEqual(len(client_ws.sent), 1)
        self.assertEqual(json.loads(str(client_ws.sent[0]))["id"], 2)
        self.assertNotIn(
            "child-1",
            self._fcodex_operation_service(handler)._direct_root_ids,
        )
        self.assertIsNone(handler._interaction_lease_store.load("child-1"))

        malformed = ThreadSummary(
            thread_id="child-malformed",
            cwd="/tmp/project",
            name="malformed child",
            preview="",
            created_at=1,
            updated_at=1,
            source="subAgent",
            source_status="malformed",
            status="active",
            parent_thread_id="root-1",
        )
        handler._adapter.thread_snapshots[("child-malformed", False)] = ThreadSnapshot(
            summary=malformed
        )
        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "thread/read",
                    "params": {"threadId": "child-malformed"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )
        self.assertEqual(
            [json.loads(str(payload))["method"] for payload in backend_ws.sent],
            ["thread/loaded/list", "thread/read"],
        )
        self.assertIn("malformed source", str(client_ws.sent[-1]))

    def test_fcodex_child_metadata_exception_rejects_history_and_other_reads(self) -> None:
        """Only the exact no-turn metadata read may cross the child boundary."""

        handler, _ = self._make_handler()
        child = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=1,
            updated_at=1,
            source="subAgent",
            status="active",
            parent_thread_id="root-1",
            subagent_kind="threadSpawn",
        )
        handler._adapter.thread_snapshots[("child-1", False)] = ThreadSnapshot(summary=child)

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:child-read-strict",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()

        for request_id, method, request_params in (
            (1, "thread/read", {"threadId": "child-1", "includeTurns": True}),
            (2, "thread/goal/get", {"threadId": "child-1"}),
        ):
            gate.handle_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": request_params,
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

        self.assertEqual(backend_ws.sent, [])
        self.assertEqual(len(client_ws.sent), 2)
        for request_id, payload in enumerate(client_ws.sent, start=1):
            response = json.loads(str(payload))
            self.assertEqual(response["id"], request_id)
            self.assertIn("ThreadSpawn", response["error"]["message"])
        self.assertEqual(
            handler._adapter.read_thread_calls,
            [
                {"thread_id": "child-1", "include_turns": False},
                {"thread_id": "child-1", "include_turns": False},
            ],
        )
        self.assertNotIn(
            "child-1",
            self._fcodex_operation_service(handler)._direct_root_ids,
        )
        self.assertIsNone(handler._interaction_lease_store.load("child-1"))

    def test_fcodex_proxy_rejects_fork_before_backend_forward(self) -> None:
        """Fork is a deliberate product non-goal, not a temporary root RPC."""

        handler, _ = self._make_handler()
        handler._adapter.thread_snapshots[("root-1", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="root-1",
                cwd="/tmp/project",
                name="root",
                preview="",
                created_at=1,
                updated_at=1,
                source="appServer",
                status="idle",
            )
        )

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:fork-denied",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()

        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "thread/fork",
                    "params": {"threadId": "root-1"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(backend_ws.sent, [])
        self.assertEqual(len(client_ws.sent), 1)
        response = json.loads(str(client_ws.sent[0]))
        self.assertEqual(response["id"], 1)
        self.assertIn("thread/fork", response["error"]["message"])
        self.assertIsNone(handler._interaction_lease_store.load("root-1"))

    def test_fcodex_admission_fails_closed_when_authority_target_read_is_unusable(self) -> None:
        """A failed or mismatched point read cannot fall back to cache state."""

        handler, _ = self._make_handler()

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:unusable-target-read",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        client_ws = _Ws()
        backend_ws = _Ws()
        handler._adapter.thread_snapshots[("unavailable", False)] = RuntimeError("backend unavailable")
        handler._adapter.thread_snapshots[("mismatch", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="other-thread",
                cwd="/tmp/project",
                name="other",
                preview="",
                created_at=1,
                updated_at=1,
                source="appServer",
                status="idle",
            )
        )

        with self.assertLogs("bot.focus_runtime", level="WARNING") as logged:
            for request_id, thread_id in enumerate(("unavailable", "mismatch"), start=1):
                gate.handle_client_message(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "turn/start",
                            "params": {"threadId": thread_id, "input": []},
                        }
                    ),
                    client_ws=client_ws,
                    backend_ws=backend_ws,
                )

        self.assertEqual(backend_ws.sent, [])
        self.assertEqual(len(client_ws.sent), 2)
        for payload in client_ws.sent:
            response = json.loads(str(payload))
            self.assertEqual(response["error"]["code"], -32002)
        self.assertEqual(len(logged.output), 1)
        self.assertIn("Unable to authority-read fcodex direct target", logged.output[0])
        roots = self._fcodex_operation_service(handler)._direct_root_ids
        self.assertNotIn("unavailable", roots)
        self.assertNotIn("mismatch", roots)

    def test_fcodex_admission_preserves_non_thread_spawn_direct_targets(self) -> None:
        """Only ThreadSpawn ancestry is parent-owned for direct fcodex use."""

        handler, _ = self._make_handler()

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:auxiliary-guard",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        client_ws = _Ws()
        backend_ws = _Ws()
        for request_id, subagent_kind in enumerate(("auxiliary", "review", "guardian"), start=1):
            thread_id = f"{subagent_kind}-1"
            handler._adapter.thread_snapshots[(thread_id, False)] = ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id=thread_id,
                    cwd="/tmp/project",
                    name=subagent_kind,
                    preview="",
                    created_at=1,
                    updated_at=1,
                    source="subAgent",
                    status="idle",
                    parent_thread_id="root-1",
                    subagent_kind=subagent_kind,
                )
            )
            gate.handle_client_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "thread/name/set",
                        "params": {"threadId": thread_id, "name": f"{subagent_kind} renamed"},
                    }
                ),
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

        self.assertEqual(client_ws.sent, [])
        self.assertEqual(
            [json.loads(str(payload))["params"]["threadId"] for payload in backend_ws.sent],
            ["auxiliary-1", "review-1", "guardian-1"],
        )
        self.assertEqual(
            handler._adapter.read_thread_calls,
            [
                {"thread_id": "auxiliary-1", "include_turns": False},
                {"thread_id": "review-1", "include_turns": False},
                {"thread_id": "guardian-1", "include_turns": False},
            ],
        )
        for thread_id in ("auxiliary-1", "review-1", "guardian-1"):
            self.assertIn(
                thread_id,
                self._fcodex_operation_service(handler)._direct_root_ids,
            )

    def test_fcodex_no_goal_resume_remains_an_observer_subscription(self) -> None:
        handler, _ = self._make_handler()
        root = ThreadSummary(
            thread_id="root-1",
            cwd="/tmp/project",
            name="root",
            preview="",
            created_at=1,
            updated_at=1,
            source="appServer",
            status="idle",
        )
        handler._adapter.thread_snapshots[("root-1", False)] = ThreadSnapshot(summary=root)

        class _Ws:
            def __init__(self) -> None:
                self.sent: list[str | bytes] = []

            def send(self, payload: str | bytes) -> None:
                self.sent.append(payload)

        def _control(_data_dir: pathlib.Path, method: str, params: dict) -> dict:
            return handler._handle_service_control_request(method, params)

        gate = _ProxyInteractionGate(
            cwd="/tmp/project",
            data_dir=handler._data_dir,
            participant_id="fcodex:test:no-goal-resume",
            connection_id="connection-1",
            control_request_fn=_control,
        )
        self.addCleanup(gate.close)
        client_ws = _Ws()
        backend_ws = _Ws()
        gate.handle_client_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "thread/resume",
                    "params": {"threadId": "root-1"},
                }
            ),
            client_ws=client_ws,
            backend_ws=backend_ws,
        )

        self.assertEqual(len(backend_ws.sent), 1)
        self.assertIsNone(handler._interaction_lease_store.load("root-1"))
