import os
import pathlib
import tempfile
import threading
import unittest

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.reason_codes import (
    PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
)
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseHolder,
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


class ThreadAccessPolicyTests(unittest.TestCase):
    def _make_policy(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        registry = ThreadSubscriptionRegistry()
        interaction_store = InteractionLeaseStore(data_dir)
        group_modes: dict[str, str] = {}
        policy = ThreadAccessPolicy(
            lock=lock,
            is_group_chat=lambda chat_id, message_id: chat_id.startswith("chat-group"),
            group_mode_for_chat=lambda chat_id: group_modes.get(chat_id, "assistant"),
            thread_subscribers_locked=registry.subscribers,
            current_interaction_lease_locked=interaction_store.load,
            feishu_interaction_holder=lambda binding: make_feishu_interaction_holder(
                binding[0],
                binding[1],
                owner_pid=os.getpid(),
            ),
        )
        return lock, registry, interaction_store, group_modes, policy

    def test_all_mode_thread_exclusivity_violation_rejects_all_mode_when_thread_is_shared(
        self,
    ) -> None:
        lock, registry, _interaction_store, group_modes, policy = self._make_policy()
        group_modes["chat-group"] = "all"
        with lock:
            registry.subscribe(
                (GROUP_SHARED_BINDING_OWNER_ID, "chat-group"), "thread-1"
            )
            registry.subscribe(("ou_user2", "chat-other"), "thread-1")

        violation = policy.all_mode_thread_exclusivity_violation(
            "chat-group", "thread-1", message_id="msg-1"
        )
        check = policy.all_mode_thread_exclusivity_violation_check(
            "chat-group", "thread-1", message_id="msg-1"
        )
        queue_check = policy.prompt_queue_admission_check(
            (GROUP_SHARED_BINDING_OWNER_ID, "chat-group"),
            "chat-group",
            "thread-1",
            "turn-1",
            message_id="msg-1",
        )

        self.assertIn("`all` 模式", violation)
        self.assertIn("不能与其他飞书会话共享", violation)
        self.assertEqual(check.reason_code, PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING)
        self.assertEqual(
            queue_check.reason_code,
            PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING,
        )

    def test_all_mode_thread_exclusivity_violation_rejects_when_other_all_group_is_attached(
        self,
    ) -> None:
        lock, registry, _interaction_store, group_modes, policy = self._make_policy()
        group_modes["chat-other"] = "all"
        with lock:
            registry.subscribe(
                (GROUP_SHARED_BINDING_OWNER_ID, "chat-group"), "thread-1"
            )
            registry.subscribe(
                (GROUP_SHARED_BINDING_OWNER_ID, "chat-other"), "thread-1"
            )

        violation = policy.all_mode_thread_exclusivity_violation(
            "chat-group", "thread-1", message_id="msg-1"
        )

        self.assertIn("已被处于 `all` 模式的其他群聊独占", violation)

    def test_prompt_write_denial_text_rejects_other_feishu_interaction_owner(
        self,
    ) -> None:
        lock, registry, interaction_store, _group_modes, policy = self._make_policy()
        binding = ("ou_user", "chat-p2p")
        other_binding = ("ou_other", "chat-other")
        with lock:
            registry.subscribe(binding, "thread-1")
        interaction_store.force_acquire(
            "thread-1",
            make_feishu_interaction_holder(
                other_binding[0], other_binding[1], owner_pid=os.getpid()
            ),
        )

        denial = policy.prompt_write_denial_text(binding, "chat-p2p", "thread-1")
        check = policy.prompt_write_denial_check(binding, "chat-p2p", "thread-1")
        queue_check = policy.prompt_queue_admission_check(
            binding,
            "chat-p2p",
            "thread-1",
            "turn-1",
        )

        self.assertIn("另一飞书会话", denial)
        self.assertIn("暂时不能写入", denial)
        self.assertEqual(check.reason_code, PROMPT_DENIED_BY_INTERACTION_OWNER)
        self.assertEqual(
            queue_check.reason_code,
            PROMPT_DENIED_BY_INTERACTION_OWNER,
        )

    def test_prompt_queue_allows_exact_current_process_web_and_fcodex_turns(
        self,
    ) -> None:
        for kind in ("web", "fcodex"):
            with self.subTest(kind=kind):
                _lock, _registry, interaction_store, _group_modes, policy = (
                    self._make_policy()
                )
                holder = (
                    make_web_interaction_holder("document-1", owner_pid=os.getpid())
                    if kind == "web"
                    else make_fcodex_interaction_holder(
                        "fcodex:participant-1",
                        owner_pid=os.getpid(),
                    )
                )
                acquired = interaction_store.acquire("thread-1", holder)
                assert acquired.lease is not None
                interaction_store.activate_turn(acquired.lease, "turn-1")

                check = policy.prompt_queue_admission_check(
                    ("ou_user", "chat-p2p"),
                    "chat-p2p",
                    "thread-1",
                    "turn-1",
                )

                self.assertTrue(check.allowed)
                self.assertEqual(
                    policy.current_process_local_turn_id("thread-1"),
                    "turn-1",
                )

    def test_prompt_queue_keeps_same_binding_admission_before_turn_id_arrives(
        self,
    ) -> None:
        _lock, _registry, interaction_store, _group_modes, policy = self._make_policy()
        binding = ("ou_user", "chat-p2p")
        acquired = interaction_store.acquire(
            "thread-1",
            make_feishu_interaction_holder(
                binding[0],
                binding[1],
                owner_pid=os.getpid(),
            ),
        )
        self.assertIsNotNone(acquired.lease)

        check = policy.prompt_queue_admission_check(
            binding,
            "chat-p2p",
            "thread-1",
            "",
        )

        self.assertTrue(check.allowed)
        self.assertEqual(policy.current_process_local_turn_id("thread-1"), "")

    def test_prompt_queue_requires_exact_turn_for_no_lease_autonomous_mirror(
        self,
    ) -> None:
        _lock, _registry, _interaction_store, _group_modes, policy = self._make_policy()
        binding = ("ou_user", "chat-p2p")

        exact = policy.prompt_queue_admission_check(
            binding,
            "chat-p2p",
            "thread-1",
            "turn-autonomous",
        )
        blank = policy.prompt_queue_admission_check(
            binding,
            "chat-p2p",
            "thread-1",
            "",
        )

        self.assertTrue(exact.allowed)
        self.assertFalse(blank.allowed)
        self.assertEqual(blank.reason_code, "prompt_denied_by_running_turn")

    def test_prompt_queue_rejects_blank_mismatched_and_nonlocal_frontend_leases(
        self,
    ) -> None:
        cases = ("blank", "mismatched", "foreign_process")
        for case in cases:
            with self.subTest(case=case):
                _lock, _registry, interaction_store, _group_modes, policy = (
                    self._make_policy()
                )
                if case == "foreign_process":
                    holder = InteractionLeaseHolder(
                        kind="web",
                        holder_id="web:stale",
                        owner_pid=0,
                    )
                else:
                    holder = make_web_interaction_holder(
                        "document-1",
                        owner_pid=os.getpid(),
                    )
                acquired = interaction_store.acquire("thread-1", holder)
                assert acquired.lease is not None
                if case != "blank":
                    interaction_store.activate_turn(acquired.lease, "turn-1")

                check = policy.prompt_queue_admission_check(
                    ("ou_user", "chat-p2p"),
                    "chat-p2p",
                    "thread-1",
                    "turn-2" if case == "mismatched" else "turn-1",
                )

                self.assertFalse(check.allowed)
                self.assertEqual(
                    check.reason_code,
                    PROMPT_DENIED_BY_INTERACTION_OWNER,
                )
                self.assertEqual(
                    policy.current_process_local_turn_id("thread-1"),
                    "turn-1" if case == "mismatched" else "",
                )

    def test_prompt_queue_rejects_stale_current_pid_process_identity(self) -> None:
        _lock, _registry, _interaction_store, _group_modes, policy = self._make_policy()
        stale_lease = InteractionLease(
            thread_id="thread-1",
            holder=InteractionLeaseHolder(
                kind="web",
                holder_id="web:stale-incarnation",
                owner_pid=os.getpid(),
                owner_process_identity="stale-process-incarnation",
            ),
            lease_id="stale-lease",
            updated_at=0.0,
            turn_id="turn-1",
        )
        policy._current_interaction_lease_locked = lambda _thread_id: stale_lease

        check = policy.prompt_queue_admission_check(
            ("ou_user", "chat-p2p"),
            "chat-p2p",
            "thread-1",
            "turn-1",
        )

        self.assertFalse(check.allowed)
        self.assertEqual(check.reason_code, PROMPT_DENIED_BY_INTERACTION_OWNER)
        self.assertEqual(policy.current_process_local_turn_id("thread-1"), "")


if __name__ == "__main__":
    unittest.main()
