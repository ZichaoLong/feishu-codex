"""Owner-level fcodex admission, reconnect, routing, and release regressions."""

from __future__ import annotations

import inspect as inspect
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot.adapters.base import ThreadSummary as ThreadSummary
from bot.direct_thread_target_policy import (
    DirectThreadTargetPolicyError as DirectThreadTargetPolicyError,
)
from bot.fcodex.interaction_contract import (
    fcodex_client_request_key as fcodex_client_request_key,
)
from bot.fcodex.participant_runtime_registry import (
    FcodexParticipantRuntimeRegistry,
    FcodexParticipantRuntimeRegistryPorts,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.operation_owner_coordinator import OperationOwnerCoordinator
from bot.reason_codes import ReasonedCheck
from bot.process_utils import process_identity
from bot.runtime_loop import RuntimeLoopContextError as RuntimeLoopContextError
from bot.server_request_contract import ServerRequestIdentity
from bot.server_request_registry import ServerRequestRegistry
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder as make_fcodex_interaction_holder,
    make_web_interaction_holder as make_web_interaction_holder,
)
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)
from bot.thread_runtime_authority import ThreadRuntimeAuthority


def _server_request_identity(request_id, thread_id="root-1", command="ls"):
    return ServerRequestIdentity(
        request_id=request_id,
        connection_generation=1,
        method="item/commandExecution/requestApproval",
        params={"threadId": thread_id, "command": command},
    )


def _service_server_request(coordinator, request_id, thread_id="root-1", command="ls"):
    return coordinator.service_server_request(
        _server_request_identity(request_id, thread_id, command)
    )


class FcodexOperationHarness(unittest.TestCase):
    participant_id = "fcodex:alice:incarnation-1"


    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temporary_directory.name)
        self.interaction_leases = InteractionLeaseStore(self.data_dir)
        self.runtime_leases = ThreadRuntimeLeaseStore(self.data_dir)
        self.server_requests = ServerRequestRegistry(resolved_limit=512)
        self.server_requests.activate_connection_epoch(1)
        self.responses: list[tuple[int | str, dict | None, dict | None]] = []
        self.owner_changes: list[tuple[str, str]] = []
        self.participant_expiries: list[tuple[str, int, float]] = []
        self.proxy_delivery_expiries: list[tuple[str, int, float]] = []
        self.connection_expiries: list[tuple[str, str, int, float]] = []
        self.participant_schedule_error: Exception | None = None
        self.respond_error: Exception | None = None
        self._admitted_requests: dict[
            tuple[str, str, str], tuple[int, str, str]
        ] = {}
        self.effective_settings = ThreadEffectiveSettingsRegistry()
        self.participant_runtime = self._make_participant_runtime()
        self.thread_runtime_authority = ThreadRuntimeAuthority(
            adapter=Mock(),
            effective_settings=self.effective_settings,
            acquire_runtime_lease=Mock(
                side_effect=AssertionError(
                    "external fcodex create must use the Registry holder"
                )
            ),
            release_runtime_lease=Mock(),
            resume_failure_known_no_effect=lambda _exc: False,
        )
        self.coordinator = OperationOwnerCoordinator(
            interaction_lease_store=self.interaction_leases,
            participant_runtime_registry=self.participant_runtime,
            external_thread_create_authority=self.thread_runtime_authority,
            effective_settings=self.effective_settings,
            server_request_is_resolved=(
                self.server_requests.request_is_resolved
            ),
            server_request_response_authority_is_revoked=(
                self.server_requests.request_response_authority_is_revoked
            ),
            runtime_context_guard=lambda: None,
            respond=self._respond,
            schedule_proxy_delivery_expiry=lambda request_key, generation, delay: self.proxy_delivery_expiries.append(
                (request_key, generation, delay)
            ),
            owner_changed=lambda thread_id, reason: self.owner_changes.append((thread_id, reason)),
        )
        self.operation_service = self.coordinator._operation_service
        for thread_id in ("root-1", "root-2"):
            self.coordinator.remember_authoritative_direct_target(
                ThreadSummary(
                    thread_id,
                    "/repo",
                    "",
                    "",
                    0,
                    0,
                    "cli",
                    "idle",
                ),
                expected_thread_id=thread_id,
                operation="test admission",
            )

    def _make_participant_runtime(self) -> FcodexParticipantRuntimeRegistry:
        return FcodexParticipantRuntimeRegistry(
            ports=FcodexParticipantRuntimeRegistryPorts(
                thread_runtime_lease_store=self.runtime_leases,
                runtime_holder_for_participant=self._runtime_holder,
                global_loaded_gate=lambda _thread_id: ReasonedCheck.allow(),
                schedule_participant_expiry=self._schedule_participant_expiry,
                schedule_connection_expiry=lambda participant_id, connection_id, generation, delay: self.connection_expiries.append(
                    (participant_id, connection_id, generation, delay)
                ),
            ),
            runtime_context_guard=lambda: None,
            disconnect_grace_seconds=15,
            connection_heartbeat_timeout_seconds=12,
        )

    def _schedule_participant_expiry(
        self,
        participant_id: str,
        generation: int,
        delay: float,
    ) -> None:
        if self.participant_schedule_error is not None:
            raise self.participant_schedule_error
        self.participant_expiries.append((participant_id, generation, delay))

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _runtime_holder(self, participant_id: str) -> ThreadRuntimeLeaseHolder:
        return ThreadRuntimeLeaseHolder(
            holder_id=participant_id,
            holder_type="fcodex",
            instance_name="test",
            owner_pid=os.getpid(),
            owner_process_identity=process_identity(os.getpid()),
            owner_service_token="service-token",
            control_endpoint="tcp://127.0.0.1:1",
            backend_url="ws://127.0.0.1:2",
            updated_at=time.time(),
        )

    def _respond(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        result: dict | None = None,
        error: dict | None = None,
    ) -> None:
        if self.respond_error is not None:
            raise self.respond_error
        if connection_generation <= 0:
            raise AssertionError("test responder requires an exact connection generation")
        self.responses.append((request_id, result, error))

    def _connect(
        self,
        connection_id: str = "connection-a",
        *,
        participant_id: str | None = None,
    ) -> dict:
        return self.coordinator.participant_connected(
            participant_id or self.participant_id,
            connection_id,
        )

    def _admit(
        self,
        *,
        participant_id: str | None = None,
        connection_id: str = "connection-a",
        request_id: int | str = 1,
        method: str = "turn/start",
        thread_id: str = "root-1",
        request_params: object = None,
        resume_may_autostart: bool = False,
        continuation_risk: bool = False,
    ) -> dict:
        exact_participant_id = participant_id or self.participant_id
        decision = self.coordinator.admit(
            participant_id=exact_participant_id,
            connection_id=connection_id,
            request_id=request_id,
            method=method,
            thread_id=thread_id,
            request_params=request_params,
            resume_may_autostart=resume_may_autostart,
            continuation_risk=continuation_risk,
        )
        request_token = decision.get("request_token")
        if (
            decision.get("allowed")
            and isinstance(request_token, int)
            and not isinstance(request_token, bool)
            and request_token > 0
        ):
            self._admitted_requests[
                (exact_participant_id, connection_id, jsonrpc_id_key(request_id))
            ] = (request_token, method, thread_id)
        return decision

    def _client_response(self, **kwargs) -> dict:
        """Send the exact admission capability through the strict control contract."""

        participant_id = str(kwargs.get("participant_id", self.participant_id) or "")
        connection_id = str(kwargs.get("connection_id", "connection-a") or "")
        kwargs.setdefault("participant_id", participant_id)
        kwargs.setdefault("connection_id", connection_id)
        request_id = kwargs.get("request_id")
        admitted = self._admitted_requests.get(
            (participant_id, connection_id, jsonrpc_id_key(request_id))
        )
        if admitted is None:
            raise AssertionError("test response has no exact tracked admission")
        request_token, method, thread_id = admitted
        kwargs.setdefault("request_token", request_token)
        if kwargs.get("outcome") == "success" and method == "thread/start":
            observed_thread_id = kwargs.setdefault(
                "observed_thread_id",
                f"created-{connection_id}-{jsonrpc_id_key(request_id)}",
            )
            kwargs.setdefault("observed_root_thread_id", observed_thread_id)
        if kwargs.get("outcome") == "success" and method == "thread/resume":
            kwargs.setdefault("observed_thread_id", thread_id)
            kwargs.setdefault("observed_root_thread_id", thread_id)
        return self.coordinator.client_response(**kwargs)

    def _seed_fcodex_active_lease(
        self,
        connection_id: str = "connection-a",
    ) -> None:
        blank = self.interaction_leases.force_acquire(
            "root-1",
            make_fcodex_interaction_holder(
                self.participant_id,
                connection_id=connection_id,
                owner_pid=os.getpid(),
            ),
        )
        self.assertEqual(blank.turn_id, "")
        self.coordinator.notification(
            "turn/started",
            {"threadId": "root-1", "turn": {"id": "turn-1"}},
        )
        active = self.interaction_leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active and active.turn_id, "turn-1")

    def _settle_ordinary_turn_start(
        self,
        *,
        connection_id: str = "connection-a",
        request_id: int | str = 1,
        outcome: str = "success",
    ) -> dict:
        admitted = self._admit(
            connection_id=connection_id,
            request_id=request_id,
            method="turn/start",
        )
        self.assertTrue(admitted["allowed"])
        return self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id=connection_id,
            request_id=request_id,
            request_token=admitted["request_token"],
            outcome=outcome,
            response_result={"turn": {"id": "submission-1"}},
        )

    def _forget_connection_runtime_source(
        self,
        thread_id: str = "root-1",
        connection_id: str = "connection-a",
    ) -> None:
        self.assertTrue(
            self.participant_runtime.forget_connection_source(
                self.participant_id,
                connection_id,
                thread_id,
            )
        )
