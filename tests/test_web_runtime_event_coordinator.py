import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from bot.runtime_loop import RuntimeLoop
from bot.server_request_contract import ServerRequestIdentity
from bot.web_runtime.interaction_inbox import (
    WebInteractionChange,
    WebInteractionMutation,
    WebInteractionResolution,
)
from bot.web_runtime.mutation_recovery import (
    WebMutationRecoveryRegistry,
    WebUnknownMutation,
)
from bot.web_runtime.event_coordinator import (
    WebRuntimeEventCoordinator,
    WebRuntimeEventPorts,
)
from bot.web_runtime.thread_read_model import WebThreadNotificationUpdate
from bot.web_runtime.thread_read_model import WebThreadReadObservationReceipt


class WebRuntimeEventCoordinatorTests(unittest.TestCase):
    def _build(
        self,
        *,
        guard=None,
        schedule_projection=None,
        schedule_attachment_cleanup=None,
    ):
        runtime_interest = Mock(name="runtime_interest")

        interaction_inbox = Mock(name="interaction_inbox")
        interaction_inbox.hide_for_lifecycle.return_value = WebInteractionMutation()
        interaction_inbox.resolve_exact.return_value = WebInteractionResolution(
            outcome="missing",
            request_key="request-1",
        )

        read_model = Mock(name="read_model")
        read_model.apply_notification.return_value = None
        read_model.capture_observation.side_effect = (
            lambda thread_id: WebThreadReadObservationReceipt(thread_id, 1)
        )
        read_model.observation_is_current.return_value = True
        read_model.turns.return_value = ()
        read_model.collaboration_turns.return_value = ()
        read_model.turn_thread_ids.return_value = ()
        read_model.latest_turn.return_value = None
        read_model.cwd.return_value = "/work/project"

        operations = Mock(name="operations")
        operations.has_unknown_mutation.return_value = False
        prompt_results = Mock(name="prompt_results")

        lifecycle = Mock(name="lifecycle")
        attachments = Mock(name="attachments")
        coordinator_holder: list[WebRuntimeEventCoordinator] = []

        def run_projection(receipt):
            coordinator = coordinator_holder[0]
            detail = coordinator.project_notification(receipt)
            coordinator.settle_notification_projection(receipt, detail)

        def run_attachment_cleanup(scope_key):
            coordinator_holder[0].run_attachment_cleanup(scope_key)

        callbacks = SimpleNamespace(
            clear_thread_selection_facts=Mock(
                name="clear_thread_selection_facts"
            ),
            attachment_url_for_path=Mock(
                name="attachment_url_for_path",
                side_effect=lambda path, *, cwd: f"path:{cwd}:{path}",
            ),
            attachment_url_for_id=Mock(
                name="attachment_url_for_id",
                side_effect=lambda attachment_id: f"id:{attachment_id}",
            ),
            publish_interaction_changes=Mock(
                name="publish_interaction_changes"
            ),
            publish_projection=Mock(
                name="publish_projection",
                return_value={},
            ),
            projection_coordinates=Mock(
                name="projection_coordinates",
                return_value={"runtime_epoch": "epoch-1", "revision": 0},
            ),
            schedule_notification_projection=Mock(
                name="schedule_notification_projection",
                side_effect=schedule_projection or run_projection,
            ),
            schedule_attachment_cleanup=Mock(
                name="schedule_attachment_cleanup",
                side_effect=(
                    schedule_attachment_cleanup or run_attachment_cleanup
                ),
            ),
        )
        ports = WebRuntimeEventPorts(
            runtime_interest=runtime_interest,
            interaction_inbox=interaction_inbox,
            read_model=read_model,
            operations=operations,
            prompt_results=prompt_results,
            lifecycle=lifecycle,
            attachments=attachments,
            clear_thread_selection_facts=(
                callbacks.clear_thread_selection_facts
            ),
            attachment_url_for_path=callbacks.attachment_url_for_path,
            attachment_url_for_id=callbacks.attachment_url_for_id,
            publish_interaction_changes=(
                callbacks.publish_interaction_changes
            ),
            publish_projection=callbacks.publish_projection,
            projection_coordinates=callbacks.projection_coordinates,
            schedule_notification_projection=(
                callbacks.schedule_notification_projection
            ),
            schedule_attachment_cleanup=(
                callbacks.schedule_attachment_cleanup
            ),
        )
        coordinator = WebRuntimeEventCoordinator(
            ports=ports,
            runtime_context_guard=guard or (lambda: None),
        )
        coordinator_holder.append(coordinator)
        owners = SimpleNamespace(
            runtime_interest=runtime_interest,
            interaction_inbox=interaction_inbox,
            read_model=read_model,
            operations=operations,
            prompt_results=prompt_results,
            lifecycle=lifecycle,
            attachments=attachments,
        )
        return coordinator, owners, callbacks

    @staticmethod
    def _identity() -> ServerRequestIdentity:
        return ServerRequestIdentity(
            request_id="request-1",
            connection_generation=1,
            method="item/tool/requestUserInput",
            params={"threadId": "child-1", "turnId": "turn-1"},
        )

    @staticmethod
    def _turn_update(
        text: str,
        *,
        method: str = "item/completed",
        local_image_path: str = "",
    ) -> WebThreadNotificationUpdate:
        items: list[dict] = [
            {
                "id": "agent-1",
                "type": "agentMessage",
                "text": text,
            }
        ]
        if local_image_path:
            items.insert(
                0,
                {
                    "id": "user-1",
                    "type": "userMessage",
                    "content": [
                        {"type": "localImage", "path": local_image_path}
                    ],
                },
            )
        return WebThreadNotificationUpdate(
            method=method,
            thread_id="root-1",
            detail={"method": method, "turn_id": "turn-1"},
            raw_turn={
                "id": "turn-1",
                "status": "inProgress",
                "items": items,
            },
        )

    def test_runtime_guard_fails_before_any_port_call(self) -> None:
        commands = {
            "resolve": lambda owner: owner.remove_resolved_server_request(
                self._identity()
            ),
            "notification": lambda owner: owner.handle_notification(
                "turn/completed",
                {"threadId": "root-1"},
            ),
            "drop": lambda owner: owner.drop_thread_after_lifecycle("root-1"),
            "schedule_cleanup": lambda owner: owner.schedule_attachment_cleanup(
                "root-1"
            ),
        }
        for name, invoke in commands.items():
            with self.subTest(command=name):
                guard = Mock(side_effect=RuntimeError("outside RuntimeLoop"))
                coordinator, owners, callbacks = self._build(guard=guard)

                with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
                    invoke(coordinator)

                guard.assert_called_once_with()
                for port in vars(owners).values():
                    self.assertEqual(port.mock_calls, [])
                for callback in vars(callbacks).values():
                    self.assertEqual(callback.mock_calls, [])

    def test_exact_server_request_resolution_orders_local_removal_and_projection(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        calls: list[str] = []
        identity = self._identity()
        owners.interaction_inbox.resolve_exact.side_effect = lambda _identity: (
            calls.append("resolve_inbox")
            or WebInteractionResolution(
                outcome="resolved",
                request_key=identity.request_key,
                owner_thread_id="root-1",
                thread_id="root-1",
                changes=(
                    WebInteractionChange("root-1", "request_resolved"),
                ),
            )
        )
        callbacks.publish_interaction_changes.side_effect = (
            lambda _changes: calls.append("publish_changes")
        )
        removal = coordinator.remove_resolved_server_request(identity)

        self.assertEqual(removal.outcome, "removed")
        self.assertEqual(removal.root_thread_id, "root-1")
        self.assertEqual(
            calls,
            [
                "resolve_inbox",
                "publish_changes",
            ],
        )
        owners.lifecycle.maybe_release_runtime.assert_called_once_with("root-1")

    def test_turn_projection_and_attachment_materialization_run_after_schedule(
        self,
    ) -> None:
        scheduled = []
        coordinator, owners, callbacks = self._build(
            schedule_projection=scheduled.append,
        )
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.return_value = self._turn_update(
            "ready",
            local_image_path="/work/project/result.png",
        )

        coordinator.handle_notification(
            "item/completed",
            {"threadId": "root-1", "turnId": "turn-1"},
        )

        self.assertEqual(len(scheduled), 1)
        callbacks.attachment_url_for_path.assert_not_called()
        callbacks.publish_projection.assert_not_called()

        receipt = scheduled[0]
        detail = coordinator.project_notification(receipt)
        callbacks.attachment_url_for_path.assert_called_once_with(
            "/work/project/result.png",
            cwd="/work/project",
        )
        self.assertIsNone(
            coordinator.settle_notification_projection(receipt, detail)
        )
        callbacks.publish_projection.assert_called_once_with(
            "thread_delta",
            thread_id="root-1",
            reason="item/completed",
            detail=detail,
        )

    def test_notification_projection_keeps_one_flight_and_latest_successor(
        self,
    ) -> None:
        scheduled = []
        coordinator, owners, callbacks = self._build(
            schedule_projection=scheduled.append,
        )
        revision = [0]

        def observe(_thread_id):
            revision[0] += 1
            return revision[0]

        owners.read_model.observe_notification.side_effect = observe
        owners.read_model.capture_observation.side_effect = (
            lambda thread_id: WebThreadReadObservationReceipt(
                thread_id,
                revision[0],
            )
        )
        owners.read_model.observation_is_current.side_effect = (
            lambda receipt: receipt.revision == revision[0]
        )
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.side_effect = (
            lambda _method, params: self._turn_update(str(params["text"]))
        )

        for text in ("first", "second", "latest"):
            coordinator.handle_notification(
                "item/completed",
                {
                    "threadId": "root-1",
                    "turnId": "turn-1",
                    "text": text,
                },
            )

        self.assertEqual(len(scheduled), 1)
        first = scheduled[0]
        self.assertIsNone(coordinator.settle_notification_projection(
            first,
            coordinator.project_notification(first),
        ))
        callbacks.publish_projection.assert_not_called()
        self.assertEqual(len(scheduled), 2)

        successor = scheduled[1]
        latest_detail = coordinator.project_notification(successor)
        self.assertIn("latest", repr(latest_detail["turns"]))
        self.assertNotIn("second", repr(latest_detail["turns"]))
        self.assertIsNone(
            coordinator.settle_notification_projection(
                successor,
                latest_detail,
            )
        )
        self.assertEqual(
            [call.args[0] for call in callbacks.publish_projection.call_args_list],
            ["thread_delta"],
        )

    def test_successor_admission_failure_retires_flight_and_invalidates(
        self,
    ) -> None:
        scheduled = []

        def schedule(receipt) -> None:
            if scheduled:
                raise RuntimeError("service ingress is stopping")
            scheduled.append(receipt)

        coordinator, owners, callbacks = self._build(
            schedule_projection=schedule,
        )
        revision = [0]
        owners.read_model.observe_notification.side_effect = (
            lambda _thread_id: revision.__setitem__(0, revision[0] + 1)
            or revision[0]
        )
        owners.read_model.capture_observation.side_effect = (
            lambda thread_id: WebThreadReadObservationReceipt(
                thread_id,
                revision[0],
            )
        )
        owners.read_model.observation_is_current.side_effect = (
            lambda receipt: receipt.revision == revision[0]
        )
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.side_effect = (
            lambda _method, params: self._turn_update(str(params["text"]))
        )

        coordinator.handle_notification(
            "item/completed",
            {"threadId": "root-1", "turnId": "turn-1", "text": "first"},
        )
        coordinator.handle_notification(
            "item/completed",
            {"threadId": "root-1", "turnId": "turn-1", "text": "latest"},
        )

        with self.assertLogs(
            "bot.web_runtime.event_coordinator",
            level="ERROR",
        ):
            coordinator.settle_notification_projection(
                scheduled[0],
                coordinator.project_notification(scheduled[0]),
            )

        self.assertEqual(len(scheduled), 1)
        callbacks.publish_projection.assert_called_once_with(
            "thread_invalidated",
            thread_id="root-1",
            reason="item/completed",
        )

    def test_stale_runtime_epoch_drops_detached_projection(self) -> None:
        scheduled = []
        coordinator, owners, callbacks = self._build(
            schedule_projection=scheduled.append,
        )
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.return_value = self._turn_update(
            "stale"
        )

        coordinator.handle_notification(
            "item/completed",
            {"threadId": "root-1", "turnId": "turn-1"},
        )
        receipt = scheduled[0]
        callbacks.projection_coordinates.return_value = {
            "runtime_epoch": "replacement-epoch",
            "revision": 0,
        }

        self.assertIsNone(
            coordinator.settle_notification_projection(
                receipt,
                coordinator.project_notification(receipt),
            )
        )
        callbacks.publish_projection.assert_not_called()

    def test_projection_schedule_failure_publishes_lightweight_invalidation(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build(
            schedule_projection=Mock(side_effect=RuntimeError("worker closed")),
        )
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.return_value = self._turn_update(
            "not projected"
        )

        with self.assertLogs(
            "bot.web_runtime.event_coordinator",
            level="ERROR",
        ):
            coordinator.handle_notification(
                "item/completed",
                {"threadId": "root-1", "turnId": "turn-1"},
            )

        callbacks.attachment_url_for_path.assert_not_called()
        callbacks.publish_projection.assert_called_once_with(
            "thread_invalidated",
            thread_id="root-1",
            reason="item/completed",
        )

    def test_slow_attachment_projection_does_not_block_runtime_loop(self) -> None:
        runtime_loop = RuntimeLoop(name="web-notification-projection-test")
        projection_entered = threading.Event()
        release_projection = threading.Event()
        workers: list[threading.Thread] = []
        worker_errors: list[BaseException] = []
        coordinator_holder: list[WebRuntimeEventCoordinator] = []

        def schedule(receipt) -> None:
            def run() -> None:
                try:
                    coordinator = coordinator_holder[0]
                    detail = coordinator.project_notification(receipt)
                    runtime_loop.call(
                        coordinator.settle_notification_projection,
                        receipt,
                        detail,
                    )
                except BaseException as exc:
                    worker_errors.append(exc)

            worker = threading.Thread(
                target=run,
                name="slow-web-notification-projection",
            )
            workers.append(worker)
            worker.start()

        coordinator, owners, callbacks = self._build(
            guard=runtime_loop.assert_worker_context,
            schedule_projection=schedule,
        )
        coordinator_holder.append(coordinator)
        owners.runtime_interest.has_managed_interest.return_value = True
        owners.read_model.apply_notification.return_value = self._turn_update(
            "ready",
            local_image_path="/work/project/slow.png",
        )

        def slow_attachment(path, *, cwd):
            self.assertEqual(path, "/work/project/slow.png")
            self.assertEqual(cwd, "/work/project")
            projection_entered.set()
            if not release_projection.wait(timeout=2):
                raise TimeoutError("test did not release attachment projection")
            return "/api/attachments/observed-image"

        callbacks.attachment_url_for_path.side_effect = slow_attachment
        runtime_loop.start()
        try:
            runtime_loop.call(
                coordinator.handle_notification,
                "item/completed",
                {"threadId": "root-1", "turnId": "turn-1"},
            )
            self.assertTrue(projection_entered.wait(timeout=1))
            self.assertEqual(
                runtime_loop.call_with_deadline(0.5, lambda: "sentinel"),
                "sentinel",
            )
        finally:
            release_projection.set()
            for worker in workers:
                worker.join(timeout=2)
            runtime_loop.stop(timeout=2)

        self.assertEqual(worker_errors, [])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        callbacks.publish_projection.assert_called_once()

    def test_root_and_child_terminal_notifications_do_not_extend_main_turn_owner(
        self,
    ) -> None:
        for thread_id in ("root-1", "child-1"):
            with self.subTest(thread_id=thread_id):
                coordinator, owners, callbacks = self._build()
                calls: list[str] = []
                owners.interaction_inbox.hide_for_lifecycle.side_effect = (
                    lambda *_args, **_kwargs: (
                        calls.append("hide_interaction")
                        or WebInteractionMutation()
                    )
                )
                callbacks.publish_interaction_changes.side_effect = (
                    lambda _changes: calls.append("publish_changes")
                )
                owners.read_model.apply_notification.side_effect = (
                    lambda *_args: calls.append("apply_read_model") or None
                )
                coordinator.handle_notification(
                    "turn/completed",
                    {
                        "threadId": thread_id,
                        "turn": {"id": "turn-1", "status": "completed"},
                    },
                )

                self.assertEqual(
                    calls,
                    [
                        "hide_interaction",
                        "publish_changes",
                        "apply_read_model",
                    ],
                )
                owners.lifecycle.maybe_release_runtime.assert_called_once_with(
                    thread_id,
                    known_non_active=True,
                )

    def test_archive_and_delete_converge_selection_read_and_attachments(
        self,
    ) -> None:
        for method in ("thread/archived", "thread/deleted"):
            with self.subTest(method=method):
                coordinator, owners, callbacks = self._build()
                calls: list[str] = []
                owners.interaction_inbox.hide_for_lifecycle.side_effect = (
                    lambda *_args, **_kwargs: WebInteractionMutation()
                )
                callbacks.clear_thread_selection_facts.side_effect = (
                    lambda *_args, **_kwargs: calls.append("clear_selection")
                )
                owners.read_model.forget_thread.side_effect = (
                    lambda _thread_id: calls.append("forget_read")
                )
                owners.attachments.delete_scope.side_effect = (
                    lambda _scope: calls.append("delete_attachments")
                )
                owners.lifecycle.maybe_release_runtime.side_effect = (
                    lambda *_args, **_kwargs: calls.append("release_root")
                )
                callbacks.publish_projection.side_effect = (
                    lambda event, **_kwargs: calls.append(f"project:{event}") or {}
                )

                coordinator.handle_notification(
                    method,
                    {"threadId": "root-1"},
                )

                expected = []
                if method == "thread/deleted":
                    expected.append("delete_attachments")
                expected.extend(
                    [
                        "clear_selection",
                        "forget_read",
                        "release_root",
                        "project:thread_invalidated",
                    ]
                )
                self.assertEqual(calls, expected)
                callbacks.clear_thread_selection_facts.assert_called_once_with(
                    "root-1",
                    reason="web_lifecycle_selection_cleared",
                )
                if method == "thread/archived":
                    owners.attachments.delete_scope.assert_not_called()
                else:
                    owners.attachments.delete_scope.assert_called_once_with(
                        "thread:root-1"
                    )

    def test_deleted_attachment_scope_is_only_touched_by_scheduled_worker(
        self,
    ) -> None:
        scheduled_scopes: list[str] = []
        coordinator, owners, callbacks = self._build(
            schedule_attachment_cleanup=scheduled_scopes.append,
        )

        coordinator.handle_notification(
            "thread/deleted",
            {"threadId": "root-1"},
        )

        self.assertEqual(scheduled_scopes, ["thread:root-1"])
        owners.attachments.delete_scope.assert_not_called()
        coordinator.run_attachment_cleanup(scheduled_scopes[0])
        owners.attachments.delete_scope.assert_called_once_with(
            "thread:root-1"
        )
        self.assertEqual(
            callbacks.publish_projection.call_args.args[0],
            "thread_invalidated",
        )

    def test_deleted_attachment_cleanup_failure_does_not_block_fact_convergence(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        calls: list[str] = []
        owners.attachments.delete_scope.side_effect = RuntimeError(
            "attachment store unavailable"
        )
        callbacks.clear_thread_selection_facts.side_effect = (
            lambda *_args, **_kwargs: calls.append("clear_selection")
        )
        owners.read_model.forget_thread.side_effect = (
            lambda _thread_id: calls.append("forget_read")
        )
        owners.lifecycle.maybe_release_runtime.side_effect = (
            lambda *_args, **_kwargs: calls.append("release_root")
        )
        with self.assertLogs(
            "bot.web_runtime.event_coordinator",
            level="ERROR",
        ):
            coordinator.handle_notification(
                "thread/deleted",
                {"threadId": "root-1"},
            )

        self.assertEqual(
            calls,
            [
                "clear_selection",
                "forget_read",
                "release_root",
            ],
        )
        self.assertEqual(
            callbacks.publish_projection.call_args.args[0],
            "thread_invalidated",
        )

    def test_lifecycle_notifications_preserve_same_operation_replacement(self) -> None:
        cases = (
            ("archive", "thread/archived"),
            ("unarchive", "thread/unarchived"),
            ("delete", "thread/deleted"),
        )
        for index, (operation, method) in enumerate(cases, start=1):
            with self.subTest(operation=operation):
                coordinator, owners, callbacks = self._build()
                registry = WebMutationRecoveryRegistry(
                    runtime_context_guard=lambda: None,
                )
                thread_id = f"root-aba-{index}"
                original = WebUnknownMutation.create(
                    thread_id=thread_id,
                    operation=operation,
                    client_id="tab-1",
                    durability="process_local",
                )
                replacement = WebUnknownMutation.create(
                    thread_id=thread_id,
                    operation=operation,
                    client_id="tab-1",
                    durability="process_local",
                )
                registry.remember(original)
                registry.settle_exact(
                    thread_id,
                    original.mutation_id,
                    "user_discard",
                )
                registry.remember(replacement)
                owners.operations.has_unknown_mutation.side_effect = registry.contains
                owners.operations.reconcile_unknown_from_turns.side_effect = (
                    lambda target_thread_id, turns: registry.reconcile_turns(
                        target_thread_id,
                        turns,
                    )
                    is not None
                )

                coordinator.handle_notification(method, {"threadId": thread_id})

                self.assertNotEqual(original.mutation_id, replacement.mutation_id)
                self.assertEqual(registry.get(thread_id), replacement)
                self.assertNotIn(
                    "mutation_reconciled",
                    [
                        published.args[0]
                        for published in callbacks.publish_projection.call_args_list
                    ],
                )

    def test_projection_distinguishes_delta_invalidation_and_ignored_events(
        self,
    ) -> None:
        coordinator, owners, callbacks = self._build()
        delta_methods = {
            "thread/tokenUsage/updated",
            "turn/diff/updated",
            "turn/plan/updated",
            "item/mcpToolCall/progress",
        }
        owners.read_model.apply_notification.side_effect = lambda method, _params: (
            WebThreadNotificationUpdate(
                method=method,
                thread_id="root-1",
                detail={"method": method},
            )
            if method in delta_methods
            else None
        )

        for method in delta_methods:
            coordinator.handle_notification(
                method,
                {"threadId": "root-1", "turnId": "turn-1"},
            )
        for method in (
            "rawResponseItem/completed",
            "rawResponse/completed",
            "process/outputDelta",
            "unknown/notification",
        ):
            coordinator.handle_notification(method, {"threadId": "root-1"})
        for method, params in (
            (
                "thread/compacted",
                {"threadId": "root-1", "turnId": "turn-1"},
            ),
            ("thread/archived", {"threadId": "root-1"}),
            ("error", {"threadId": "root-1", "turnId": "turn-1"}),
            ("thread/started", {"thread": {"id": "thread-new"}}),
        ):
            coordinator.handle_notification(method, params)

        self.assertEqual(
            [call.args[0] for call in callbacks.publish_projection.call_args_list],
            ["thread_delta"] * len(delta_methods) + ["thread_invalidated"] * 4,
        )
        self.assertEqual(
            {
                call.kwargs["reason"]
                for call in callbacks.publish_projection.call_args_list[
                    : len(delta_methods)
                ]
            },
            delta_methods,
        )
        self.assertEqual(
            [
                call.kwargs["reason"]
                for call in callbacks.publish_projection.call_args_list[
                    len(delta_methods) :
                ]
            ],
            ["thread/compacted", "thread/archived", "error", "thread/started"],
        )

    def test_error_notice_follows_existing_thread_invalidation(self) -> None:
        coordinator, owners, callbacks = self._build()
        owners.runtime_interest.has_managed_interest.return_value = False

        coordinator.handle_notification(
            "error",
            {
                "threadId": "root-1",
                "turnId": "turn-1",
                "willRetry": True,
                "error": {
                    "message": "stream disconnected",
                    "additionalDetails": "Reconnecting 1/5",
                    "codexErrorInfo": {"httpStatusCode": 502},
                },
            },
        )

        self.assertEqual(
            [call.args[0] for call in callbacks.publish_projection.call_args_list],
            ["thread_invalidated", "runtime_notice"],
        )
        self.assertEqual(
            callbacks.publish_projection.call_args_list[-1].kwargs,
            {
                "thread_id": "root-1",
                "reason": "error",
                "detail": {
                    "method": "error",
                    "message": "stream disconnected",
                    "additional_details": "Reconnecting 1/5",
                    "will_retry": True,
                    "turn_id": "turn-1",
                },
            },
        )

    def test_warning_notice_has_no_runtime_or_lifecycle_side_effect(self) -> None:
        for params, expected_thread_id in (
            ({"message": "skills trimmed"}, ""),
            ({"threadId": "root-1", "message": "skills trimmed"}, "root-1"),
        ):
            with self.subTest(params=params):
                coordinator, owners, callbacks = self._build()

                coordinator.handle_notification("warning", params)

                callbacks.publish_projection.assert_called_once_with(
                    "runtime_notice",
                    thread_id=expected_thread_id,
                    reason="warning",
                    detail={
                        "method": "warning",
                        "message": "skills trimmed",
                    },
                )
                for owner in vars(owners).values():
                    self.assertEqual(owner.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
