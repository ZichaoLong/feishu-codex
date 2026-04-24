from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias

from bot.approval_policy import normalize_approval_policy
from bot.binding_identity import binding_kind, format_binding_id
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_NOT_APPLICABLE,
    FEISHU_RUNTIME_RELEASED,
    RuntimeStateMessage,
    RuntimeStateDict,
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
from bot.thread_subscription_registry import ThreadSubscriptionRegistry

ChatBindingKey: TypeAlias = tuple[str, str]


@dataclass(frozen=True)
class ResolvedRuntimeBinding:
    binding: ChatBindingKey
    state: RuntimeStateDict


@dataclass(frozen=True)
class UnsubscribeThreadResult:
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
        thread_subscription_registry: ThreadSubscriptionRegistry,
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
        self._thread_subscription_registry = thread_subscription_registry
        self._interaction_lease_store = interaction_lease_store
        self._is_group_chat = is_group_chat
        self._runtime_state_by_binding: dict[ChatBindingKey, RuntimeStateDict] = {}

    @staticmethod
    def apply_runtime_state_message_locked(
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
    ) -> None:
        apply_runtime_state_message(state, message)

    def apply_persisted_runtime_state_message_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
    ) -> None:
        self.apply_runtime_state_message_locked(state, message)
        self.sync_stored_binding_locked(binding, state)

    def build_default_stored_binding(self) -> dict[str, str]:
        return {
            "working_dir": "",
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "approval_policy": "",
            "sandbox": "",
            "collaboration_mode": "",
        }

    def build_default_runtime_state(self) -> RuntimeStateDict:
        return {
            "active": False,
            "working_dir": self._default_working_dir,
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
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
            "approval_policy": self._default_approval_policy,
            "sandbox": self._default_sandbox,
            "collaboration_mode": self._default_collaboration_mode,
            "model": self._default_model,
            "reasoning_effort": self._default_reasoning_effort,
            "plan_message_id": "",
            "plan_turn_id": "",
            "plan_explanation": "",
            "plan_steps": [],
            "plan_text": "",
        }

    def hydrate_stored_binding_locked(self, state: RuntimeStateDict, stored_binding: dict[str, str]) -> None:
        apply_runtime_state_message(
            state,
            StoredBindingHydrated(
                working_dir=stored_binding["working_dir"] or self._default_working_dir,
                current_thread_id=stored_binding["current_thread_id"],
                current_thread_title=stored_binding["current_thread_title"],
                feishu_runtime_state=stored_binding["feishu_runtime_state"],
                approval_policy=normalize_approval_policy(
                    stored_binding["approval_policy"] or self._default_approval_policy,
                ),
                sandbox=stored_binding["sandbox"] or self._default_sandbox,
                collaboration_mode=stored_binding["collaboration_mode"] or self._default_collaboration_mode,
            ),
        )

    def subscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._thread_subscription_registry.subscribe(binding, thread_id)

    def unsubscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._thread_subscription_registry.unsubscribe(binding, thread_id)

    def thread_subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        return self._thread_subscription_registry.subscribers(thread_id)

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

    def get_or_create_runtime_state_locked(self, binding: ChatBindingKey) -> RuntimeStateDict:
        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            return state

        state = self.build_default_runtime_state()
        stored_binding = self._chat_binding_store.load(binding)
        if stored_binding is not None:
            self.hydrate_stored_binding_locked(state, stored_binding)
            current_thread_id = str(state["current_thread_id"] or "").strip()
            if state["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                self.subscribe_thread_locked(binding, current_thread_id)
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

    def get_runtime_state(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeStateDict:
        return self.resolve_runtime_binding(sender_id, chat_id, message_id).state

    def get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView:
        state = self.resolve_runtime_binding(sender_id, chat_id, message_id).state
        with self._lock:
            return build_runtime_view(state)

    def stored_binding_from_runtime(self, binding: ChatBindingKey, state: RuntimeStateDict) -> dict[str, str]:
        del binding
        current_thread_id = str(state["current_thread_id"]).strip()
        feishu_runtime_state = str(state["feishu_runtime_state"]).strip()
        if not current_thread_id:
            feishu_runtime_state = ""
        working_dir = str(state["working_dir"]).strip()
        approval_policy = normalize_approval_policy(str(state["approval_policy"]).strip())
        sandbox = str(state["sandbox"]).strip()
        collaboration_mode = str(state["collaboration_mode"]).strip()
        return {
            "working_dir": "" if working_dir == self._default_working_dir else working_dir,
            "current_thread_id": current_thread_id,
            "current_thread_title": str(state["current_thread_title"]).strip(),
            "feishu_runtime_state": feishu_runtime_state,
            "approval_policy": "" if approval_policy == self._default_approval_policy else approval_policy,
            "sandbox": "" if sandbox == self._default_sandbox else sandbox,
            "collaboration_mode": (
                ""
                if collaboration_mode == self._default_collaboration_mode
                else collaboration_mode
            ),
        }

    def sync_stored_binding_locked(self, binding: ChatBindingKey, state: RuntimeStateDict) -> None:
        stored_binding = self.stored_binding_from_runtime(binding, state)
        if all(not str(value or "").strip() for value in stored_binding.values()):
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
                self.hydrate_stored_binding_locked(state, stored_binding)
                self._runtime_state_by_binding[binding] = state
            for binding, stored_binding in sorted(stored_bindings.items()):
                state = self._runtime_state_by_binding[binding]
                current_thread_id = str(state["current_thread_id"] or "").strip()
                if state["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                    self.subscribe_thread_locked(binding, current_thread_id)

    @staticmethod
    def binding_has_inflight_turn_locked(state: RuntimeStateDict) -> bool:
        return bool(state["running"] or state["awaiting_local_turn_started"] or state["current_turn_id"])

    def deactivate_binding_locked(
        self,
        binding: ChatBindingKey,
        *,
        on_deactivate_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> str:
        state = self._runtime_state_by_binding.pop(binding, None)
        self._chat_binding_store.clear(binding)
        if state is None:
            return ""
        if on_deactivate_state is not None:
            on_deactivate_state(state)
        thread_id = str(state["current_thread_id"] or "").strip()
        self.release_interaction_lease_for_binding(binding, thread_id)
        if self.unsubscribe_thread_locked(binding, thread_id):
            return thread_id
        return ""

    def visit_runtime_states_locked(self, visitor: Callable[[RuntimeStateDict], None]) -> None:
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
            feishu_runtime_state=str(state["feishu_runtime_state"] or "").strip(),
            has_inflight_turn=self.binding_has_inflight_turn_locked(state),
        )

    def bind_thread_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
        *,
        thread_id: str,
        thread_title: str,
        working_dir: str,
        on_thread_replaced: Callable[[RuntimeStateDict], None] | None = None,
        on_after_bind: Callable[[RuntimeStateDict], None] | None = None,
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
                feishu_runtime_state=FEISHU_RUNTIME_ATTACHED,
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
        state: RuntimeStateDict,
        *,
        on_clear_state: Callable[[RuntimeStateDict], None] | None = None,
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
                feishu_runtime_state="",
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
                and str(state["feishu_runtime_state"] or "").strip() == FEISHU_RUNTIME_ATTACHED
            )
        )

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

    def unsubscribe_feishu_runtime_by_thread_id_locked(
        self,
        thread_id: str,
        *,
        unsubscribe_availability: Callable[[str], tuple[bool, str]],
        on_release_binding_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> UnsubscribeThreadResult:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        if not bound_bindings:
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if attached_bindings:
            unsubscribe_available, unsubscribe_reason = unsubscribe_availability(normalized_thread_id)
            if not unsubscribe_available:
                raise ValueError(unsubscribe_reason)
        released_binding_ids: list[str] = []
        for binding in attached_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            self.release_interaction_lease_for_binding(binding, normalized_thread_id)
            self.unsubscribe_thread_locked(binding, normalized_thread_id)
            if on_release_binding_state is not None:
                on_release_binding_state(state)
            self.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                ThreadStateChanged(feishu_runtime_state=FEISHU_RUNTIME_RELEASED),
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
        return UnsubscribeThreadResult(
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
        unsubscribe_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = self.binding_status_state_snapshot_locked(binding)
        thread_id = str(snapshot["thread_id"] or "").strip()
        unsubscribe_available, unsubscribe_reason = unsubscribe_availability(thread_id)
        summary, backend_thread_status = read_thread_summary_for_status(thread_id)
        if summary is not None:
            snapshot["thread_title"] = summary.title or str(snapshot["thread_title"] or "").strip()
            snapshot["working_dir"] = summary.cwd or str(snapshot["working_dir"] or "").strip()
        snapshot["backend_thread_status"] = backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
        snapshot["backend_running_turn"] = backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
        snapshot["reprofile_possible"] = bool(
            thread_id and backend_thread_status == BACKEND_THREAD_STATUS_NOT_LOADED
        )
        snapshot["unsubscribe_available"] = bool(thread_id and unsubscribe_available)
        snapshot["unsubscribe_reason"] = unsubscribe_reason
        return snapshot

    def binding_status_state_snapshot_locked(self, binding: ChatBindingKey) -> dict[str, Any]:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            raise ValueError(f"未找到绑定：{format_binding_id(binding)}")
        thread_id = str(state["current_thread_id"] or "").strip()
        return {
            "binding_id": format_binding_id(binding),
            "binding_kind": binding_kind(binding),
            "sender_id": binding[0],
            "chat_id": binding[1],
            "binding_state": "bound" if thread_id else "unbound",
            "thread_id": thread_id,
            "thread_title": str(state["current_thread_title"] or "").strip(),
            "working_dir": str(state["working_dir"] or "").strip(),
            "feishu_runtime_state": (
                str(state["feishu_runtime_state"] or "").strip() or FEISHU_RUNTIME_NOT_APPLICABLE
            ),
            "interaction_owner": self.interaction_owner_snapshot_locked(
                thread_id,
                current_binding=binding,
            ),
            "running_turn": self.binding_has_inflight_turn_locked(state),
            "current_turn_id": str(state["current_turn_id"] or "").strip(),
            "approval_policy": str(state["approval_policy"] or "").strip(),
            "sandbox": str(state["sandbox"] or "").strip(),
            "collaboration_mode": str(state["collaboration_mode"] or "").strip(),
        }

    def thread_binding_snapshot_locked(
        self,
        thread_id: str,
        *,
        unsubscribe_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        interaction_owner = self.interaction_owner_snapshot_locked(normalized_thread_id)
        unsubscribe_available, unsubscribe_reason = unsubscribe_availability(normalized_thread_id)
        if not bound_bindings:
            unsubscribe_available = False
            unsubscribe_reason = "当前没有 Feishu 绑定指向该线程。"
        attached_binding_set = set(attached_bindings)
        return {
            "thread_id": normalized_thread_id,
            "bound_binding_ids": [format_binding_id(binding) for binding in bound_bindings],
            "attached_binding_ids": [format_binding_id(binding) for binding in attached_bindings],
            "released_binding_ids": [
                format_binding_id(binding) for binding in bound_bindings if binding not in attached_binding_set
            ],
            "interaction_owner": interaction_owner,
            "unsubscribe_available": bool(unsubscribe_available and bound_bindings),
            "unsubscribe_reason": unsubscribe_reason,
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
                        str(state["feishu_runtime_state"] or "").strip() or FEISHU_RUNTIME_NOT_APPLICABLE
                    ),
                    "running_turn": self.binding_has_inflight_turn_locked(state),
                    "approval_policy": str(state["approval_policy"] or "").strip(),
                    "sandbox": str(state["sandbox"] or "").strip(),
                    "collaboration_mode": str(state["collaboration_mode"] or "").strip(),
                }
            )
        return items
