from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import Mock, patch

from bot.adapter_ingress_gate import (
    AdapterIngressGate,
    AdapterOutboundRequestEpochLost,
)
from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.codex_protocol.client import CodexRpcTransportError
from bot.runtime_loop import RuntimeLoop
from bot.stores.interaction_lease_store import (
    make_feishu_interaction_holder,
)
from bot.thread_runtime_authority import (
    ThreadResumeLocalCommitFailed,
    ThreadResumeSettlementOutcome,
)
from bot.thread_runtime_coordination import (
    ManagedInstanceLoadedThreadInventory,
    ManagedLoadedThreadInventorySnapshot,
    ThreadRuntimeAdmissionError,
)
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.thread_open_coordinator import (
    WebThreadOpenCoordinator,
    WebThreadOpenPorts,
)
from bot.web_runtime.projection import project_thread_snapshot
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebThreadOpenCoordinatorTests(WebRuntimeControllerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.guard_calls = 0
        self.next_turn_settings = Mock(
            side_effect=self.next_turn_settings_store.load
        )
        self.open = self._make_open()

    def _guard(self) -> None:
        self.guard_calls += 1

    def _make_open(
        self,
        *,
        runtime_context_guard=None,
        runtime_call=None,
        **port_overrides,
    ) -> WebThreadOpenCoordinator:
        ports = WebThreadOpenPorts(
            list_threads=self.fake.list_threads,
            read_thread=self.fake.read_thread,
            list_loaded_thread_ids=self.fake.list_loaded_thread_ids,
            managed_loaded_thread_inventory=(
                self.fake.list_managed_loaded_thread_inventory
            ),
            list_thread_runtime_leases=(
                self.fake.list_thread_runtime_leases
            ),
            begin_resume_thread_page=(self.resume_authority.begin_resume_thread_page),
            claim_resume_thread_page=(
                self.resume_authority.claim_resume_thread_page
            ),
            acquire_claimed_resume_thread_page=(
                self.resume_authority.acquire_claimed_resume_thread_page
            ),
            complete_claimed_resume_thread_page=(
                self.resume_authority.complete_claimed_resume_thread_page
            ),
            abandon_resume_thread_page_claim=(
                self.resume_authority.abandon_resume_thread_page_claim
            ),
            abandon_acquired_resume_thread_page=(
                self.resume_authority.abandon_acquired_resume_thread_page
            ),
            execute_prepared_resume_thread_page=(
                self.resume_authority.execute_prepared_resume_thread_page
            ),
            settle_prepared_resume_thread_page=(
                self.resume_authority.settle_prepared_resume_thread_page
            ),
            list_thread_turns=self.fake.list_thread_turns,
            get_thread_goal=self.fake.get_thread_goal,
            prepare_runtime_lease_preflight=lambda _thread_id: None,
            capture_connection_generation=(
                lambda: self.backend_connection_generation
            ),
            run_if_connection_generation=lambda generation, callback: (
                self.require_connection_generation(generation),
                callback(),
            )[1],
        )
        if port_overrides:
            ports = replace(ports, **port_overrides)
        owner = WebThreadOpenCoordinator(
            instance_name="default",
            documents=self.document_registry,
            workspace=self.controller._workspace,
            operations=self.operations,
            lifecycle=self.controller._lifecycle,
            direct_targets=self.controller._direct_targets,
            goal_resume_policy=self.controller._goal_resume_policy,
            read_model=self.controller._thread_read_model,
            runtime_interest=self.controller._runtime_interest,
            selection=self.controller._selection,
            projection=self.projection,
            interaction_leases=self.store,
            interaction_inbox=self.interaction_inbox,
            active_turn_disclosure=self.controller._active_turn_disclosure,
            next_turn_settings=self.next_turn_settings,
            shared_interaction_eligible=lambda *_args: False,
            ports=ports,
            runtime_context_guard=runtime_context_guard or self._guard,
            runtime_call=runtime_call
            or (
                lambda callback, *args, **kwargs: callback(
                    *args,
                    **kwargs,
                )
            ),
            thread_limit=2,
        )
        # Tests keep concise call sites while exercising the production staged
        # methods; the coordinator intentionally has no synchronous read entry.
        setattr(
            owner,
            "read_thread",
            lambda client_id, thread_id, **kwargs: self._read_thread(
                client_id, thread_id, owner=owner, **kwargs
            ),
        )
        setattr(
            owner,
            "list_threads",
            lambda **kwargs: self._list_threads(owner=owner, **kwargs),
        )
        setattr(
            owner,
            "list_older_turns",
            lambda client_id, thread_id, **kwargs: self._list_older_turns(
                client_id, thread_id, owner=owner, **kwargs
            ),
        )
        return owner

    def _read_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        owner: WebThreadOpenCoordinator | None = None,
        **kwargs,
    ):
        owner = owner or self.open
        prepared = owner._runtime_call(  # noqa: SLF001
            owner.prepare_read_thread, client_id, thread_id, **kwargs
        )
        try:
            observed = owner.execute_read_thread_observation(prepared)
        except Exception as exc:
            owner.finish_read_thread_observation_failure(prepared, exc)
            raise AssertionError("thread observation failure did not raise")
        effect_preparation = owner._runtime_call(  # noqa: SLF001
            owner.prepare_read_thread_effect, prepared, observed
        )
        effect = owner.execute_read_thread_effect(effect_preparation)
        return owner.finish_read_thread_effect(effect_preparation, effect)

    def _list_threads(
        self,
        *,
        owner: WebThreadOpenCoordinator,
        **kwargs,
    ):
        prepared = owner._runtime_call(  # noqa: SLF001
            owner.prepare_list_threads, **kwargs
        )
        effect = owner.execute_list_threads(prepared)
        projection = owner._runtime_call(  # noqa: SLF001
            owner.settle_list_threads, prepared, effect
        )
        payload = owner.project_list_threads(projection)
        return owner._runtime_call(  # noqa: SLF001
            owner.finalize_list_threads, projection, payload
        )

    def _list_older_turns(
        self,
        client_id: str,
        thread_id: str,
        *,
        owner: WebThreadOpenCoordinator | None = None,
        **kwargs,
    ):
        owner = owner or self.open
        prepared = owner._runtime_call(  # noqa: SLF001
            owner.prepare_list_older_turns,
            client_id,
            thread_id,
            **kwargs,
        )
        effect = owner.execute_list_older_turns(prepared)
        projection = owner._runtime_call(  # noqa: SLF001
            owner.settle_list_older_turns, prepared, effect
        )
        payload = owner.project_older_turns(projection)
        return owner._runtime_call(  # noqa: SLF001
            owner.finalize_older_turns, projection, payload
        )

    def _assert_external_stage_keeps_loop_responsive(
        self,
        loop: RuntimeLoop,
        entered: threading.Event,
        release: threading.Event,
        action,
    ) -> tuple[list[object], list[BaseException]]:
        results: list[object] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                results.append(action())
            except BaseException as exc:  # pragma: no cover - returned to assertion
                failures.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=1.0))
            self.assertEqual(loop.call(lambda: "sentinel"), "sentinel")
        finally:
            release.set()
            worker.join(timeout=2.0)
            loop.stop(timeout=2.0)
        self.assertFalse(worker.is_alive())
        return results, failures

    def _establish_active_goal(self) -> None:
        self.fake.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Continue autonomously",
            status="active",
        )
        self.controller.client_connected("writer-tab")

    def _execute_unknown_goal_resume(
        self,
        *,
        owner: WebThreadOpenCoordinator | None = None,
    ):
        owner = owner or self.open
        self.fake.resume_error = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "connection lost"},
        )
        initial = owner._runtime_call(  # noqa: SLF001
            owner.prepare_read_thread, "writer-tab", "thread-1"
        )
        observed = owner.execute_read_thread_observation(initial)
        prepared = owner._runtime_call(  # noqa: SLF001
            owner.prepare_read_thread_effect, initial, observed
        )
        effect = owner.execute_read_thread_effect(prepared)
        self.assertIsNotNone(effect.autonomous_admission)
        return prepared, effect

    def test_idle_cold_open_commits_bounded_resume_and_interest(self) -> None:
        self.fake.turns = [
            {
                "id": f"turn-{index}",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": f"reply-{index}"}],
            }
            for index in range(12)
        ]

        result = self.open.read_thread(
            "tab-1",
            "thread-1",
            intent_generation=1,
        )

        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertEqual(self.fake.resume_calls[0]["limit"], 10)
        self.assertEqual(
            [message["text"] for message in result["turns"]],
            [f"reply-{index}" for index in range(2, 12)],
        )
        self.assertTrue(result["thread"]["observed_here"])
        self.assertEqual(result["thread"]["loaded_instance"], "default")
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertEqual(interest.outcome, "confirmed")
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertGreaterEqual(self.guard_calls, 1)
        self.next_turn_settings.assert_not_called()
        self.assertIsNone(self.fake.resume_calls[0]["model"])
        self.assertIsNone(self.fake.resume_calls[0]["config_overrides"])
        self.assertIsNone(self.fake.resume_calls[0]["approval_policy"])
        self.assertIsNone(self.fake.resume_calls[0]["permissions_profile_id"])

    def test_loaded_elsewhere_open_is_a_409_without_resume_or_local_lease(self) -> None:
        def reject_resume(*_args, **_kwargs):
            raise ThreadRuntimeAdmissionError(
                "loaded elsewhere",
                blocking_instance="explorer",
                blocking_status="idle",
            )

        open_coordinator = self._make_open(
            acquire_claimed_resume_thread_page=reject_resume,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            open_coordinator.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "thread_loaded_elsewhere")
        self.assertIn("explorer", str(caught.exception))
        self.assertEqual(self.fake.resume_calls, [])
        self.assertEqual(self.service_runtime_leases, set())
        self.assertIsNone(self.controller._runtime_interest.snapshot("thread-1"))

    def test_unverified_runtime_open_is_a_503_without_resume_or_local_lease(self) -> None:
        def reject_resume(*_args, **_kwargs):
            raise ThreadRuntimeAdmissionError(
                "remote control unavailable",
                blocking_instance="explorer",
                blocking_status="unknown",
            )

        open_coordinator = self._make_open(
            acquire_claimed_resume_thread_page=reject_resume,
        )
        profile = self.profile_store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir=self.fake.cwd,
        )
        self.document_registry.materialize_thread("tab-1", "thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            open_coordinator.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "thread_runtime_unverified")
        self.assertIn("explorer", str(caught.exception))
        self.assertNotIn("web open", str(caught.exception))
        self.assertEqual(self.fake.resume_calls, [])
        self.assertEqual(self.service_runtime_leases, set())
        self.assertIsNone(self.controller._runtime_interest.snapshot("thread-1"))
        self.assertEqual(self.profile_store.load("tab-1"), profile)
        self.assertEqual(
            self.document_registry.materialized_thread_id("tab-1"),
            "thread-1",
        )

    def test_direct_target_rejection_cleanup_stays_generation_fenced(self) -> None:
        in_generation_settle = False

        def run_if_generation(generation, callback):
            nonlocal in_generation_settle
            self.require_connection_generation(generation)
            in_generation_settle = True
            try:
                return callback()
            finally:
                in_generation_settle = False

        def read_child(thread_id, include_turns, **kwargs):
            snapshot = self.fake.read_thread(thread_id, include_turns, **kwargs)
            return replace(
                snapshot,
                summary=replace(
                    snapshot.summary,
                    parent_thread_id="root-thread",
                    subagent_kind="threadSpawn",
                ),
            )

        open_owner = self._make_open(
            read_thread=read_child,
            run_if_connection_generation=run_if_generation,
        )

        cleanup_receipt = object()

        def prepare_cleanup(*_args, **_kwargs):
            self.assertFalse(
                in_generation_settle,
                "direct-target profile cleanup blocked RuntimeLoop",
            )
            return cleanup_receipt

        def settle_cleanup(receipt):
            self.assertTrue(
                in_generation_settle,
                "direct-target cleanup settlement escaped the generation fence",
            )
            self.assertIs(receipt, cleanup_receipt)

        with (
            patch.object(
                self.controller._direct_targets,
                "prepare_unusable_thread_cleanup",
                side_effect=prepare_cleanup,
            ) as prepare,
            patch.object(
                self.controller._direct_targets,
                "settle_unusable_thread_cleanup",
                side_effect=settle_cleanup,
            ) as settle,
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                open_owner.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        prepare.assert_called_once_with(
            "thread-1",
            reason="web_direct_target_selection_cleared",
            delete_attachment_scope=True,
        )
        settle.assert_called_once_with(cleanup_receipt)

    def test_global_directory_materializes_missing_remote_loaded_root_only(self) -> None:
        remote_root = ThreadSummary(
            thread_id="thread-remote-root",
            cwd="/work/remote",
            name="Remote root",
            preview="remote root",
            created_at=1,
            updated_at=2,
            source="appServer",
            status="idle",
        )
        remote_child = ThreadSummary(
            thread_id="thread-remote-child",
            cwd="/work/remote",
            name="Remote child",
            preview="remote child",
            created_at=1,
            updated_at=2,
            source="appServer",
            status="idle",
            parent_thread_id="thread-remote-root",
            subagent_kind="threadSpawn",
        )
        self.fake.extra_summaries = [remote_root, remote_child]
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    loaded_thread_ids=(
                        remote_root.thread_id,
                        remote_child.thread_id,
                    ),
                ),
            )
        )
        open_coordinator = self._make_open(
            list_threads=lambda **_kwargs: [self.fake.summary()],
        )

        result = open_coordinator.list_threads(client_id="tab-1", scope="global")

        projected = {thread["id"]: thread for thread in result["threads"]}
        self.assertIn(remote_root.thread_id, projected)
        self.assertNotIn(remote_child.thread_id, projected)
        self.assertEqual(
            projected[remote_root.thread_id]["loaded_instance"],
            "explorer",
        )
        self.assertFalse(projected[remote_root.thread_id]["selectable"])
        self.assertEqual(
            self.fake.reads,
            [
                (remote_child.thread_id, False),
                (remote_root.thread_id, False),
            ],
        )

    def test_global_directory_bounds_missing_remote_loaded_metadata_reads(self) -> None:
        remote_summaries = [
            ThreadSummary(
                thread_id=f"thread-remote-{suffix}",
                cwd="/work/remote",
                name=f"Remote {suffix}",
                preview=f"remote {suffix}",
                created_at=1,
                updated_at=2,
                source="appServer",
                status="idle",
            )
            for suffix in ("a", "b", "c")
        ]
        self.fake.extra_summaries = remote_summaries
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    loaded_thread_ids=tuple(
                        summary.thread_id for summary in reversed(remote_summaries)
                    ),
                ),
            )
        )
        open_coordinator = self._make_open(
            list_threads=lambda **_kwargs: [self.fake.summary()],
        )

        result = open_coordinator.list_threads(client_id="tab-1", scope="global")

        self.assertEqual(
            self.fake.reads,
            [
                (remote_summaries[0].thread_id, False),
                (remote_summaries[1].thread_id, False),
            ],
        )
        projected_ids = {thread["id"] for thread in result["threads"]}
        self.assertIn(remote_summaries[0].thread_id, projected_ids)
        self.assertIn(remote_summaries[1].thread_id, projected_ids)
        self.assertNotIn(remote_summaries[2].thread_id, projected_ids)

    def test_list_bulk_lease_read_runs_outside_real_generation_gate(
        self,
    ) -> None:
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))
        projection_entered = threading.Event()
        allow_projection = threading.Event()
        disconnect_finished = threading.Event()
        effects: list[object] = []
        failures: list[BaseException] = []

        def blocking_runtime_leases():
            projection_entered.set()
            allow_projection.wait(timeout=2.0)
            return self.fake.list_thread_runtime_leases()

        open_owner = self._make_open(
            list_thread_runtime_leases=blocking_runtime_leases,
            capture_connection_generation=(
                gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=gate.run_if_connection_generation,
        )
        prepared = open_owner.prepare_list_threads(client_id="tab-1")

        def execute() -> None:
            try:
                effects.append(open_owner.execute_list_threads(prepared))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(projection_entered.wait(timeout=1.0))

        def disconnect() -> None:
            gate.fence_disconnect(1)
            disconnect_finished.set()

        disconnector = threading.Thread(target=disconnect)
        disconnector.start()
        self.assertTrue(
            disconnect_finished.wait(timeout=0.5),
            "directory projection still holds the connection-generation gate",
        )
        allow_projection.set()
        worker.join(timeout=2.0)
        disconnector.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertFalse(disconnector.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(effects), 1)

    def test_slow_list_bulk_lease_read_does_not_starve_runtime_loop(self) -> None:
        loop = RuntimeLoop(name="web-list-staged-test")
        lease_read_entered = threading.Event()
        allow_lease_read = threading.Event()
        failures: list[BaseException] = []

        def blocking_runtime_leases():
            lease_read_entered.set()
            if not allow_lease_read.wait(timeout=2.0):
                raise AssertionError("test did not release bulk lease read")
            return self.fake.list_thread_runtime_leases()

        open_owner = self._make_open(
            list_thread_runtime_leases=blocking_runtime_leases,
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )

        def list_threads() -> None:
            try:
                open_owner.list_threads(client_id="tab-1")
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        worker = threading.Thread(target=list_threads)
        worker.start()
        try:
            self.assertTrue(lease_read_entered.wait(timeout=1.0))
            self.assertEqual(loop.call(lambda: "sentinel"), "sentinel")
        finally:
            allow_lease_read.set()
            worker.join(timeout=2.0)
            loop.stop(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])

    def test_slow_attachment_projection_does_not_starve_runtime_loop(self) -> None:
        loop = RuntimeLoop(name="web-open-projection-test")
        projection_entered = threading.Event()
        allow_projection = threading.Event()
        failures: list[BaseException] = []
        results: list[dict] = []
        self.fake.turns = [
            {
                "id": "turn-image",
                "status": "completed",
                "items": [
                    {
                        "id": "image-view-1",
                        "type": "imageView",
                        "status": "completed",
                        "path": "/tmp/focus-projection-image.png",
                    }
                ],
            }
        ]

        def blocking_attachment_projection(path: str, *, cwd: str = "") -> str:
            del path, cwd
            projection_entered.set()
            if not allow_projection.wait(timeout=2.0):
                raise AssertionError("test did not release attachment projection")
            return "/api/attachments/projected"

        open_owner = self._make_open(
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )

        def read_thread() -> None:
            try:
                results.append(open_owner.read_thread("tab-1", "thread-1"))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        with patch.object(
            self.controller._workspace,
            "materialize_attachment_url_for_path",
            side_effect=blocking_attachment_projection,
        ):
            worker = threading.Thread(target=read_thread)
            worker.start()
            try:
                self.assertTrue(projection_entered.wait(timeout=1.0))
                self.assertEqual(loop.call(lambda: "sentinel"), "sentinel")
            finally:
                allow_projection.set()
                worker.join(timeout=2.0)
                loop.stop(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 1)

    def test_list_projection_rejects_f5_revision_change(self) -> None:
        prepared = self.open.prepare_list_threads(client_id="tab-1")
        effect = self.open.execute_list_threads(prepared)
        projection = self.open.settle_list_threads(prepared, effect)
        payload = self.open.project_list_threads(projection)

        self.projection.publish(
            "thread_invalidated",
            thread_id="thread-1",
            reason="test_f5",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finalize_list_threads(projection, payload)

        self.assertEqual(caught.exception.code, "stale_thread_list")

    def test_open_projection_rejects_later_notification_without_rolling_back_resume(
        self,
    ) -> None:
        initial = self.open.prepare_read_thread("tab-1", "thread-1")
        observed = self.open.execute_read_thread_observation(initial)
        effect_preparation = self.open.prepare_read_thread_effect(initial, observed)
        effect = self.open.execute_read_thread_effect(effect_preparation)
        settlement = self.open.settle_read_thread(effect_preparation, effect)
        commit = self.open._claim_read_thread_settlement(  # noqa: SLF001
            effect_preparation, effect, settlement, False
        )
        selection = self.open.persist_read_thread_selection(
            effect_preparation, effect, commit
        )
        projection = self.open.commit_read_thread(
            effect_preparation, effect, commit, selection
        )
        payload = self.open.project_read_thread(projection)
        self.controller._thread_read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finalize_read_thread_projection(projection, payload)

        self.assertEqual(caught.exception.code, "stale_thread_read")
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertTrue(interest.ever_confirmed)
        self.assertEqual(self.fake.resumed, ["thread-1"])

    def test_open_projection_rejects_reissued_document_after_known_resume(
        self,
    ) -> None:
        initial = self.open.prepare_read_thread("tab-1", "thread-1")
        observed = self.open.execute_read_thread_observation(initial)
        effect_preparation = self.open.prepare_read_thread_effect(initial, observed)
        effect = self.open.execute_read_thread_effect(effect_preparation)
        settlement = self.open.settle_read_thread(effect_preparation, effect)
        commit = self.open._claim_read_thread_settlement(  # noqa: SLF001
            effect_preparation, effect, settlement, False
        )
        selection = self.open.persist_read_thread_selection(
            effect_preparation, effect, commit
        )
        projection = self.open.commit_read_thread(
            effect_preparation, effect, commit, selection
        )
        payload = self.open.project_read_thread(projection)
        self.document_registry.mark_document_reissued("tab-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finalize_read_thread_projection(projection, payload)

        self.assertEqual(caught.exception.code, "stale_document_read")
        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertTrue(
            self.controller._runtime_interest.snapshot("thread-1").ever_confirmed
        )

    def test_history_projection_rejects_backend_generation_replacement(self) -> None:
        self.open.read_thread("tab-1", "thread-1")
        prepared = self.open.prepare_list_older_turns(
            "tab-1",
            "thread-1",
            cursor="older",
            items_view="full",
        )
        page = self.open.execute_list_older_turns(prepared)
        projection = self.open.settle_list_older_turns(prepared, page)
        payload = self.open.project_older_turns(projection)
        self.backend_connection_generation += 1

        with self.assertRaisesRegex(RuntimeError, "generation changed"):
            self.open.finalize_older_turns(projection, payload)

    def test_history_settlement_reports_newer_notification_as_typed_stale(self) -> None:
        self.open.read_thread("tab-1", "thread-1")
        prepared = self.open.prepare_list_older_turns(
            "tab-1",
            "thread-1",
            cursor="older",
            items_view="full",
        )
        effect = self.open.execute_list_older_turns(prepared)
        self.controller._thread_read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.settle_list_older_turns(prepared, effect)

        self.assertEqual(caught.exception.code, "stale_thread_read")
        self.assertEqual(caught.exception.status, 409)

    def test_open_forwards_an_explicit_supported_turn_window(self) -> None:
        open_coordinator = self._make_open()
        self.fake.turns = [
            {"id": f"turn-{index}", "status": "completed", "items": []}
            for index in range(20)
        ]

        open_coordinator.read_thread("tab-1", "thread-1", turn_limit=20)

        self.assertEqual(self.fake.resume_calls[-1]["limit"], 20)
        self.assertEqual(
            self.controller._thread_read_model.turn_ids("thread-1"),
            tuple(f"turn-{index}" for index in range(20)),
        )

    def test_slow_resume_lease_acquire_does_not_starve_runtime_loop(self) -> None:
        loop = RuntimeLoop(name="web-resume-lease-test")
        acquire_entered = threading.Event()
        allow_acquire = threading.Event()
        failures: list[BaseException] = []
        results: list[dict] = []
        original_acquire = self.resume_authority.acquire_claimed_resume_thread_page

        def blocking_acquire(*args, **kwargs):
            acquire_entered.set()
            if not allow_acquire.wait(timeout=2.0):
                raise AssertionError("test did not release runtime-lease acquire")
            return original_acquire(*args, **kwargs)

        open_owner = self._make_open(
            acquire_claimed_resume_thread_page=blocking_acquire,
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )

        def read_thread() -> None:
            try:
                results.append(open_owner.read_thread("tab-1", "thread-1"))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                failures.append(exc)

        worker = threading.Thread(target=read_thread)
        worker.start()
        try:
            self.assertTrue(acquire_entered.wait(timeout=1.0))
            self.assertEqual(loop.call(lambda: "sentinel"), "sentinel")
        finally:
            allow_acquire.set()
            worker.join(timeout=2.0)
            loop.stop(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]["thread"]["id"], "thread-1")

    def test_legacy_profile_rewrite_does_not_starve_runtime_loop(self) -> None:
        loop = RuntimeLoop(name="web-profile-load-test")
        entered = threading.Event()
        release = threading.Event()
        profile_path = self.profile_store._path()  # noqa: SLF001
        profile_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "profiles": {
                        "tab-1": {
                            "selected_thread_id": "",
                            "working_dir": self.fake.cwd,
                            "scope_generation": 1,
                            "updated_at": 0,
                            "model": "retired-setting",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        original_write = self.profile_store._write_all  # noqa: SLF001

        def blocking_write(profiles):
            if not entered.is_set():
                entered.set()
                if not release.wait(timeout=2.0):
                    raise AssertionError("test did not release legacy rewrite")
            return original_write(profiles)

        open_owner = self._make_open(
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )
        with patch.object(
            self.profile_store,
            "_write_all",
            side_effect=blocking_write,
        ):
            results, failures = self._assert_external_stage_keeps_loop_responsive(
                loop,
                entered,
                release,
                lambda: open_owner.read_thread("tab-1", "thread-1"),
            )

        self.assertEqual(failures, [])
        self.assertEqual(results[0]["thread"]["id"], "thread-1")

    def test_slow_next_turn_settings_load_does_not_starve_runtime_loop(self) -> None:
        self._establish_active_goal()
        loop = RuntimeLoop(name="web-next-turn-settings-test")
        entered = threading.Event()
        release = threading.Event()

        def blocking_settings():
            entered.set()
            if not release.wait(timeout=2.0):
                raise AssertionError("test did not release next-turn settings")
            return self.next_turn_settings_store.load()

        self.next_turn_settings.side_effect = blocking_settings
        open_owner = self._make_open(
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )
        results, failures = self._assert_external_stage_keeps_loop_responsive(
            loop,
            entered,
            release,
            lambda: open_owner.read_thread("writer-tab", "thread-1"),
        )

        self.assertEqual(failures, [])
        self.assertEqual(results[0]["thread"]["id"], "thread-1")

    def test_slow_profile_selection_cas_does_not_starve_runtime_loop(self) -> None:
        loop = RuntimeLoop(name="web-profile-cas-test")
        entered = threading.Event()
        release = threading.Event()
        original_update = self.profile_store.update_if_matches

        def blocking_update(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=2.0):
                raise AssertionError("test did not release profile CAS")
            return original_update(*args, **kwargs)

        open_owner = self._make_open(
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )
        with patch.object(
            self.profile_store,
            "update_if_matches",
            side_effect=blocking_update,
        ):
            results, failures = self._assert_external_stage_keeps_loop_responsive(
                loop,
                entered,
                release,
                lambda: open_owner.read_thread("tab-1", "thread-1"),
            )

        self.assertEqual(failures, [])
        self.assertEqual(results[0]["thread"]["id"], "thread-1")

    def test_slow_autonomous_lease_acquire_does_not_starve_runtime_loop(self) -> None:
        self._establish_active_goal()
        loop = RuntimeLoop(name="web-lease-acquire-test")
        entered = threading.Event()
        release = threading.Event()
        original_acquire = self.store.acquire

        def blocking_acquire(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=2.0):
                raise AssertionError("test did not release interaction acquire")
            return original_acquire(*args, **kwargs)

        open_owner = self._make_open(
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )
        with patch.object(self.store, "acquire", side_effect=blocking_acquire):
            results, failures = self._assert_external_stage_keeps_loop_responsive(
                loop,
                entered,
                release,
                lambda: open_owner.read_thread("writer-tab", "thread-1"),
            )

        self.assertEqual(failures, [])
        self.assertEqual(results[0]["thread"]["id"], "thread-1")

    def test_slow_autonomous_lease_release_does_not_starve_runtime_loop(self) -> None:
        self._establish_active_goal()
        loop = RuntimeLoop(name="web-lease-release-test")
        entered = threading.Event()
        release = threading.Event()
        original_resume = self.resume_authority.execute_prepared_resume_thread_page
        original_release = self.store.release_if_matches

        def resume_then_pause(prepared):
            page = original_resume(prepared)
            self.fake.goal = ThreadGoalSummary(
                thread_id="thread-1",
                objective="Continue autonomously",
                status="paused",
            )
            return page

        def blocking_release(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=2.0):
                raise AssertionError("test did not release interaction cleanup")
            return original_release(*args, **kwargs)

        open_owner = self._make_open(
            execute_prepared_resume_thread_page=resume_then_pause,
            runtime_context_guard=loop.assert_worker_context,
            runtime_call=loop.call,
        )
        with patch.object(
            self.store,
            "release_if_matches",
            side_effect=blocking_release,
        ):
            results, failures = self._assert_external_stage_keeps_loop_responsive(
                loop,
                entered,
                release,
                lambda: open_owner.read_thread("writer-tab", "thread-1"),
            )

        self.assertEqual(failures, [])
        self.assertEqual(results[0]["goal"]["status"], "paused")

    def test_subscribed_open_forwards_the_exact_requested_turn_window(self) -> None:
        self.open.read_thread("tab-1", "thread-1")
        resume_call_count = len(self.fake.resume_calls)

        self.open.read_thread("tab-1", "thread-1", turn_limit=5)

        self.assertEqual(len(self.fake.resume_calls), resume_call_count)
        self.assertEqual(self.fake.turn_pages[-1]["limit"], 5)
        self.assertEqual(self.fake.turn_pages[-1]["items_view"], "full")

    def test_cold_open_rebuild_preserves_response_reasoning_effort(self) -> None:
        projected_snapshots = []

        def capture_snapshot(snapshot, **kwargs):
            projected_snapshots.append(snapshot)
            return project_thread_snapshot(snapshot, **kwargs)

        with patch(
            "bot.web_runtime.thread_read_projection.project_thread_snapshot",
            side_effect=capture_snapshot,
        ):
            self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(len(projected_snapshots), 1)
        self.assertEqual(
            projected_snapshots[0].effective_reasoning_effort,
            "high",
        )

    def test_active_turn_open_projects_exact_context_from_runtime_owners(self) -> None:
        self.fake.turns = [
            {"id": "turn-1", "status": "inProgress", "items": []}
        ]
        self.fake.subscribers = (
            ("subscriber", "chat-b"),
            ("initiator", "chat-a"),
        )
        acquired = self.store.acquire(
            "thread-1",
            make_feishu_interaction_holder(
                "initiator",
                "chat-a",
                owner_pid=0,
            ),
        )
        self.assertIsNotNone(acquired.lease)
        assert acquired.lease is not None
        self.assertIsNotNone(
            self.store.activate_turn(acquired.lease, "turn-1")
        )
        self.effective_settings.record_start_or_resume(
            "thread-1",
            model="gpt-test",
            reasoning_effort="ultra",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        self.effective_settings.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        result = self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(
            result["active_turn_context"],
            {
                "turn_id": "turn-1",
                "initiator": {
                    "kind": "feishu",
                    "binding_id": "p2p:initiator:chat-a",
                },
                "feishu_audience": [
                    "p2p:initiator:chat-a",
                    "p2p:subscriber:chat-b",
                ],
                "settings": {
                    "model": {"value": "gpt-test", "source": "inherited"},
                    "reasoning_effort": {"value": "ultra", "source": "inherited"},
                    "approval_policy": {"value": "never", "source": "inherited"},
                    "permissions_profile_id": {
                        "value": ":workspace",
                        "source": "inherited",
                    },
                },
            },
        )

    def test_active_turn_open_reuses_one_external_lease_observation(self) -> None:
        self.fake.turns = [
            {"id": "turn-1", "status": "inProgress", "items": []}
        ]
        real_load = self.store.load
        load_calls: list[str] = []
        def observe_load(thread_id: str):
            load_calls.append(thread_id)
            return real_load(thread_id)

        with patch.object(
            self.store,
            "load",
            side_effect=observe_load,
        ):
            result = self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(
            load_calls,
            ["thread-1"],
        )
        self.assertEqual(
            result["active_turn_context"]["initiator"],
            {"kind": "autonomous_or_unknown", "binding_id": ""},
        )

    def test_idle_open_projects_null_active_turn_context(self) -> None:
        result = self.open.read_thread("tab-1", "thread-1")

        self.assertIsNone(result["active_turn_context"])

    def test_active_goal_resume_acquires_a_fresh_blank_main_turn_lease(self) -> None:
        self.fake.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Continue autonomously",
            status="active",
        )

        result = self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertTrue(result["thread"]["observed_here"])
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertEqual(lease.holder.holder_id, "web:tab-1")
        self.assertEqual(lease.turn_id, "")
        self.next_turn_settings.assert_called_once_with()
        resume_call = self.fake.resume_calls[-1]
        self.assertEqual(resume_call["model"], "gpt-test")
        self.assertEqual(
            resume_call["config_overrides"],
            {"model_reasoning_effort": "high"},
        )
        self.assertEqual(resume_call["approval_policy"], "never")
        self.assertEqual(
            resume_call["permissions_profile_id"],
            ":danger-full-access",
        )

    def test_document_reissue_before_resume_send_releases_fresh_blank(self) -> None:
        self._establish_active_goal()

        def runtime_call(callback, *args, **kwargs):
            if callback.__name__ == "_complete_read_thread_resume":
                self.document_registry.begin_operation(
                    "writer-tab",
                    operation="thread_open",
                    target_thread_id="thread-1",
                )
            return callback(*args, **kwargs)

        open_owner = self._make_open(runtime_call=runtime_call)

        with self.assertRaises(WebRuntimeError) as caught:
            open_owner.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "stale_document_read")
        self.assertEqual(self.fake.resumed, [])
        self.assertIsNone(self.store.load("thread-1"))

    def test_notification_before_resume_send_releases_fresh_blank(self) -> None:
        self._establish_active_goal()

        def runtime_call(callback, *args, **kwargs):
            if callback.__name__ == "_complete_read_thread_resume":
                self.controller._thread_read_model.observe_notification("thread-1")
            return callback(*args, **kwargs)

        open_owner = self._make_open(runtime_call=runtime_call)

        with self.assertRaises(WebRuntimeError) as caught:
            open_owner.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "stale_thread_read")
        self.assertEqual(self.fake.resumed, [])
        self.assertIsNone(self.store.load("thread-1"))

    def test_backend_replacement_before_resume_send_releases_fresh_blank(self) -> None:
        self._establish_active_goal()
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))

        def runtime_call(callback, *args, **kwargs):
            if callback.__name__ == "_complete_read_thread_resume":
                self.assertTrue(gate.fence_disconnect(1))
            return callback(*args, **kwargs)

        open_owner = self._make_open(
            capture_connection_generation=(
                gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=gate.run_if_connection_generation,
            runtime_call=runtime_call,
        )

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            open_owner.read_thread("writer-tab", "thread-1")

        self.assertEqual(self.fake.resumed, [])
        self.assertIsNone(self.store.load("thread-1"))

    def test_pending_unknown_skips_plain_resume_and_still_materializes_read(
        self,
    ) -> None:
        self.controller.client_connected("reader-tab")
        pending = self.operations.record_unknown_mutation(
            "thread-1",
            operation="rename",
            client_id="reader-tab",
        )

        result = self.open.read_thread("reader-tab", "thread-1")

        self.assertEqual(self.fake.resumed, [])
        self.assertFalse(result["thread"]["observed_here"])
        self.assertEqual(result["mutation_unknown"]["mutation_id"], pending.mutation_id)
        self.assertEqual(
            self.document_registry.materialized_thread_id("reader-tab"),
            "thread-1",
        )

    def test_unknown_goal_resume_releases_fresh_blank_and_allows_explicit_retry(
        self,
    ) -> None:
        self._establish_active_goal()
        self.fake.resume_error = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "connection lost"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertEqual(caught.exception.status, 503)
        self.assertIsNone(self.operations.unknown_mutation_projection("thread-1"))
        self.assertIsNone(self.store.load("thread-1"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertEqual(interest.outcome, "unknown")
        self.assertEqual(interest.desired_client_ids, ("writer-tab",))
        self.assertFalse(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])

        self.fake.resume_error = None
        result = self.open.read_thread("writer-tab", "thread-1")

        self.assertEqual(result["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])
        retry_lease = self.store.load("thread-1")
        self.assertIsNotNone(retry_lease)
        assert retry_lease is not None
        self.assertEqual(retry_lease.holder.holder_id, "web:writer-tab")
        self.assertEqual(retry_lease.turn_id, "")

    def test_unknown_resume_selects_from_a_fresh_profile_successor(self) -> None:
        self._establish_active_goal()
        prepared, effect = self._execute_unknown_goal_resume()
        successor_workspace = self.workspace.parent / "successor-workspace"
        successor_workspace.mkdir()
        successor = self.profile_store.update(
            "writer-tab",
            working_dir=str(successor_workspace),
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finish_read_thread_effect(prepared, effect)

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        selected = self.profile_store.load("writer-tab")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.selected_thread_id, "thread-1")
        self.assertEqual(selected.working_dir, successor.working_dir)
        self.assertEqual(selected.scope_generation, successor.scope_generation + 1)
        self.assertEqual(
            self.document_registry.materialized_thread_id("writer-tab"),
            "thread-1",
        )
        self.assertIsNone(self.store.load("thread-1"))

    def test_unknown_resume_release_survives_stale_document(self) -> None:
        self._establish_active_goal()
        prepared, effect = self._execute_unknown_goal_resume()
        self.document_registry.begin_operation(
            "writer-tab",
            operation="thread_open",
            target_thread_id="thread-1",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finish_read_thread_effect(prepared, effect)

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertIsNone(self.profile_store.load("writer-tab"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.desired_client_ids, ())

    def test_unknown_resume_release_survives_newer_notification(self) -> None:
        self._establish_active_goal()
        prepared, effect = self._execute_unknown_goal_resume()
        self.controller._thread_read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finish_read_thread_effect(prepared, effect)

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertIsNone(self.profile_store.load("writer-tab"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.desired_client_ids, ())

    def test_unknown_resume_release_survives_backend_replacement(self) -> None:
        self._establish_active_goal()
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))
        open_owner = self._make_open(
            capture_connection_generation=(
                gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=gate.run_if_connection_generation,
        )
        prepared, effect = self._execute_unknown_goal_resume(owner=open_owner)
        self.assertTrue(gate.fence_disconnect(1))

        with self.assertRaises(WebRuntimeError) as caught:
            open_owner.finish_read_thread_effect(prepared, effect)

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertIsNone(self.profile_store.load("writer-tab"))
        self.assertIsNone(self.controller._runtime_interest.snapshot("thread-1"))

    def test_ordinary_runtime_error_is_unknown_and_never_auto_retried(self) -> None:
        self.fake.resume_error = RuntimeError("adapter failed after dispatch")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertEqual(self.fake.resumed, ["thread-1"])
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertEqual(interest.outcome, "unknown")

        self.fake.resume_error = None
        result = self.open.read_thread("tab-1", "thread-1")

        self.assertEqual(result["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])

    def test_known_resume_success_settles_before_stale_document_rejection(
        self,
    ) -> None:
        initial = self.open.prepare_read_thread(
            "tab-1",
            "thread-1",
            intent_generation=1,
        )
        observed = self.open.execute_read_thread_observation(initial)
        effect_preparation = self.open.prepare_read_thread_effect(initial, observed)
        effect = self.open.execute_read_thread_effect(effect_preparation)
        self.assertEqual(self.fake.resumed, ["thread-1"])

        self.document_registry.begin_operation(
            "tab-1",
            operation="thread_open",
            target_thread_id="thread-1",
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finish_read_thread_effect(effect_preparation, effect)

        self.assertEqual(caught.exception.code, "stale_document_read")
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertTrue(interest.ever_confirmed)
        self.assertEqual(interest.desired_client_ids, ())
        self.assertEqual(
            self.document_registry.materialized_thread_id("tab-1"),
            "",
        )
        self.assertEqual(self.controller._thread_read_model.turns("thread-1"), ())

        self.controller._runtime_interest.mark_subscription_absent("thread-1")
        retried = self.open.read_thread("tab-1", "thread-1", intent_generation=2)

        self.assertEqual(retried["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])

    def test_known_resume_notification_revision_rejects_before_projection_commit(
        self,
    ) -> None:
        initial = self.open.prepare_read_thread("tab-1", "thread-1")
        observed = self.open.execute_read_thread_observation(initial)
        effect_preparation = self.open.prepare_read_thread_effect(initial, observed)
        effect = self.open.execute_read_thread_effect(effect_preparation)
        self.controller._thread_read_model.observe_notification("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.finish_read_thread_effect(effect_preparation, effect)

        self.assertEqual(caught.exception.code, "stale_thread_read")
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertTrue(interest.ever_confirmed)
        self.assertEqual(interest.desired_client_ids, ())
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.controller._thread_read_model.turns("thread-1"), ())
        self.assertEqual(
            self.document_registry.materialized_thread_id("tab-1"),
            "",
        )

    def test_known_resume_success_consumes_receipt_without_replacement_interest(
        self,
    ) -> None:
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))
        open_owner = self._make_open(
            capture_connection_generation=(
                gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=gate.run_if_connection_generation,
        )
        initial = open_owner.prepare_read_thread("tab-1", "thread-1")
        observed = open_owner.execute_read_thread_observation(initial)
        effect_preparation = open_owner.prepare_read_thread_effect(initial, observed)
        effect = open_owner.execute_read_thread_effect(effect_preparation)
        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertTrue(gate.fence_disconnect(1))

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            open_owner.finish_read_thread_effect(effect_preparation, effect)

        self.assertIsNone(
            self.controller._runtime_interest.snapshot("thread-1")
        )
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        replacement = self.resume_authority.prepare_resume_thread_page(
            "thread-1",
            limit=10,
        )
        self.assertGreater(
            replacement.lease_receipt.generation,
            effect.resume.lease_receipt.generation
            if effect.resume is not None
            else 0,
        )
        self.resume_authority.invalidate_connection()

    def test_interest_commit_precedes_post_resume_projection(self) -> None:
        self._establish_active_goal()
        events: list[str] = []
        original_execute = self.resume_authority.execute_prepared_resume_thread_page
        original_mark = self.controller._runtime_interest.mark_confirmed
        original_remember = self.controller._thread_read_model.install_prepared_turns
        original_goal = self.fake.get_thread_goal

        def execute_resume(prepared):
            page = original_execute(prepared)
            events.append("resume_response")
            return page

        def mark_interest(thread_id: str, *, client_id: str = "") -> None:
            events.append("interest_commit")
            original_mark(thread_id, client_id=client_id)

        def remember_turns(*args, **kwargs) -> None:
            events.append("history_cache")
            original_remember(*args, **kwargs)

        def read_goal(thread_id: str, **kwargs):
            events.append("goal_read")
            return original_goal(thread_id, **kwargs)

        open_owner = self._make_open(
            execute_prepared_resume_thread_page=execute_resume,
            get_thread_goal=read_goal,
        )
        with (
            patch.object(
                self.controller._runtime_interest,
                "mark_confirmed",
                side_effect=mark_interest,
            ),
            patch.object(
                self.controller._thread_read_model,
                "install_prepared_turns",
                side_effect=remember_turns,
            ),
        ):
            open_owner.read_thread("writer-tab", "thread-1")

        interest_index = events.index("interest_commit")
        resume_index = events.index("resume_response")
        post_resume_goal_index = events.index("goal_read", resume_index)
        self.assertLess(resume_index, post_resume_goal_index)
        self.assertLess(post_resume_goal_index, interest_index)
        self.assertLess(interest_index, events.index("history_cache"))

    def test_direct_target_cache_and_projection_commits_stay_generation_fenced(
        self,
    ) -> None:
        in_generation_settle = False
        fenced_calls: list[str] = []

        def run_if_generation(generation, callback):
            nonlocal in_generation_settle
            self.require_connection_generation(generation)
            in_generation_settle = True
            try:
                return callback()
            finally:
                in_generation_settle = False

        def fenced(name, callback):
            def invoke(*args, **kwargs):
                self.assertTrue(
                    in_generation_settle,
                    f"{name} escaped the connection-generation fence",
                )
                fenced_calls.append(name)
                return callback(*args, **kwargs)

            return invoke

        open_owner = self._make_open(
            run_if_connection_generation=run_if_generation,
        )
        targets = (
            (
                self.controller._direct_targets,
                "remember_verified_snapshot",
                "direct_target",
            ),
            (self.controller._runtime_interest, "mark_confirmed", "interest"),
            (self.controller._runtime_interest, "add_desired_client", "interest"),
            (self.controller._thread_read_model, "install_prepared_turns", "turns"),
            (
                self.controller._workspace,
                "materialize_persisted_selection",
                "selection",
            ),
        )
        with ExitStack() as stack:
            for owner, attribute, label in targets:
                original = getattr(owner, attribute)
                stack.enter_context(
                    patch.object(owner, attribute, side_effect=fenced(label, original))
                )
            result = open_owner.read_thread("tab-1", "thread-1")

        self.assertEqual(result["thread"]["id"], "thread-1")
        self.assertEqual(
            set(fenced_calls),
            {"direct_target", "interest", "turns", "selection"},
        )

    def test_safe_observer_interest_commit_failure_compensates_resume(self) -> None:
        with patch.object(
            self.controller._runtime_interest,
            "mark_confirmed",
            side_effect=TimeoutError("runtime interest store timed out"),
        ):
            with self.assertRaises(ThreadResumeLocalCommitFailed) as caught:
                self.open.read_thread("tab-1", "thread-1")

        self.assertFalse(caught.exception.recovery_required)
        self.assertEqual(
            caught.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.COMPENSATED,
        )
        self.assertEqual(self.fake.unsubscribed, ["thread-1"])
        self.assertEqual(self.fake.released, ["thread-1"])
        self.assertFalse(self.controller.retains_runtime("thread-1"))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_resume_compensation_runs_after_real_generation_gate_is_released(
        self,
    ) -> None:
        gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        self.assertTrue(gate.accept(1))
        disconnect_finished = threading.Event()
        disconnect_threads: list[threading.Thread] = []
        original_unsubscribe = self.fake.unsubscribe_thread

        def unsubscribe_after_disconnect_can_fence(thread_id: str) -> None:
            def disconnect() -> None:
                gate.fence_disconnect(1)
                disconnect_finished.set()

            disconnector = threading.Thread(target=disconnect)
            disconnect_threads.append(disconnector)
            disconnector.start()
            if not disconnect_finished.wait(timeout=0.5):
                raise RuntimeError(
                    "resume compensation ran while the generation gate was held"
                )
            original_unsubscribe(thread_id)

        open_owner = self._make_open(
            capture_connection_generation=(
                gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=gate.run_if_connection_generation,
        )
        with (
            patch.object(
                self.controller._runtime_interest,
                "mark_confirmed",
                side_effect=TimeoutError("runtime interest store timed out"),
            ),
            patch.object(
                self.fake,
                "unsubscribe_thread",
                side_effect=unsubscribe_after_disconnect_can_fence,
            ),
        ):
            with self.assertRaises(ThreadResumeLocalCommitFailed) as caught:
                open_owner.read_thread("tab-1", "thread-1")

        for thread in disconnect_threads:
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(
            caught.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.COMPENSATED,
        )
        self.assertEqual(self.fake.unsubscribed, ["thread-1"])

    def test_goal_interest_commit_failure_retains_resume_as_unknown(self) -> None:
        self._establish_active_goal()

        with patch.object(
            self.controller._runtime_interest,
            "mark_confirmed",
            side_effect=RuntimeError("runtime interest commit failed"),
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.open.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        assert interest is not None
        self.assertEqual(interest.outcome, "unknown")
        self.assertEqual(interest.desired_client_ids, ("writer-tab",))
        unknown = self.operations.unknown_mutation_projection("thread-1")
        self.assertIsNotNone(unknown)
        assert unknown is not None
        self.assertEqual(unknown["operation"], "resume")
        self.assertEqual(unknown["durability"], "process_local")
        retained_lease = self.store.load("thread-1")
        self.assertIsNotNone(retained_lease)
        self.assertEqual(retained_lease and retained_lease.turn_id, "")

    def test_unknown_goal_resume_preserves_a_preexisting_active_writer(self) -> None:
        self._establish_active_goal()
        admitted = self.operations.admit_autonomous_turn(
            "writer-tab",
            "thread-1",
            allow_fresh=True,
        )
        active = self.store.activate_turn(admitted.lease, "turn-1")
        self.assertIsNotNone(active)
        self.fake.resume_error = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "connection lost"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertIn("fresh exact blank", str(caught.exception))
        self.assertEqual(self.store.load("thread-1"), active)
        self.assertIsNone(self.operations.unknown_mutation_projection("thread-1"))

    def test_post_resume_paused_goal_releases_the_fresh_blank_lease(self) -> None:
        self._establish_active_goal()
        original_execute = self.resume_authority.execute_prepared_resume_thread_page

        def resume_then_pause(prepared):
            page = original_execute(prepared)
            self.fake.goal = ThreadGoalSummary(
                thread_id="thread-1",
                objective="Continue autonomously",
                status="paused",
            )
            return page

        open_owner = self._make_open(
            execute_prepared_resume_thread_page=resume_then_pause
        )

        snapshot = open_owner.read_thread("writer-tab", "thread-1")

        self.assertEqual(snapshot["goal"]["status"], "paused")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_older_history_requires_materialization_and_uses_bounded_page(self) -> None:
        with self.assertRaises(WebRuntimeError) as caught:
            self.open.list_older_turns("tab-1", "thread-1", cursor="older")
        self.assertEqual(caught.exception.code, "thread_not_selected")

        self.fake.turns = [
            {
                "id": f"turn-{index}",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": f"reply-{index}"}],
            }
            for index in range(12)
        ]
        self.open.read_thread("tab-1", "thread-1")
        result = self.open.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="older",
        )

        self.assertEqual(self.fake.turn_pages[-1]["cursor"], "older")
        self.assertEqual(self.fake.turn_pages[-1]["limit"], 10)
        self.assertEqual(self.fake.turn_pages[-1]["items_view"], "full")
        self.assertEqual(result["items_view"], "full")
        self.assertEqual(
            [message["text"] for message in result["turns"]],
            [f"reply-{index}" for index in range(2, 12)],
        )

    def test_history_forwards_exact_supported_summary_and_full_widths(self) -> None:
        open_coordinator = self._make_open()
        open_coordinator.read_thread("tab-1", "thread-1")

        open_coordinator.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="older",
            items_view="summary",
            turn_limit=20,
        )

        self.assertEqual(self.fake.turn_pages[-1]["limit"], 20)
        self.assertEqual(self.fake.turn_pages[-1]["items_view"], "summary")

        open_coordinator.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="detail-page",
            items_view="full",
            turn_limit=5,
        )

        self.assertEqual(self.fake.turn_pages[-1]["limit"], 5)
        self.assertEqual(self.fake.turn_pages[-1]["items_view"], "full")

    def test_summary_history_is_projected_without_merging_into_read_model(self) -> None:
        long_prompt = "  first\n\tprompt  " + ("x" * 500)
        self.fake.turns = [
            {
                "id": "turn-recent",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "recent prompt"}],
                    },
                    {"type": "agentMessage", "text": "recent reply"},
                ],
            }
        ]
        self.open.read_thread("tab-1", "thread-1")
        recent_turn_ids = self.controller._thread_read_model.turn_ids("thread-1")
        self.fake.turns = [
            {
                "id": "turn-summary",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": long_prompt}],
                    },
                    {"type": "agentMessage", "text": "unused final reply" * 1000},
                ],
            }
        ]

        result = self.open.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="summary-page",
            items_view="summary",
        )

        self.assertEqual(self.fake.turn_pages[-1]["cursor"], "summary-page")
        self.assertEqual(self.fake.turn_pages[-1]["limit"], 10)
        self.assertEqual(self.fake.turn_pages[-1]["items_view"], "summary")
        self.assertEqual(result["items_view"], "summary")
        self.assertEqual(result["page_cursor"], "page:summary-page")
        self.assertEqual(len(result["turns"]), 1)
        self.assertEqual(
            set(result["turns"][0]),
            {"id", "role", "no", "text", "title_truncated"},
        )
        self.assertEqual(result["turns"][0]["id"], "turn-summary:user")
        self.assertEqual(result["turns"][0]["role"], "user")
        self.assertEqual(len(result["turns"][0]["text"]), 160)
        self.assertTrue(result["turns"][0]["text"].endswith("…"))
        self.assertTrue(result["turns"][0]["title_truncated"])
        self.assertTrue(result["turns"][0]["text"].startswith("first prompt "))
        self.assertNotIn("unused final reply", str(result))
        self.assertEqual(
            self.controller._thread_read_model.turn_ids("thread-1"),
            recent_turn_ids,
        )

    def test_summary_head_returns_stable_cursor_reusable_for_full_page(self) -> None:
        self.fake.turns = [
            {
                "id": "turn-head",
                "status": "completed",
                "items": [
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": "head prompt"}],
                    }
                ],
            }
        ]
        self.open.read_thread("tab-1", "thread-1")

        summary = self.open.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="",
            items_view="summary",
        )

        self.assertIsNone(self.fake.turn_pages[-1]["cursor"])
        self.assertEqual(summary["page_cursor"], "page:head")
        detail = self.open.list_older_turns(
            "tab-1",
            "thread-1",
            cursor=summary["page_cursor"],
            items_view="full",
        )
        self.assertEqual(self.fake.turn_pages[-1]["cursor"], "page:head")
        self.assertEqual(detail["items_view"], "full")
        self.assertEqual(detail["turns"][0]["id"], "turn-head:user")

    def test_full_history_a_to_b_does_not_accumulate_in_read_model(self) -> None:
        self.fake.turns = [
            {
                "id": "turn-recent",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": "recent"}],
            }
        ]
        self.open.read_thread("tab-1", "thread-1")
        recent_turn_ids = self.controller._thread_read_model.turn_ids("thread-1")

        for cursor, turn_id in (("page-a", "turn-a"), ("page-b", "turn-b")):
            self.fake.turns = [
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": cursor}],
                }
            ]
            result = self.open.list_older_turns(
                "tab-1",
                "thread-1",
                cursor=cursor,
                items_view="full",
            )
            self.assertEqual(result["items_view"], "full")
            self.assertEqual([turn["text"] for turn in result["turns"]], [cursor])
            self.assertEqual(
                self.controller._thread_read_model.turn_ids("thread-1"),
                recent_turn_ids,
            )

    def test_older_history_rejects_unknown_items_view(self) -> None:
        self.open.read_thread("tab-1", "thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.open.list_older_turns(
                "tab-1",
                "thread-1",
                cursor="older",
                items_view="compact",
            )

        self.assertEqual(caught.exception.code, "invalid_items_view")
        self.assertEqual(self.fake.turn_pages, [])

    def test_inventory_is_bounded_and_never_projects_thread_spawn_as_root(self) -> None:
        seen_limits: list[int] = []

        def list_threads(**kwargs):
            seen_limits.append(kwargs["limit"])
            return [
                self.fake.summary(),
                ThreadSummary(
                    thread_id="child-1",
                    cwd=self.fake.cwd,
                    name="child",
                    preview="child",
                    created_at=1,
                    updated_at=2,
                    source="appServer",
                    status="idle",
                    parent_thread_id="thread-1",
                    subagent_kind="threadSpawn",
                ),
            ]

        open_owner = self._make_open(list_threads=list_threads)
        result = open_owner.list_threads(client_id="tab-1")

        self.assertEqual(seen_limits, [2])
        self.assertEqual([thread["id"] for thread in result["threads"]], ["thread-1"])


def test_thread_open_requires_runtime_loop_guard() -> None:
    try:
        WebThreadOpenCoordinator(
            instance_name="default",
            documents=None,  # type: ignore[arg-type]
            workspace=None,  # type: ignore[arg-type]
            operations=None,  # type: ignore[arg-type]
            lifecycle=None,  # type: ignore[arg-type]
            direct_targets=None,  # type: ignore[arg-type]
            goal_resume_policy=None,  # type: ignore[arg-type]
            read_model=None,  # type: ignore[arg-type]
            runtime_interest=None,  # type: ignore[arg-type]
            selection=None,  # type: ignore[arg-type]
            projection=None,  # type: ignore[arg-type]
            interaction_leases=None,  # type: ignore[arg-type]
            interaction_inbox=None,  # type: ignore[arg-type]
            active_turn_disclosure=None,  # type: ignore[arg-type]
            next_turn_settings=lambda: None,  # type: ignore[return-value]
            shared_interaction_eligible=lambda *_args: False,
            ports=WebThreadOpenPorts(
                list_threads=lambda **_kwargs: [],
                read_thread=lambda _thread_id, _include_turns: None,  # type: ignore[arg-type,return-value]
                list_loaded_thread_ids=lambda: [],
                managed_loaded_thread_inventory=(
                    lambda: ManagedLoadedThreadInventorySnapshot()
                ),
                list_thread_runtime_leases=lambda: [],
                begin_resume_thread_page=lambda **_kwargs: None,  # type: ignore[arg-type,return-value]
                claim_resume_thread_page=lambda _thread_id: None,  # type: ignore[arg-type,return-value]
                acquire_claimed_resume_thread_page=lambda *_args, **_kwargs: None,  # type: ignore[arg-type,return-value]
                complete_claimed_resume_thread_page=lambda *_args, **_kwargs: None,  # type: ignore[arg-type,return-value]
                abandon_resume_thread_page_claim=lambda _claim: None,
                abandon_acquired_resume_thread_page=lambda _receipt: None,
                execute_prepared_resume_thread_page=lambda _prepared: None,  # type: ignore[arg-type,return-value]
                settle_prepared_resume_thread_page=lambda **_kwargs: None,  # type: ignore[arg-type,return-value]
                list_thread_turns=lambda **_kwargs: None,  # type: ignore[arg-type,return-value]
                get_thread_goal=lambda **_kwargs: None,
                prepare_runtime_lease_preflight=lambda _thread_id: None,
                capture_connection_generation=lambda: 1,
                run_if_connection_generation=(
                    lambda _generation, callback: callback()
                ),
            ),
            runtime_context_guard=None,  # type: ignore[arg-type]
            runtime_call=lambda callback, *args, **kwargs: callback(
                *args,
                **kwargs,
            ),
        )
    except TypeError as exc:
        assert "RuntimeLoop" in str(exc)
    else:
        raise AssertionError("missing RuntimeLoop guard was accepted")
