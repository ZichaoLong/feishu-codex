from __future__ import annotations

import os
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.adapter_ingress_gate import AdapterOutboundRequestBlocked
from bot.adapters.base import ThreadSummary
from bot.focus_runtime.runtime import FocusRuntime as CodexHandler
from bot.focus_runtime.feishu_surface import FeishuSurface
from bot.codex_protocol.client import CodexRpcPreSendError, CodexRpcTransportError
from bot.feishu_execution_queue import FeishuBindingExecutionSnapshot
from bot.stores.chat_binding_store import ChatBindingStore
from bot.service_runtime_lifecycle import ServiceRuntimePhase
from bot.stores.interaction_lease_store import (
    InteractionLease,
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)
from tests.focus_runtime.codex_handler_fakes import (
    _FakeAdapter,
    _FakeBot,
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_test_harness import _admit_adapter_connection


class _EpochGuardedFakeAdapter(_FakeAdapter):
    """Exercise the production adapter's issue/confirm composition in Handler tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ordinary_transport_calls: list[str] = []
        self.existing_authority_calls: list[str] = []

    def _ordinary(self, method: str, operation):
        try:
            permit = self.issue_outbound_request(method)
        except Exception as exc:
            raise CodexRpcPreSendError(method, exc) from exc
        self.ordinary_transport_calls.append(method)
        result = operation()
        try:
            self.confirm_outbound_request(permit)
        except Exception as exc:
            raise CodexRpcTransportError(
                method,
                {"code": -32000, "message": "backend epoch changed"},
            ) from exc
        return result

    def list_threads_all(self, **kwargs):
        return self._ordinary(
            "thread/list",
            lambda: _FakeAdapter.list_threads_all(self, **kwargs),
        )

    def create_thread(self, **kwargs):
        return self._ordinary(
            "thread/start",
            lambda: _FakeAdapter.create_thread(self, **kwargs),
        )

    def resume_thread(self, thread_id: str, **kwargs):
        return self._ordinary(
            "thread/resume",
            lambda: _FakeAdapter.resume_thread(self, thread_id, **kwargs),
        )

    def start_turn(self, **kwargs):
        return self._ordinary(
            "turn/start",
            lambda: _FakeAdapter.start_turn(self, **kwargs),
        )

    def respond(self, request_id: str, *, result=None, error=None, **kwargs) -> None:
        return self._ordinary(
            "serverRequest/response",
            lambda: _FakeAdapter.respond(
                self,
                request_id,
                result=result,
                error=error,
                **kwargs,
            ),
        )

    def respond_with_existing_backend_authority(
        self,
        request_id: str,
        *,
        connection_generation: int,
        result=None,
        error=None,
        timeout: float | None = None,
    ) -> None:
        self.existing_authority_calls.append(request_id)
        _FakeAdapter.respond(
            self,
            request_id,
            connection_generation=connection_generation,
            result=result,
            error=error,
            timeout=timeout,
            require_existing_connection=True,
        )


class CodexHandlerStartupRuntimeTests(unittest.TestCase):
    def _make_handler(
        self,
        data_dir: pathlib.Path,
        *,
        adapter_type: type[_FakeAdapter] = _FakeAdapter,
    ) -> tuple[CodexHandler, _FakeBot]:
        patches = (
            patch(
                "bot.focus_runtime.runtime.load_config_file",
                return_value={"mirror_watchdog_seconds": 999999},
            ),
            patch("bot.focus_runtime.runtime.CodexAppServerAdapter", adapter_type),
            patch.dict(
                os.environ,
                {
                    "FOCUS_GLOBAL_DATA_DIR": str(data_dir / "_global"),
                    "FOCUS_INSTANCE": "default",
                },
                clear=False,
            ),
        )
        for active_patch in patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)
        handler = CodexHandler(data_dir=data_dir)
        handler._service_runtime_lifecycle._set_phase(ServiceRuntimePhase.ACTIVE)
        handler._service_instance_lease._owner_token = (
            "test-unstarted-service-generation"
        )
        bot = _FakeBot(data_dir)
        handler._feishu_platform.attach(bot)
        self.addCleanup(handler.shutdown)
        return handler, bot

    @staticmethod
    def _register_handler(handler: CodexHandler, bot: _FakeBot) -> None:
        handler._service_runtime_lifecycle._set_phase(ServiceRuntimePhase.ASSEMBLED)
        handler.start(bot)

    @staticmethod
    def _seed_current_process_interaction_leases(
        handler: CodexHandler,
    ) -> dict[str, InteractionLease]:
        owner_pid = os.getpid()
        holders = {
            "thread-reset-feishu": make_feishu_interaction_holder(
                "reset-sender",
                "reset-chat",
                owner_pid=owner_pid,
            ),
            "thread-reset-web": make_web_interaction_holder(
                "reset-client",
                owner_pid=owner_pid,
            ),
            "thread-reset-fcodex": make_fcodex_interaction_holder(
                "fcodex:reset-participant",
                connection_id="reset-connection",
                owner_pid=owner_pid,
            ),
        }
        return {
            thread_id: handler._interaction_lease_store.force_acquire(
                thread_id,
                holder,
            )
            for thread_id, holder in holders.items()
        }

    def test_service_adapter_uses_the_single_backend_epoch_admission(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _bot = self._make_handler(pathlib.Path(tempdir.name))

        permit = handler._adapter.issue_outbound_request("thread/list")
        handler._adapter.confirm_outbound_request(permit)
        handler._adapter_ingress_gate.fence_backend_reset()

        with self.assertRaises(AdapterOutboundRequestBlocked):
            handler._adapter.issue_outbound_request("thread/list")

    def test_failed_reset_blocks_all_ordinary_surfaces_before_transport(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _bot = self._make_handler(
            pathlib.Path(tempdir.name),
            adapter_type=_EpochGuardedFakeAdapter,
        )
        adapter = handler._adapter

        with (
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
                side_effect=RuntimeError("replacement publication failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "replacement publication failed"),
        ):
            handler._runtime_call(handler._reset_current_instance_backend, False)

        self.assertTrue(
            handler._adapter_ingress_gate.snapshot().backend_reset_blocked
        )

        # Feishu cold prompt and Web draft creation share the same guarded
        # thread/start boundary even though they have different local owners.
        with self.assertLogs("bot.feishu_prompt_failure_presentation", level="ERROR"):
            handler.handle_message("ou-user", "chat-feishu", "must not run")
        handler._runtime_call(handler._web_runtime.client_connected, "web-document")
        with self.assertRaises(RuntimeError):
            handler._runtime_call(
                handler._web_runtime.start_thread,
                "web-document",
                text="must not run",
                cwd="/tmp",
            )

        # Read and resume paths must be closed too; a ready replacement socket
        # is not usable until publication atomically reopens this epoch.
        with self.assertRaises(AdapterOutboundRequestBlocked):
            handler._runtime_call(
                handler._web_runtime.list_threads,
                client_id="web-document",
            )
        with self.assertRaises(CodexRpcPreSendError):
            handler._runtime_call(
                handler._thread_runtime_authority.begin_resume_thread,
                "thread-known",
            )
        with self.assertRaises(CodexRpcPreSendError):
            handler._runtime_call(
                handler._thread_runtime_authority.start_turn,
                thread_id="thread-known",
                input_items=[{"type": "text", "text": "must not run"}],
            )
        with self.assertRaises(CodexRpcPreSendError):
            adapter.respond(
                "request-ordinary",
                connection_generation=1,
                result={"decision": "accept"},
            )

        self.assertEqual(adapter.ordinary_transport_calls, [])
        self.assertEqual(adapter.create_thread_calls, [])
        self.assertEqual(adapter.resume_thread_calls, [])
        self.assertEqual(adapter.start_turn_calls, [])
        self.assertEqual(adapter.respond_calls, [])

        # An already-admitted server-request response keeps its bounded
        # old-socket capability. It does not reopen ordinary traffic or
        # transfer main-turn ownership.
        adapter.respond_with_existing_backend_authority(
            "request-stop-settlement",
            connection_generation=1,
            error={"code": -1, "message": "stopped"},
        )
        self.assertEqual(
            adapter.existing_authority_calls,
            ["request-stop-settlement"],
        )
        self.assertEqual(
            [call["request_id"] for call in adapter.respond_calls],
            ["request-stop-settlement"],
        )

    def test_reset_discards_detached_binding_prompt_fifo(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _bot = self._make_handler(pathlib.Path(tempdir.name))
        binding = ("ou_user", "c1")
        thread = ThreadSummary(
            thread_id="thread-before-reset",
            cwd="/tmp/project",
            name="before reset",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, thread)
        handler._runtime_call(
            handler._feishu_execution_queue.enqueue_prompt,
            FeishuBindingExecutionSnapshot(
                binding=binding,
                root_thread_id=thread.thread_id,
                active=True,
                attached=True,
                has_inflight_execution=True,
                current_turn_id="turn-before-reset",
            ),
            sender_id=binding[0],
            chat_id=binding[1],
            message_id="queued-before-reset",
            text="must-not-run",
            input_items=({"type": "text", "text": "must-not-run"},),
        )
        with patch.object(
            handler._service_runtime_authority,
            "register_instance_runtime",
        ):
            result = handler._runtime_call(
                handler._reset_current_instance_backend,
                False,
            )

        self.assertEqual(result["detached_binding_ids"], ["p2p:ou_user:c1"])
        queue_snapshot = handler._runtime_call(
            handler._feishu_execution_queue.snapshot,
            binding,
        )
        self.assertFalse(queue_snapshot.has_pending_or_draining)
        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)
        self.assertEqual(handler._adapter.start_turn_calls, [])

    def test_reset_discards_orphan_queue_key_absent_from_binding_inventory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _bot = self._make_handler(pathlib.Path(tempdir.name))
        orphan_binding = ("orphan-user", "orphan-chat")
        handler._runtime_call(
            handler._feishu_execution_queue.enqueue_prompt,
            FeishuBindingExecutionSnapshot(
                binding=orphan_binding,
                root_thread_id="orphan-thread-before-reset",
                active=True,
                attached=True,
                has_inflight_execution=True,
                current_turn_id="orphan-turn-before-reset",
            ),
            sender_id=orphan_binding[0],
            chat_id=orphan_binding[1],
            message_id="orphan-before-reset",
            text="must-not-run",
            input_items=({"type": "text", "text": "must-not-run"},),
        )
        with handler._lock:
            self.assertNotIn(
                orphan_binding,
                handler._binding_runtime.binding_keys_locked(),
            )
        with patch.object(
            handler._service_runtime_authority,
            "register_instance_runtime",
        ):
            handler._runtime_call(
                handler._reset_current_instance_backend,
                False,
            )

        queue_snapshot = handler._runtime_call(
            handler._feishu_execution_queue.snapshot,
            orphan_binding,
        )
        self.assertFalse(queue_snapshot.has_pending_or_draining)
        handler._runtime_call(
            handler._feishu_execution_queue_service.drain,
            orphan_binding,
        )
        self.assertEqual(handler._adapter.start_turn_calls, [])



    def test_runtime_lease_restore_composition_runs_on_runtime_loop(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir)
        observed_workers: list[threading.Thread] = []
        original_restore = handler._restore_service_thread_runtime_leases

        def restore_runtime_leases() -> None:
            handler._runtime_loop.assert_worker_context()
            observed_workers.append(threading.current_thread())
            original_restore()

        handler._restore_service_thread_runtime_leases = restore_runtime_leases

        self._register_handler(handler, bot)

        self.assertEqual(observed_workers, [handler._runtime_loop._worker])

    def test_is_sender_active_hydrates_binding_on_runtime_loop(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou_user", "chat-a")
        thread_id = "thread-1"
        ChatBindingStore(data_dir).save(
            binding,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": thread_id,
                "current_thread_title": "demo",
                "feishu_runtime_state": "attached",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "",
                "reasoning_effort": "",
            },
        )
        handler, _bot = self._make_handler(data_dir)
        observed_workers: list[threading.Thread] = []
        original_owner_loss = handler._feishu_root_operations.settle_owner_loss

        def owner_loss(loss):
            handler._runtime_loop.assert_worker_context()
            observed_workers.append(threading.current_thread())
            return original_owner_loss(loss)

        handler._feishu_root_operations.settle_owner_loss = owner_loss

        self.assertFalse(handler.is_sender_active(binding[0], binding[1]))

        self.assertEqual(observed_workers, [handler._runtime_loop._worker])
        stored = handler._chat_binding_store.load(binding)
        self.assertIsNotNone(stored)
        self.assertEqual(stored and stored["feishu_runtime_state"], "detached")

    def test_only_pure_fast_ack_card_actions_bypass_runtime_call(self) -> None:
        self.assertTrue(
            FeishuSurface.should_bypass_runtime_for_card_action(
                {"action": "attach_runtime"}
            )
        )
        self.assertTrue(
            FeishuSurface.should_bypass_runtime_for_card_action(
                {
                    "action": "goal_apply_confirm",
                    "status": "active",
                    "objective": "",
                }
            )
        )
        self.assertFalse(
            FeishuSurface.should_bypass_runtime_for_card_action(
                {"action": "resume_thread"}
            )
        )
        self.assertFalse(
            FeishuSurface.should_bypass_runtime_for_card_action(
                {"action": "goal_resume"}
            )
        )

    def test_stale_goal_confirmation_cannot_read_or_mutate_new_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou_user", "chat-a")
        ChatBindingStore(data_dir).save(
            binding,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": "thread-current",
                "current_thread_title": "current",
                "feishu_runtime_state": "detached",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "",
                "reasoning_effort": "",
            },
        )
        handler, bot = self._make_handler(data_dir)

        handler._runtime_call(
            handler._goal_domain.resume_goal_on_runtime,
            binding[0],
            binding[1],
            "thread-stale",
            True,
            "msg-1",
        )

        self.assertEqual(handler._adapter.read_thread_calls, [])
        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.update_thread_settings_calls, [])
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])
        self.assertTrue(bot.cards)
        self.assertIn("已过期", bot.cards[-1][1]["elements"][0]["content"])


    def test_reset_backend_closes_fcodex_epoch_before_reuse(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))
        participant_id = "fcodex:test:reset-incarnation"
        connection_id = "reset-connection"
        thread_id = "thread-fcodex-before-backend-reset"
        handler._runtime_call(
            handler._fcodex_participant_runtime.connect,
            participant_id,
            connection_id,
        )
        handler._runtime_call(
            handler._fcodex_participant_runtime.retain_connection_source,
            participant_id,
            connection_id,
            thread_id,
        )
        self.assertIsNotNone(handler._thread_runtime_lease_store.load(thread_id))

        old_generation = handler._adapter.connection_generation_value
        original_start = handler._adapter.start

        def start_replacement() -> None:
            original_start()
            handler._adapter.connection_generation_value = old_generation + 1

        handler._adapter.start = start_replacement
        with (
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ),
            patch.object(
                handler._operation_owner,
                "close_backend_epoch_after_machine_replace",
                wraps=(
                    handler._operation_owner.close_backend_epoch_after_machine_replace
                ),
            ) as close_fcodex_epoch,
        ):
            result = handler._runtime_call(
                handler._reset_current_instance_backend,
                False,
            )

        close_fcodex_epoch.assert_called_once_with()
        self.assertIn(thread_id, result["purged_thread_ids"])
        self.assertIsNone(
            handler._runtime_call(
                handler._fcodex_participant_runtime.snapshot,
                participant_id,
            )
        )
        after_reset = handler._runtime_call(
            handler._fcodex_participant_runtime.source_snapshot,
            participant_id,
            thread_id,
        )
        self.assertEqual(after_reset.connection_ids, ())
        self.assertEqual(after_reset.holder_presence, "absent")
        self.assertIsNone(handler._thread_runtime_lease_store.load(thread_id))
        with self.assertRaisesRegex(RuntimeError, "participant 未注册"):
            handler._runtime_call(
                handler._fcodex_participant_runtime.heartbeat,
                participant_id,
                connection_id,
            )
        with self.assertRaisesRegex(RuntimeError, "participant 未注册"):
            handler._runtime_call(
                handler._fcodex_participant_runtime.retain_connection_source,
                participant_id,
                connection_id,
                thread_id,
            )

        acquire_calls: list[str] = []
        original_acquire = handler._thread_runtime_lease_store.acquire

        def record_acquire(candidate_thread_id, holder):
            acquire_calls.append(candidate_thread_id)
            return original_acquire(candidate_thread_id, holder)

        handler._thread_runtime_lease_store.acquire = record_acquire
        try:
            handler._runtime_call(
                handler._fcodex_participant_runtime.connect,
                participant_id,
                connection_id,
            )
            handler._runtime_call(
                handler._fcodex_participant_runtime.retain_connection_source,
                participant_id,
                connection_id,
                thread_id,
            )
        finally:
            handler._thread_runtime_lease_store.acquire = original_acquire

        self.assertEqual(acquire_calls, [thread_id])
        self.assertIsNotNone(handler._thread_runtime_lease_store.load(thread_id))
        self.assertEqual(
            handler._runtime_call(
                handler._fcodex_participant_runtime.source_snapshot,
                participant_id,
                thread_id,
            ).holder_presence,
            "confirmed",
        )

    def test_reset_backend_composes_all_post_stop_retirements_once(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))
        seeded_leases = self._seed_current_process_interaction_leases(handler)
        old_generation = handler._adapter.connection_generation_value
        original_start = handler._adapter.start

        def start_replacement() -> None:
            original_start()
            handler._adapter.connection_generation_value = old_generation + 1

        with (
            patch.object(
                handler._interaction_lease_store,
                "capture_current_process_for_backend_stop",
                wraps=(
                    handler._interaction_lease_store.capture_current_process_for_backend_stop
                ),
            ) as capture_leases,
            patch.object(
                handler._interaction_lease_store,
                "retire_after_backend_stop",
                wraps=handler._interaction_lease_store.retire_after_backend_stop,
            ) as retire_leases,
            # Observe the coordinator's explicit post-stop registry port.
            # Later gate invalidation reasserts the same owner cleanup
            # idempotently through the disconnect composition.
            patch.object(
                handler._backend_reset_coordinator,
                "_retire_server_requests_after_stop",
                wraps=(
                    handler._backend_reset_coordinator._retire_server_requests_after_stop
                ),
            ) as retire_server_requests,
            patch.object(
                handler._operation_owner,
                "settle_backend_epoch_after_stop",
                wraps=handler._operation_owner.settle_backend_epoch_after_stop,
            ) as retire_fcodex,
            patch.object(
                handler._web_runtime,
                "retire_backend_epoch_after_stop",
                wraps=handler._web_runtime.retire_backend_epoch_after_stop,
            ) as retire_web,
            patch.object(
                handler._feishu_root_operations,
                "retire_backend_epoch_after_stop",
                wraps=(handler._feishu_root_operations.retire_backend_epoch_after_stop),
            ) as retire_feishu_roots,
            patch.object(
                handler._interaction_requests,
                "retire_backend_epoch_after_stop",
                wraps=handler._interaction_requests.retire_backend_epoch_after_stop,
            ) as retire_feishu_requests,
            patch.object(
                handler._adapter,
                "start",
                side_effect=start_replacement,
            ) as start_adapter,
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ) as publish_replacement,
        ):
            result = handler._runtime_call(
                handler._reset_current_instance_backend,
                False,
            )

        capture_leases.assert_called_once_with()
        retire_leases.assert_called_once()
        lease_capture = retire_leases.call_args.args[0]
        self.assertEqual(
            lease_capture.leases,
            tuple(seeded_leases[thread_id] for thread_id in sorted(seeded_leases)),
        )
        retire_server_requests.assert_called_once_with()
        retire_fcodex.assert_called_once_with()
        retire_web.assert_called_once_with()
        retire_feishu_roots.assert_called_once_with()
        retire_feishu_requests.assert_called_once_with()
        self.assertEqual(handler._adapter.stop_calls, 1)
        start_adapter.assert_called_once_with()
        publish_replacement.assert_called_once_with(
            app_server_url=handler._adapter.current_app_server_url()
        )
        self.assertEqual(
            handler._adapter.connection_generation_value,
            old_generation + 1,
        )
        self.assertEqual(
            result["app_server_url"],
            handler._adapter.current_app_server_url(),
        )
        admitted_gate = handler._adapter_ingress_gate.snapshot()
        self.assertEqual(admitted_gate.latest_generation, old_generation + 1)
        self.assertFalse(admitted_gate.backend_reset_blocked)
        self.assertFalse(admitted_gate.cleanup_required)
        self.assertFalse(admitted_gate.disconnect_cleanup_pending)
        for thread_id in seeded_leases:
            self.assertIsNone(handler._interaction_lease_store.load(thread_id))

    def test_reset_backend_stop_failure_preserves_all_surface_leases(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))
        seeded_leases = self._seed_current_process_interaction_leases(handler)
        binding = ("reset-candidate-user", "reset-candidate-chat")
        root = ThreadSummary(
            thread_id="thread-reset-candidate",
            cwd="/tmp/project",
            name="reset candidate",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, root)

        def seed_candidate():
            token = handler._feishu_root_operations.admit(
                binding,
                root.thread_id,
                chat_id=binding[1],
                message_id="reset-candidate-message",
                reason="test_backend_reset_stop_failure",
                operation_kind="prompt",
            )
            handler._feishu_root_operations.arm_continuation(token)
            handler._feishu_root_operations.accept_prompt_start(
                token,
                "submission-before-failed-stop",
            )
            claim = handler._feishu_root_operations.claim_prompt_interrupt_candidate(
                binding,
                root.thread_id,
            )
            assert claim is not None
            return claim

        candidate_claim = handler._runtime_call(seed_candidate)
        candidate_lease = handler._interaction_lease_store.load(root.thread_id)
        candidate_snapshot = handler._runtime_call(
            handler._feishu_root_operations.snapshot,
            root.thread_id,
        )
        fcodex_root = ThreadSummary(
            thread_id="thread-reset-fcodex-operation",
            cwd="/tmp/project",
            name="reset fcodex operation",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        participant_id = "fcodex:reset:stop-failure"
        connection_id = "stop-failure-connection"

        def seed_fcodex_operation():
            handler._operation_owner.participant_connected(
                participant_id,
                connection_id,
            )
            handler._operation_owner.remember_authoritative_direct_target(
                fcodex_root,
                expected_thread_id=fcodex_root.thread_id,
                operation="test backend reset stop failure",
            )
            decision = handler._operation_owner.admit(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=1,
                method="turn/start",
                thread_id=fcodex_root.thread_id,
                request_params=None,
                resume_may_autostart=False,
                continuation_risk=False,
            )
            if not decision.get("allowed"):
                raise AssertionError(f"fcodex seed was denied: {decision}")
            return handler._fcodex_participant_runtime.snapshot(participant_id)

        fcodex_participant = handler._runtime_call(seed_fcodex_operation)
        fcodex_lease = handler._interaction_lease_store.load(
            fcodex_root.thread_id
        )

        with (
            patch.object(
                handler._interaction_lease_store,
                "capture_current_process_for_backend_stop",
                wraps=(
                    handler._interaction_lease_store.capture_current_process_for_backend_stop
                ),
            ) as capture_leases,
            patch.object(
                handler._interaction_lease_store,
                "retire_after_backend_stop",
                wraps=handler._interaction_lease_store.retire_after_backend_stop,
            ) as retire_leases,
            # Match the explicit post-stop stage observed in the success case.
            patch.object(
                handler._backend_reset_coordinator,
                "_retire_server_requests_after_stop",
                wraps=(
                    handler._backend_reset_coordinator._retire_server_requests_after_stop
                ),
            ) as retire_server_requests,
            patch.object(
                handler._operation_owner,
                "settle_backend_epoch_after_stop",
                wraps=handler._operation_owner.settle_backend_epoch_after_stop,
            ) as retire_fcodex,
            patch.object(
                handler._web_runtime,
                "retire_backend_epoch_after_stop",
                wraps=handler._web_runtime.retire_backend_epoch_after_stop,
            ) as retire_web,
            patch.object(
                handler._feishu_root_operations,
                "retire_backend_epoch_after_stop",
                wraps=(handler._feishu_root_operations.retire_backend_epoch_after_stop),
            ) as retire_feishu_roots,
            patch.object(
                handler._interaction_requests,
                "retire_backend_epoch_after_stop",
                wraps=handler._interaction_requests.retire_backend_epoch_after_stop,
            ) as retire_feishu_requests,
            patch.object(
                handler._adapter,
                "stop",
                side_effect=RuntimeError("owned backend stop failed"),
            ) as stop_adapter,
            patch.object(
                handler._adapter,
                "start",
                wraps=handler._adapter.start,
            ) as start_adapter,
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ) as publish_replacement,
            self.assertRaisesRegex(RuntimeError, "owned backend stop failed"),
        ):
            handler._runtime_call(handler._reset_current_instance_backend, False)

        capture_leases.assert_called_once_with()
        stop_adapter.assert_called_once_with()
        retire_leases.assert_not_called()
        retire_server_requests.assert_not_called()
        retire_fcodex.assert_not_called()
        retire_web.assert_not_called()
        retire_feishu_roots.assert_not_called()
        retire_feishu_requests.assert_not_called()
        start_adapter.assert_not_called()
        publish_replacement.assert_not_called()
        self.assertTrue(handler._adapter_ingress_gate.snapshot().backend_reset_blocked)
        self.assertEqual(
            {
                thread_id: handler._interaction_lease_store.load(thread_id)
                for thread_id in seeded_leases
            },
            seeded_leases,
        )
        self.assertEqual(
            handler._interaction_lease_store.load(root.thread_id),
            candidate_lease,
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root.thread_id,
            ),
            candidate_snapshot,
        )
        self.assertEqual(
            handler._binding_runtime.resolve_session(*binding).thread.feishu_runtime_state,
            "attached",
        )
        self.assertTrue(
            handler._runtime_call(
                handler._feishu_root_operations.restore_prompt_interrupt_candidate_after_pre_send,
                candidate_claim,
                error=CodexRpcPreSendError(
                    "turn/interrupt",
                    RuntimeError("pre-send after failed stop"),
                ),
            )
        )
        restored_claim = handler._runtime_call(
            handler._feishu_root_operations.claim_prompt_interrupt_candidate,
            binding,
            root.thread_id,
        )
        self.assertIsNotNone(restored_claim)
        self.assertEqual(
            restored_claim and restored_claim.turn_id,
            "submission-before-failed-stop",
        )
        self.assertEqual(
            handler._runtime_call(
                handler._fcodex_participant_runtime.snapshot,
                participant_id,
            ),
            fcodex_participant,
        )
        self.assertEqual(
            handler._interaction_lease_store.load(fcodex_root.thread_id),
            fcodex_lease,
        )

    def test_reset_preview_counts_only_canonical_pending_requests(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))

        with (
            patch.object(
                handler._backend_reset_interactions,
                "_pending_count",
                return_value=1,
            ),
        ):
            preview = handler._runtime_call(
                handler._runtime_admin.backend_reset_preview
            )

        self.assertEqual(preview.pending_request_count, 1)
        self.assertEqual(preview.blocking_pending_request_count, 1)

    def test_reset_backend_requires_replacement_generation_to_advance(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))
        _admit_adapter_connection(
            handler,
            handler._adapter.connection_generation_value,
        )

        with (
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
            ),
            self.assertRaisesRegex(RuntimeError, "did not advance"),
        ):
            handler._runtime_call(handler._reset_current_instance_backend, False)

        self.assertTrue(handler._adapter_ingress_gate.snapshot().backend_reset_blocked)
        self.assertEqual(
            handler._handle_service_control_request(
                "service/status",
                {},
            )["app_server_url"],
            "",
        )

    def test_reset_backend_publication_failure_keeps_endpoint_unpublished(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        handler, _ = self._make_handler(pathlib.Path(tempdir.name))
        old_generation = handler._adapter.connection_generation_value
        original_start = handler._adapter.start

        def start_replacement() -> None:
            original_start()
            handler._adapter.connection_generation_value = old_generation + 1

        handler._adapter.start = start_replacement
        with (
            patch.object(
                handler._service_runtime_authority,
                "register_instance_runtime",
                side_effect=RuntimeError("publication failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "publication failed"),
        ):
            handler._runtime_call(handler._reset_current_instance_backend, False)

        self.assertTrue(handler._adapter.current_app_server_url())
        self.assertTrue(handler._adapter_ingress_gate.snapshot().backend_reset_blocked)
        self.assertEqual(
            handler._handle_service_control_request(
                "service/status",
                {},
            )["app_server_url"],
            "",
        )


if __name__ == "__main__":
    unittest.main()
