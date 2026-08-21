import os
import uuid
from unittest.mock import Mock, patch

from bot.adapters.base import (
    ThreadGoalSummary,
)
from bot.codex_protocol.client import (
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.stores.interaction_lease_store import (
    make_feishu_interaction_holder,
    make_fcodex_interaction_holder,
)
from bot.thread_runtime_authority import ThreadResumeInProgress
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
)


class WebRuntimeInterestResumeTests(WebRuntimeControllerHarness):
    def _establish_active_goal(
        self,
        client_id: str = "writer-tab",
    ) -> None:
        self.fake.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Finish safely",
            status="active",
            token_budget=1000,
            tokens_used=25,
            time_used_seconds=3,
        )
        self.controller.client_connected(client_id)

    def test_first_observer_resumes_and_additional_observer_reads_without_resuming(
        self,
    ):
        settings_snapshot = Mock(
            side_effect=self.controller._thread_open._next_turn_settings
        )
        self.controller._thread_open._next_turn_settings = settings_snapshot

        first = self.controller.read_thread("tab-1", "thread-1")
        second = self.controller.read_thread("tab-2", "thread-1")

        self.assertEqual(first["thread"]["id"], "thread-1")
        self.assertEqual(first["thread"]["loaded_instance"], "default")
        self.assertTrue(first["thread"]["observed_here"])
        self.assertEqual(second["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertEqual(self.fake.resume_calls[0]["limit"], 10)
        self.assertEqual(self.fake.reads, [("thread-1", False), ("thread-1", False)])
        self.assertEqual(len(self.fake.turn_pages), 1)
        self.assertTrue(self.controller.retains_runtime("thread-1"))
        self.assertEqual(
            self.controller._runtime_interest.snapshot("thread-1").desired_client_ids,
            ("tab-1", "tab-2"),
        )
        settings_snapshot.assert_not_called()

        self.controller.client_disconnected("tab-1")

        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        self.assertEqual(
            self.controller._runtime_interest.snapshot("thread-1").desired_client_ids,
            ("tab-2",),
        )

        self.controller.client_disconnected("tab-2")

        self.assertEqual(self.fake.unsubscribed, ["thread-1"])
        self.assertEqual(self.fake.released, ["thread-1"])

    def test_thread_closed_invalidates_subscription_but_preserves_browser_desire(self):
        self.controller.read_thread("tab-1", "thread-1")

        self.controller.handle_notification(
            "thread/closed",
            {"threadId": "thread-1"},
        )

        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertEqual(interest.subscription_epoch, 0)
        self.assertFalse(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )

        self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])

    def test_not_loaded_status_invalidates_subscription_without_clearing_desire(self):
        self.controller.read_thread("tab-1", "thread-1")

        self.controller.handle_notification(
            "thread/status/changed",
            {"threadId": "thread-1", "status": {"type": "notLoaded"}},
        )

        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertEqual(interest.subscription_epoch, 0)
        self.assertFalse(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )

    def test_broadcast_metadata_after_backend_disconnect_cannot_skip_resume_preflight(
        self,
    ):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.backend_disconnected()
        goal_reads = 0
        original_get_goal = self.fake.get_thread_goal

        def count_goal_read(thread_id: str, **_kwargs):
            nonlocal goal_reads
            goal_reads += 1
            return original_get_goal(thread_id)

        self.fake.get_thread_goal = count_goal_read

        # Upstream broadcasts name updates to every initialized connection;
        # this does not prove that the reconnected service subscribed or
        # loaded this thread.
        self.controller.handle_notification(
            "thread/name/updated",
            {"threadId": "thread-1", "threadName": "Renamed elsewhere"},
        )

        self.assertFalse(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )
        self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(goal_reads, 1)
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])

    def test_thread_scoped_notification_can_confirm_current_subscription(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.backend_disconnected()

        # Turn lifecycle is sent by upstream's ThreadScopedOutgoingMessageSender
        # to this thread's subscribed connection ids.
        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-live", "status": "inProgress", "items": []},
            },
        )

        self.assertTrue(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )
        self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1"])

    def test_scoped_child_notification_does_not_confirm_root_subscription(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.backend_disconnected()

        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "child-1",
                "turn": {"id": "child-turn", "status": "inProgress", "items": []},
            },
        )

        self.assertFalse(
            self.controller._runtime_interest.subscription_is_current("thread-1")
        )

    def test_post_commit_goal_read_failure_keeps_confirmed_runtime_interest(self):
        self._establish_active_goal()
        original_goal = self.fake.get_thread_goal
        goal_reads = 0

        def fail_second_goal_read(thread_id: str):
            nonlocal goal_reads
            goal_reads += 1
            if goal_reads == 2:
                raise TimeoutError("goal projection timed out")
            return original_goal(thread_id)

        self.fake.get_thread_goal = fail_second_goal_read

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "goal_state_unconfirmed")
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.outcome, "confirmed")
        self.assertEqual(interest.desired_client_ids, ("writer-tab",))
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "")

    def test_post_resume_projection_failure_retires_the_exact_claim(self):
        self.controller._thread_read_model.prepare_turn_replacement = Mock(
            side_effect=OSError("projection preparation failed")
        )

        with self.assertRaisesRegex(OSError, "projection preparation failed"):
            self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(self.fake.resumed, ["thread-1"])
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.outcome, "confirmed")
        claim = self.resume_authority.claim_resume_thread_page("thread-1")
        self.resume_authority.abandon_resume_thread_page_claim(claim)

    def test_fatal_post_resume_projection_retires_claim_before_reraising(self):
        self.controller._thread_read_model.prepare_turn_replacement = Mock(
            side_effect=KeyboardInterrupt()
        )

        with self.assertRaises(KeyboardInterrupt):
            self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(self.fake.resumed, ["thread-1"])
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.outcome, "confirmed")
        claim = self.resume_authority.claim_resume_thread_page("thread-1")
        self.resume_authority.abandon_resume_thread_page_claim(claim)

    def test_unknown_goal_resume_keeps_the_fresh_blank_lease(self):
        """An unreadable goal can resume only under an exact local submission lease."""

        goal_reads = 0

        def unreadable_goal(_thread_id: str, **_kwargs):
            nonlocal goal_reads
            goal_reads += 1
            raise RuntimeError("goal read temporarily unavailable")

        self.fake.get_thread_goal = unreadable_goal
        self.controller.client_connected("observer-tab")

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("observer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "goal_state_unconfirmed")
        self.assertEqual(goal_reads, 2)
        self.assertEqual(self.fake.resumed, ["thread-1"])
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.holder.holder_id, "web:observer-tab")
        self.assertEqual(lease and lease.turn_id, "")

    def test_future_goal_status_resumes_under_a_fresh_blank_lease(self):
        """A newer status is continuation-capable until proven otherwise."""

        self.fake.goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Finish safely",
            status="awaitingExternalResult",
            token_budget=1000,
            tokens_used=25,
            time_used_seconds=3,
        )
        self.controller.client_connected("observer-tab")

        snapshot = self.controller.read_thread("observer-tab", "thread-1")

        self.assertEqual(snapshot["goal"]["status"], "awaitingExternalResult")
        self.assertTrue(snapshot["thread"]["observed_here"])
        self.assertEqual(self.fake.resumed, ["thread-1"])
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.holder.holder_id, "web:observer-tab")
        self.assertEqual(lease and lease.turn_id, "")

    def test_noncontinuing_goal_resume_does_not_disturb_an_existing_turn_lease(
        self,
    ):
        fcodex_holder = make_fcodex_interaction_holder("fcodex-owner", owner_pid=0)
        self.assertTrue(self.store.acquire("thread-1", fcodex_holder).granted)

        snapshot = self.controller.read_thread("observer-tab", "thread-1")

        self.assertEqual(snapshot["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertTrue(self.store.load("thread-1").holder.same_holder(fcodex_holder))

    def test_active_goal_resume_consumes_one_next_turn_settings_snapshot(self):
        self._establish_active_goal()
        settings_snapshot = Mock(
            side_effect=self.controller._thread_open._next_turn_settings
        )
        self.controller._thread_open._next_turn_settings = settings_snapshot

        snapshot = self.controller.read_thread("writer-tab", "thread-1")

        self.assertEqual(snapshot["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1"])
        settings_snapshot.assert_called_once_with()
        resume_call = self.fake.resume_calls[-1]
        self.assertEqual(resume_call["approval_policy"], "never")
        self.assertEqual(
            resume_call["permissions_profile_id"],
            ":danger-full-access",
        )
        self.assertEqual(resume_call["model"], "gpt-test")
        self.assertEqual(
            resume_call["config_overrides"],
            {"model_reasoning_effort": "high"},
        )
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.holder.holder_id, "web:writer-tab")
        self.assertEqual(lease and lease.holder.owner_pid, os.getpid())
        self.assertEqual(lease and lease.turn_id, "")

    def test_active_goal_open_uses_the_loop_external_settings_snapshot(self):
        self._establish_active_goal()
        self.controller._next_turn_settings._runtime_context_guard = Mock(
            side_effect=AssertionError("guarded settings snapshot ran off-loop")
        )

        snapshot = self.controller.read_thread("writer-tab", "thread-1")

        self.assertEqual(snapshot["thread"]["id"], "thread-1")
        self.assertEqual(self.fake.resumed, ["thread-1"])

    def test_replaced_autonomous_blank_rejects_resume_before_transport(self):
        self._establish_active_goal()
        original_acquire = self.operations.acquire_autonomous_turn_external
        feishu_holder = make_feishu_interaction_holder(
            "sender",
            "chat",
            owner_pid=0,
        )

        def acquire_then_replace(client_id: str, thread_id: str):
            receipt = original_acquire(client_id, thread_id)
            self.assertTrue(self.store.release_if_matches(receipt.lease))
            self.assertTrue(self.store.acquire(thread_id, feishu_holder).granted)
            return receipt

        with patch.object(
            self.operations,
            "acquire_autonomous_turn_external",
            side_effect=acquire_then_replace,
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.controller.read_thread("writer-tab", "thread-1")

        self.assertEqual(caught.exception.code, "interaction_state_unavailable")
        self.assertEqual(self.fake.resumed, [])
        current = self.store.load("thread-1")
        self.assertIsNotNone(current)
        self.assertTrue(current and current.holder.same_holder(feishu_holder))
        claim = self.resume_authority.claim_resume_thread_page("thread-1")
        self.resume_authority.abandon_resume_thread_page_claim(claim)

    def test_fatal_resume_retires_claim_as_unknown_before_reraising(self):
        self._establish_active_goal()
        self.fake.resume_error = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.controller.read_thread("writer-tab", "thread-1")

        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertIsNone(self.store.load("thread-1"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.outcome, "unknown")
        try:
            claim = self.resume_authority.claim_resume_thread_page("thread-1")
        except ThreadResumeInProgress as exc:  # pragma: no cover - assertion aid
            self.fail(f"fatal resume leaked its exact claim: {exc}")
        self.resume_authority.abandon_resume_thread_page_claim(claim)

    def test_stale_browser_intent_is_rejected_before_state_write(self):
        self.controller.read_thread("tab-1", "thread-1", intent_generation=4)
        self.controller.client_disconnected("tab-1")
        self.controller.client_connected("tab-1")

        with self.assertRaises(WebRuntimeError) as profile_error:
            self.controller.update_profile(
                "tab-1",
                {"working_dir": "/work/old"},
                intent_generation=3,
            )
        with self.assertRaises(WebRuntimeError) as goal_error:
            self.controller.set_goal(
                "tab-1",
                "thread-1",
                objective="stale goal",
                intent_generation=2,
            )

        self.assertEqual(profile_error.exception.code, "stale_intent")
        self.assertEqual(goal_error.exception.code, "stale_intent")
        self.assertNotEqual(
            self.profile_store.load("tab-1").working_dir,
            "/work/old",
        )
        self.assertEqual(self.fake.goal_sets, [])

    def test_failed_observer_resume_preserves_previous_selection(self):
        self.controller.read_thread("tab-1", "thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "missing-thread")

        self.assertEqual(caught.exception.code, "thread_not_found")
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )
        self.controller.client_disconnected("tab-1")
        self.assertEqual(self.fake.unsubscribed, ["thread-1"])
        self.assertEqual(self.fake.released, ["thread-1"])

    def test_missing_persisted_selection_is_cleared_during_open(self):
        self.profile_store.update(
            "tab-1",
            selected_thread_id="missing-thread",
            working_dir="/work/project",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "missing-thread")

        self.assertEqual(caught.exception.code, "thread_not_found")
        self.assertEqual(self.profile_store.load("tab-1").selected_thread_id, "")

    def test_web_interest_is_dropped_without_unsubscribe_when_feishu_still_subscribes(
        self,
    ):
        self.fake.subscribers = (("sender", "chat"),)
        self.controller.read_thread("tab-1", "thread-1")

        self.controller.client_disconnected("tab-1")

        self.assertEqual(self.fake.resumed, ["thread-1"])
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        self.assertFalse(self.controller.retains_runtime("thread-1"))

    def test_shutdown_does_not_admit_new_external_cleanup(self):
        self.controller.read_thread("tab-1", "thread-1")

        self.controller.shutdown()

        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        self.assertFalse(self.controller.retains_runtime("thread-1"))

    def test_observer_resume_unknown_reconciles_on_authoritative_idle(self):
        self.fake.resume_error = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "connection lost"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertTrue(self.controller.retains_runtime("thread-1"))
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.outcome, "unknown")
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

        self.fake.resume_error = None
        self.controller.client_disconnected("tab-1")

        self.assertFalse(self.controller.retains_runtime("thread-1"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNone(interest)
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, ["thread-1"])

    def test_prompt_does_not_own_observer_resume_unknown_or_retry(
        self,
    ):
        self.fake.resume_error = CodexRpcProtocolError(
            "thread/resume",
            "malformed response",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.assertEqual(self.fake.started, [])
        self.assertIsNone(self.store.load("thread-1"))
        self.assertIsNone(self.operations.unknown_mutation_projection("thread-1"))
        self.assertTrue(self.controller.retains_runtime("thread-1"))
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.outcome, "unknown")
        self.assertEqual(interest and interest.desired_client_ids, ("tab-1",))
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])

        profile = self.controller.meta("tab-1")["writer_profile"]
        generation = int(profile["scope_generation"])
        prompt = self.controller.prepare_prompt(
            "tab-1",
            "thread-1",
            mutation_id=str(uuid.uuid4()),
            text="hello",
            attachment_ids=[],
            source_scope_generation=generation,
            source_attachment_scope="thread:thread-1",
            source_composer_scope_id=(
                f"tab-1:generation:{generation}:thread:thread-1"
            ),
        )
        self.assertEqual(prompt.mode, "start")
        self.assertTrue(self.controller.abandon_prompt(prompt))
        self.assertEqual(self.fake.resumed, ["thread-1"])

        self.fake.resume_error = None
        self.controller.read_thread("tab-1", "thread-1")
        result = self.submit_web_prompt("tab-1", "thread-1", text="retry")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])
        self.assertEqual(len(self.fake.started), 1)
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.outcome, "confirmed")
        # Ordinary Web prompt dispatch deliberately does not recreate a
        # cross-surface writer after the explicit retry is accepted.
        self.assertIsNone(self.store.load("thread-1"))

    def test_prompt_has_no_post_commit_history_cache_lifecycle(
        self,
    ):
        self.controller.read_thread("tab-1", "thread-1")
        fail_history_cache = Mock(side_effect=RuntimeError("history cache failed"))

        self.controller._thread_read_model.replace_turns = fail_history_cache

        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.fake.started), 1)
        fail_history_cache.assert_not_called()
        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.outcome, "confirmed")
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertIsNone(self.store.load("thread-1"))
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
