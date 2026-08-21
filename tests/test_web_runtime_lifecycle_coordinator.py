from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from bot.runtime_loop import RuntimeLoop
from bot.stores.interaction_lease_store import (
    InteractionLease,
    make_fcodex_interaction_holder,
    make_web_interaction_holder,
)
from bot.thread_runtime_authority import ThreadUnsubscribeOutcomeUnknown
from bot.web_runtime.controller import WebRuntimeController
from bot.web_runtime.document_registry import WebDocumentMutation, WebDocumentSnapshot
from bot.web_runtime.interaction_inbox import WebInteractionChange, WebInteractionMutation
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.lifecycle_coordinator import (
    WebRuntimeLifecycleCoordinator,
    WebRuntimeLifecyclePorts,
)
from bot.web_runtime.interest import WebRuntimeInterestSnapshot
from bot.web_runtime.selection_coordinator import WebSelectionConvergence


class WebRuntimeLifecycleCoordinatorTests(unittest.TestCase):
    def _build(self, *, guard=None):
        operations = Mock(name="operations")
        operations.owned_main_turn_thread_ids.return_value = ()
        operations.has_unknown_mutation.return_value = False

        documents = Mock(name="documents")
        documents.client_ids.return_value = ()
        disconnected_document = WebDocumentSnapshot(
            client_id="tab-1",
            connected=False,
            materialized_thread_id="",
            latest_intent_generation=0,
        )
        documents.mark_connected.return_value = WebDocumentMutation(
            outcome="changed",
            previous=disconnected_document,
            current=WebDocumentSnapshot(
                client_id="tab-1",
                connected=True,
                materialized_thread_id="",
                latest_intent_generation=0,
            ),
        )

        runtime_interest = Mock(name="runtime_interest")
        runtime_interest.has_interest.return_value = False
        runtime_interest.is_unknown.return_value = False
        runtime_interest.has_managed_interest.return_value = False
        runtime_interest.has_desired_clients.return_value = False
        runtime_interest.subscription_is_current.return_value = False
        runtime_interest.snapshot.return_value = None

        interaction_inbox = Mock(name="interaction_inbox")
        interaction_inbox.root_ids_for_client.return_value = ()
        interaction_inbox.fail_close_client.return_value = WebInteractionMutation()
        interaction_inbox.has_for_root.return_value = False
        interaction_inbox.backend_disconnected.return_value = WebInteractionMutation()

        read_model = Mock(name="read_model")
        read_model.turn_thread_ids.return_value = ()
        read_model.latest_turn_is_active.return_value = False

        selection = Mock(name="selection")
        selection.lose_document.return_value = WebSelectionConvergence()

        interaction_leases = Mock(name="interaction_leases")
        interaction_leases.load.return_value = None

        callbacks = SimpleNamespace(
            require_client_id=Mock(
                name="require_client_id",
                side_effect=lambda value: str(value or "").strip(),
            ),
            read_thread=Mock(
                name="read_thread",
                return_value=SimpleNamespace(
                    summary=SimpleNamespace(status="idle")
                ),
            ),
            list_loaded_thread_ids=Mock(
                name="list_loaded_thread_ids",
                return_value=[],
            ),
            capture_connection_generation=Mock(
                name="capture_connection_generation",
                return_value=7,
            ),
            run_if_connection_generation=Mock(
                name="run_if_connection_generation",
                side_effect=lambda _generation, callback: callback(),
            ),
            prepare_unsubscribe_thread=Mock(name="prepare_unsubscribe_thread"),
            execute_prepared_unsubscribe_thread=Mock(
                name="execute_prepared_unsubscribe_thread"
            ),
            settle_prepared_unsubscribe_thread=Mock(
                name="settle_prepared_unsubscribe_thread"
            ),
            abandon_prepared_unsubscribe_thread=Mock(
                name="abandon_prepared_unsubscribe_thread"
            ),
            prepare_service_thread_runtime_lease_release=Mock(
                name="prepare_service_thread_runtime_lease_release",
                side_effect=lambda thread_id: (
                    "service-runtime-release",
                    thread_id,
                ),
            ),
            release_prepared_service_thread_runtime_lease=Mock(
                name="release_prepared_service_thread_runtime_lease",
                return_value=True,
            ),
            schedule_runtime_cleanup=Mock(name="schedule_runtime_cleanup"),
            thread_subscribers=Mock(name="thread_subscribers", return_value=()),
            has_external_pending_interaction_for_root=Mock(
                name="has_external_pending_interaction_for_root",
                return_value=False,
            ),
            shared_interaction_reprojection_roots=Mock(
                name="shared_interaction_reprojection_roots",
                return_value=(),
            ),
            publish_interaction_changes=Mock(name="publish_interaction_changes"),
            publish_projection=Mock(name="publish_projection", return_value={}),
        )
        coordinator = WebRuntimeLifecycleCoordinator(
            ports=WebRuntimeLifecyclePorts(
                operations=operations,
                documents=documents,
                runtime_interest=runtime_interest,
                interaction_inbox=interaction_inbox,
                read_model=read_model,
                selection=selection,
                interaction_leases=interaction_leases,
                require_client_id=callbacks.require_client_id,
                read_thread=callbacks.read_thread,
                list_loaded_thread_ids=callbacks.list_loaded_thread_ids,
                capture_connection_generation=(
                    callbacks.capture_connection_generation
                ),
                run_if_connection_generation=(
                    callbacks.run_if_connection_generation
                ),
                prepare_unsubscribe_thread=callbacks.prepare_unsubscribe_thread,
                execute_prepared_unsubscribe_thread=(
                    callbacks.execute_prepared_unsubscribe_thread
                ),
                settle_prepared_unsubscribe_thread=(
                    callbacks.settle_prepared_unsubscribe_thread
                ),
                abandon_prepared_unsubscribe_thread=(
                    callbacks.abandon_prepared_unsubscribe_thread
                ),
                prepare_service_thread_runtime_lease_release=(
                    callbacks.prepare_service_thread_runtime_lease_release
                ),
                release_prepared_service_thread_runtime_lease=(
                    callbacks.release_prepared_service_thread_runtime_lease
                ),
                schedule_runtime_cleanup=callbacks.schedule_runtime_cleanup,
                thread_subscribers=callbacks.thread_subscribers,
                has_external_pending_interaction_for_root=(
                    callbacks.has_external_pending_interaction_for_root
                ),
                shared_interaction_reprojection_roots=(
                    callbacks.shared_interaction_reprojection_roots
                ),
                publish_interaction_changes=callbacks.publish_interaction_changes,
                publish_projection=callbacks.publish_projection,
            ),
            runtime_context_guard=guard or (lambda: None),
        )
        owners = SimpleNamespace(
            operations=operations,
            documents=documents,
            runtime_interest=runtime_interest,
            interaction_inbox=interaction_inbox,
            read_model=read_model,
            selection=selection,
            interaction_leases=interaction_leases,
        )
        return coordinator, owners, callbacks

    def test_runtime_guard_fails_before_any_port_call(self) -> None:
        guard = Mock(side_effect=RuntimeError("outside RuntimeLoop"))
        coordinator, owners, callbacks = self._build(guard=guard)

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            coordinator.client_disconnected("tab-1")

        self.assertEqual(owners.documents.mock_calls, [])
        self.assertEqual(owners.operations.mock_calls, [])
        self.assertEqual(callbacks.publish_projection.mock_calls, [])

    def test_transport_loss_closes_delivery_without_mutating_turn_owner(self) -> None:
        coordinator, owners, callbacks = self._build()
        owners.operations.owned_main_turn_thread_ids.return_value = (
            "root-submission-or-active",
        )
        owners.interaction_inbox.root_ids_for_client.return_value = ("root-inbox",)
        first_change = WebInteractionChange("root-inbox", "closed")
        second_change = WebInteractionChange("root-submission-or-active", "closed")
        owners.interaction_inbox.fail_close_client.side_effect = [
            WebInteractionMutation((first_change,)),
            WebInteractionMutation((second_change,)),
        ]

        coordinator.client_transport_disconnected("tab-1")

        owners.documents.mark_transport_disconnected.assert_called_once_with("tab-1")
        owners.operations.owned_main_turn_thread_ids.assert_called_once_with("tab-1")
        self.assertEqual(
            owners.interaction_inbox.fail_close_client.call_args_list,
            [
                call("tab-1", "root-inbox"),
                call("tab-1", "root-submission-or-active"),
            ],
        )
        self.assertEqual(
            callbacks.publish_interaction_changes.call_args_list,
            [call((first_change,)), call((second_change,))],
        )
        owners.interaction_leases.assert_not_called()
        self.assertEqual(
            owners.operations.mock_calls,
            [call.owned_main_turn_thread_ids("tab-1")],
        )

    def test_document_reissue_revokes_continuity_before_delivery_only(self) -> None:
        coordinator, owners, callbacks = self._build()
        owners.operations.owned_main_turn_thread_ids.return_value = ("root-1",)
        owners.interaction_inbox.root_ids_for_client.return_value = ()
        events: list[str] = []
        owners.documents.mark_document_reissued.side_effect = (
            lambda _client_id: events.append("document_reissued")
        )
        owners.interaction_inbox.fail_close_client.side_effect = (
            lambda *_args, **_kwargs: (
                events.append("delivery_closed") or WebInteractionMutation()
            )
        )

        coordinator.client_document_reissued(" tab-1 ")

        self.assertEqual(events, ["document_reissued", "delivery_closed"])
        owners.documents.mark_transport_disconnected.assert_not_called()
        owners.selection.lose_document.assert_not_called()
        owners.runtime_interest.assert_not_called()
        owners.interaction_leases.assert_not_called()
        callbacks.publish_projection.assert_not_called()

    def test_document_loss_runs_delivery_close_before_selection_cleanup(self) -> None:
        coordinator, owners, callbacks = self._build()
        owners.operations.owned_main_turn_thread_ids.return_value = ("root-1",)
        events: list[str] = []
        owners.documents.mark_transport_disconnected.side_effect = (
            lambda _client_id: events.append("transport")
        )
        owners.interaction_inbox.fail_close_client.side_effect = (
            lambda *_args, **_kwargs: (
                events.append("inbox") or WebInteractionMutation()
            )
        )
        owners.selection.lose_document.side_effect = (
            lambda _client_id: (
                events.append("selection") or WebSelectionConvergence()
            )
        )

        coordinator.client_disconnected("tab-1")

        self.assertEqual(events, ["transport", "inbox", "selection"])

    def test_reconnect_marks_document_without_reconstructing_writer(self) -> None:
        coordinator, owners, callbacks = self._build()

        coordinator.client_connected(" tab-1 ")

        owners.documents.mark_connected.assert_called_once_with("tab-1")
        self.assertEqual(owners.operations.mock_calls, [])

    def test_changed_connect_reprojects_only_current_shared_approval_roots(self) -> None:
        coordinator, _owners, callbacks = self._build()
        callbacks.shared_interaction_reprojection_roots.return_value = (
            "root-a",
            "root-b",
        )

        coordinator.client_connected("tab-1")

        callbacks.shared_interaction_reprojection_roots.assert_called_once_with("tab-1")
        callbacks.publish_interaction_changes.assert_called_once_with(
            (
                WebInteractionChange("root-a", "document_connected"),
                WebInteractionChange("root-b", "document_connected"),
            )
        )

    def test_second_socket_does_not_repeat_shared_approval_reprojection(self) -> None:
        coordinator, owners, callbacks = self._build()
        unchanged = owners.documents.mark_connected.return_value
        owners.documents.mark_connected.return_value = WebDocumentMutation(
            outcome="unchanged",
            previous=unchanged.current,
            current=unchanged.current,
        )
        callbacks.shared_interaction_reprojection_roots.return_value = ("root-a",)

        coordinator.client_connected("tab-1")

        callbacks.shared_interaction_reprojection_roots.assert_not_called()
        callbacks.publish_interaction_changes.assert_not_called()

    def test_shutdown_disconnects_documents_then_clears_process_state(self) -> None:
        coordinator, owners, _callbacks = self._build()
        owners.documents.client_ids.return_value = ("tab-b", "tab-a")

        coordinator.shutdown()

        self.assertEqual(
            owners.documents.mark_transport_disconnected.call_args_list,
            [call("tab-a"), call("tab-b")],
        )
        self.assertEqual(
            owners.selection.lose_document.call_args_list,
            [call("tab-a"), call("tab-b")],
        )
        owners.runtime_interest.clear.assert_called_once_with()
        owners.documents.clear.assert_called_once_with()
        with self.assertRaises(WebRuntimeError) as raised:
            coordinator.client_connected("tab-c")
        self.assertEqual(raised.exception.code, "service_shutting_down")

    def test_finish_shutdown_requires_successful_prepare(self) -> None:
        coordinator, owners, _callbacks = self._build()

        with self.assertRaisesRegex(RuntimeError, "before prepare"):
            coordinator.finish_shutdown()

        owners.runtime_interest.clear.assert_not_called()
        owners.documents.clear.assert_not_called()

    def test_backend_disconnect_invalidates_delivery_before_projection(self) -> None:
        coordinator, owners, callbacks = self._build()
        change = WebInteractionChange("root-1", "backend_disconnected")
        owners.interaction_inbox.backend_disconnected.return_value = (
            WebInteractionMutation((change,))
        )
        events: list[str] = []
        owners.interaction_inbox.backend_disconnected.side_effect = lambda: (
            events.append("inbox") or WebInteractionMutation((change,))
        )
        owners.runtime_interest.backend_disconnected.side_effect = (
            lambda: events.append("interest")
        )
        owners.read_model.backend_disconnected.side_effect = (
            lambda: events.append("read_model")
        )
        callbacks.publish_interaction_changes.side_effect = (
            lambda _changes: events.append("changes")
        )
        callbacks.publish_projection.side_effect = (
            lambda *_args, **_kwargs: events.append("projection") or {}
        )

        coordinator.backend_disconnected()

        self.assertEqual(
            events,
            ["inbox", "interest", "read_model", "changes", "projection"],
        )

    def test_cleanup_candidate_only_schedules_external_work_in_runtime_loop(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()

        coordinator.maybe_release_web_runtime("root-1", known_non_active=True)

        callbacks.schedule_runtime_cleanup.assert_called_once_with("root-1", False)
        owners.interaction_leases.load.assert_not_called()
        callbacks.read_thread.assert_not_called()
        callbacks.list_loaded_thread_ids.assert_not_called()

    def test_cleanup_schedule_coalesces_one_successor_without_terminal_hint(
        self,
    ) -> None:
        coordinator, _owners, callbacks = self._build()

        coordinator.maybe_release_web_runtime("root-1")
        coordinator.maybe_release_web_runtime(
            "root-1",
            known_non_active=True,
        )
        coordinator.maybe_release_web_runtime("root-1")

        callbacks.schedule_runtime_cleanup.assert_called_once_with(
            "root-1",
            False,
        )

        coordinator.finish_runtime_cleanup("root-1")

        self.assertEqual(
            callbacks.schedule_runtime_cleanup.call_args_list,
            [call("root-1", False), call("root-1", False)],
        )
        coordinator.finish_runtime_cleanup("root-1")
        self.assertEqual(len(callbacks.schedule_runtime_cleanup.call_args_list), 2)

    def test_shutdown_does_not_admit_coalesced_cleanup_successor(self) -> None:
        coordinator, _owners, callbacks = self._build()
        coordinator.maybe_release_web_runtime("root-1")
        coordinator.maybe_release_web_runtime("root-1")

        coordinator.prepare_shutdown()
        coordinator.finish_runtime_cleanup("root-1")

        callbacks.schedule_runtime_cleanup.assert_called_once_with(
            "root-1",
            False,
        )

    def test_controller_finishes_cleanup_flight_when_probe_is_blocked(self) -> None:
        controller = object.__new__(WebRuntimeController)
        lifecycle = Mock(name="lifecycle")
        prepared = SimpleNamespace(thread_id="root-1")
        probe = SimpleNamespace(disposition="blocked")
        lifecycle.prepare_runtime_cleanup.return_value = prepared
        lifecycle.execute_runtime_cleanup_probe.return_value = probe
        lifecycle.settle_runtime_cleanup_probe.return_value = None
        controller._lifecycle = lifecycle
        controller._runtime_call = (
            lambda callback, *args, **kwargs: callback(*args, **kwargs)
        )

        controller.run_runtime_cleanup_transaction("root-1")

        lifecycle.finish_runtime_cleanup.assert_called_once_with("root-1")

    def test_fcodex_interaction_holder_does_not_block_web_cleanup_probe(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=3,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = interest
        owners.runtime_interest.subscription_is_current.return_value = True
        owners.interaction_leases.load.return_value = InteractionLease(
            thread_id="root-1",
            holder=make_fcodex_interaction_holder("terminal-1", owner_pid=0),
            lease_id="lease-1",
            updated_at=1.0,
            turn_id="turn-1",
        )
        prepared = coordinator.prepare_runtime_cleanup(
            "root-1",
            known_non_active=True,
        )
        assert prepared is not None

        probe = coordinator.execute_runtime_cleanup_probe(prepared)

        self.assertEqual(probe.disposition, "unsubscribe")
        callbacks.read_thread.assert_called_once_with(
            "root-1",
            False,
            expected_connection_generation=7,
        )
        callbacks.prepare_service_thread_runtime_lease_release.assert_called_once_with(
            "root-1"
        )

    def test_stale_terminal_hint_still_probes_successor_active_status(self) -> None:
        coordinator, owners, callbacks = self._build()
        successor_interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=9,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = successor_interest
        owners.runtime_interest.subscription_is_current.return_value = True
        callbacks.read_thread.return_value = SimpleNamespace(
            summary=SimpleNamespace(status="active")
        )

        prepared = coordinator.prepare_runtime_cleanup(
            "root-1",
            known_non_active=True,
        )
        assert prepared is not None
        probe = coordinator.execute_runtime_cleanup_probe(prepared)

        self.assertEqual(probe.disposition, "active")
        callbacks.read_thread.assert_called_once_with(
            "root-1",
            False,
            expected_connection_generation=7,
        )
        callbacks.prepare_service_thread_runtime_lease_release.assert_not_called()

    def test_slow_cleanup_probe_does_not_block_runtime_loop(self) -> None:
        runtime_loop = RuntimeLoop(name="web-runtime-cleanup-staged-test")
        coordinator, owners, callbacks = self._build(
            guard=runtime_loop.assert_worker_context
        )
        interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=4,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = interest
        owners.runtime_interest.subscription_is_current.return_value = True
        probe_entered = threading.Event()
        release_probe = threading.Event()
        sentinel_done = threading.Event()
        worker_errors: list[BaseException] = []

        def slow_read(*_args, **_kwargs):
            probe_entered.set()
            if not release_probe.wait(timeout=2):
                raise TimeoutError("test did not release cleanup probe")
            return SimpleNamespace(summary=SimpleNamespace(status="active"))

        callbacks.read_thread.side_effect = slow_read
        controller = object.__new__(WebRuntimeController)
        controller._lifecycle = coordinator
        controller._runtime_call = runtime_loop.call

        def run_cleanup() -> None:
            try:
                controller.run_runtime_cleanup_transaction(
                    "root-1",
                    known_non_active=True,
                )
            except BaseException as exc:
                worker_errors.append(exc)

        worker = threading.Thread(target=run_cleanup)
        worker.start()
        try:
            self.assertTrue(probe_entered.wait(timeout=1))
            runtime_loop.submit(sentinel_done.set)
            self.assertTrue(sentinel_done.wait(timeout=1))
        finally:
            release_probe.set()
            worker.join(timeout=2)
            runtime_loop.stop(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])

    def test_external_cleanup_probe_retains_active_web_turn_lease(self) -> None:
        coordinator, owners, callbacks = self._build()
        interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=3,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = interest
        owners.runtime_interest.subscription_is_current.return_value = True
        owners.interaction_leases.load.return_value = InteractionLease(
            thread_id="root-1",
            holder=make_web_interaction_holder("tab-1", owner_pid=0),
            lease_id="lease-1",
            updated_at=1.0,
            turn_id="turn-1",
        )
        prepared = coordinator.prepare_runtime_cleanup(
            "root-1",
            known_non_active=True,
        )
        assert prepared is not None

        probe = coordinator.execute_runtime_cleanup_probe(prepared)

        self.assertEqual(probe.disposition, "blocked")
        callbacks.prepare_unsubscribe_thread.assert_not_called()
        callbacks.prepare_service_thread_runtime_lease_release.assert_not_called()
        callbacks.release_prepared_service_thread_runtime_lease.assert_not_called()

    def test_inactive_cleanup_pins_generation_and_releases_after_known_success(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=4,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = interest
        owners.runtime_interest.subscription_is_current.return_value = True
        prepared_unsubscribe = Mock(name="prepared_unsubscribe")
        pending_unsubscribe = Mock(name="pending_unsubscribe")
        pending_unsubscribe.commit_local_state.side_effect = lambda callback: callback()
        callbacks.prepare_unsubscribe_thread.return_value = prepared_unsubscribe
        callbacks.settle_prepared_unsubscribe_thread.return_value = (
            pending_unsubscribe
        )
        prepared = coordinator.prepare_runtime_cleanup("root-1")
        assert prepared is not None

        probe = coordinator.execute_runtime_cleanup_probe(prepared)
        claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)
        assert claim is not None
        coordinator.execute_runtime_cleanup_unsubscribe(claim)
        release = coordinator.settle_runtime_cleanup_unsubscribe(claim)
        assert release is not None
        coordinator.release_runtime_cleanup_lease(release)
        coordinator.finalize_runtime_cleanup_release(release)

        callbacks.read_thread.assert_called_once_with(
            "root-1",
            False,
            expected_connection_generation=7,
        )
        callbacks.prepare_unsubscribe_thread.assert_called_once_with(
            "root-1",
            expected_connection_generation=7,
        )
        callbacks.execute_prepared_unsubscribe_thread.assert_called_once_with(
            prepared_unsubscribe
        )
        callbacks.settle_prepared_unsubscribe_thread.assert_called_once_with(
            prepared_unsubscribe,
            upstream_succeeded=True,
            subscription_already_absent=False,
            error=None,
        )
        callbacks.prepare_service_thread_runtime_lease_release.assert_called_once_with(
            "root-1"
        )
        callbacks.release_prepared_service_thread_runtime_lease.assert_called_once_with(
            ("service-runtime-release", "root-1")
        )
        owners.runtime_interest.forget.assert_called_once_with("root-1")
        owners.read_model.forget_runtime.assert_called_once_with("root-1")

    def test_notification_revision_rejects_late_cleanup_probe(self) -> None:
        coordinator, owners, callbacks = self._build()
        prepared_interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=8,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = prepared_interest
        owners.runtime_interest.subscription_is_current.return_value = True
        prepared = coordinator.prepare_runtime_cleanup(
            "root-1",
            known_non_active=True,
        )
        assert prepared is not None
        probe = coordinator.execute_runtime_cleanup_probe(prepared)
        owners.runtime_interest.snapshot.return_value = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=9,
            unsubscribe_outcome_unknown=False,
        )

        claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)

        self.assertIsNone(claim)
        callbacks.prepare_unsubscribe_thread.assert_not_called()

    def test_pre_send_recheck_rejects_new_local_interest(self) -> None:
        blocker_cases = ("desired", "pending", "subscriber")
        for blocker in blocker_cases:
            with self.subTest(blocker=blocker):
                coordinator, owners, callbacks = self._build()
                prepared_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=1,
                    outcome="confirmed",
                    revision=8,
                    unsubscribe_outcome_unknown=False,
                )
                owners.runtime_interest.snapshot.return_value = prepared_interest
                owners.runtime_interest.subscription_is_current.return_value = True
                callbacks.prepare_unsubscribe_thread.return_value = Mock(
                    name="prepared_unsubscribe"
                )
                prepared = coordinator.prepare_runtime_cleanup(
                    "root-1",
                    known_non_active=True,
                )
                assert prepared is not None
                probe = coordinator.execute_runtime_cleanup_probe(prepared)
                claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)
                assert claim is not None

                if blocker == "desired":
                    owners.runtime_interest.has_desired_clients.return_value = True
                    owners.runtime_interest.snapshot.return_value = (
                        WebRuntimeInterestSnapshot(
                            thread_id="root-1",
                            desired_client_ids=("tab-2",),
                            ever_confirmed=True,
                            subscription_epoch=0,
                            outcome="confirmed",
                            revision=9,
                            unsubscribe_outcome_unknown=False,
                        )
                    )
                elif blocker == "pending":
                    callbacks.has_external_pending_interaction_for_root.return_value = (
                        True
                    )
                else:
                    callbacks.thread_subscribers.return_value = (
                        ("feishu", "group:1"),
                    )

                allowed = coordinator.confirm_runtime_cleanup_unsubscribe_send(
                    claim
                )
                coordinator.abandon_runtime_cleanup_claim(claim)

                self.assertFalse(allowed)
                if blocker == "desired":
                    owners.runtime_interest.mark_confirmed.assert_not_called()
                else:
                    owners.runtime_interest.mark_confirmed.assert_called_once_with(
                        "root-1"
                    )
                callbacks.execute_prepared_unsubscribe_thread.assert_not_called()

    def test_unknown_unsubscribe_updates_only_exact_claimed_interest(self) -> None:
        for successor_arrived in (False, True):
            with self.subTest(successor_arrived=successor_arrived):
                coordinator, owners, callbacks = self._build()
                prepared_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=1,
                    outcome="confirmed",
                    revision=8,
                    unsubscribe_outcome_unknown=False,
                )
                claimed_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=0,
                    outcome="confirmed",
                    revision=9,
                    unsubscribe_outcome_unknown=False,
                )
                successor_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=("tab-2",),
                    ever_confirmed=True,
                    subscription_epoch=1,
                    outcome="confirmed",
                    revision=10,
                    unsubscribe_outcome_unknown=False,
                )
                current_interest = {"value": prepared_interest}
                owners.runtime_interest.snapshot.side_effect = (
                    lambda _thread_id: current_interest["value"]
                )
                owners.runtime_interest.subscription_is_current.return_value = True
                owners.runtime_interest.mark_subscription_absent.side_effect = (
                    lambda _thread_id: current_interest.__setitem__(
                        "value",
                        claimed_interest,
                    )
                )
                callbacks.prepare_unsubscribe_thread.return_value = Mock(
                    name="prepared_unsubscribe"
                )
                prepared = coordinator.prepare_runtime_cleanup("root-1")
                assert prepared is not None
                probe = coordinator.execute_runtime_cleanup_probe(prepared)
                claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)
                assert claim is not None
                if successor_arrived:
                    current_interest["value"] = successor_interest
                effect_error = RuntimeError("wire lost")
                callbacks.settle_prepared_unsubscribe_thread.side_effect = (
                    ThreadUnsubscribeOutcomeUnknown(
                        SimpleNamespace(thread_id="root-1"),  # type: ignore[arg-type]
                        effect_error,
                    )
                )

                release = coordinator.settle_runtime_cleanup_unsubscribe(
                    claim,
                    error=effect_error,
                )

                self.assertIsNone(release)
                if successor_arrived:
                    owners.runtime_interest.mark_unsubscribe_unknown.assert_not_called()
                else:
                    owners.runtime_interest.mark_unsubscribe_unknown.assert_called_once_with(
                        "root-1"
                    )

    def test_known_no_effect_restores_only_exact_claimed_interest(self) -> None:
        for successor_arrived in (False, True):
            with self.subTest(successor_arrived=successor_arrived):
                coordinator, owners, callbacks = self._build()
                prepared_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=1,
                    outcome="confirmed",
                    revision=8,
                    unsubscribe_outcome_unknown=False,
                )
                claimed_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=0,
                    outcome="confirmed",
                    revision=9,
                    unsubscribe_outcome_unknown=False,
                )
                successor_interest = WebRuntimeInterestSnapshot(
                    thread_id="root-1",
                    desired_client_ids=(),
                    ever_confirmed=True,
                    subscription_epoch=1,
                    outcome="confirmed",
                    revision=10,
                    unsubscribe_outcome_unknown=False,
                )
                current_interest = {"value": prepared_interest}
                owners.runtime_interest.snapshot.side_effect = (
                    lambda _thread_id: current_interest["value"]
                )
                owners.runtime_interest.subscription_is_current.return_value = True
                owners.runtime_interest.mark_subscription_absent.side_effect = (
                    lambda _thread_id: current_interest.__setitem__(
                        "value",
                        claimed_interest,
                    )
                )
                callbacks.prepare_unsubscribe_thread.return_value = Mock(
                    name="prepared_unsubscribe"
                )
                prepared = coordinator.prepare_runtime_cleanup("root-1")
                assert prepared is not None
                probe = coordinator.execute_runtime_cleanup_probe(prepared)
                claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)
                assert claim is not None
                if successor_arrived:
                    current_interest["value"] = successor_interest
                effect_error = RuntimeError("known no effect")
                callbacks.settle_prepared_unsubscribe_thread.side_effect = effect_error

                with self.assertRaisesRegex(RuntimeError, "known no effect"):
                    coordinator.settle_runtime_cleanup_unsubscribe(
                        claim,
                        error=effect_error,
                    )

                if successor_arrived:
                    owners.runtime_interest.mark_confirmed.assert_not_called()
                else:
                    owners.runtime_interest.mark_confirmed.assert_called_once_with(
                        "root-1"
                    )

    def test_known_unsubscribe_success_retains_holder_when_interest_changes(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        interest = WebRuntimeInterestSnapshot(
            thread_id="root-1",
            desired_client_ids=(),
            ever_confirmed=True,
            subscription_epoch=1,
            outcome="confirmed",
            revision=4,
            unsubscribe_outcome_unknown=False,
        )
        owners.runtime_interest.snapshot.return_value = interest
        owners.runtime_interest.subscription_is_current.return_value = True
        prepared_unsubscribe = Mock(name="prepared_unsubscribe")
        pending_unsubscribe = Mock(name="pending_unsubscribe")
        pending_unsubscribe.commit_local_state.side_effect = lambda callback: callback()
        callbacks.prepare_unsubscribe_thread.return_value = prepared_unsubscribe
        callbacks.settle_prepared_unsubscribe_thread.return_value = (
            pending_unsubscribe
        )
        prepared = coordinator.prepare_runtime_cleanup(
            "root-1",
            known_non_active=True,
        )
        assert prepared is not None
        probe = coordinator.execute_runtime_cleanup_probe(prepared)
        claim = coordinator.settle_runtime_cleanup_probe(prepared, probe)
        assert claim is not None
        self.assertTrue(
            coordinator.confirm_runtime_cleanup_unsubscribe_send(claim)
        )
        coordinator.execute_runtime_cleanup_unsubscribe(claim)
        release = coordinator.settle_runtime_cleanup_unsubscribe(claim)
        assert release is not None
        callbacks.has_external_pending_interaction_for_root.return_value = True

        release_allowed = coordinator.confirm_runtime_cleanup_lease_release(
            release
        )
        coordinator.finalize_runtime_cleanup_release(release)

        self.assertFalse(release_allowed)
        callbacks.release_prepared_service_thread_runtime_lease.assert_not_called()
        owners.runtime_interest.forget.assert_not_called()
        owners.read_model.forget_runtime.assert_not_called()
        pending_unsubscribe.commit_local_state.assert_called_once()

    def test_machine_release_mismatch_retires_claim_without_replaying_unsubscribe(
        self,
    ) -> None:
        controller = object.__new__(WebRuntimeController)
        lifecycle = Mock(name="lifecycle")
        prepared = SimpleNamespace(thread_id="root-1")
        probe = SimpleNamespace(disposition="unsubscribe")
        claim = SimpleNamespace(execute_unsubscribe=True)
        release = SimpleNamespace(
            claim=SimpleNamespace(preparation=prepared),
        )
        lifecycle.prepare_runtime_cleanup.return_value = prepared
        lifecycle.execute_runtime_cleanup_probe.return_value = probe
        lifecycle.settle_runtime_cleanup_probe.return_value = claim
        lifecycle.confirm_runtime_cleanup_unsubscribe_send.return_value = True
        lifecycle.settle_runtime_cleanup_unsubscribe.return_value = release
        lifecycle.confirm_runtime_cleanup_lease_release.return_value = True
        lifecycle.release_runtime_cleanup_lease.return_value = False
        controller._lifecycle = lifecycle
        controller._runtime_call = (
            lambda callback, *args, **kwargs: callback(*args, **kwargs)
        )

        controller.run_runtime_cleanup_transaction("root-1")

        lifecycle.execute_runtime_cleanup_unsubscribe.assert_called_once_with(claim)
        lifecycle.settle_runtime_cleanup_lease_release_failure.assert_called_once_with(
            release
        )
        lifecycle.finalize_runtime_cleanup_release.assert_not_called()
        lifecycle.finish_runtime_cleanup.assert_called_once_with("root-1")

    def test_fatal_unsubscribe_is_settled_unknown_before_reraising(self) -> None:
        controller = object.__new__(WebRuntimeController)
        lifecycle = Mock(name="lifecycle")
        prepared = SimpleNamespace(thread_id="root-1")
        claim = SimpleNamespace(execute_unsubscribe=True)
        lifecycle.prepare_runtime_cleanup.return_value = prepared
        lifecycle.execute_runtime_cleanup_probe.return_value = Mock()
        lifecycle.settle_runtime_cleanup_probe.return_value = claim
        lifecycle.confirm_runtime_cleanup_unsubscribe_send.return_value = True
        lifecycle.execute_runtime_cleanup_unsubscribe.side_effect = KeyboardInterrupt()
        lifecycle.settle_runtime_cleanup_unsubscribe.return_value = None
        controller._lifecycle = lifecycle
        controller._runtime_call = (
            lambda callback, *args, **kwargs: callback(*args, **kwargs)
        )

        with self.assertRaises(KeyboardInterrupt):
            controller.run_runtime_cleanup_transaction("root-1")

        settlement = lifecycle.settle_runtime_cleanup_unsubscribe.call_args
        self.assertIs(settlement.args[0], claim)
        self.assertIsInstance(settlement.kwargs["error"], RuntimeError)
        lifecycle.abandon_runtime_cleanup_claim.assert_not_called()
        lifecycle.finish_runtime_cleanup.assert_called_once_with("root-1")

    def test_fatal_machine_release_is_settled_unknown_before_reraising(self) -> None:
        controller = object.__new__(WebRuntimeController)
        lifecycle = Mock(name="lifecycle")
        prepared = SimpleNamespace(thread_id="root-1")
        claim = SimpleNamespace(execute_unsubscribe=True)
        release = SimpleNamespace(claim=SimpleNamespace(preparation=prepared))
        lifecycle.prepare_runtime_cleanup.return_value = prepared
        lifecycle.execute_runtime_cleanup_probe.return_value = Mock()
        lifecycle.settle_runtime_cleanup_probe.return_value = claim
        lifecycle.confirm_runtime_cleanup_unsubscribe_send.return_value = True
        lifecycle.settle_runtime_cleanup_unsubscribe.return_value = release
        lifecycle.confirm_runtime_cleanup_lease_release.return_value = True
        lifecycle.release_runtime_cleanup_lease.side_effect = KeyboardInterrupt()
        controller._lifecycle = lifecycle
        controller._runtime_call = (
            lambda callback, *args, **kwargs: callback(*args, **kwargs)
        )

        with self.assertRaises(KeyboardInterrupt):
            controller.run_runtime_cleanup_transaction("root-1")

        lifecycle.settle_runtime_cleanup_lease_release_failure.assert_called_once_with(
            release
        )
        lifecycle.abandon_runtime_cleanup_release.assert_not_called()
        lifecycle.finalize_runtime_cleanup_release.assert_not_called()
        lifecycle.finish_runtime_cleanup.assert_called_once_with("root-1")


if __name__ == "__main__":
    unittest.main()
