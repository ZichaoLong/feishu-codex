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
from bot.stores.interaction_lease_store import InteractionLeaseStore, make_feishu_interaction_holder
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_lease_registry import ThreadLeaseRegistry


class ThreadAccessPolicyTests(unittest.TestCase):
    def _make_policy(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        registry = ThreadLeaseRegistry()
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
            thread_write_owner_locked=registry.lease_owner,
        )
        return lock, registry, interaction_store, group_modes, policy

    def test_thread_sharing_policy_violation_rejects_all_mode_when_thread_is_shared(self) -> None:
        lock, registry, _interaction_store, group_modes, policy = self._make_policy()
        group_modes["chat-group"] = "all"
        with lock:
            registry.subscribe((GROUP_SHARED_BINDING_OWNER_ID, "chat-group"), "thread-1")
            registry.subscribe(("ou_user2", "chat-other"), "thread-1")

        violation = policy.thread_sharing_policy_violation("chat-group", "thread-1", message_id="msg-1")
        check = policy.thread_sharing_policy_violation_check("chat-group", "thread-1", message_id="msg-1")

        self.assertIn("`all` 模式", violation)
        self.assertIn("不能与其他飞书会话共享", violation)
        self.assertEqual(check.reason_code, PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING)

    def test_thread_sharing_policy_violation_rejects_when_other_all_group_is_attached(self) -> None:
        lock, registry, _interaction_store, group_modes, policy = self._make_policy()
        group_modes["chat-other"] = "all"
        with lock:
            registry.subscribe((GROUP_SHARED_BINDING_OWNER_ID, "chat-group"), "thread-1")
            registry.subscribe((GROUP_SHARED_BINDING_OWNER_ID, "chat-other"), "thread-1")

        violation = policy.thread_sharing_policy_violation("chat-group", "thread-1", message_id="msg-1")

        self.assertIn("已被处于 `all` 模式的其他群聊独占", violation)

    def test_prompt_write_denial_text_rejects_other_feishu_interaction_owner(self) -> None:
        lock, registry, interaction_store, _group_modes, policy = self._make_policy()
        binding = ("ou_user", "chat-p2p")
        other_binding = ("ou_other", "chat-other")
        with lock:
            registry.subscribe(binding, "thread-1")
        interaction_store.force_acquire(
            "thread-1",
            make_feishu_interaction_holder(other_binding[0], other_binding[1], owner_pid=os.getpid()),
        )

        denial = policy.prompt_write_denial_text(binding, "chat-p2p", "thread-1")
        check = policy.prompt_write_denial_check(binding, "chat-p2p", "thread-1")

        self.assertIn("另一飞书会话", denial)
        self.assertIn("暂时不能写入", denial)
        self.assertEqual(check.reason_code, PROMPT_DENIED_BY_INTERACTION_OWNER)


if __name__ == "__main__":
    unittest.main()
