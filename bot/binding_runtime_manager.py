from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias

from bot.approval_policy import normalize_approval_policy
from bot.binding_identity import binding_kind, format_binding_id
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.execution_transcript import ExecutionTranscript
from bot.feishu_types import StoredChatBinding
from bot.permissions_profile import (
    BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
    normalize_permissions_profile_id,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    FEISHU_RUNTIME_NOT_APPLICABLE,
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
logger = logging.getLogger(__name__)


class _NoOpTimer:
    def cancel(self) -> None:
        return None


@dataclass(frozen=True)
class ResolvedRuntimeBinding:
    binding: ChatBindingKey
    state: RuntimeStateDict


@dataclass(frozen=True)
class DetachThreadResult:
    thread_id: str
    thread_title: str
    working_dir: str
    bound_binding_ids: list[str]
    detached_binding_ids: list[str]
    changed: bool
    already_detached: bool
    unsubscribe_thread_id: str = ""


@dataclass(frozen=True)
class DetachBindingResult:
    thread_id: str
    thread_title: str
    working_dir: str
    binding_id: str
    changed: bool
    already_detached: bool
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
        default_permissions_profile_id: str = "",
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
        self._default_permissions_profile_id = normalize_permissions_profile_id(
            str(default_permissions_profile_id or "").strip(),
            fallback=BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
        )
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
        staged_state = self._staged_runtime_state_after_message_locked(state, message)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        self.apply_runtime_state_message_locked(state, message)

    def build_default_stored_binding(self) -> StoredChatBinding:
        return {
            "working_dir": "",
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "approval_policy": "",
            "permissions_profile_id": "",
            "model": "",
            "reasoning_effort": "",
            "configured_settings": [],
        }

    def build_default_runtime_state(self) -> RuntimeStateDict:
        return {
            "active": False,
            "working_dir": self._default_working_dir,
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "goal_objective": "",
            "goal_status": "",
            "goal_token_budget": None,
            "goal_tokens_used": 0,
            "goal_time_used_seconds": 0,
            "goal_created_at": 0,
            "goal_updated_at": 0,
            "current_turn_id": "",
            "running": False,
            "cancelled": False,
            "pending_cancel": False,
            "current_message_id": "",
            "last_execution_message_id": "",
            "current_execution_kind": "",
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
            "awaiting_attach_status_settle": False,
            "approval_policy": self._default_approval_policy,
            "permissions_profile_id": self._default_permissions_profile_id,
            "model": self._default_model,
            "reasoning_effort": self._default_reasoning_effort,
            "configured_settings": [],
            "plan_message_id": "",
            "plan_turn_id": "",
            "plan_explanation": "",
            "plan_steps": [],
            "plan_text": "",
        }

    def hydrate_stored_binding_locked(self, state: RuntimeStateDict, stored_binding: StoredChatBinding) -> bool:
        feishu_runtime_state = stored_binding["feishu_runtime_state"]
        downgraded_attached = False
        if feishu_runtime_state == FEISHU_RUNTIME_ATTACHED:
            # A persisted `attached` only says the previous service connection had
            # runtime residency. A new process / backend connection must attach
            # explicitly before it can receive live thread events.
            feishu_runtime_state = FEISHU_RUNTIME_DETACHED
            downgraded_attached = True
        apply_runtime_state_message(
            state,
            StoredBindingHydrated(
                working_dir=stored_binding["working_dir"] or self._default_working_dir,
                current_thread_id=stored_binding["current_thread_id"],
                current_thread_title=stored_binding["current_thread_title"],
                feishu_runtime_state=feishu_runtime_state,
                approval_policy=normalize_approval_policy(
                    stored_binding["approval_policy"] or self._default_approval_policy,
                ),
                permissions_profile_id=normalize_permissions_profile_id(
                    stored_binding.get("permissions_profile_id", "") or self._default_permissions_profile_id,
                    fallback=self._default_permissions_profile_id,
                ),
                model=str(stored_binding.get("model", "") or "").strip(),
                reasoning_effort=str(stored_binding.get("reasoning_effort", "") or "").strip(),
                configured_settings=list(stored_binding.get("configured_settings", [])),
            ),
        )
        return downgraded_attached

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
            downgraded_attached = self.hydrate_stored_binding_locked(state, stored_binding)
            if downgraded_attached:
                self.release_interaction_lease_for_binding(binding, str(state["current_thread_id"] or "").strip())
                self.sync_stored_binding_locked(binding, state)
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

    def stored_binding_from_runtime(self, binding: ChatBindingKey, state: RuntimeStateDict) -> StoredChatBinding:
        del binding
        current_thread_id = str(state["current_thread_id"]).strip()
        feishu_runtime_state = str(state["feishu_runtime_state"]).strip()
        if not current_thread_id:
            feishu_runtime_state = ""
        working_dir = str(state["working_dir"]).strip()
        approval_policy = normalize_approval_policy(str(state["approval_policy"]).strip())
        permissions_profile_id = normalize_permissions_profile_id(
            str(state["permissions_profile_id"]).strip(),
            fallback=self._default_permissions_profile_id,
        )
        model = str(state["model"]).strip()
        reasoning_effort = str(state["reasoning_effort"]).strip()
        return {
            "working_dir": "" if working_dir == self._default_working_dir else working_dir,
            "current_thread_id": current_thread_id,
            "current_thread_title": str(state["current_thread_title"]).strip(),
            "feishu_runtime_state": feishu_runtime_state,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "configured_settings": list(state.get("configured_settings", [])),
        }

    def sync_stored_binding_locked(self, binding: ChatBindingKey, state: RuntimeStateDict) -> None:
        stored_binding = self.stored_binding_from_runtime(binding, state)
        self._persist_stored_binding_locked(binding, stored_binding)

    def _persist_stored_binding_locked(
        self,
        binding: ChatBindingKey,
        stored_binding: StoredChatBinding,
    ) -> None:
        if self._is_empty_stored_binding(stored_binding):
            self._chat_binding_store.clear(binding)
            return
        self._chat_binding_store.save(binding, stored_binding)

    def _is_empty_stored_binding(self, stored_binding: StoredChatBinding) -> bool:
        return (
            not str(stored_binding.get("working_dir", "") or "").strip()
            and not str(stored_binding.get("current_thread_id", "") or "").strip()
            and not str(stored_binding.get("current_thread_title", "") or "").strip()
            and not str(stored_binding.get("feishu_runtime_state", "") or "").strip()
            and not list(stored_binding.get("configured_settings", []))
        )

    def save_stored_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        resolved = self.resolve_runtime_binding(sender_id, chat_id, message_id)
        with self._lock:
            self.sync_stored_binding_locked(resolved.binding, resolved.state)

    def hydrate_stored_bindings(self, *, read_only: bool = False, replace: bool = False) -> None:
        stored_bindings = self._chat_binding_store.load_all()
        with self._lock:
            if replace:
                self._runtime_state_by_binding.clear()
                self._thread_subscription_registry.clear()
            if stored_bindings:
                self.hydrate_missing_stored_bindings_locked(stored_bindings, read_only=read_only)

    def hydrate_missing_stored_bindings_locked(
        self,
        stored_bindings: dict[ChatBindingKey, StoredChatBinding] | None = None,
        *,
        read_only: bool = False,
    ) -> tuple[ChatBindingKey, ...]:
        loaded_bindings = stored_bindings if stored_bindings is not None else self._chat_binding_store.load_all()
        if not loaded_bindings:
            return ()

        hydrated_bindings: list[ChatBindingKey] = []
        for binding, stored_binding in sorted(loaded_bindings.items()):
            if binding in self._runtime_state_by_binding:
                continue
            state = self.build_default_runtime_state()
            downgraded_attached = self.hydrate_stored_binding_locked(state, stored_binding)
            self._runtime_state_by_binding[binding] = state
            if downgraded_attached and not read_only:
                self.release_interaction_lease_for_binding(binding, str(state["current_thread_id"] or "").strip())
                self.sync_stored_binding_locked(binding, state)
            current_thread_id = str(state["current_thread_id"] or "").strip()
            if state["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                self.subscribe_thread_locked(binding, current_thread_id)
            hydrated_bindings.append(binding)
        return tuple(hydrated_bindings)

    @staticmethod
    def binding_has_inflight_turn_locked(state: RuntimeStateDict) -> bool:
        return bool(state["running"] or state["awaiting_local_turn_started"] or state["current_turn_id"])

    def deactivate_binding_locked(
        self,
        binding: ChatBindingKey,
        *,
        on_deactivate_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> str:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            self._chat_binding_store.clear(binding)
            return ""
        unsubscribe_thread_ids = self.deactivate_bindings_locked(
            [binding],
            on_deactivate_state=on_deactivate_state,
        )
        return unsubscribe_thread_ids[0] if unsubscribe_thread_ids else ""

    def deactivate_bindings_locked(
        self,
        bindings: list[ChatBindingKey] | tuple[ChatBindingKey, ...],
        *,
        on_deactivate_state: Callable[[RuntimeStateDict], None] | None = None,
        cleanup_errors: list[str] | None = None,
    ) -> tuple[str, ...]:
        plans: list[tuple[ChatBindingKey, RuntimeStateDict, str, StoredChatBinding]] = []
        seen: set[ChatBindingKey] = set()
        for binding in bindings:
            if binding in seen:
                continue
            seen.add(binding)
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            if on_deactivate_state is not None:
                staged_state = self._clone_runtime_state_for_staging(state)
                on_deactivate_state(staged_state)
            plans.append(
                (
                    binding,
                    state,
                    str(state["current_thread_id"] or "").strip(),
                    self.stored_binding_from_runtime(binding, state),
                )
            )
        if not plans:
            return ()

        rollback_entries: list[tuple[ChatBindingKey, StoredChatBinding]] = []
        try:
            for binding, _state, _thread_id, original_stored_binding in plans:
                self._chat_binding_store.clear(binding)
                rollback_entries.append((binding, original_stored_binding))
        except Exception:
            self._rollback_stored_binding_updates_locked(rollback_entries)
            raise

        unsubscribe_thread_ids: list[str] = []
        interaction_lease_errors: list[str] = []
        for binding, state, thread_id, original_stored_binding in plans:
            release_error = self._release_interaction_lease_for_binding_commit_locked(binding, thread_id)
            if release_error:
                interaction_lease_errors.append(release_error)
                try:
                    self._persist_stored_binding_locked(binding, original_stored_binding)
                except Exception as exc:
                    logger.exception(
                        "恢复待清理 binding 标记失败: binding=%s",
                        format_binding_id(binding),
                    )
                    interaction_lease_errors.append(
                        f"恢复待清理 binding 标记失败: {format_binding_id(binding)}: {exc}"
                    )
                continue
            self._apply_commit_state_callback_locked(
                binding,
                state,
                on_deactivate_state,
                action="提交 deactivate runtime state",
            )
            unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(
                binding,
                thread_id,
            )
            self.unsubscribe_thread_locked(binding, thread_id)
            self._runtime_state_by_binding.pop(binding, None)
            if unsubscribe_thread_id:
                unsubscribe_thread_ids.append(unsubscribe_thread_id)
        if interaction_lease_errors:
            if cleanup_errors is not None:
                cleanup_errors.extend(interaction_lease_errors)
            else:
                raise RuntimeError("；".join(interaction_lease_errors))
        return tuple(unsubscribe_thread_ids)

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

    @staticmethod
    def _clone_runtime_state_for_staging(state: RuntimeStateDict) -> RuntimeStateDict:
        staged_state = dict(state)
        staged_state["execution_transcript"] = state["execution_transcript"].clone()
        staged_state["plan_steps"] = list(state["plan_steps"])
        staged_state["patch_timer"] = _NoOpTimer() if state["patch_timer"] is not None else None
        staged_state["mirror_watchdog_timer"] = (
            _NoOpTimer() if state["mirror_watchdog_timer"] is not None else None
        )
        return staged_state  # type: ignore[return-value]

    def _staged_runtime_state_after_message_locked(
        self,
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
        *,
        on_stage_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> RuntimeStateDict:
        staged_state = self._clone_runtime_state_for_staging(state)
        if on_stage_state is not None:
            on_stage_state(staged_state)
        self.apply_runtime_state_message_locked(staged_state, message)
        return staged_state

    def _rollback_stored_binding_updates_locked(
        self,
        stored_bindings: list[tuple[ChatBindingKey, StoredChatBinding]],
    ) -> None:
        for binding, stored_binding in reversed(stored_bindings):
            try:
                self._persist_stored_binding_locked(binding, stored_binding)
            except Exception:
                logger.exception("回滚 binding 持久化失败: binding=%s", format_binding_id(binding))

    def _apply_commit_state_callback_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
        callback: Callable[[RuntimeStateDict], None] | None,
        *,
        action: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(state)
        except Exception:
            logger.exception("%s 失败: binding=%s", action, format_binding_id(binding))

    def _release_interaction_lease_for_binding_commit_locked(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> str:
        holder = self._feishu_interaction_holder(binding)
        try:
            self._interaction_lease_store.release(thread_id, holder)
            remaining = self._interaction_lease_store.load(thread_id)
        except Exception as exc:
            logger.exception(
                "释放 interaction lease 失败: binding=%s thread=%s",
                format_binding_id(binding),
                thread_id[:12],
            )
            return f"释放 interaction lease 失败: {format_binding_id(binding)}: {exc}"
        if remaining is not None and remaining.holder.same_holder(holder):
            message = f"interaction lease 仍由已清理 binding 持有: {format_binding_id(binding)}"
            logger.error("%s thread=%s", message, thread_id[:12])
            return message
        return ""

    def _unsubscribe_thread_id_if_last_subscriber_locked(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ""
        subscribers = self.thread_subscribers(normalized_thread_id)
        if len(subscribers) == 1 and subscribers[0] == binding:
            return normalized_thread_id
        return ""

    def _unsubscribe_thread_id_if_last_attached_binding_locked(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ""
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if len(attached_bindings) == 1 and attached_bindings[0] == binding:
            return normalized_thread_id
        return ""

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
        old_thread_id = str(state["current_thread_id"] or "").strip()
        staged_state = self._clone_runtime_state_for_staging(state)
        if old_thread_id != normalized_thread_id and on_thread_replaced is not None:
            on_thread_replaced(staged_state)
        self.apply_runtime_state_message_locked(
            staged_state,
            ThreadStateChanged(
                current_thread_id=normalized_thread_id,
                current_thread_title=str(thread_title or "").strip(),
                feishu_runtime_state=FEISHU_RUNTIME_ATTACHED,
                working_dir=str(working_dir or staged_state["working_dir"]).strip(),
            ),
        )
        if on_after_bind is not None:
            on_after_bind(staged_state)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = ""
        if old_thread_id != normalized_thread_id:
            unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(binding, old_thread_id)
            if on_thread_replaced is not None:
                on_thread_replaced(state)
        self.apply_runtime_state_message_locked(
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
        if old_thread_id != normalized_thread_id:
            try:
                self.release_interaction_lease_for_binding(binding, old_thread_id)
            except Exception:
                logger.exception("释放旧 interaction lease 失败: thread=%s", old_thread_id[:12])
            self.unsubscribe_thread_locked(binding, old_thread_id)
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
        staged_state = self._clone_runtime_state_for_staging(state)
        if on_clear_state is not None:
            on_clear_state(staged_state)
        self.apply_runtime_state_message_locked(
            staged_state,
            ThreadStateChanged(
                current_thread_id="",
                current_thread_title="",
                feishu_runtime_state="",
            ),
        )
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(binding, thread_id)
        if on_clear_state is not None:
            on_clear_state(state)
        self.apply_runtime_state_message_locked(
            state,
            ThreadStateChanged(
                current_thread_id="",
                current_thread_title="",
                feishu_runtime_state="",
            ),
        )
        try:
            self.release_interaction_lease_for_binding(binding, thread_id)
        except Exception:
            logger.exception("释放 interaction lease 失败: thread=%s", thread_id[:12])
        self.unsubscribe_thread_locked(binding, thread_id)
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

    def detach_thread_bindings_locked(
        self,
        thread_id: str,
        *,
        detach_availability: Callable[[str], tuple[bool, str]],
        on_release_binding_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> DetachThreadResult:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        if not bound_bindings:
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if attached_bindings:
            detach_available, detach_reason = detach_availability(normalized_thread_id)
            if not detach_available:
                raise ValueError(detach_reason)
        detach_message = ThreadStateChanged(feishu_runtime_state=FEISHU_RUNTIME_DETACHED)
        plans: list[tuple[ChatBindingKey, RuntimeStateDict, StoredChatBinding, StoredChatBinding]] = []
        for binding in attached_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            staged_state = self._staged_runtime_state_after_message_locked(
                state,
                detach_message,
                on_stage_state=on_release_binding_state,
            )
            plans.append(
                (
                    binding,
                    state,
                    self.stored_binding_from_runtime(binding, state),
                    self.stored_binding_from_runtime(binding, staged_state),
                )
            )

        rollback_entries: list[tuple[ChatBindingKey, StoredChatBinding]] = []
        try:
            for binding, _state, original_stored_binding, detached_stored_binding in plans:
                self._persist_stored_binding_locked(binding, detached_stored_binding)
                rollback_entries.append((binding, original_stored_binding))
        except Exception:
            self._rollback_stored_binding_updates_locked(rollback_entries)
            raise

        detached_binding_ids: list[str] = []
        for binding, state, _original_stored_binding, _detached_stored_binding in plans:
            self.apply_runtime_state_message_locked(
                state,
                detach_message,
            )
            self._apply_commit_state_callback_locked(
                binding,
                state,
                on_release_binding_state,
                action="提交 detach runtime state",
            )
            self._release_interaction_lease_for_binding_commit_locked(binding, normalized_thread_id)
            self.unsubscribe_thread_locked(binding, normalized_thread_id)
            detached_binding_ids.append(format_binding_id(binding))
        unsubscribe_thread_id = normalized_thread_id if plans else ""
        existing_title = ""
        existing_cwd = ""
        for binding in bound_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            existing_title = existing_title or str(state["current_thread_title"] or "").strip()
            existing_cwd = existing_cwd or str(state["working_dir"] or "").strip()
        return DetachThreadResult(
            thread_id=normalized_thread_id,
            thread_title=existing_title,
            working_dir=existing_cwd,
            bound_binding_ids=[format_binding_id(binding) for binding in bound_bindings],
            detached_binding_ids=detached_binding_ids,
            changed=bool(detached_binding_ids),
            already_detached=bool(bound_bindings) and not attached_bindings,
            unsubscribe_thread_id=unsubscribe_thread_id,
        )

    def detach_binding_locked(
        self,
        binding: ChatBindingKey,
        *,
        on_detach_binding_state: Callable[[RuntimeStateDict], None] | None = None,
    ) -> DetachBindingResult:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            raise ValueError("当前 binding 不存在。")
        thread_id = str(state["current_thread_id"] or "").strip()
        if not thread_id:
            raise ValueError("当前没有绑定 thread。")
        if str(state["feishu_runtime_state"] or "").strip() != FEISHU_RUNTIME_ATTACHED:
            return DetachBindingResult(
                thread_id=thread_id,
                thread_title=str(state["current_thread_title"] or "").strip(),
                working_dir=str(state["working_dir"] or "").strip(),
                binding_id=format_binding_id(binding),
                changed=False,
                already_detached=True,
            )
        detach_message = ThreadStateChanged(feishu_runtime_state=FEISHU_RUNTIME_DETACHED)
        staged_state = self._staged_runtime_state_after_message_locked(
            state,
            detach_message,
            on_stage_state=on_detach_binding_state,
        )
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_attached_binding_locked(binding, thread_id)
        self.apply_runtime_state_message_locked(state, detach_message)
        self._apply_commit_state_callback_locked(
            binding,
            state,
            on_detach_binding_state,
            action="提交 detach runtime state",
        )
        self._release_interaction_lease_for_binding_commit_locked(binding, thread_id)
        self.unsubscribe_thread_locked(binding, thread_id)
        return DetachBindingResult(
            thread_id=thread_id,
            thread_title=str(state["current_thread_title"] or "").strip(),
            working_dir=str(state["working_dir"] or "").strip(),
            binding_id=format_binding_id(binding),
            changed=True,
            already_detached=False,
            unsubscribe_thread_id=unsubscribe_thread_id,
        )

    def binding_status_snapshot(
        self,
        binding: ChatBindingKey,
        *,
        read_thread_summary_for_status: Callable[[str], tuple[Any, str]],
        detach_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = self.binding_status_state_snapshot_locked(binding)
        thread_id = str(snapshot["thread_id"] or "").strip()
        detach_available, detach_reason = detach_availability(thread_id)
        summary, backend_thread_status = read_thread_summary_for_status(thread_id)
        if summary is not None:
            snapshot["thread_title"] = summary.title or str(snapshot["thread_title"] or "").strip()
            snapshot["working_dir"] = summary.cwd or str(snapshot["working_dir"] or "").strip()
        snapshot["backend_thread_status"] = backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
        snapshot["backend_running_turn"] = backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
        snapshot["detach_available"] = bool(thread_id and detach_available)
        snapshot["detach_reason"] = detach_reason
        return snapshot

    def binding_status_state_snapshot_locked(self, binding: ChatBindingKey) -> dict[str, Any]:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            raise ValueError(f"未找到绑定：{format_binding_id(binding)}")
        thread_id = str(state["current_thread_id"] or "").strip()
        binding_state = "bound" if thread_id else self._unbound_binding_state_label(binding)
        return {
            "binding_id": format_binding_id(binding),
            "binding_kind": binding_kind(binding),
            "sender_id": binding[0],
            "chat_id": binding[1],
            "binding_state": binding_state,
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
            "permissions_profile_id": str(state["permissions_profile_id"] or "").strip(),
            "model": str(state["model"] or "").strip(),
            "reasoning_effort": str(state["reasoning_effort"] or "").strip(),
            "goal_objective": str(state.get("goal_objective") or "").strip(),
            "goal_status": str(state.get("goal_status") or "").strip(),
            "goal_token_budget": state.get("goal_token_budget"),
            "goal_tokens_used": int(state.get("goal_tokens_used") or 0),
            "goal_time_used_seconds": int(state.get("goal_time_used_seconds") or 0),
            "goal_created_at": int(state.get("goal_created_at") or 0),
            "goal_updated_at": int(state.get("goal_updated_at") or 0),
        }

    def thread_binding_snapshot_locked(
        self,
        thread_id: str,
        *,
        detach_availability: Callable[[str], tuple[bool, str]],
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        interaction_owner = self.interaction_owner_snapshot_locked(normalized_thread_id)
        detach_available, detach_reason = detach_availability(normalized_thread_id)
        if not bound_bindings:
            detach_available = False
            detach_reason = "当前没有 Feishu 绑定指向该线程。"
        attached_binding_set = set(attached_bindings)
        existing_title = ""
        existing_cwd = ""
        for binding in bound_bindings:
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                continue
            existing_title = existing_title or str(state["current_thread_title"] or "").strip()
            existing_cwd = existing_cwd or str(state["working_dir"] or "").strip()
        return {
            "thread_id": normalized_thread_id,
            "thread_title": existing_title,
            "working_dir": existing_cwd,
            "bound_binding_ids": [format_binding_id(binding) for binding in bound_bindings],
            "attached_binding_ids": [format_binding_id(binding) for binding in attached_bindings],
            "detached_binding_ids": [
                format_binding_id(binding) for binding in bound_bindings if binding not in attached_binding_set
            ],
            "interaction_owner": interaction_owner,
            "detach_available": bool(detach_available and bound_bindings),
            "detach_reason": detach_reason,
        }

    def binding_inventory_locked(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        stored_bindings = self._chat_binding_store.load_all()
        for binding, state in sorted(self._runtime_state_by_binding.items(), key=lambda item: format_binding_id(item[0])):
            thread_id = str(state["current_thread_id"] or "").strip()
            binding_state = "bound" if thread_id else self._unbound_binding_state_label(
                binding,
                stored_bindings=stored_bindings,
            )
            items.append(
                {
                    "binding_id": format_binding_id(binding),
                    "binding_kind": binding_kind(binding),
                    "sender_id": binding[0],
                    "chat_id": binding[1],
                    "binding_state": binding_state,
                    "thread_id": thread_id,
                    "thread_title": str(state["current_thread_title"] or "").strip(),
                    "working_dir": str(state["working_dir"] or "").strip(),
                    "feishu_runtime_state": (
                        str(state["feishu_runtime_state"] or "").strip() or FEISHU_RUNTIME_NOT_APPLICABLE
                    ),
                    "running_turn": self.binding_has_inflight_turn_locked(state),
                    "approval_policy": str(state["approval_policy"] or "").strip(),
                    "permissions_profile_id": str(state["permissions_profile_id"] or "").strip(),
                    "model": str(state["model"] or "").strip(),
                    "reasoning_effort": str(state["reasoning_effort"] or "").strip(),
                }
            )
        return items

    def _unbound_binding_state_label(
        self,
        binding: ChatBindingKey,
        *,
        stored_bindings: dict[ChatBindingKey, StoredChatBinding] | None = None,
    ) -> str:
        loaded = stored_bindings if stored_bindings is not None else self._chat_binding_store.load_all()
        return "configured/unbound" if binding in loaded else "unbound"
