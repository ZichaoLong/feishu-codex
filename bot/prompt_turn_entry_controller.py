from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeAlias

from bot.adapters.base import ThreadSnapshot, ThreadSummary, TurnInputItem
from bot.cards import build_markdown_card
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_card_publisher import build_execution_card_model
from bot.runtime_state import BACKEND_THREAD_STATUS_IDLE, ExecutionStateChanged, RuntimeStateDict
from bot.runtime_view import build_runtime_view
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from bot.reason_codes import ReasonedCheck

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = RuntimeStateDict


class _ThreadAccessPolicy(Protocol):
    def prompt_write_denial_text(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str: ...

    def all_mode_thread_exclusivity_violation(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str: ...

    def interaction_denied_text(self, lease: Any) -> str: ...


@dataclass(frozen=True, slots=True)
class PromptTurnEntryPorts:
    resolve_runtime_binding: Callable[[str, str, str], Any]
    get_runtime_state: Callable[[str, str, str], RuntimeState]
    get_runtime_view: Callable[[str, str, str], Any]
    bind_thread: Callable[..., None]
    clear_thread_binding: Callable[..., None]
    resume_snapshot_by_id: Callable[..., ThreadSnapshot]
    create_thread: Callable[..., ThreadSnapshot]
    effective_default_profile: Callable[[], str]
    persist_new_thread_profile_seed: Callable[[str, str], str]
    thread_profile_for_thread: Callable[[str], str]
    message_reply_in_thread: Callable[[str], bool]
    group_actor_open_id: Callable[[str], str]
    access_policy: _ThreadAccessPolicy
    released_runtime_reattach_check: Callable[[str], ReasonedCheck]
    acquire_interaction_lease_for_binding: Callable[[ChatBindingKey, str], Any]
    release_interaction_lease_for_binding: Callable[[ChatBindingKey, str], bool]
    sync_stored_binding_locked: Callable[[ChatBindingKey, RuntimeState], None]
    clear_plan_state: Callable[[RuntimeState], None]
    apply_runtime_state_message_locked: Callable[[RuntimeState, Any], None]
    claim_reserved_execution_card: Callable[[str], str]
    patch_message: Callable[[str, str], bool]
    card_publisher_factory: Callable[[], Any]
    send_execution_card: Callable[..., str | None]
    flush_execution_card: Callable[..., None]
    retire_execution_anchor: Callable[[str, str], None]
    schedule_mirror_watchdog: Callable[[str, str], None]
    reconcile_execution_snapshot: Callable[..., bool]
    refresh_terminal_execution_card_from_state: Callable[[str, str], bool]
    finalize_execution_card_from_state: Callable[[str, str], bool]
    mark_runtime_degraded: Callable[..., None]
    runtime_recovery_reason: Callable[[Exception], str]
    is_turn_thread_not_found_error: Callable[[Exception], bool]
    is_thread_not_found_error: Callable[[Exception], bool]
    is_transport_disconnect: Callable[[Exception], bool]
    is_request_timeout_error: Callable[[Exception], bool]
    start_turn: Callable[..., dict[str, Any]]
    interrupt_running_turn: Callable[..., None]
    reply_text: Callable[..., None]
    mirror_watchdog_seconds: Callable[[], float]
    card_reply_limit: Callable[[], int]
    card_log_limit: Callable[[], int]


class PromptTurnEntryController:
    def __init__(
        self,
        *,
        lock,
        turn_execution: TurnExecutionCoordinator,
        ports: PromptTurnEntryPorts,
    ) -> None:
        self._lock = lock
        self._turn_execution = turn_execution
        self._resolve_runtime_binding = ports.resolve_runtime_binding
        self._get_runtime_state = ports.get_runtime_state
        self._get_runtime_view = ports.get_runtime_view
        self._bind_thread = ports.bind_thread
        self._clear_thread_binding = ports.clear_thread_binding
        self._resume_snapshot_by_id = ports.resume_snapshot_by_id
        self._create_thread = ports.create_thread
        self._effective_default_profile = ports.effective_default_profile
        self._persist_new_thread_profile_seed = ports.persist_new_thread_profile_seed
        self._thread_profile_for_thread = ports.thread_profile_for_thread
        self._message_reply_in_thread = ports.message_reply_in_thread
        self._group_actor_open_id = ports.group_actor_open_id
        self._access_policy = ports.access_policy
        self._released_runtime_reattach_check = ports.released_runtime_reattach_check
        self._acquire_interaction_lease_for_binding = ports.acquire_interaction_lease_for_binding
        self._release_interaction_lease_for_binding = ports.release_interaction_lease_for_binding
        self._sync_stored_binding_locked = ports.sync_stored_binding_locked
        self._clear_plan_state = ports.clear_plan_state
        self._apply_runtime_state_message_locked = ports.apply_runtime_state_message_locked
        self._claim_reserved_execution_card = ports.claim_reserved_execution_card
        self._patch_message = ports.patch_message
        self._card_publisher_factory = ports.card_publisher_factory
        self._send_execution_card = ports.send_execution_card
        self._flush_execution_card = ports.flush_execution_card
        self._retire_execution_anchor = ports.retire_execution_anchor
        self._schedule_mirror_watchdog = ports.schedule_mirror_watchdog
        self._reconcile_execution_snapshot = ports.reconcile_execution_snapshot
        self._refresh_terminal_execution_card_from_state = ports.refresh_terminal_execution_card_from_state
        self._finalize_execution_card_from_state = ports.finalize_execution_card_from_state
        self._mark_runtime_degraded = ports.mark_runtime_degraded
        self._runtime_recovery_reason = ports.runtime_recovery_reason
        self._is_turn_thread_not_found_error = ports.is_turn_thread_not_found_error
        self._is_thread_not_found_error = ports.is_thread_not_found_error
        self._is_transport_disconnect = ports.is_transport_disconnect
        self._is_request_timeout_error = ports.is_request_timeout_error
        self._start_turn = ports.start_turn
        self._interrupt_running_turn = ports.interrupt_running_turn
        self._reply_text = ports.reply_text
        self._mirror_watchdog_seconds = ports.mirror_watchdog_seconds
        self._card_reply_limit = ports.card_reply_limit
        self._card_log_limit = ports.card_log_limit

    @staticmethod
    def extract_turn_id_from_start_response(response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        turn = response.get("turn")
        if isinstance(turn, dict):
            turn_id = str(turn.get("id", "") or "").strip()
            if turn_id:
                return turn_id
        return str(response.get("turnId", "") or "").strip()

    def preflight_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        if self.handle_running_prompt(sender_id, chat_id, "", message_id=message_id):
            return False
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        with self._lock:
            runtime = build_runtime_view(resolved.state)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            return True
        denial_text = self._access_policy.prompt_write_denial_text(
            resolved.binding,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if not denial_text:
            return True
        self._reply_text(
            chat_id,
            denial_text,
            message_id=message_id,
            reply_in_thread=self._message_reply_in_thread(message_id),
        )
        return False

    def render_start_failure(self, *, chat_id: str, message_id: str, text: str) -> None:
        reserved_card_id = self._claim_reserved_execution_card(message_id)
        if reserved_card_id:
            card = build_markdown_card("Codex 启动失败", text, template="red")
            if self._patch_message(reserved_card_id, json.dumps(card, ensure_ascii=False)):
                return
        self._reply_text(
            chat_id,
            text,
            message_id=message_id,
            reply_in_thread=self._message_reply_in_thread(message_id),
        )

    def ensure_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> tuple[str, str]:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.current_thread_id:
            return runtime.current_thread_id, ""
        seed_profile = self._effective_default_profile().strip()
        snapshot = self._create_thread(
            cwd=runtime.working_dir,
            profile=seed_profile or None,
            approval_policy=runtime.approval_policy or None,
            sandbox=runtime.sandbox or None,
        )
        seed_warning = self._persist_new_thread_profile_seed(snapshot.summary.thread_id, seed_profile)
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        return snapshot.summary.thread_id, seed_warning

    def resume_bound_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            raise RuntimeError("当前没有可恢复的线程绑定")
        summary = ThreadSummary(
            thread_id=thread_id,
            cwd=runtime.working_dir,
            name=runtime.current_thread_title,
            preview=runtime.current_thread_title,
            created_at=0,
            updated_at=0,
            source="appServer",
            status=BACKEND_THREAD_STATUS_IDLE,
        )
        snapshot = self._resume_snapshot_by_id(
            thread_id,
            original_arg=thread_id,
            summary=summary,
        )
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        return snapshot.summary.thread_id

    def ensure_binding_runtime_attached(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            raise RuntimeError("当前没有可恢复的线程绑定")
        if runtime.binding.feishu_runtime_attached:
            return thread_id
        return self.resume_bound_thread(sender_id, chat_id, message_id=message_id)

    def handle_running_prompt(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> bool:
        del text
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        if not runtime.running:
            return False
        thread_id = runtime.current_thread_id.strip()
        turn_id = runtime.execution.current_turn_id.strip()
        last_runtime_event_at = runtime.execution.last_runtime_event_at
        if thread_id and last_runtime_event_at and (
            time.monotonic() - last_runtime_event_at >= self._mirror_watchdog_seconds()
        ):
            self._reconcile_execution_snapshot(
                sender_id,
                chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if not self._get_runtime_view(sender_id, chat_id, message_id).running:
                return False
        self._reply_text(chat_id, "当前线程仍在执行，请等待结束或先执行 `/cancel`。", message_id=message_id)
        return True

    def handle_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        input_items: list[TurnInputItem] | tuple[TurnInputItem, ...] | None = None,
    ) -> bool:
        if self.handle_running_prompt(sender_id, chat_id, text, message_id=message_id):
            return False
        return self.start_prompt_turn(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            input_items=input_items,
        )

    def start_prompt_turn(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[TurnInputItem] | tuple[TurnInputItem, ...] | None = None,
    ) -> bool:
        effective_input_items = list(input_items) if input_items is not None else [{"type": "text", "text": text}]
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        chat_binding_key = resolved.binding
        with self._lock:
            runtime = build_runtime_view(state)
        released_thread_id = runtime.current_thread_id.strip()
        preattached_interaction_lease = None
        if released_thread_id and not runtime.binding.feishu_runtime_attached:
            denial_text = self._access_policy.prompt_write_denial_text(
                chat_binding_key,
                chat_id,
                released_thread_id,
                message_id=message_id,
            )
            if denial_text:
                self._reply_text(
                    chat_id,
                    denial_text,
                    message_id=message_id,
                    reply_in_thread=self._message_reply_in_thread(message_id),
                )
                return False
            reattach_check = self._released_runtime_reattach_check(released_thread_id)
            if not reattach_check.allowed:
                self._reply_text(
                    chat_id,
                    reattach_check.reason_text,
                    message_id=message_id,
                    reply_in_thread=self._message_reply_in_thread(message_id),
                )
                return False
            with self._lock:
                preattached_interaction_lease = self._acquire_interaction_lease_for_binding(
                    chat_binding_key,
                    released_thread_id,
                )
            if not preattached_interaction_lease.granted:
                self._reply_text(
                    chat_id,
                    self._access_policy.interaction_denied_text(preattached_interaction_lease.lease),
                    message_id=message_id,
                    reply_in_thread=self._message_reply_in_thread(message_id),
                )
                return False
        seed_warning = ""
        try:
            thread_id, seed_warning = self.ensure_thread(sender_id, chat_id, message_id=message_id)
            thread_id = self.ensure_binding_runtime_attached(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            if preattached_interaction_lease is not None and preattached_interaction_lease.acquired:
                self._release_interaction_lease_for_binding(chat_binding_key, released_thread_id)
            logger.exception("准备线程失败")
            self.render_start_failure(
                chat_id=chat_id,
                message_id=message_id,
                text=f"准备线程失败：{exc}",
            )
            return False

        all_mode_exclusivity_violation = self._access_policy.all_mode_thread_exclusivity_violation(
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if all_mode_exclusivity_violation:
            if preattached_interaction_lease is not None and preattached_interaction_lease.acquired:
                self._release_interaction_lease_for_binding(chat_binding_key, released_thread_id)
            self._reply_text(
                chat_id,
                all_mode_exclusivity_violation,
                message_id=message_id,
                reply_in_thread=self._message_reply_in_thread(message_id),
            )
            return False
        interaction_lease = preattached_interaction_lease
        with self._lock:
            if interaction_lease is None:
                interaction_lease = self._acquire_interaction_lease_for_binding(chat_binding_key, thread_id)
            if interaction_lease.granted:
                self._sync_stored_binding_locked(chat_binding_key, state)
        if not interaction_lease.granted:
            self._reply_text(
                chat_id,
                self._access_policy.interaction_denied_text(interaction_lease.lease),
                message_id=message_id,
                reply_in_thread=self._message_reply_in_thread(message_id),
            )
            return False

        prompt_reply_in_thread = self._message_reply_in_thread(message_id)
        with self._lock:
            started_at = time.monotonic()
            self._turn_execution.prime_prompt_turn_locked(
                state,
                prompt_message_id=str(message_id or "").strip(),
                prompt_reply_in_thread=prompt_reply_in_thread,
                actor_open_id=str(actor_open_id or "").strip() or self._group_actor_open_id(message_id),
                started_at=started_at,
            )
            self._clear_plan_state(state)

        card_id = ""
        if message_id:
            card_id = self._claim_reserved_execution_card(message_id)
            if card_id:
                self._card_publisher_factory().patch_execution_card(
                    card_id,
                    build_execution_card_model(
                        ExecutionTranscript(),
                        running=True,
                        elapsed=0,
                        cancelled=False,
                        log_limit=self._card_log_limit(),
                        reply_limit=self._card_reply_limit(),
                    ),
                )
        if not card_id:
            card_id = self._send_execution_card(
                chat_id,
                message_id,
                reply_in_thread=prompt_reply_in_thread,
            ) or ""
        if not card_id:
            self._retire_execution_anchor(sender_id, chat_id)
            self._reply_text(
                chat_id,
                "执行卡片发送失败，未启动 Codex；请稍后重试。",
                message_id=message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
            return False
        with self._lock:
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(current_message_id=card_id),
            )

        def _start_turn_once(bound_thread_id: str) -> dict[str, Any]:
            thread_profile = self._thread_profile_for_thread(bound_thread_id).strip()
            return self._start_turn(
                thread_id=bound_thread_id,
                input_items=effective_input_items,
                cwd=state["working_dir"],
                model=state["model"] or None,
                profile=thread_profile or None,
                approval_policy=state["approval_policy"] or None,
                sandbox=state["sandbox"] or None,
                reasoning_effort=state["reasoning_effort"] or None,
                collaboration_mode=state["collaboration_mode"] or None,
            )

        try:
            start_response = _start_turn_once(thread_id)
        except Exception as exc:
            if self._is_turn_thread_not_found_error(exc) and str(state["current_thread_id"] or "").strip():
                logger.info("检测到线程未加载，自动恢复后重试: thread=%s", thread_id[:12])
                try:
                    thread_id = self.resume_bound_thread(sender_id, chat_id, message_id=message_id)
                    start_response = _start_turn_once(thread_id)
                except Exception as retry_exc:
                    logger.exception("自动恢复线程后重试 turn 失败")
                    self._handle_start_failure(
                        sender_id,
                        chat_id,
                        state=state,
                        error_text=f"启动失败：{retry_exc}",
                        card_id=card_id,
                        message_id=message_id,
                        prompt_reply_in_thread=prompt_reply_in_thread,
                        clear_thread_binding=self._is_thread_not_found_error(retry_exc),
                    )
                    return False
            else:
                logger.exception("启动 turn 失败")
                self._handle_start_failure(
                    sender_id,
                    chat_id,
                    state=state,
                    error_text=f"启动失败：{exc}",
                    card_id=card_id,
                    message_id=message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    clear_thread_binding=False,
                )
                return False

        turn_id = self.extract_turn_id_from_start_response(start_response)
        with self._lock:
            should_interrupt_started_turn = self._turn_execution.record_started_turn_id_locked(
                state,
                turn_id=turn_id,
            )
        if should_interrupt_started_turn:
            try:
                self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
            except Exception:
                logger.exception("延迟取消 turn 失败")
            else:
                with self._lock:
                    self._apply_runtime_state_message_locked(
                        state,
                        ExecutionStateChanged(pending_cancel=False),
                    )
        self._schedule_mirror_watchdog(sender_id, chat_id)
        if seed_warning:
            self._reply_text(
                chat_id,
                seed_warning,
                message_id=message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
        return True

    def cancel_current_turn(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> tuple[bool, str]:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        with self._lock:
            runtime = build_runtime_view(state)
        thread_id = runtime.current_thread_id
        turn_id = runtime.execution.current_turn_id
        if not runtime.running or not thread_id:
            if runtime.execution.current_message_id or runtime.execution.last_execution_message_id:
                self._refresh_terminal_execution_card_from_state(sender_id, chat_id)
                return True, "当前执行已结束，已刷新卡片状态。"
            return False, "当前没有正在执行的 turn。"
        denial_text = self._access_policy.prompt_write_denial_text(
            resolved.binding,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if denial_text:
            return False, denial_text
        if not turn_id:
            with self._lock:
                self._turn_execution.request_cancel_without_turn_id_locked(state)
            return True, "已请求停止当前执行。"
        try:
            self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            if self._is_turn_thread_not_found_error(exc) or self._is_thread_not_found_error(exc):
                self._finalize_execution_card_from_state(sender_id, chat_id)
                return True, "当前执行已结束，已刷新卡片状态。"
            if self._is_transport_disconnect(exc) or self._is_request_timeout_error(exc):
                self._mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return True, "取消请求已发送，但当前后端状态暂不可确认；稍后会自动对账。"
            logger.exception("取消 turn 失败")
            return False, f"取消失败：{exc}"
        with self._lock:
            self._turn_execution.confirm_cancel_requested_locked(state)
        return True, "已请求停止当前执行。"

    def _handle_start_failure(
        self,
        sender_id: str,
        chat_id: str,
        *,
        state: RuntimeState,
        error_text: str,
        card_id: str,
        message_id: str,
        prompt_reply_in_thread: bool,
        clear_thread_binding: bool,
    ) -> None:
        with self._lock:
            self._turn_execution.record_start_failure_locked(
                state,
                error_text=error_text,
            )
        if clear_thread_binding:
            self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        self._flush_execution_card(sender_id, chat_id, immediate=True)
        self._retire_execution_anchor(sender_id, chat_id)
        if not card_id:
            self._reply_text(
                chat_id,
                error_text,
                message_id=message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
