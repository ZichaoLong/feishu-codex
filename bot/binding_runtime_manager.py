from __future__ import annotations

import logging
import os
from typing import Any, Callable, TypeAlias

from bot.approval_policy import normalize_approval_policy
from bot.binding_identity import binding_kind, format_binding_id
from bot.binding_owner_authority import BindingOwnerAuthority
from bot.binding_runtime_contract import (
    OWNER_LOSS_DISPOSITION_ABANDON,
    OWNER_LOSS_DISPOSITION_TERMINAL,
    BindingDetachOwnerLossReceipt,
    BindingDeactivationCommitReceipt,
    BindingGoalSnapshot,
    BindingOwnerLossCommand,
    BindingOwnerLossSettlementReceipt,
    BindingOwnerRevisionReceipt,
    BindingRecordSnapshot,
    BindingRuntimeHandle,
    BindingRuntimeSnapshot,
    BindingSessionSnapshot,
    BindingThreadBindResult,
    BindingThreadClearResult,
    DetachBindingResult,
    DetachThreadResult,
    OwnerLossDisposition,
)
from bot.binding_runtime_lifecycle import (
    BindingRuntimeLifecycleTransitions,
    RuntimeTimerCancellationEffect,
)
from bot.binding_runtime_session_authority import BindingRuntimeSessionAuthority
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.binding_runtime_state_factory import BindingRuntimeStateFactory
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.feishu_types import StoredChatBinding
from bot.permissions_profile import normalize_permissions_profile_id
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    FEISHU_RUNTIME_NOT_APPLICABLE,
    UNSET,
    BindingActivated,
    ExecutionPatchTimerRegistration,
    MirrorWatchdogRegistration,
    RuntimeStateMessage,
    RuntimeStateDict,
    RuntimeSettingsChanged,
    ThreadGoalCleared,
    ThreadGoalStateChanged,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseStore,
    feishu_binding_from_holder,
    make_feishu_interaction_holder,
)
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator

ChatBindingKey: TypeAlias = tuple[str, str]
HydrationPlan: TypeAlias = tuple[
    ChatBindingKey, RuntimeStateDict, bool, StoredChatBinding
]
logger = logging.getLogger(__name__)


class _NoOpTimer:
    def cancel(self) -> None:
        return None


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
        owner_loss_settler: Callable[
            [BindingOwnerLossCommand], BindingOwnerLossSettlementReceipt
        ]
        | None = None,
    ) -> None:
        self._lock = lock
        self._state_factory = BindingRuntimeStateFactory(
            default_working_dir=default_working_dir,
            default_approval_policy=default_approval_policy,
            default_permissions_profile_id=default_permissions_profile_id,
            default_model=default_model,
            default_reasoning_effort=default_reasoning_effort,
        )
        self._default_working_dir = self._state_factory.default_working_dir
        self._default_approval_policy = self._state_factory.default_approval_policy
        self._default_permissions_profile_id = (
            self._state_factory.default_permissions_profile_id
        )
        self._chat_binding_store = chat_binding_store
        self._thread_subscription_registry = thread_subscription_registry
        self._interaction_lease_store = interaction_lease_store
        self._is_group_chat = is_group_chat
        self._owner_loss_settler = owner_loss_settler
        self._runtime_state_by_binding: dict[ChatBindingKey, RuntimeStateDict] = {}
        self._binding_owner_authority = BindingOwnerAuthority()
        self._session_authority = BindingRuntimeSessionAuthority()
        self._lifecycle = BindingRuntimeLifecycleTransitions(
            turn_execution=TurnExecutionCoordinator(),
        )

    @staticmethod
    def _apply_runtime_state_message_locked(
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
    ) -> None:
        apply_runtime_state_message(state, message)

    def _apply_persisted_runtime_state_message_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
    ) -> None:
        self._require_resident_state_current_locked(binding, state)
        staged_state = self._staged_runtime_state_after_message_locked(state, message)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        self._apply_runtime_state_message_locked(state, message)

    def build_default_stored_binding(self) -> StoredChatBinding:
        return self._state_factory.build_default_stored_binding()

    def build_default_runtime_state(self) -> RuntimeStateDict:
        return self._state_factory.build_default_runtime_state()

    def hydrate_stored_binding_locked(self, state: RuntimeStateDict, stored_binding: StoredChatBinding) -> bool:
        return self._state_factory.hydrate_stored_binding(state, stored_binding)

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

    def _get_or_create_runtime_state_locked(self, binding: ChatBindingKey) -> RuntimeStateDict:
        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            return state

        state = self.build_default_runtime_state()
        stored_binding = self._chat_binding_store.load(binding)
        if stored_binding is not None:
            downgraded_attached = self.hydrate_stored_binding_locked(state, stored_binding)
            if downgraded_attached:
                _owner, settlement = self._settle_binding_owner_loss_locked(
                    binding,
                    str(state["current_thread_id"] or "").strip(),
                    reason="binding_hydrated",
                    disposition=OWNER_LOSS_DISPOSITION_ABANDON,
                )
                if (
                    binding in self._runtime_state_by_binding
                    or self._chat_binding_store.load(binding) != stored_binding
                ):
                    raise RuntimeError(
                        "binding 在 hydration owner-loss 期间发生变化："
                        f"{format_binding_id(binding)}"
                )
                self._sync_staged_stored_binding_locked(binding, state)
                self._advance_binding_owner_revision_locked(
                    binding,
                    settled_command=settlement.command,
                )
            current_thread_id = str(state["current_thread_id"] or "").strip()
            if state["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                self.subscribe_thread_locked(binding, current_thread_id)
        self._runtime_state_by_binding[binding] = state
        self._session_authority.install(binding, resident_state=state)
        return state

    def resolve_session(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> BindingSessionSnapshot:
        with self._lock:
            existing = self.existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                state = self._get_or_create_runtime_state_locked(existing)
                return self._session_snapshot_for_state_locked(existing, state)

        binding = self.fresh_chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            existing = self.existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                binding = existing
            state = self._get_or_create_runtime_state_locked(binding)
            return self._session_snapshot_for_state_locked(binding, state)

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        if type(handle) is not BindingRuntimeHandle:
            raise RuntimeError("binding runtime handle 缺少 typed identity。")
        state = self._resident_state_for_handle_locked(handle)
        return project_binding_session_snapshot(state, handle=handle)

    def resident_session_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None:
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            return None
        return self._session_snapshot_for_state_locked(binding, state)

    def binding_session_inventory_locked(self) -> tuple[BindingSessionSnapshot, ...]:
        return tuple(
            self._session_snapshot_for_state_locked(binding, state)
            for binding, state in sorted(self._runtime_state_by_binding.items())
        )

    def _session_snapshot_for_state_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> BindingSessionSnapshot:
        handle = self._session_authority.install(binding, resident_state=state)
        return project_binding_session_snapshot(state, handle=handle)

    def _resident_state_for_handle_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> RuntimeStateDict:
        if type(handle) is not BindingRuntimeHandle:
            raise RuntimeError("binding runtime handle 缺少 typed identity。")
        state = self._runtime_state_by_binding.get(handle.binding)
        if state is None:
            raise RuntimeError("binding runtime handle 已退役或被替换。")
        self._session_authority.require(
            handle,
            binding=handle.binding,
            resident_state=state,
        )
        return state

    def stored_binding_from_runtime(self, binding: ChatBindingKey, state: RuntimeStateDict) -> StoredChatBinding:
        del binding
        return self._state_factory.stored_binding_from_runtime(state)

    def persist_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        state = self._resident_state_for_handle_locked(handle)
        self._sync_staged_stored_binding_locked(handle.binding, state)
        return project_binding_session_snapshot(state, handle=handle)

    def _sync_resident_state_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> None:
        """Persist a raw resident for manager-internal staging tests only."""

        self._require_resident_state_current_locked(binding, state)
        self._sync_staged_stored_binding_locked(binding, state)

    def activate_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        state = self._resident_state_for_handle_locked(handle)
        if not state["active"]:
            self._apply_runtime_state_message_locked(state, BindingActivated())
        return project_binding_session_snapshot(state, handle=handle)

    def update_runtime_settings_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        approval_policy: Any = UNSET,
        permissions_profile_id: Any = UNSET,
        model: Any = UNSET,
        reasoning_effort: Any = UNSET,
    ) -> BindingSessionSnapshot:
        state = self._resident_state_for_handle_locked(handle)
        self._apply_persisted_runtime_state_message_locked(
            handle.binding,
            state,
            RuntimeSettingsChanged(
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            ),
        )
        return project_binding_session_snapshot(state, handle=handle)

    def update_thread_metadata_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        expected_thread_id: str,
        current_thread_title: Any = UNSET,
        working_dir: Any = UNSET,
    ) -> BindingSessionSnapshot | None:
        state = self._resident_state_for_handle_locked(handle)
        normalized_thread_id = str(expected_thread_id or "").strip()
        if (
            not normalized_thread_id
            or str(state["current_thread_id"] or "").strip()
            != normalized_thread_id
        ):
            return None
        self._apply_persisted_runtime_state_message_locked(
            handle.binding,
            state,
            ThreadStateChanged(
                current_thread_title=current_thread_title,
                working_dir=working_dir,
            ),
        )
        return project_binding_session_snapshot(state, handle=handle)

    def project_thread_goal_locked(
        self,
        handle: BindingRuntimeHandle,
        goal: BindingGoalSnapshot | None,
        *,
        expected_thread_id: str = "",
    ) -> BindingSessionSnapshot | None:
        state = self._resident_state_for_handle_locked(handle)
        normalized_thread_id = str(expected_thread_id or "").strip()
        if normalized_thread_id and (
            str(state["current_thread_id"] or "").strip()
            != normalized_thread_id
        ):
            return None
        if goal is None:
            message: RuntimeStateMessage = ThreadGoalCleared()
        elif type(goal) is BindingGoalSnapshot:
            message = ThreadGoalStateChanged(
                goal_objective=goal.objective,
                goal_status=goal.status,
                goal_token_budget=goal.token_budget,
                goal_tokens_used=goal.tokens_used,
                goal_time_used_seconds=goal.time_used_seconds,
                goal_created_at=goal.created_at,
                goal_updated_at=goal.updated_at,
            )
        else:
            raise TypeError("thread goal projection requires an exact snapshot")
        self._apply_runtime_state_message_locked(state, message)
        return project_binding_session_snapshot(state, handle=handle)

    def _sync_staged_stored_binding_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> None:
        stored_binding = self.stored_binding_from_runtime(binding, state)
        self._persist_stored_binding_locked(binding, stored_binding)

    def _require_resident_state_current_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> None:
        if self._runtime_state_by_binding.get(binding) is not state:
            raise RuntimeError(
                "binding runtime persistence 使用了 stale resident state："
                f"{format_binding_id(binding)}"
            )

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
        return self._state_factory.is_empty_stored_binding(stored_binding)

    def save_stored_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        resolved = self.resolve_session(sender_id, chat_id, message_id)
        with self._lock:
            binding = self.existing_chat_binding_key_locked(sender_id, chat_id)
            if binding is None:
                binding = resolved.binding
            current = self._session_snapshot_for_state_locked(
                binding,
                self._get_or_create_runtime_state_locked(binding),
            )
            self.persist_session_locked(current.handle)

    def hydrate_stored_bindings(self, *, replace: bool = False) -> None:
        """Reconcile durable bindings into runtime and settle stale owners.

        This is a mutating startup/recovery operation.  Observation and
        planning must use the immutable record snapshot APIs instead.
        """

        stored_bindings = self._chat_binding_store.load_all()
        with self._lock:
            preflighted_owner_losses: dict[
                ChatBindingKey,
                tuple[
                    BindingOwnerRevisionReceipt,
                    BindingOwnerLossSettlementReceipt,
                    bool,
                ],
            ] = {}
            if not replace:
                if stored_bindings:
                    self._hydrate_missing_stored_bindings_locked(stored_bindings)
                return

            resident_states = dict(self._runtime_state_by_binding)
            resident_owners = {
                binding: self._binding_owner_receipt_locked(
                    binding,
                    str(state.get("current_thread_id", "") or "").strip(),
                )
                for binding, state in resident_states.items()
            }
            candidates: dict[ChatBindingKey, str] = {}
            for binding, state in resident_states.items():
                if str(state.get("feishu_runtime_state", "") or "").strip() == FEISHU_RUNTIME_ATTACHED:
                    thread_id = str(state.get("current_thread_id", "") or "").strip()
                    if thread_id:
                        candidates[binding] = thread_id
            for binding, stored_binding in stored_bindings.items():
                if str(stored_binding.get("feishu_runtime_state", "") or "").strip() != FEISHU_RUNTIME_ATTACHED:
                    continue
                thread_id = str(stored_binding.get("current_thread_id", "") or "").strip()
                current_candidate = candidates.get(binding)
                if current_candidate and current_candidate != thread_id:
                    raise RuntimeError(
                        "runtime 与 stored binding 指向不同 owner；"
                        f"已按 fail-closed 拒绝 replace：{format_binding_id(binding)}"
                    )
                if thread_id:
                    candidates[binding] = thread_id
            for binding, thread_id in sorted(candidates.items()):
                owner, settlement = self._settle_binding_owner_loss_locked(
                    binding,
                    thread_id,
                    reason="binding_hydrated",
                    disposition=OWNER_LOSS_DISPOSITION_ABANDON,
                )
                preflighted_owner_losses[binding] = (owner, settlement, True)

            if set(self._runtime_state_by_binding) != set(resident_states) or any(
                self._runtime_state_by_binding[binding] is not state
                for binding, state in resident_states.items()
            ):
                raise RuntimeError("binding 在 replace hydration 预检期间被替换。")
            for binding, owner in resident_owners.items():
                self.require_binding_owner_receipt_current(owner)
                preflighted = preflighted_owner_losses.get(binding)
                if preflighted is None:
                    self._binding_owner_authority.require_owner_loss_not_pending(owner)
                elif preflighted[0] is not owner:
                    raise RuntimeError("replace hydration owner receipt 不匹配。")

            plans, settlement_by_binding = self._prepare_hydration_plans_locked(
                stored_bindings,
                settled_owner_losses=preflighted_owner_losses,
                include_resident=True,
            )
            for binding in resident_states:
                preflighted = preflighted_owner_losses.get(binding)
                self._retire_binding_owner_generation_locked(
                    binding,
                    settled_command=(
                        preflighted[1].command if preflighted is not None else None
                    ),
                )
                settlement_by_binding.pop(binding, None)
            self._session_authority.retire_all()
            self._runtime_state_by_binding.clear()
            self._thread_subscription_registry.clear()
            self._install_hydration_plans_locked(
                plans,
                settlement_by_binding=settlement_by_binding,
            )

    def _hydrate_missing_stored_bindings_locked(
        self,
        stored_bindings: dict[ChatBindingKey, StoredChatBinding] | None = None,
        *,
        preflighted_owner_losses: dict[
            ChatBindingKey,
            tuple[
                BindingOwnerRevisionReceipt,
                BindingOwnerLossSettlementReceipt,
                bool,
            ],
        ]
        | None = None,
    ) -> tuple[ChatBindingKey, ...]:
        """Commit startup hydration; inspection callers must use snapshots."""

        loaded_bindings = stored_bindings if stored_bindings is not None else self._chat_binding_store.load_all()
        if not loaded_bindings:
            return ()
        plans, settlement_by_binding = self._prepare_hydration_plans_locked(
            loaded_bindings,
            settled_owner_losses=preflighted_owner_losses or {},
            include_resident=False,
        )
        return self._install_hydration_plans_locked(
            plans,
            settlement_by_binding=settlement_by_binding,
        )

    def _prepare_hydration_plans_locked(
        self,
        loaded_bindings: dict[ChatBindingKey, StoredChatBinding],
        *,
        settled_owner_losses: dict[
            ChatBindingKey,
            tuple[
                BindingOwnerRevisionReceipt,
                BindingOwnerLossSettlementReceipt,
                bool,
            ],
        ],
        include_resident: bool,
    ) -> tuple[
        list[HydrationPlan],
        dict[ChatBindingKey, BindingOwnerLossSettlementReceipt],
    ]:
        plans: list[HydrationPlan] = []
        for binding, stored_binding in sorted(loaded_bindings.items()):
            if not include_resident and binding in self._runtime_state_by_binding:
                continue
            state = self.build_default_runtime_state()
            downgraded_attached = self.hydrate_stored_binding_locked(state, stored_binding)
            plans.append((binding, state, downgraded_attached, stored_binding))

        owner_receipts: list[BindingOwnerRevisionReceipt] = []
        settlement_by_binding = {
            binding: settlement
            for binding, (_owner, settlement, active) in settled_owner_losses.items()
            if active
        }
        for binding, state, downgraded_attached, _stored_binding in plans:
            if not downgraded_attached:
                continue
            thread_id = str(state["current_thread_id"] or "").strip()
            preflighted = settled_owner_losses.get(binding)
            if preflighted is not None and preflighted[0].expected_thread_id == thread_id:
                if preflighted[2]:
                    owner_receipts.append(preflighted[0])
                continue
            if include_resident:
                raise RuntimeError("replace hydration 缺少 exact owner-loss preflight。")
            owner, settlement = self._settle_binding_owner_loss_locked(
                binding,
                thread_id,
                reason="binding_hydrated",
                disposition=OWNER_LOSS_DISPOSITION_ABANDON,
            )
            owner_receipts.append(owner)
            settlement_by_binding[binding] = settlement

        for owner in owner_receipts:
            self.require_binding_owner_receipt_current(owner)
        for binding, _state, _downgraded, original_stored_binding in plans:
            if (
                (not include_resident and binding in self._runtime_state_by_binding)
                or self._chat_binding_store.load(binding) != original_stored_binding
            ):
                raise RuntimeError(
                    "binding 在 hydration 提交前发生变化："
                    f"{format_binding_id(binding)}"
                )

        rollback_entries: list[tuple[ChatBindingKey, StoredChatBinding]] = []
        try:
            for binding, state, downgraded_attached, original_stored_binding in plans:
                if downgraded_attached:
                    rollback_entries.append((binding, original_stored_binding))
                    self._sync_staged_stored_binding_locked(binding, state)
        except Exception:
            self._rollback_stored_binding_updates_locked(rollback_entries)
            raise
        return plans, settlement_by_binding

    def _install_hydration_plans_locked(
        self,
        plans: list[HydrationPlan],
        *,
        settlement_by_binding: dict[
            ChatBindingKey, BindingOwnerLossSettlementReceipt
        ],
    ) -> tuple[ChatBindingKey, ...]:
        hydrated_bindings: list[ChatBindingKey] = []
        for binding, state, downgraded_attached, _stored_binding in plans:
            if downgraded_attached:
                settlement = settlement_by_binding.get(binding)
                self._advance_binding_owner_revision_locked(
                    binding,
                    settled_command=(
                        settlement.command if settlement is not None else None
                    ),
                )
            self._runtime_state_by_binding[binding] = state
            self._session_authority.install(binding, resident_state=state)
            current_thread_id = str(state["current_thread_id"] or "").strip()
            if state["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                self.subscribe_thread_locked(binding, current_thread_id)
            hydrated_bindings.append(binding)
        return tuple(hydrated_bindings)

    @staticmethod
    def binding_has_inflight_turn_locked(state: RuntimeStateDict) -> bool:
        return bool(
            state["running"]
            or state["awaiting_local_turn_started"]
            or state["current_turn_id"]
        )

    def deactivate_bindings_with_receipts_locked(
        self,
        bindings: list[ChatBindingKey] | tuple[ChatBindingKey, ...],
        *,
        cleanup_errors: list[str] | None = None,
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
    ) -> tuple[BindingDeactivationCommitReceipt, ...]:
        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
        plans: list[
            tuple[ChatBindingKey, RuntimeStateDict | None, str, StoredChatBinding]
        ] = []
        seen: set[ChatBindingKey] = set()
        for binding in bindings:
            if binding in seen:
                continue
            seen.add(binding)
            state = self._runtime_state_by_binding.get(binding)
            if state is None:
                stored_binding = self._chat_binding_store.load(binding)
                if stored_binding is None:
                    continue
                plans.append(
                    (
                        binding,
                        None,
                        str(
                            stored_binding.get("current_thread_id", "") or ""
                        ).strip(),
                        stored_binding,
                    )
                )
                continue
            staged_state = self._clone_runtime_state_for_staging(state)
            self._lifecycle.project_deactivated_locked(binding, staged_state)
            if self._runtime_state_by_binding.get(binding) is not state:
                raise RuntimeError(
                    f"binding 在 deactivate 预检期间被替换：{format_binding_id(binding)}"
                )
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

        # Preflight every affected binding before any persistent or in-memory
        # cleanup, so one failed settlement cannot leave a partial batch.
        owner_receipts: list[BindingOwnerRevisionReceipt] = []
        settlement_by_binding: dict[
            ChatBindingKey, BindingOwnerLossSettlementReceipt
        ] = {}
        for binding, _state, thread_id, _original_stored_binding in plans:
            owner, settlement = self._settle_binding_owner_loss_locked(
                binding,
                thread_id,
                reason="binding_deactivated",
                disposition=normalized_disposition,
            )
            owner_receipts.append(owner)
            settlement_by_binding[binding] = settlement
        # Owner-loss settlement may call back into runtime control while this
        # RLock is held.  Reject an obsolete plan before clearing any store.
        for owner in owner_receipts:
            self.require_binding_owner_receipt_current(owner)
        for binding, state, thread_id, original_stored_binding in plans:
            if state is None:
                current_store = self._chat_binding_store.load(binding)
                if (
                    binding in self._runtime_state_by_binding
                    or current_store != original_stored_binding
                ):
                    raise RuntimeError(
                        f"binding 在 deactivate 核验期间发生变化：{format_binding_id(binding)}"
                    )
                continue
            if (
                self._runtime_state_by_binding.get(binding) is not state
                or str(state["current_thread_id"] or "").strip() != thread_id
            ):
                raise RuntimeError(
                    f"binding 在 deactivate 核验期间发生变化：{format_binding_id(binding)}"
                )

        rollback_entries: list[tuple[ChatBindingKey, StoredChatBinding]] = []
        try:
            for binding, _state, _thread_id, original_stored_binding in plans:
                rollback_entries.append((binding, original_stored_binding))
                self._chat_binding_store.clear(binding)
        except Exception:
            self._rollback_stored_binding_updates_locked(rollback_entries)
            raise

        committed_removals: list[BindingDeactivationCommitReceipt] = []
        commit_errors: list[str] = []

        def retain_original_store(
            binding: ChatBindingKey,
            state: RuntimeStateDict | None,
            original_stored_binding: StoredChatBinding,
        ) -> None:
            current = self._runtime_state_by_binding.get(binding)
            if current is not None and current is not state:
                return
            if state is None and self._chat_binding_store.load(binding) is not None:
                return
            try:
                self._persist_stored_binding_locked(binding, original_stored_binding)
            except Exception as exc:
                logger.exception(
                    "恢复待清理 binding 标记失败: binding=%s",
                    format_binding_id(binding),
                )
                commit_errors.append(
                    f"恢复待清理 binding 标记失败: {format_binding_id(binding)}: {exc}"
                )

        for binding, state, thread_id, original_stored_binding in plans:
            current = self._runtime_state_by_binding.get(binding)
            if (state is None and current is not None) or (
                state is not None and current is not state
            ):
                retain_original_store(binding, state, original_stored_binding)
                commit_errors.append(
                    f"binding 在 deactivate 提交前被替换: {format_binding_id(binding)}"
                )
                continue
            if state is None and self._chat_binding_store.load(binding) is not None:
                commit_errors.append(
                    f"binding 在 deactivate 提交前重新出现: {format_binding_id(binding)}"
                )
                continue
            if state is None:
                self._retire_binding_owner_generation_locked(
                    binding,
                    settled_command=settlement_by_binding[binding].command,
                )
                committed_removals.append(
                    BindingDeactivationCommitReceipt(
                        binding=binding,
                        thread_id=thread_id,
                    )
                )
                continue
            if self._runtime_state_by_binding.get(binding) is not state:
                commit_errors.append(
                    f"binding 在 deactivate lease 清理期间被替换: {format_binding_id(binding)}"
                )
                continue
            timer_cancellations = self._lifecycle.project_deactivated_locked(
                binding,
                state,
            )
            unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(
                binding,
                thread_id,
            )
            if self._runtime_state_by_binding.get(binding) is not state:
                commit_errors.append(
                    f"binding 在 deactivate unsubscribe 前被替换: {format_binding_id(binding)}"
                )
                continue
            self.unsubscribe_thread_locked(binding, thread_id)
            if self._runtime_state_by_binding.get(binding) is not state:
                replacement = self._runtime_state_by_binding.get(binding)
                if replacement is not None:
                    replacement_thread_id = str(
                        replacement["current_thread_id"] or ""
                    ).strip()
                    if replacement["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED:
                        self.subscribe_thread_locked(binding, replacement_thread_id)
                commit_errors.append(
                    f"binding 在 deactivate unsubscribe 期间被替换: {format_binding_id(binding)}"
                )
                continue
            self._retire_binding_owner_generation_locked(
                binding,
                settled_command=settlement_by_binding[binding].command,
            )
            del self._runtime_state_by_binding[binding]
            committed_removals.append(
                BindingDeactivationCommitReceipt(
                    binding=binding,
                    thread_id=thread_id,
                    unsubscribe_thread_id=unsubscribe_thread_id,
                    timer_cancellations=timer_cancellations,
                )
            )
        proof_available = True
        try:
            retained_thread_ids = {
                record.thread_id
                for record in self.binding_record_inventory_locked()
                if record.thread_id
            }
            retained_subscriber_thread_ids = {
                receipt.unsubscribe_thread_id
                for receipt in committed_removals
                if receipt.unsubscribe_thread_id
                and self.thread_subscribers(receipt.unsubscribe_thread_id)
            }
        except Exception as exc:
            proof_available = False
            retained_thread_ids = set()
            retained_subscriber_thread_ids = set()
            logger.exception("deactivate post-commit finalizer 核验失败")
            commit_errors.append(f"deactivate post-commit finalizer 核验失败: {exc}")
        safe_receipts = tuple(
            BindingDeactivationCommitReceipt(
                binding=receipt.binding,
                thread_id=receipt.thread_id,
                unsubscribe_thread_id=(
                    receipt.unsubscribe_thread_id
                    if proof_available
                    and receipt.unsubscribe_thread_id not in retained_thread_ids
                    and receipt.unsubscribe_thread_id
                    not in retained_subscriber_thread_ids
                    else ""
                ),
                timer_cancellations=receipt.timer_cancellations,
            )
            for receipt in committed_removals
        )
        if commit_errors:
            if cleanup_errors is not None:
                cleanup_errors.extend(commit_errors)
            else:
                raise RuntimeError("；".join(commit_errors))
        return safe_receipts

    def prepare_all_timer_cancellations_locked(
        self,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        effects: list[RuntimeTimerCancellationEffect] = []
        for binding in sorted(self._runtime_state_by_binding):
            effects.extend(
                self._lifecycle.project_deactivated_locked(
                    binding,
                    self._runtime_state_by_binding[binding],
                )
            )
        return tuple(effects)

    def binding_keys_locked(self) -> tuple[ChatBindingKey, ...]:
        return tuple(sorted(self._runtime_state_by_binding))

    def resident_runtime_state_locked(
        self,
        binding: ChatBindingKey,
    ) -> RuntimeStateDict | None:
        """Return the exact resident state without hydrating or remapping it.

        Delayed callbacks use this lookup so a callback for a cleared binding
        cannot recreate runtime state, and a callback for a replaced binding
        cannot silently resolve to the replacement through ingress rules.
        """

        return self._runtime_state_by_binding.get(binding)

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

    def binding_record_inventory_locked(self) -> tuple[BindingRecordSnapshot, ...]:
        """Inspect all effective binding records without hydrating runtime."""

        stored_bindings = self._chat_binding_store.load_all()
        bindings = sorted(set(self._runtime_state_by_binding) | set(stored_bindings))
        records: list[BindingRecordSnapshot] = []
        for binding in bindings:
            record = self._binding_record_snapshot_locked(
                binding,
                stored_binding=stored_bindings.get(binding),
            )
            if record is not None:
                records.append(record)
        return tuple(records)

    def binding_record_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingRecordSnapshot | None:
        """Inspect one effective record without creating runtime state."""

        if binding in self._runtime_state_by_binding:
            return self._binding_record_snapshot_locked(
                binding,
                stored_binding=None,
            )
        return self._binding_record_snapshot_locked(
            binding,
            stored_binding=self._chat_binding_store.load(binding),
        )

    def _binding_record_snapshot_locked(
        self,
        binding: ChatBindingKey,
        *,
        stored_binding: StoredChatBinding | None,
    ) -> BindingRecordSnapshot | None:
        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            return BindingRecordSnapshot(
                binding=binding,
                runtime_resident=True,
                thread_id=str(state["current_thread_id"] or "").strip(),
                thread_title=str(state["current_thread_title"] or "").strip(),
                working_dir=str(state["working_dir"] or "").strip(),
                feishu_runtime_state=str(
                    state["feishu_runtime_state"] or ""
                ).strip(),
                has_inflight_turn=self.binding_has_inflight_turn_locked(state),
                approval_policy=str(state["approval_policy"] or "").strip(),
                permissions_profile_id=str(
                    state["permissions_profile_id"] or ""
                ).strip(),
                model=str(state["model"] or "").strip(),
                reasoning_effort=str(state["reasoning_effort"] or "").strip(),
            )
        if stored_binding is None:
            return None

        persisted_runtime_state = str(
            stored_binding.get("feishu_runtime_state", "") or ""
        ).strip()
        effective_runtime_state = (
            FEISHU_RUNTIME_DETACHED
            if persisted_runtime_state == FEISHU_RUNTIME_ATTACHED
            else persisted_runtime_state
        )
        return BindingRecordSnapshot(
            binding=binding,
            runtime_resident=False,
            thread_id=str(
                stored_binding.get("current_thread_id", "") or ""
            ).strip(),
            thread_title=str(
                stored_binding.get("current_thread_title", "") or ""
            ).strip(),
            working_dir=(
                str(stored_binding.get("working_dir", "") or "").strip()
                or self._default_working_dir
            ),
            feishu_runtime_state=effective_runtime_state,
            has_inflight_turn=False,
            approval_policy=normalize_approval_policy(
                str(stored_binding.get("approval_policy", "") or "").strip()
                or self._default_approval_policy
            ),
            permissions_profile_id=normalize_permissions_profile_id(
                str(
                    stored_binding.get("permissions_profile_id", "") or ""
                ).strip()
                or self._default_permissions_profile_id,
                fallback=self._default_permissions_profile_id,
            ),
            model=str(stored_binding.get("model", "") or "").strip(),
            reasoning_effort=str(
                stored_binding.get("reasoning_effort", "") or ""
            ).strip(),
        )

    def binding_exists_locked(self, binding: ChatBindingKey) -> bool:
        if binding in self._runtime_state_by_binding:
            return True
        return self._chat_binding_store.load(binding) is not None

    def binding_owner_thread_id_locked(self, binding: ChatBindingKey) -> str:
        """Read the thread owner targeted by a future binding mutation.

        The in-memory runtime is authoritative once hydrated.  A store-only
        fallback is still needed for fail-closed cleanup of legacy/recovery
        entries, but callers must not reach into ``ChatBindingStore`` and
        duplicate that precedence rule.
        """

        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            return str(state.get("current_thread_id", "") or "").strip()
        stored_binding = self._chat_binding_store.load(binding)
        if stored_binding is None:
            return ""
        return str(stored_binding.get("current_thread_id", "") or "").strip()

    @staticmethod
    def _clone_runtime_state_for_staging(state: RuntimeStateDict) -> RuntimeStateDict:
        staged_state = dict(state)
        staged_state["execution_transcript"] = state["execution_transcript"].clone()
        staged_state["plan_steps"] = list(state["plan_steps"])
        patch_registration = state["patch_timer_registration"]
        staged_state["patch_timer_registration"] = (
            ExecutionPatchTimerRegistration(
                ticket=patch_registration.ticket,
                timer=_NoOpTimer(),
            )
            if patch_registration is not None
            else None
        )
        watchdog_registration = state["mirror_watchdog_registration"]
        staged_state["mirror_watchdog_registration"] = (
            MirrorWatchdogRegistration(
                ticket=watchdog_registration.ticket,
                timer=_NoOpTimer(),
            )
            if watchdog_registration is not None
            else None
        )
        return staged_state  # type: ignore[return-value]

    def _staged_runtime_state_after_message_locked(
        self,
        state: RuntimeStateDict,
        message: RuntimeStateMessage,
    ) -> RuntimeStateDict:
        staged_state = self._clone_runtime_state_for_staging(state)
        self._apply_runtime_state_message_locked(staged_state, message)
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

    @staticmethod
    def _normalize_owner_loss_disposition(value: str) -> OwnerLossDisposition:
        normalized = str(value or "").strip()
        if normalized == OWNER_LOSS_DISPOSITION_ABANDON:
            return OWNER_LOSS_DISPOSITION_ABANDON
        if normalized == OWNER_LOSS_DISPOSITION_TERMINAL:
            return OWNER_LOSS_DISPOSITION_TERMINAL
        raise ValueError("owner_loss_disposition 必须是 `abandon` 或 `terminal`。")

    def _binding_owner_receipt_locked(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> BindingOwnerRevisionReceipt:
        try:
            return self._binding_owner_authority.issue_owner(
                binding,
                expected_thread_id=thread_id,
                current_thread_id=self.binding_owner_thread_id_locked(binding),
            )
        except RuntimeError as exc:
            raise RuntimeError(
                "binding owner receipt 的 expected thread 已过期："
                f"{format_binding_id(binding)}"
            ) from exc

    def require_binding_owner_receipt_current(
        self,
        receipt: BindingOwnerRevisionReceipt,
    ) -> None:
        """Reject a forged, retired, replaced, or ABA binding owner receipt."""

        if not isinstance(receipt, BindingOwnerRevisionReceipt):
            raise RuntimeError("binding owner receipt 缺少 typed identity。")
        with self._lock:
            self._binding_owner_authority.require_owner_current(
                receipt,
                current_thread_id=self.binding_owner_thread_id_locked(
                    receipt.binding
                ),
            )

    def _advance_binding_owner_revision_locked(
        self,
        binding: ChatBindingKey,
        *,
        settled_command: BindingOwnerLossCommand | None = None,
    ) -> None:
        self._binding_owner_authority.advance_owner(
            binding,
            settled_command=settled_command,
        )
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            return
        handle = self._session_authority.current(
            binding,
            resident_state=state,
        )
        if handle is not None:
            self._session_authority.advance(
                handle,
                binding=binding,
                resident_state=state,
            )

    def _retire_binding_owner_generation_locked(
        self,
        binding: ChatBindingKey,
        *,
        settled_command: BindingOwnerLossCommand | None = None,
    ) -> None:
        state = self._runtime_state_by_binding.get(binding)
        if state is not None:
            handle = self._session_authority.current(
                binding,
                resident_state=state,
            )
            if handle is not None:
                self._session_authority.retire(
                    handle,
                    binding=binding,
                    resident_state=state,
                )
        self._binding_owner_authority.retire_owner(
            binding,
            settled_command=settled_command,
        )

    def _settle_binding_owner_loss_locked(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        *,
        reason: str,
        disposition: OwnerLossDisposition,
    ) -> tuple[BindingOwnerRevisionReceipt, BindingOwnerLossSettlementReceipt]:
        normalized_thread_id = str(thread_id or "").strip()
        owner = self._binding_owner_receipt_locked(binding, normalized_thread_id)
        command = BindingOwnerLossCommand(
            owner=owner,
            reason=str(reason or "").strip(),
            disposition=self._normalize_owner_loss_disposition(disposition),
        )
        self._binding_owner_authority.reserve_owner_loss(command)
        if not normalized_thread_id:
            settlement = BindingOwnerLossSettlementReceipt(
                command=command,
                _settler_nonce=0,
                _transaction_nonce=0,
            )
        else:
            if self._owner_loss_settler is None:
                raise RuntimeError(
                    "binding owner-loss settler 未装配；已按 fail-closed 拒绝 mutation。"
                )
            settlement = self._owner_loss_settler(command)
        if (
            not isinstance(settlement, BindingOwnerLossSettlementReceipt)
            or settlement.command is not command
        ):
            raise RuntimeError("owner-loss settler 未返回 exact command receipt。")
        self.require_binding_owner_receipt_current(owner)
        return owner, settlement

    def _require_detach_owner_loss_receipt_current_locked(
        self,
        receipt: BindingDetachOwnerLossReceipt,
        *,
        thread_id: str,
        bindings: tuple[ChatBindingKey, ...],
        disposition: OwnerLossDisposition,
    ) -> tuple[BindingOwnerRevisionReceipt, ...]:
        owners = self._binding_owner_authority.consume_detach(
            receipt,
            thread_id=thread_id,
            bindings=bindings,
            disposition=disposition,
        )
        # The cross-lock receipt is a one-shot commit capability.  A failed
        # commit may be retried only through a fresh preflight for the still-
        # current owner revision.
        for owner in owners:
            self.require_binding_owner_receipt_current(owner)
        return owners

    def discard_detach_owner_loss_receipt(
        self,
        receipt: BindingDetachOwnerLossReceipt,
    ) -> None:
        """Discard an exact preflight when the external unsubscribe failed."""

        if not isinstance(receipt, BindingDetachOwnerLossReceipt):
            raise RuntimeError("detach owner-loss preflight 缺少 typed receipt。")
        with self._lock:
            self._binding_owner_authority.discard_detach(receipt)

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
        handle: BindingRuntimeHandle,
        *,
        thread_id: str,
        thread_title: str,
        working_dir: str,
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
    ) -> BindingThreadBindResult:
        state = self._resident_state_for_handle_locked(handle)
        binding = handle.binding
        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
        normalized_thread_id = str(thread_id or "").strip()
        old_thread_id = str(state["current_thread_id"] or "").strip()
        owner_receipt = self._binding_owner_receipt_locked(binding, old_thread_id)
        settlement: BindingOwnerLossSettlementReceipt | None = None
        if old_thread_id == normalized_thread_id:
            self._binding_owner_authority.require_owner_loss_not_pending(
                owner_receipt
            )
        staged_state = self._clone_runtime_state_for_staging(state)
        if old_thread_id != normalized_thread_id:
            self._lifecycle.project_thread_replaced_locked(
                binding,
                staged_state,
            )
            self.require_binding_owner_receipt_current(owner_receipt)
        self._apply_runtime_state_message_locked(
            staged_state,
            ThreadStateChanged(
                current_thread_id=normalized_thread_id,
                current_thread_title=str(thread_title or "").strip(),
                feishu_runtime_state=FEISHU_RUNTIME_ATTACHED,
                working_dir=str(working_dir or staged_state["working_dir"]).strip(),
            ),
        )
        self._lifecycle.project_after_bind_locked(staged_state)
        self.require_binding_owner_receipt_current(owner_receipt)
        if old_thread_id != normalized_thread_id:
            owner_receipt, settlement = self._settle_binding_owner_loss_locked(
                binding,
                old_thread_id,
                reason="binding_replaced",
                disposition=normalized_disposition,
            )
        self.require_binding_owner_receipt_current(owner_receipt)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = ""
        if old_thread_id != normalized_thread_id:
            unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(binding, old_thread_id)
        self._apply_runtime_state_message_locked(
            state,
            ThreadStateChanged(
                current_thread_id=normalized_thread_id,
                current_thread_title=str(thread_title or "").strip(),
                feishu_runtime_state=FEISHU_RUNTIME_ATTACHED,
                working_dir=str(working_dir or state["working_dir"]).strip(),
            ),
        )
        timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()
        if old_thread_id != normalized_thread_id:
            timer_cancellations = (
                self._lifecycle.project_thread_replaced_locked(binding, state)
            )
        self._lifecycle.project_after_bind_locked(state)
        self._advance_binding_owner_revision_locked(
            binding,
            settled_command=(
                settlement.command if settlement is not None else None
            ),
        )
        if old_thread_id != normalized_thread_id:
            self.unsubscribe_thread_locked(binding, old_thread_id)
        self.subscribe_thread_locked(binding, normalized_thread_id)
        return BindingThreadBindResult(
            unsubscribe_thread_id=unsubscribe_thread_id,
            timer_cancellations=timer_cancellations,
        )

    def clear_thread_binding_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        working_dir_after_clear: str | None = None,
        require_no_inflight_turn: bool = False,
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
    ) -> BindingThreadClearResult:
        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
        if type(handle) is not BindingRuntimeHandle:
            raise RuntimeError("clear 缺少 exact binding runtime handle。")
        binding = handle.binding
        state = self._runtime_state_by_binding.get(binding)
        if state is None:
            raise RuntimeError("clear 使用的 binding runtime 已退役或被替换。")
        self._session_authority.require(handle, binding=binding, resident_state=state)
        if require_no_inflight_turn and self.binding_has_inflight_turn_locked(state):
            raise RuntimeError("binding clear 要求当前没有 inflight turn。")
        thread_id = str(state["current_thread_id"] or "").strip()
        next_working_dir = (
            str(working_dir_after_clear).strip()
            if working_dir_after_clear is not None
            else str(state["working_dir"] or "").strip()
        )
        if working_dir_after_clear is not None and not next_working_dir:
            raise ValueError("clear 后的 working_dir 不能为空。")
        clear_message = ThreadStateChanged(
            working_dir=next_working_dir,
            current_thread_id="",
            current_thread_title="",
            feishu_runtime_state="",
        )
        owner_receipt = self._binding_owner_receipt_locked(binding, thread_id)
        staged_state = self._clone_runtime_state_for_staging(state)
        self._lifecycle.project_thread_cleared_locked(binding, staged_state)
        self.require_binding_owner_receipt_current(owner_receipt)
        self._apply_runtime_state_message_locked(staged_state, clear_message)
        owner_receipt, settlement = self._settle_binding_owner_loss_locked(
            binding,
            thread_id,
            reason="binding_cleared",
            disposition=normalized_disposition,
        )
        self.require_binding_owner_receipt_current(owner_receipt)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_subscriber_locked(binding, thread_id)
        self._apply_runtime_state_message_locked(state, clear_message)
        timer_cancellations = self._lifecycle.project_thread_cleared_locked(
            binding,
            state,
        )
        self.unsubscribe_thread_locked(binding, thread_id)
        self._advance_binding_owner_revision_locked(
            binding,
            settled_command=settlement.command,
        )
        return BindingThreadClearResult(
            unsubscribe_thread_id=unsubscribe_thread_id,
            timer_cancellations=timer_cancellations,
        )

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
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
        owner_loss_receipt: BindingDetachOwnerLossReceipt | None = None,
    ) -> DetachThreadResult:
        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
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
            staged_state = self._clone_runtime_state_for_staging(state)
            self._lifecycle.project_detached_locked(binding, staged_state)
            self._apply_runtime_state_message_locked(staged_state, detach_message)
            plans.append(
                (
                    binding,
                    state,
                    self.stored_binding_from_runtime(binding, state),
                    self.stored_binding_from_runtime(binding, staged_state),
                )
            )

        owner_receipts: list[BindingOwnerRevisionReceipt] = []
        settlements: list[BindingOwnerLossSettlementReceipt] = []
        if owner_loss_receipt is not None:
            owner_receipts.extend(
                self._require_detach_owner_loss_receipt_current_locked(
                    owner_loss_receipt,
                    thread_id=normalized_thread_id,
                    bindings=tuple(binding for binding, *_rest in plans),
                    disposition=normalized_disposition,
                )
            )
            settlements.extend(owner_loss_receipt.settlements)
        else:
            # Like deactivation, detach is a batch: settle every owner before
            # the first binding is persisted or its local delivery path moves.
            for binding, _state, _original_stored_binding, _detached_stored_binding in plans:
                owner, settlement = self._settle_binding_owner_loss_locked(
                    binding,
                    normalized_thread_id,
                    reason="binding_detached",
                    disposition=normalized_disposition,
                )
                owner_receipts.append(owner)
                settlements.append(settlement)
        for owner in owner_receipts:
            self.require_binding_owner_receipt_current(owner)

        rollback_entries: list[tuple[ChatBindingKey, StoredChatBinding]] = []
        try:
            for binding, _state, original_stored_binding, detached_stored_binding in plans:
                rollback_entries.append((binding, original_stored_binding))
                self._persist_stored_binding_locked(binding, detached_stored_binding)
        except Exception:
            self._rollback_stored_binding_updates_locked(rollback_entries)
            raise

        detached_binding_ids: list[str] = []
        timer_cancellations: list[RuntimeTimerCancellationEffect] = []
        for (
            binding,
            state,
            _original_stored_binding,
            _detached_stored_binding,
        ), settlement in zip(plans, settlements):
            self._apply_runtime_state_message_locked(
                state,
                detach_message,
            )
            timer_cancellations.extend(
                self._lifecycle.project_detached_locked(binding, state)
            )
            self.unsubscribe_thread_locked(binding, normalized_thread_id)
            self._advance_binding_owner_revision_locked(
                binding,
                settled_command=settlement.command,
            )
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
            timer_cancellations=tuple(timer_cancellations),
        )

    def preflight_detach_thread_bindings_locked(
        self,
        thread_id: str,
        *,
        detach_availability: Callable[[str], tuple[bool, str]],
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
    ) -> BindingDetachOwnerLossReceipt | None:
        """Settle attached writers before an external thread unsubscribe.

        A caller which must ask the app-server to unsubscribe before committing
        local detach can use this phase to ensure a store/callback failure
        leaves the binding, lease, and delivery path untouched.  The returned
        single-use exact capability is accepted by
        :meth:`detach_thread_bindings_locked` to avoid a duplicate owner-loss
        callback after the external step succeeds.
        """

        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
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
        owners: list[BindingOwnerRevisionReceipt] = []
        settlements: list[BindingOwnerLossSettlementReceipt] = []
        for binding in attached_bindings:
            owner, settlement = self._settle_binding_owner_loss_locked(
                binding,
                normalized_thread_id,
                reason="binding_detached",
                disposition=normalized_disposition,
            )
            owners.append(owner)
            settlements.append(settlement)
        if not owners:
            return None
        for owner in owners:
            self.require_binding_owner_receipt_current(owner)
        return self._binding_owner_authority.issue_detach(
            thread_id=normalized_thread_id,
            owners=tuple(owners),
            settlements=tuple(settlements),
        )

    def detach_binding_locked(
        self,
        binding: ChatBindingKey,
        *,
        owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON,
    ) -> DetachBindingResult:
        normalized_disposition = self._normalize_owner_loss_disposition(owner_loss_disposition)
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
        staged_state = self._clone_runtime_state_for_staging(state)
        self._lifecycle.project_detached_locked(binding, staged_state)
        self._apply_runtime_state_message_locked(staged_state, detach_message)
        owner_receipt, settlement = self._settle_binding_owner_loss_locked(
            binding,
            thread_id,
            reason="binding_detached",
            disposition=normalized_disposition,
        )
        self.require_binding_owner_receipt_current(owner_receipt)
        self._persist_stored_binding_locked(
            binding,
            self.stored_binding_from_runtime(binding, staged_state),
        )
        unsubscribe_thread_id = self._unsubscribe_thread_id_if_last_attached_binding_locked(binding, thread_id)
        self._apply_runtime_state_message_locked(state, detach_message)
        timer_cancellations = self._lifecycle.project_detached_locked(
            binding,
            state,
        )
        self.unsubscribe_thread_locked(binding, thread_id)
        self._advance_binding_owner_revision_locked(
            binding,
            settled_command=settlement.command,
        )
        return DetachBindingResult(
            thread_id=thread_id,
            thread_title=str(state["current_thread_title"] or "").strip(),
            working_dir=str(state["working_dir"] or "").strip(),
            binding_id=format_binding_id(binding),
            changed=True,
            already_detached=False,
            unsubscribe_thread_id=unsubscribe_thread_id,
            timer_cancellations=timer_cancellations,
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
        bindings = sorted(
            set(self._runtime_state_by_binding) | set(stored_bindings),
            key=format_binding_id,
        )
        for binding in bindings:
            record = self._binding_record_snapshot_locked(
                binding,
                stored_binding=stored_bindings.get(binding),
            )
            if record is None:
                continue
            thread_id = record.thread_id
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
                    "thread_title": record.thread_title,
                    "working_dir": record.working_dir,
                    "feishu_runtime_state": (
                        record.feishu_runtime_state
                        or FEISHU_RUNTIME_NOT_APPLICABLE
                    ),
                    "running_turn": record.has_inflight_turn,
                    "approval_policy": record.approval_policy,
                    "permissions_profile_id": record.permissions_profile_id,
                    "model": record.model,
                    "reasoning_effort": record.reasoning_effort,
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
