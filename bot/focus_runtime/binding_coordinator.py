"""Cross-owner coordination for Feishu binding runtime operations."""

from __future__ import annotations

import logging
from typing import Any, Callable, ContextManager, TypeAlias

from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.feishu_binding_transition import (
    ClearFeishuThreadCommand,
    FeishuBindingTransitionOwner,
)
from bot.feishu_execution_queue import FeishuBindingExecutionSnapshot
from bot.feishu_execution_queue_service import FeishuQueueIngressSnapshot
from bot.focus_runtime.service_authority import ServiceRuntimeAuthority
from bot.focus_runtime.thread_targets import CodexThreadTargetService
from bot.runtime_admin.binding_clear import RuntimeBindingBatchDeactivationOwner
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    FEISHU_RUNTIME_ATTACHED,
    UNSET,
)
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_runtime_authority import ThreadRuntimeAuthority


logger = logging.getLogger("bot.focus_runtime")

ChatBindingKey: TypeAlias = tuple[str, str]


class BindingRuntimeCoordinator:
    """Coordinate binding transactions without mirroring mutable owner facts."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: BindingRuntimeManager,
        binding_batch_deactivation: RuntimeBindingBatchDeactivationOwner,
        interaction_lease_store: InteractionLeaseStore,
        thread_runtime_authority: ThreadRuntimeAuthority,
        service_runtime_authority: ServiceRuntimeAuthority,
        runtime_interest_retained: Callable[[str], bool],
        codex_thread_targets: CodexThreadTargetService,
        feishu_binding_transitions: FeishuBindingTransitionOwner,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._binding_batch_deactivation = binding_batch_deactivation
        self._interaction_lease_store = interaction_lease_store
        self._thread_runtime_authority = thread_runtime_authority
        self._service_runtime_authority = service_runtime_authority
        self._runtime_interest_retained = runtime_interest_retained
        self._codex_thread_targets = codex_thread_targets
        self._feishu_binding_transitions = feishu_binding_transitions

    def activate_binding_if_needed(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> None:
        session = self._binding_runtime.resolve_session(sender_id, chat_id, message_id)
        with self._lock:
            self._binding_runtime.activate_session_locked(session.handle)

    def is_sender_active_on_runtime(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> bool:
        return self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        ).active

    def feishu_binding_execution_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> FeishuBindingExecutionSnapshot | None:
        snapshot = self._binding_runtime.resident_session_snapshot_locked(binding)
        if snapshot is None:
            return None
        return FeishuBindingExecutionSnapshot(
            binding=snapshot.binding,
            root_thread_id=snapshot.current_thread_id,
            active=snapshot.active,
            attached=(
                snapshot.thread.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED
            ),
            has_inflight_execution=bool(
                snapshot.execution.running
                or snapshot.execution.awaiting_local_turn_started
                or snapshot.execution.current_turn_id
            ),
            current_turn_id=snapshot.execution.current_turn_id,
        )

    def feishu_queue_ingress_snapshot(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> FeishuQueueIngressSnapshot:
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        return FeishuQueueIngressSnapshot(
            binding=runtime.binding,
            current_root_thread_id=runtime.current_thread_id,
            current_turn_id=runtime.execution.current_turn_id,
            has_execution_anchor=runtime.execution.has_execution_anchor,
        )

    def deactivate_sender_impl(
        self,
        sender_id: str,
        chat_id: str = "",
        *,
        message_id: str = "",
    ) -> None:
        key = self.chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            planned_thread_id = self._binding_runtime.binding_owner_thread_id_locked(
                key
            )
        with self._lock:
            if (
                self._binding_runtime.binding_owner_thread_id_locked(key)
                != planned_thread_id
            ):
                raise RuntimeError(
                    "binding 在 sender deactivation recovery 核验期间发生变化；请重试。"
                )
            receipt = self._binding_batch_deactivation.deactivate_locked((key,))
        cancel_runtime_timer_effects(receipt.timer_cancellations)
        unsubscribe_thread_id = next(
            (
                removal.unsubscribe_thread_id
                for removal in receipt.confirmed_removals
                if removal.unsubscribe_thread_id
            ),
            "",
        )
        if unsubscribe_thread_id:
            self.finalize_deactivated_feishu_binding_thread_runtime(
                unsubscribe_thread_id,
                cleanup_reason="sender_deactivated",
            )

    def cancel_frontend_runtime_timers(self) -> None:
        """Close Feishu-owned timers before stopping external ingress."""

        with self._lock:
            effects = self._binding_runtime.prepare_all_timer_cancellations_locked()
        cancel_runtime_timer_effects(effects)

    def release_main_turn_for_binding(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_thread_id or not normalized_turn_id:
            return False
        lease = self._interaction_lease_store.load(normalized_thread_id)
        if (
            lease is None
            or not lease.holder.same_holder(
                self._binding_runtime.feishu_interaction_holder(binding)
            )
        ):
            return False
        return self._interaction_lease_store.release_turn(
            normalized_thread_id,
            normalized_turn_id,
        )

    def interactive_binding_for_thread(
        self,
        thread_id: str,
    ) -> tuple[ChatBindingKey | None, bool]:
        with self._lock:
            lease = self._binding_runtime.current_interaction_lease_locked(thread_id)
            if lease is None:
                for binding in self._binding_runtime.thread_subscribers(thread_id):
                    session = (
                        self._binding_runtime.resident_session_snapshot_locked(
                            binding
                        )
                    )
                    if (
                        session is not None
                        and session.current_thread_id.strip() == thread_id
                        and session.execution.current_execution_kind.strip()
                        == ACTIVE_OBSERVER_EXECUTION_KIND
                    ):
                        return None, True
                return None, False
            binding, handled_elsewhere = (
                self._binding_runtime.interactive_binding_for_thread_locked(
                    thread_id,
                    adopt_sole_subscriber=False,
                )
            )
            if handled_elsewhere:
                return None, True
            if (
                binding is None
                or not lease.holder.same_holder(
                    self._binding_runtime.feishu_interaction_holder(binding)
                )
            ):
                return None, False
            session = self._binding_runtime.resident_session_snapshot_locked(
                binding
            )
            if (
                session is not None
                and session.execution.current_execution_kind.strip()
                == ACTIVE_OBSERVER_EXECUTION_KIND
            ):
                return None, True
            return binding, False

    def thread_subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        with self._lock:
            return self._binding_runtime.thread_subscribers(thread_id)

    def unsubscribe_thread_unless_web_runtime_requires_interest(
        self,
        thread_id: str,
    ) -> None:
        if self._runtime_interest_retained(thread_id):
            return
        self._thread_runtime_authority.unsubscribe_thread(thread_id)

    def release_service_thread_runtime_lease_unless_web_runtime_requires_interest(
        self,
        thread_id: str,
    ) -> None:
        if self._runtime_interest_retained(thread_id):
            return
        self._service_runtime_authority.release_service_thread_runtime_lease(thread_id)

    def finalize_deactivated_feishu_binding_thread_runtime(
        self,
        thread_id: str,
        *,
        cleanup_reason: str,
    ) -> None:
        """Retire one last-subscriber Feishu runtime without child control.

        A normal binding deactivation is allowed to discard its *local*
        bookmark, lease, and subscription registry entry even when that
        legacy binding turns out to point at a ThreadSpawn child.  It must not
        use that cleanup as an upstream ``thread/unsubscribe`` operation,
        however: children are parent-owned and only a direct root thread may be
        directly managed by a frontend.

        The binding manager has already completed the local transaction when
        this helper runs.  Perform the authoritative point read only before
        the possible upstream unsubscribe.  A child, malformed response, or
        unreadable target is deliberately a no-unsubscribe *and no-release*
        result rather than a rollback of safe local cleanup.
        """

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return

        if self._runtime_interest_retained(normalized_thread_id):
            return
        try:
            self._codex_thread_targets.read_direct_thread_summary_authoritatively(
                normalized_thread_id,
                original_arg=normalized_thread_id,
                operation="取消飞书 thread 订阅",
            )
        except Exception as exc:
            logger.warning(
                "Retaining runtime after Feishu binding cleanup; target is not "
                "an authoritatively verified direct root: reason=%s thread=%s error=%s",
                str(cleanup_reason or "-")[:80],
                normalized_thread_id[:12],
                exc,
            )
            return

        self.unsubscribe_thread_unless_web_runtime_requires_interest(
            normalized_thread_id
        )
        self.release_service_thread_runtime_lease_unless_web_runtime_requires_interest(
            normalized_thread_id
        )

    def resident_session(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            return self._binding_runtime.resident_session_snapshot_locked(binding)

    def update_runtime_settings(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        approval_policy: Any = UNSET,
        permissions_profile_id: Any = UNSET,
        model: Any = UNSET,
        reasoning_effort: Any = UNSET,
    ) -> None:
        session = self._binding_runtime.resolve_session(sender_id, chat_id, message_id)
        with self._lock:
            self._binding_runtime.update_runtime_settings_locked(
                session.handle,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )

    def rename_bound_thread_title(
        self,
        sender_id: str,
        chat_id: str,
        title: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> bool:
        normalized_title = str(title or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        session = self._binding_runtime.resolve_session(sender_id, chat_id, message_id)
        expected_thread_id = normalized_thread_id or session.current_thread_id
        if not expected_thread_id:
            return False
        with self._lock:
            updated = self._binding_runtime.update_thread_metadata_locked(
                session.handle,
                expected_thread_id=expected_thread_id,
                current_thread_title=normalized_title,
            )
        return updated is not None

    def chat_binding_key(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> ChatBindingKey:
        with self._lock:
            existing = self._binding_runtime.existing_chat_binding_key_locked(
                sender_id,
                chat_id,
            )
            if existing is not None:
                return existing
        return self._binding_runtime.fresh_chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )

    def clear_thread_binding(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        session: BindingSessionSnapshot | None = None,
        working_dir_after_clear: str | None = None,
        require_no_inflight_turn: bool = False,
    ) -> bool:
        session = session or self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        committed = self._feishu_binding_transitions.clear_thread(
            ClearFeishuThreadCommand(
                session=session,
                working_dir_after_clear=working_dir_after_clear,
                require_no_inflight_turn=require_no_inflight_turn,
            )
        )
        cleanup_incomplete = committed.queue_cleanup_failed
        unsubscribe_thread_id = committed.unsubscribe_thread_id
        if unsubscribe_thread_id:
            try:
                self.unsubscribe_thread_unless_web_runtime_requires_interest(
                    unsubscribe_thread_id
                )
                self.release_service_thread_runtime_lease_unless_web_runtime_requires_interest(
                    unsubscribe_thread_id
                )
            except Exception:
                cleanup_incomplete = True
                logger.exception("binding commit 后清理旧 thread runtime 失败")
        return cleanup_incomplete
