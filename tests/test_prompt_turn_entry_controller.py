import pathlib
import tempfile
import threading
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager, ResolvedRuntimeBinding
from bot.prompt_turn_entry_controller import PromptTurnEntryController, PromptTurnEntryPorts
from bot.runtime_state import ThreadStateChanged
from bot.runtime_view import build_runtime_view
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class _FakeCardPublisher:
    def __init__(self) -> None:
        self.patched: list[tuple[str, object]] = []

    def patch_execution_card(self, message_id: str, model: object) -> bool:
        self.patched.append((message_id, model))
        return True


class PromptTurnEntryControllerTests(unittest.TestCase):
    def _make_controller(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        chat_binding_store = ChatBindingStore(data_dir)
        binding_runtime = BindingRuntimeManager(
            lock=lock,
            default_working_dir="/tmp/project",
            default_approval_policy="on-request",
            default_sandbox="workspace-write",
            default_collaboration_mode="default",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=chat_binding_store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        turn_execution = TurnExecutionCoordinator()
        binding = ("ou_user", "c1")
        with lock:
            state = binding_runtime.get_or_create_runtime_state_locked(binding)

        replies: list[tuple[str, str, str, bool]] = []
        create_thread_calls: list[dict] = []
        resume_calls: list[dict] = []
        start_turn_calls: list[dict] = []
        seed_calls: list[tuple[str, str]] = []
        interrupt_calls: list[dict] = []
        sent_execution_cards: list[tuple[str, str, bool]] = []
        flushed: list[tuple[str, str, bool]] = []
        retired: list[tuple[str, str]] = []
        reconciled: list[tuple[str, str, str, str]] = []
        refreshed: list[tuple[str, str]] = []
        finalized: list[tuple[str, str]] = []
        degraded: list[tuple[str, str, str]] = []
        scheduled_watchdogs: list[tuple[str, str]] = []
        reserved_cards: dict[str, str] = {}
        fake_card_publisher = _FakeCardPublisher()

        create_thread_result = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="created",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            )
        )
        resume_summaries: dict[str, ThreadSummary] = {
            "thread-1": ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            )
        }
        start_turn_behavior = {"value": {"turnId": "turn-1"}}
        interrupt_behavior = {"exc": None}
        thread_profiles: dict[str, str] = {}
        access_policy = ThreadAccessPolicy(
            lock=lock,
            is_group_chat=lambda chat_id, message_id: False,
            group_mode_for_chat=lambda chat_id: "assistant",
            thread_subscribers_locked=binding_runtime.thread_subscribers,
            current_interaction_lease_locked=binding_runtime.current_interaction_lease_locked,
            feishu_interaction_holder=binding_runtime.feishu_interaction_holder,
        )

        def _resolve_runtime_binding(sender_id: str, chat_id: str, message_id: str = "") -> ResolvedRuntimeBinding:
            return ResolvedRuntimeBinding(binding=binding, state=state)

        def _get_runtime_state(sender_id: str, chat_id: str, message_id: str = ""):
            return state

        def _get_runtime_view(sender_id: str, chat_id: str, message_id: str = ""):
            with lock:
                return build_runtime_view(state)

        def _bind_thread(sender_id: str, chat_id: str, thread: ThreadSummary, *, message_id: str = "") -> None:
            del sender_id, chat_id, message_id
            with lock:
                binding_runtime.bind_thread_locked(
                    binding,
                    state,
                    thread_id=thread.thread_id,
                    thread_title=thread.title,
                    working_dir=thread.cwd or state["working_dir"],
                    on_after_bind=turn_execution.clear_plan_state_locked,
                )

        def _clear_thread_binding(sender_id: str, chat_id: str, *, message_id: str = "") -> None:
            del sender_id, chat_id, message_id
            with lock:
                binding_runtime.clear_thread_binding_locked(
                    binding,
                    state,
                    on_clear_state=lambda current_state: (
                        turn_execution.reset_execution_context_locked(current_state, clear_card_message=True),
                        turn_execution.clear_plan_state_locked(current_state),
                    ),
                )

        def _resume_snapshot_by_id(
            thread_id: str,
            *,
            original_arg: str,
            summary: ThreadSummary | None = None,
        ) -> ThreadSnapshot:
            resume_calls.append(
                {
                    "thread_id": thread_id,
                    "original_arg": original_arg,
                    "summary": summary.thread_id if summary is not None else "",
                }
            )
            return ThreadSnapshot(summary=resume_summaries[thread_id])

        def _create_thread(**kwargs):
            create_thread_calls.append(dict(kwargs))
            return create_thread_result

        def _persist_new_thread_profile_seed(thread_id: str, profile: str) -> str:
            seed_calls.append((thread_id, profile))
            if profile:
                thread_profiles[thread_id] = profile
            return ""

        def _start_turn(**kwargs):
            snapshot = dict(kwargs)
            input_items = [dict(item) for item in snapshot.get("input_items", [])]
            snapshot["input_items"] = input_items
            snapshot["text"] = "\n".join(
                item.get("text", "")
                for item in input_items
                if isinstance(item, dict) and item.get("type") == "text"
            )
            start_turn_calls.append(snapshot)
            value = start_turn_behavior["value"]
            if isinstance(value, Exception):
                if len(start_turn_calls) == 1 and isinstance(value, Exception):
                    start_turn_behavior["value"] = {"turnId": "turn-1"}
                    raise value
            return value

        def _interrupt_running_turn(*, thread_id: str, turn_id: str) -> None:
            interrupt_calls.append({"thread_id": thread_id, "turn_id": turn_id})
            if interrupt_behavior["exc"] is not None:
                raise interrupt_behavior["exc"]

        controller = PromptTurnEntryController(
            lock=lock,
            turn_execution=turn_execution,
            ports=PromptTurnEntryPorts(
                resolve_runtime_binding=_resolve_runtime_binding,
                get_runtime_state=_get_runtime_state,
                get_runtime_view=_get_runtime_view,
                bind_thread=_bind_thread,
                clear_thread_binding=_clear_thread_binding,
                resume_snapshot_by_id=_resume_snapshot_by_id,
                create_thread=_create_thread,
                effective_default_profile=lambda: "",
                persist_new_thread_profile_seed=_persist_new_thread_profile_seed,
                thread_profile_for_thread=lambda thread_id: thread_profiles.get(thread_id, ""),
                message_reply_in_thread=lambda message_id: message_id.startswith("thread-"),
                group_actor_open_id=lambda message_id: "ou_actor" if message_id else "",
                access_policy=access_policy,
                acquire_interaction_lease_for_binding=binding_runtime.acquire_interaction_lease_for_binding,
                release_interaction_lease_for_binding=binding_runtime.release_interaction_lease_for_binding,
                sync_stored_binding_locked=binding_runtime.sync_stored_binding_locked,
                clear_plan_state=turn_execution.clear_plan_state_locked,
                apply_runtime_state_message_locked=binding_runtime.apply_runtime_state_message_locked,
                claim_reserved_execution_card=lambda message_id: reserved_cards.pop(message_id, ""),
                patch_message=lambda message_id, content: True,
                card_publisher_factory=lambda: fake_card_publisher,
                send_execution_card=lambda chat_id, parent_message_id, *, reply_in_thread=False: (
                    sent_execution_cards.append((chat_id, parent_message_id, reply_in_thread)),
                    "card-1",
                )[1],
                flush_execution_card=lambda sender_id, chat_id, immediate=False: flushed.append(
                    (sender_id, chat_id, bool(immediate))
                ),
                retire_execution_anchor=lambda sender_id, chat_id: (
                    turn_execution.retire_execution_locked(state),
                    retired.append((sender_id, chat_id)),
                ),
                schedule_mirror_watchdog=lambda sender_id, chat_id: scheduled_watchdogs.append((sender_id, chat_id)),
                reconcile_execution_snapshot=lambda sender_id, chat_id, *, thread_id, turn_id="": (
                    reconciled.append((sender_id, chat_id, thread_id, turn_id)),
                    False,
                )[1],
                refresh_terminal_execution_card_from_state=lambda sender_id, chat_id: (
                    refreshed.append((sender_id, chat_id)),
                    True,
                )[1],
                finalize_execution_card_from_state=lambda sender_id, chat_id: (
                    finalized.append((sender_id, chat_id)),
                    True,
                )[1],
                mark_runtime_degraded=lambda sender_id, chat_id, *, reason: degraded.append((sender_id, chat_id, reason)),
                runtime_recovery_reason=lambda exc: str(exc),
                is_turn_thread_not_found_error=lambda exc: str(exc) == "thread not found",
                is_thread_not_found_error=lambda exc: str(exc) == "thread missing",
                is_transport_disconnect=lambda exc: str(exc) == "disconnect",
                is_request_timeout_error=lambda exc: str(exc) == "timeout",
                start_turn=_start_turn,
                interrupt_running_turn=_interrupt_running_turn,
                reply_text=lambda chat_id, text, **kwargs: replies.append(
                    (
                        chat_id,
                        text,
                        str(kwargs.get("message_id", "") or ""),
                        bool(kwargs.get("reply_in_thread", False)),
                    )
                ),
                mirror_watchdog_seconds=lambda: 8.0,
                card_reply_limit=lambda: 12000,
                card_log_limit=lambda: 8000,
            ),
        )

        return {
            "lock": lock,
            "binding_runtime": binding_runtime,
            "turn_execution": turn_execution,
            "binding": binding,
            "state": state,
            "controller": controller,
            "bind_thread_fn": _bind_thread,
            "replies": replies,
            "create_thread_calls": create_thread_calls,
            "resume_calls": resume_calls,
            "start_turn_calls": start_turn_calls,
            "seed_calls": seed_calls,
            "thread_profiles": thread_profiles,
            "interrupt_calls": interrupt_calls,
            "sent_execution_cards": sent_execution_cards,
            "flushed": flushed,
            "retired": retired,
            "reconciled": reconciled,
            "refreshed": refreshed,
            "finalized": finalized,
            "degraded": degraded,
            "scheduled_watchdogs": scheduled_watchdogs,
            "reserved_cards": reserved_cards,
            "resume_summaries": resume_summaries,
            "start_turn_behavior": start_turn_behavior,
            "interrupt_behavior": interrupt_behavior,
        }

    def _bind_thread(self, env, *, thread_id: str, runtime_state: str = "attached") -> None:
        thread = ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        env["resume_summaries"][thread_id] = thread
        env["bind_thread_fn"]("ou_user", "c1", thread)
        if runtime_state == "released":
            with env["lock"]:
                env["binding_runtime"].unsubscribe_thread_locked(env["binding"], thread_id)
                env["binding_runtime"].apply_persisted_runtime_state_message_locked(
                    env["binding"],
                    env["state"],
                    ThreadStateChanged(feishu_runtime_state="released"),
                )

    def test_handle_prompt_replies_when_turn_is_already_running(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        env["state"]["running"] = True
        env["state"]["current_thread_id"] = "thread-1"
        env["state"]["current_turn_id"] = "turn-1"
        env["state"]["current_message_id"] = "card-1"
        env["state"]["last_runtime_event_at"] = 0.0

        controller.handle_prompt("ou_user", "c1", "follow up", message_id="msg-1")

        self.assertEqual(env["start_turn_calls"], [])
        self.assertEqual(
            env["replies"],
            [("c1", "当前线程仍在执行，请等待结束或先执行 `/cancel`。", "msg-1", False)],
        )

    def test_start_prompt_turn_rejects_when_interaction_lease_is_held_by_another_binding(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        with env["lock"]:
            env["binding_runtime"].acquire_interaction_lease_for_binding(("ou_other", "c2"), "thread-1")

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(env["start_turn_calls"], [])
        self.assertIn("当前线程正由另一飞书会话执行", env["replies"][-1][1])

    def test_start_prompt_turn_rebinds_released_thread_before_starting(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="released")

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual([call["thread_id"] for call in env["resume_calls"]], ["thread-1"])
        self.assertEqual(env["start_turn_calls"][-1]["thread_id"], "thread-1")
        self.assertEqual(env["state"]["feishu_runtime_state"], "attached")

    def test_start_prompt_turn_retries_after_thread_not_found(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1")
        env["start_turn_behavior"]["value"] = RuntimeError("thread not found")

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual([call["thread_id"] for call in env["resume_calls"]], ["thread-1"])
        self.assertEqual([call["thread_id"] for call in env["start_turn_calls"]], ["thread-1", "thread-1"])
        self.assertEqual(env["state"]["current_turn_id"], "turn-1")
        self.assertEqual(env["scheduled_watchdogs"], [("ou_user", "c1")])

    def test_start_prompt_turn_releases_preattached_lease_by_released_thread_id_on_sharing_violation(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        self._bind_thread(env, thread_id="thread-1", runtime_state="released")

        controller.ensure_binding_runtime_attached = lambda *args, **kwargs: "thread-2"
        controller._access_policy.thread_sharing_policy_violation = lambda *args, **kwargs: "sharing denied"

        controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertEqual(env["replies"][-1][1], "sharing denied")
        with env["lock"]:
            owner = env["binding_runtime"].interaction_owner_snapshot_locked("thread-1")
        self.assertEqual(owner["kind"], "none")

    def test_start_prompt_turn_seeds_new_thread_profile_and_uses_it_for_turn(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        controller._effective_default_profile = lambda: "work"

        started = controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertTrue(started)
        self.assertEqual(env["seed_calls"], [("thread-created", "work")])
        self.assertEqual(env["thread_profiles"], {"thread-created": "work"})
        self.assertEqual(env["start_turn_calls"][-1]["profile"], "work")

    def test_start_prompt_turn_fails_closed_when_execution_card_cannot_be_sent(self) -> None:
        env = self._make_controller()
        controller = env["controller"]
        controller._send_execution_card = lambda chat_id, parent_message_id, *, reply_in_thread=False: None

        started = controller.start_prompt_turn("ou_user", "c1", "hello", message_id="msg-1")

        self.assertFalse(started)
        self.assertEqual(env["start_turn_calls"], [])
        self.assertEqual(env["scheduled_watchdogs"], [])
        self.assertEqual(env["retired"], [("ou_user", "c1")])
        self.assertEqual(
            env["replies"],
            [("c1", "执行卡片发送失败，未启动 Codex；请稍后重试。", "msg-1", False)],
        )
        self.assertFalse(env["state"]["running"])
        self.assertEqual(env["state"]["current_message_id"], "")


if __name__ == "__main__":
    unittest.main()
