from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, MutableMapping, TypeAlias

from bot.binding_identity import binding_kind, format_binding_id
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    RuntimeStateMessage,
    StoredBindingHydrated,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.runtime_view import RuntimeView, build_runtime_view
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseStore,
    feishu_binding_from_holder,
    make_feishu_interaction_holder,
)
from bot.thread_lease_registry import ThreadLeaseRegistry

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]


@dataclass(frozen=True)
class ResolvedRuntimeBinding:
    binding: ChatBindingKey
    state: MutableMapping[str, Any]


@dataclass(frozen=True)
class ReleaseFeishuRuntimeResult:
    thread_id: str
    thread_title: str
    working_dir: str
    bound_binding_ids: list[str]
    released_binding_ids: list[str]
    changed: bool
    already_released: bool
    unsubscribe_thread_id: str = ""


@dataclass(frozen=True)
class BindingRuntimeSnapshot:
    binding: ChatBindingKey
    active: bool
    thread_id: str
    thread_title: str
    working_dir: str
    feishu_runtime_state: str
    has_inflight_turn: bool


class BindingRuntimeManager:
    def __init__(
        self,
        *,
        lock,
        default_working_dir: str,
        default_approval_policy: str,
        default_sandbox: str,
        default_collaboration_mode: str,
        default_model: str,
        default_reasoning_effort: str,
        chat_binding_store: ChatBindingStore,
        thread_lease_registry: ThreadLeaseRegistry,
        interaction_lease_store: InteractionLeaseStore,
        is_group_chat: Callable[[str, str], bool],
    ) -> None:
        self._lock = lock
        self._default_working_dir = str(default_working_dir or "").strip()
        self._default_approval_policy = str(default_approval_policy or "").strip()
        self._default_sandbox = str(default_sandbox or "").strip()
        self._default_collaboration_mode = str(default_collaboration_mode or "").strip()
        self._default_model = str(default_model or "").strip()
        self._default_reasoning_effort = str(default_reasoning_effort or "").strip()
        self._chat_binding_store = chat_binding_store
        self._thread_lease_registry = thread_lease_registry
        self._interaction_lease_store = interaction_lease_store
        self._is_group_chat = is_group_chat
        self._runtime_state_by_binding: dict[ChatBindingKey, MutableMapping[str, Any]] = {}

    @staticmethod
    def apply_runtime_state_message_locked(
        state: MutableMapping[str, Any],
        message: RuntimeStateMessage,
    ) -> None:
        apply_runtime_state_message(state, message)

    def apply_persisted_runtime_state_message_locked(
        self,
        binding: ChatBindingKey,
        state: MutableMapping[str, Any],
        message: RuntimeStateMessage,
    ) -> None:
        self.apply_runtime_state_message_locked(state, message)
        self.sync_stored_binding_locked(binding, state)

    def build_default_stored_binding(self) -> dict[str, str]:
        return {
            "working_dir": self._default_working_dir,
            "current_thread_id": "",
            "current_thread_title": "",
            "current_thread_runtime_state": "",
            "current_thread_write_owner_thread_id": "",
            "approval_policy": self._default_approval_policy,
            "sandbox": self._default_sandbox,
            "collaboration_mode": self._default_collaboration_mode,
        }

    def build_default_runtime_state(self) -> MutableMapping[str, Any]:
        stored_binding = self.build_default_stored_binding()
        return {
            "active": False,
            "working_dir": stored_binding["working_dir"],
            "current_thread_id": stored_binding["current_thread_id"],
            "current_thread_title": stored_binding["current_thread_title"],
            "current_thread_runtime_state": stored_binding["current_thread_runtime_state"],
            "current_turn_id": "",
            "running": False,
            "cancelled": False,
            "pending_cancel": False,
            "current_message_id": "",
            "last_execution_message_id": "",
            "current_prompt_message_id": "",
            "current_prompt_reply_in_thread": False,
            "current_actor_open_id": "",
            "execution_transcript": ExecutionTranscript(),
            "runtime_channel_state": "live",
            "started_at": 0.0,
            "last_runtime_event_at": 0.0,
            "last_patch_at": 0.0,
            "patch_timer": None,
            "mirror_watchdog_timer": None,
            "mirror_watchdog_generation": 0,
            "followup_sent": False,
            "followup_text": "",
            "terminal_result_text": "",
            "awaiting_local_turn_started": False,
            "approval_policy": stored_binding["approval_policy"],
            "sandbox": stored_binding["sandbox"],
            "collaboration_mode": stored_binding["collaboration_mode"],
            "model": self._default_model,
            "reasoning_effort": self._default_reasoning_effort,
            "plan_message_id": "",
            "plan_turn_id": "",
            "plan_explanation": "",
            "plan_steps": [],
            "plan_text": "",
        }

    @staticmethod
    def apply_stored_binding(state: MutableMapping[str, Any], stored_binding: dict[str, str]) -> None:
        apply_runtime_state_message(
            state,
            StoredBindingHydrated(
                working_dir=stored_binding["working_dir"],
                current_thread_id=stored_binding["current_thread_id"],
                current_thread_title=stored_binding["current_thread_title"],
                current_thread_runtime_state=stored_binding["current_thread_runtime_state"],
                approval_policy=stored_binding["approval_policy"],
                sandbox=stored_binding["sandbox"],
                collaboration_mode=stored_binding["collaboration_mode"],
            ),
        )

    def subscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._thread_lease_registry.subscribe(binding, thread_id)

    def unsubscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._thread_lease_registry.unsubscribe(binding, thread_id).thread_orphaned

    def acquire_thread_write_lease_locked(self, binding: ChatBindingKey, thread_id: str):
        return self._thread_lease_registry.acquire_write_lease(binding, thread_id)

    def release_thread_write_lease_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._thread_lease_registry.release_write_lease(binding, thread_id)

    def thread_subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        return self._thread_lease_registry.subscribers(thread_id)

    def thread_write_owner(self, thread_id: str) -> ChatBindingKey | None:
        return self._thread_lease_registry.lease_owner(thread_id)

    @staticmethod
    def _feishu_interaction_holder(binding: ChatBindingKey):
        return make_feishu_interaction_holder(
            binding[0],
            binding[1],
            owner_pid=os.getpid(),
        )

    def feishu_interaction_holder(self, binding: ChatBindingKey):
        return self._feishu_interaction_holder(binding)

    def current_interaction_lease_locked(self, thread_id: str) -> InteractionLease | None:
        return self._interaction_lease_store.load(thread_id)

    def acquire_interaction_lease_for_binding(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> InteractionLeaseAcquireResult:
        return self._interaction_lease_store.acquire(
            thread_id,
            self._feishu_interaction_holder(binding),
        )

    def release_interaction_lease_for_binding(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> bool:
        return self._interaction_lease_store.release(
            thread_id,
            self._feishu_interaction_holder(binding),
        )

    def interactive_binding_for_thread_locked(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> tuple[ChatBindingKey | None, bool]:
        lease = self.current_interaction_lease_locked(thread_id)
        if lease is not None:
            binding = feishu_binding_from_holder(lease.holder)
            if binding is None:
                return None, True
            return binding, False
        subscribers = self.thread_subscribers(thread_id)
        if len(subscribers) != 1:
            return None, False
        binding = subscribers[0]
        if adopt_sole_subscriber:
            self.acquire_interaction_lease_for_binding(binding, thread_id)
        return binding, False

    def execution_binding_for_thread_locked(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> ChatBindingKey | None:
        owner = self.thread_write_owner(thread_id)
        if owner is not None:
            return owner
        subscribers = self.thread_subscribers(thread_id)
        if len(subscribers) != 1:
            return None
        binding = subscribers[0]
        if adopt_sole_subscriber:
            lease = self.acquire_thread_write_lease_locked(binding, thread_id)
            if lease.granted:
                state = self._runtime_state_by_binding.get(binding)
                if state is not None:
                    self.sync_stored_binding_locked(binding, state)
        return binding

    def existing_chat_binding_key_locked(self, sender_id: str, chat_id: str) -> ChatBindingKey | None:
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        if group_binding in self._runtime_state_by_binding:
            return group_binding
        sender_binding = (sender_id, chat_id)
        if sender_binding in self._runtime_state_by_binding:
            return sender_binding
        return None

    def fresh_chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        if sender_id == GROUP_SHARED_BINDING_OWNER_ID:
            return (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        if self._is_group_chat(chat_id, message_id):
            return (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        return (sender_id, chat_id)

    def get_or_create_runtime_state_locked(self, binding: ChatBindingKey) -> MutableMapping[str, Any]:
        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            return state

        state = self.build_default_runtime_state()
        stored_binding = self._chat_binding_store.load(binding)
        if stored_binding is not None:
            self.apply_stored_binding(state, stored_binding)
            current_thread_id = str(state["current_thread_id"] or "").strip()
            if state["current_thread_runtime_state"] == "attached":
                self.subscribe_thread_locked(binding, current_thread_id)
            owner_thread_id = str(stored_binding.get("current_thread_write_owner_thread_id", "") or "").strip()
            if (
                state["current_thread_runtime_state"] == "attached"
                and owner_thread_id
                and owner_thread_id == current_thread_id
            ):
                self.acquire_thread_write_lease_locked(binding, owner_thread_id)
        self._runtime_state_by_binding[binding] = state
        return state

    def resolve_runtime_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> ResolvedRuntimeBinding:
        with self._lock:
            existing = self.existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return ResolvedRuntimeBinding(
                    binding=existing,
                    state=self.get_or_create_runtime_state_locked(existing),
                )

        binding = self.fresh_chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            existing = self.existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                binding = existing
            return ResolvedRuntimeBinding(
                binding=binding,
                state=self.get_or_create_runtime_state_locked(binding),
            )

    def get_runtime_state(self, sender_id: str, chat_id: str, message_id: str = "") -> MutableMapping[str, Any]:
        return self.resolve_runtime_binding(sender_id, chat_id, message_id).state

    def get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView:
        state = self.resolve_runtime_binding(sender_id, chat_id, message_id).state
        with self._lock:
            return build_runtime_view(state)

    def stored_binding_from_runtime(self, binding: ChatBindingKey, state: MutableMapping[str, Any]) -> dict[str, str]:
        current_thread_id = str(state["current_thread_id"]).strip()
        current_thread_runtime_state = str(state["current_thread_runtime_state"]).strip()
        if not current_thread_id:
            current_thread_runtime_state = ""
        current_thread_write_owner_thread_id = ""
        if (
            current_thread_id
            and current_thread_runtime_state == "attached"
            and self._thread_lease_registry.lease_owner(current_thread_id) == binding
        ):
            current_thread_write_owner_thread_id = current_thread_id
        return {
            "working_dir": str(state["working_dir"]).strip(),
            "current_thread_id": current_thread_id,
            "current_thread_title": str(state["current_thread_title"]).strip(),
            "current_thread_runtime_state": current_thread_runtime_state,
            "current_thread_write_owner_thread_id": current_thread_write_owner_thread_id,
            "approval_policy": str(state["approval_policy"]).strip(),
            "sandbox": str(state["sandbox"]).strip(),
            "collaboration_mode": str(state["collaboration_mode"]).strip(),
        }

    def sync_stored_binding_locked(self, binding: ChatBindingKey, state: MutableMapping[str, Any]) -> None:
        stored_binding = self.stored_binding_from_runtime(binding, state)
        if stored_binding == self.build_default_stored_binding():
            self._chat_binding_store.clear(binding)
            return
        self._chat_binding_store.save(binding, stored_binding)

    def save_stored_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        resolved = self.resolve_runtime_binding(sender_id, chat_id, message_id)
        with self._lock:
            self.sync_stored_binding_locked(resolved.binding, resolved.state)

    def hydrate_stored_bindings(self) -> None:
        stored_bindings = self._chat_binding_store.load_all()
        if not stored_bindings:
            return
        with self._lock:
            for binding, stored_binding in sorted(stored_bindings.items()):
                if binding in self._runtime_state_by_binding:
                    continue
                state = self.build_default_runtime_state()
                self.apply_stored_binding(state, stored_binding)
                self._runtime_state_by_binding[binding] = state
            for binding, stored_binding in sorted(stored_bindings.items()):
                state = self._runtime_state_by_binding[binding]
                current_thread_id = str(state["current_thread_id"] or "").strip()
                if state["current_thread_runtime_state"] == "attached":
                    self.subscribe_thread_locked(binding, current_thread_id)
                owner_thread_id = str(stored_binding.get("current_thread_write_owner_thread_id", "") or "").strip()
                if (
                    state["current_thread_runtime_state"] == "attached"
                    and owner_thread_id
                    and owner_thread_id == current_thread_id
                ):
                    interaction_lease = self.acquire_interaction_lease_for_binding(binding, owner_thread_id)
                    if not interaction_lease.granted and interaction_lease.lease is not None:
                        logger.warning(
                            "stored interaction owner conflicted during hydration: thread=%s owner=%s ignored=%s",
                            owner_thread_id[:12],
                            interaction_lease.lease.holder.holder_id,
                            binding,
                        )
                        self.sync_stored_binding_locked(binding, state)
                        continue
                    lease = self.acquire_thread_write_lease_locked(binding, owner_thread_id)
                    if not lease.granted and lease.owner != binding:
                        logger.warning(
                            "stored write owner conflicted during hydration: thread=%s owner=%s ignored=%s",
                            owner_thread_id[:12],
                            lease.owner,
                            binding,
                        )
                        self.sync_stored_binding_locked(binding, state)

    @staticmethod
    def binding_has_inflight_turn_locked(state: MutableMapping[str, Any]) -> bool:
        return bool(state["running"] or state["awaiting_local_turn_started"] or state["current_turn_id"])

    def deactivate_binding_locked(
        self,
        binding: ChatBindingKey,
        *,
        on_deactivate_state: Callable[[MutableMapping[str, Any]], None] | None = None,
    ) -> str:
        state = self._runtime_state_by_binding.pop(binding, None)
        self._chat_binding_store.clear(binding)
        if state is None:
            return ""
        if on_deactivate_state is not None:
            on_deactivate_state(state)
        thread_id = str(state["current_thread_id"] or "").strip()
        self.release_thread_write_lease_locked(binding, thread_id)
        self.release_interaction_lease_for_binding(binding, thread_id)
        if self.unsubscribe_thread_locked(binding, thread_id):
            return thread_id
        return ""

    def visit_runtime_states_locked(self, visitor: Callable[[MutableMapping[str, Any]], None]) -> None:
        for state in list(self._runtime_state_by_binding.values()):
            visitor(state)

    def binding_keys_locked(self) -> tuple[ChatBindingKey, ...]:
        return tuple(sorted(self._runtime_state_by_binding))

    def binding_keys_for_chat_locked(self, chat_id: str) -> tuple[ChatBindingKey, ...]:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ()
        return tuple(sorted(binding for binding in self._runtime_state_by_binding if binding[1] == normalized_chat_id))

    def binding_runtime_snapshot_locked(self, binding: ChatBindingKey) -> BindingRuntimeSnapshot | None:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            return None
        return BindingRuntimeSnapshot(
            binding=binding,
            active=bool(state["active"]),
            thread_id=str(state["current_thread_id"] or "").strip(),
            thread_title=str(state["current_thread_title"] or "").strip(),
            working_dir=str(state["working_dir"] or "").strip(),
            feishu_runtime_state=str(state["current_thread_runtime_state"] or "").strip(),
            has_inflight_turn=self.binding_has_inflight_turn_locked(state),
        )

    def bind_thread_locked(
        self,
        binding: ChatBindingKey,
        state: MutableMapping[str, Any],
        *,
        thread_id: str,
        thread_title: str,
        working_dir: str,
        on_thread_replaced: Callable[[MutableMapping[str, Any]], None] | None = None,
        on_after_bind: Callable[[MutableMapping[str, Any]], None] | None = None,
    ) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        unsubscribe_thread_id = ""
        old_thread_id = str(state["current_thread_id"] or "").strip()
        if old_thread_id != normalized_thread_id:
            self.release_interaction_lease_for_binding(binding, old_thread_id)
            if self.unsubscribe_thread_locked(binding, old_thread_id):
                unsubscribe_thread_id = old_thread_id
            if on_thread_replaced is not None:
                on_thread_replaced(state)
        self.apply_persisted_runtime_state_message_locked(
            binding,
            state,
            ThreadStateChanged(
                current_thread_id=normalized_thread_id,
                current_thread_title=str(thread_title or "").strip(),
                current_thread_runtime_state="attached",
                working_dir=str(working_dir or state["working_dir"]).strip(),
            ),
        )
        if on_after_bind is not None:
            on_after_bind(state)
        self.subscribe_thread_locked(binding, normalized_thread_id)
        return unsubscribe_thread_id

    def clear_thread_binding_locked(
        self,
        binding: ChatBindingKey,
        state: MutableMapping[str, Any],
        *,
        on_clear_state: Callable[[MutableMapping[str, Any]], None] | None = None,
    ) -> str:
        thread_id = str(state["current_thread_id"] or "").strip()
        self.release_interaction_lease_for_binding(binding, thread_id)
        unsubscribe_thread_id = ""
        if self.unsubscribe_thread_locked(binding, thread_id):
            unsubscribe_thread_id = thread_id
        if on_clear_state is not None:
            on_clear_state(state)
        self.apply_persisted_runtime_state_message_locked(
            binding,
            state,
            ThreadStateChanged(
                current_thread_id="",
                current_thread_title="",
                current_thread_runtime_state="",
            ),
        )
        return unsubscribe_thread_id

    def bound_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return []
        return sorted(
            binding
            for binding, state in self._runtime_state_by_binding.items()
            if str(state["current_thread_id"] or "").strip() == normalized_thread_id
        )

    def attached_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return []
        return sorted(
            binding
            for binding, state in self._runtime_state_by_binding.items()
            if (
                str(state["current_thread_id"] or "").strip() == normalized_thread_id
                and str(state["current_thread_runtime_state"] or "").strip() == "attached"
            )
        )

    def active_chat_ids_for_thread_locked(
        self,
        thread_id: str,
        *,
        exclude_binding: ChatBindingKey | None = None,
    ) -> list[str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return []
        chat_ids: set[str] = set()
        for binding in self.thread_subscribers(normalized_thread_id):
            if exclude_binding is not None and binding == exclude_binding:
                continue
            state = self._runtime_state_by_binding.get(binding)
            if state is None or not bool(state["active"]):
                continue
            if str(state["current_thread_id"] or "").strip() != normalized_thread_id:
                continue
            chat_ids.add(binding[1])
        return sorted(chat_ids)

    def interaction_owner_snapshot_locked(
        self,
        thread_id: str,
        *,
        current_binding: ChatBindingKey | None = None,
    ) -> dict[str, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return {
                "kind": "none",
                "holder_id": "",
                "binding_id": "",
                "relation": "none",
                "label": "none",
            }
        lease = self.current_interaction_lease_locked(normalized_thread_id)
        if lease is None:
            return {
                "kind": "none",
                "holder_id": "",
                "binding_id": "",
                "relation": "none",
                "label": "none",
            }
        holder = lease.holder
        if holder.kind == "feishu":
            binding = feishu_binding_from_holder(holder)
            binding_id = format_binding_id(binding) if binding is not None else ""
            relation = "current" if binding is not None and binding == current_binding else "other"
            return {
                "kind": "feishu",
                "holder_id": holder.holder_id,
                "binding_id": binding_id,
                "relation": relation,
                "label": binding_id or "feishu:unknown",
            }
        return {
            "kind": holder.kind,
            "holder_id": holder.holder_id,
            "binding_id": "",
            "relation": "external",
            "label": holder.holder_id,
        }

    def release_feishu_runtime_availability_locked(self, thread_id: str) -> tuple[bool, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False, "当前没有绑定线程。"
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if not attached_bindings:
            return False, "当前 thread 的 Feishu runtime 已经是 `released`。"
        for binding in attached_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            if self.binding_has_inflight_turn_locked(state):
                return False, "当前有飞书侧 turn 正在运行，不能释放 runtime。"
        return True, ""

    def release_feishu_runtime_by_thread_id_locked(
        self,
        thread_id: str,
        *,
        release_feishu_runtime_availability: Callable[[str], tuple[bool, str]],
        on_release_binding_state: Callable[[MutableMapping[str, Any]], None] | None = None,
    ) -> ReleaseFeishuRuntimeResult:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        if not bound_bindings:
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if attached_bindings:
            release_available, release_reason = release_feishu_runtime_availability(normalized_thread_id)
            if not release_available:
                raise ValueError(release_reason)
        released_binding_ids: list[str] = []
        for binding in attached_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            self.release_thread_write_lease_locked(binding, normalized_thread_id)
            self.release_interaction_lease_for_binding(binding, normalized_thread_id)
            self.unsubscribe_thread_locked(binding, normalized_thread_id)
            if on_release_binding_state is not None:
                on_release_binding_state(state)
            self.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                ThreadStateChanged(current_thread_runtime_state="released"),
            )
            released_binding_ids.append(format_binding_id(binding))
        unsubscribe_thread_id = ""
        if attached_bindings and not self.attached_bindings_for_thread_locked(normalized_thread_id):
            unsubscribe_thread_id = normalized_thread_id
        existing_title = ""
        existing_cwd = ""
        for binding in bound_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            existing_title = existing_title or str(state["current_thread_title"] or "").strip()
            existing_cwd = existing_cwd or str(state["working_dir"] or "").strip()
        return ReleaseFeishuRuntimeResult(
            thread_id=normalized_thread_id,
            thread_title=existing_title,
            working_dir=existing_cwd,
            bound_binding_ids=[format_binding_id(binding) for binding in bound_bindings],
            released_binding_ids=released_binding_ids,
            changed=bool(released_binding_ids),
            already_released=bool(bound_bindings) and not attached_bindings,
            unsubscribe_thread_id=unsubscribe_thread_id,
        )

    def binding_status_snapshot(
        self,
        binding: ChatBindingKey,
        *,
        read_thread_summary_for_status: Callable[[str], tuple[Any, str]],
        release_feishu_runtime_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        with self._lock:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                raise ValueError(f"未找到绑定：{format_binding_id(binding)}")
            thread_id = str(state["current_thread_id"] or "").strip()
            thread_title = str(state["current_thread_title"] or "").strip()
            working_dir = str(state["working_dir"] or "").strip()
            feishu_runtime_state = str(state["current_thread_runtime_state"] or "").strip() or "not-applicable"
            running_turn = self.binding_has_inflight_turn_locked(state)
            current_turn_id = str(state["current_turn_id"] or "").strip()
            owner = self._thread_lease_registry.lease_owner(thread_id) if thread_id else None
            interaction_owner = self.interaction_owner_snapshot_locked(
                thread_id,
                current_binding=binding,
            )
            approval_policy = str(state["approval_policy"] or "").strip()
            sandbox = str(state["sandbox"] or "").strip()
            collaboration_mode = str(state["collaboration_mode"] or "").strip()
        release_available, release_reason = release_feishu_runtime_availability(thread_id)
        summary, backend_thread_status = read_thread_summary_for_status(thread_id)
        if summary is not None:
            thread_title = summary.title or thread_title
            working_dir = summary.cwd or working_dir
        if owner is None:
            feishu_write_owner_relation = "none"
            feishu_write_owner_binding_id = ""
        elif owner == binding:
            feishu_write_owner_relation = "current"
            feishu_write_owner_binding_id = format_binding_id(owner)
        else:
            feishu_write_owner_relation = "other"
            feishu_write_owner_binding_id = format_binding_id(owner)
        return {
            "binding_id": format_binding_id(binding),
            "binding_kind": binding_kind(binding),
            "sender_id": binding[0],
            "chat_id": binding[1],
            "binding_state": "bound" if thread_id else "unbound",
            "thread_id": thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "feishu_runtime_state": feishu_runtime_state,
            "backend_thread_status": backend_thread_status or "not-applicable",
            "backend_running_turn": backend_thread_status == "active",
            "feishu_write_owner_binding_id": feishu_write_owner_binding_id,
            "feishu_write_owner_relation": feishu_write_owner_relation,
            "interaction_owner": interaction_owner,
            "running_turn": running_turn,
            "current_turn_id": current_turn_id,
            "approval_policy": approval_policy,
            "sandbox": sandbox,
            "collaboration_mode": collaboration_mode,
            "reprofile_possible": bool(thread_id and backend_thread_status == "notLoaded"),
            "release_feishu_runtime_available": bool(thread_id and release_available),
            "release_feishu_runtime_reason": release_reason,
        }

    def thread_binding_snapshot_locked(
        self,
        thread_id: str,
        *,
        release_feishu_runtime_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        owner = self.thread_write_owner(normalized_thread_id)
        interaction_owner = self.interaction_owner_snapshot_locked(normalized_thread_id)
        release_available, release_reason = release_feishu_runtime_availability(normalized_thread_id)
        if not bound_bindings:
            release_available = False
            release_reason = "当前没有 Feishu 绑定指向该线程。"
        attached_binding_set = set(attached_bindings)
        return {
            "thread_id": normalized_thread_id,
            "bound_binding_ids": [format_binding_id(binding) for binding in bound_bindings],
            "attached_binding_ids": [format_binding_id(binding) for binding in attached_bindings],
            "released_binding_ids": [
                format_binding_id(binding) for binding in bound_bindings if binding not in attached_binding_set
            ],
            "feishu_write_owner_binding_id": format_binding_id(owner) if owner is not None else "",
            "interaction_owner": interaction_owner,
            "release_feishu_runtime_available": bool(release_available and bound_bindings),
            "release_feishu_runtime_reason": release_reason,
        }

    def binding_inventory_locked(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for binding, state in sorted(self._runtime_state_by_binding.items(), key=lambda item: format_binding_id(item[0])):
            thread_id = str(state["current_thread_id"] or "").strip()
            items.append(
                {
                    "binding_id": format_binding_id(binding),
                    "binding_kind": binding_kind(binding),
                    "sender_id": binding[0],
                    "chat_id": binding[1],
                    "binding_state": "bound" if thread_id else "unbound",
                    "thread_id": thread_id,
                    "thread_title": str(state["current_thread_title"] or "").strip(),
                    "working_dir": str(state["working_dir"] or "").strip(),
                    "feishu_runtime_state": (
                        str(state["current_thread_runtime_state"] or "").strip() or "not-applicable"
                    ),
                    "feishu_write_owner": bool(
                        thread_id and self.thread_write_owner(thread_id) == binding
                    ),
                    "running_turn": self.binding_has_inflight_turn_locked(state),
                    "approval_policy": str(state["approval_policy"] or "").strip(),
                    "sandbox": str(state["sandbox"] or "").strip(),
                    "collaboration_mode": str(state["collaboration_mode"] or "").strip(),
                }
            )
        return items
