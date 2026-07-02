from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Any, Callable, TypeAlias

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.binding_identity import format_binding_id, parse_binding_id
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.cards import (
    CommandResult,
    build_backend_reset_card,
    build_markdown_card,
    format_goal_summary_markdown,
    make_card_response,
)
from bot.constants import display_path
from bot.reason_codes import (
    BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND,
    BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN,
    BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST,
    BACKEND_RESET_FORCE_ONLY_BY_ACTIVE_LOADED_THREAD,
    BACKEND_RESET_FORCE_ONLY_BY_PENDING_REQUEST,
    BACKEND_RESET_FORCE_ONLY_BY_RUNNING_BINDING,
    BACKEND_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED,
    BACKEND_RESET_UNSUPPORTED_REMOTE,
    PROMPT_DENIED_BINDING_NOT_FOUND,
    PROMPT_DENIED_BY_RUNNING_TURN,
    DETACH_BLOCKED_BY_INFLIGHT_TURN,
    DETACH_BLOCKED_BY_PENDING_REQUEST,
    DETACH_NOT_APPLICABLE_NO_BINDING,
    DETACH_NOT_APPLICABLE_ALREADY_DETACHED,
    DETACH_NOT_APPLICABLE_NO_THREAD,
    ReasonedCheck,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_LOOKUP_ERROR,
    BACKEND_THREAD_LOOKUP_MISSING,
    BACKEND_THREAD_STATUS_NOT_LOADED,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    LOADED_BACKEND_THREAD_STATUSES,
    RuntimeStateDict,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.thread_image_delivery import ThreadImageDeliveryController

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = RuntimeStateDict

BACKEND_RESET_STATUS_AVAILABLE = "available"
BACKEND_RESET_STATUS_FORCE_ONLY = "force-only"
BACKEND_RESET_STATUS_BLOCKED = "blocked"
@dataclass(frozen=True, slots=True)
class BackendResetPreview:
    status: str
    reason_code: str
    reason_text: str
    diagnostics: tuple[str, ...] = ()
    pending_request_count: int = 0
    running_binding_ids: tuple[str, ...] = ()
    active_loaded_thread_ids: tuple[str, ...] = ()
    loaded_thread_ids: tuple[str, ...] = ()
    runtime_verification_failed: bool = False
    blocking_holder_labels: tuple[str, ...] = ()
    attached_binding_ids: tuple[str, ...] = ()
    loaded_thread_preview: tuple[str, ...] = ()
    active_loaded_thread_preview: tuple[str, ...] = ()
    blocking_active_turn_count: int = 0
    blocking_pending_request_count: int = 0
    collateral_loaded_thread_count: int = 0
    collateral_active_loaded_thread_count: int = 0


class RuntimeAdminController:
    def __init__(
        self,
        *,
        lock,
        binding_runtime: BindingRuntimeManager,
        interaction_requests,
        clear_all_stored_bindings: Callable[[], None],
        deactivate_binding_locked: Callable[[ChatBindingKey], str],
        read_thread: Callable[[str], Any],
        read_thread_for_stale_cleanup: Callable[[str], Any],
        list_loaded_thread_ids: Callable[[], list[str]],
        current_app_server_url: Callable[[], str],
        app_server_mode: Callable[[], str],
        unsubscribe_thread: Callable[[str], None],
        archive_thread: Callable[[str], None],
        release_service_thread_runtime_lease: Callable[[str], None],
        service_control_endpoint: Callable[[], str],
        instance_name: Callable[[], str],
        load_thread_runtime_lease: Callable[[str], ThreadRuntimeLease | None],
        list_pending_interaction_requests: Callable[[], list[dict[str, Any]]],
        reset_current_instance_backend: Callable[[bool], dict[str, Any]],
        attach_binding: Callable[[ChatBindingKey, str], ThreadSummary],
        permissions_summary: Callable[..., str],
        thread_image_delivery: ThreadImageDeliveryController,
        get_thread_goal: Callable[[str], ThreadGoalSummary | None],
        set_thread_goal: Callable[..., ThreadGoalSummary],
        clear_thread_goal: Callable[[str], bool],
        submit_to_runtime: Callable[..., None],
        reply_text: Callable[..., None],
        reply_card: Callable[..., None],
        submit_prompt_for_control: Callable[..., dict[str, Any]],
        prompt_write_denial_check: Callable[[ChatBindingKey, str, str, str], ReasonedCheck],
        detached_runtime_attach_check: Callable[[str], ReasonedCheck],
        resolve_thread_target_for_control_params: Callable[[dict[str, Any]], ThreadSummary],
        resolve_binding_chat_display_name: Callable[..., str],
        cancel_patch_timer_locked: Callable[[RuntimeState], None],
        cancel_mirror_watchdog_locked: Callable[[RuntimeState], None],
        is_thread_not_found_error: Callable[[Exception], bool],
        is_thread_not_loaded_error: Callable[[Exception], bool],
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._interaction_requests = interaction_requests
        self._clear_all_stored_bindings = clear_all_stored_bindings
        self._deactivate_binding_locked = deactivate_binding_locked
        self._read_thread = read_thread
        self._read_thread_for_stale_cleanup = read_thread_for_stale_cleanup
        self._list_loaded_thread_ids = list_loaded_thread_ids
        self._current_app_server_url = current_app_server_url
        self._app_server_mode = app_server_mode
        self._unsubscribe_thread = unsubscribe_thread
        self._archive_thread = archive_thread
        self._release_service_thread_runtime_lease = release_service_thread_runtime_lease
        self._service_control_endpoint = service_control_endpoint
        self._instance_name = instance_name
        self._load_thread_runtime_lease = load_thread_runtime_lease
        self._list_pending_interaction_requests = list_pending_interaction_requests
        self._reset_current_instance_backend = reset_current_instance_backend
        self._attach_binding = attach_binding
        self._permissions_summary = permissions_summary
        self._thread_image_delivery = thread_image_delivery
        self._get_thread_goal = get_thread_goal
        self._set_thread_goal = set_thread_goal
        self._clear_thread_goal = clear_thread_goal
        self._submit_to_runtime = submit_to_runtime
        self._reply_text = reply_text
        self._reply_card = reply_card
        self._submit_prompt_for_control = submit_prompt_for_control
        self._prompt_write_denial_check = prompt_write_denial_check
        self._detached_runtime_attach_check = detached_runtime_attach_check
        self._resolve_thread_target_for_control_params = resolve_thread_target_for_control_params
        self._resolve_binding_chat_display_name = resolve_binding_chat_display_name
        self._cancel_patch_timer_locked = cancel_patch_timer_locked
        self._cancel_mirror_watchdog_locked = cancel_mirror_watchdog_locked
        self._is_thread_not_found_error = is_thread_not_found_error
        self._is_thread_not_loaded_error = is_thread_not_loaded_error

    def _render_permissions_summary(self, snapshot: dict[str, Any]) -> str:
        try:
            return self._permissions_summary(snapshot["permissions_profile_id"])
        except TypeError:
            return self._permissions_summary(
                snapshot.get("approval_policy", ""),
                snapshot.get("permissions_profile_id", ""),
            )

    @staticmethod
    def binding_has_inflight_turn_locked(state: RuntimeState) -> bool:
        return BindingRuntimeManager.binding_has_inflight_turn_locked(state)

    def binding_inventory_locked(self) -> list[dict[str, Any]]:
        return self._binding_runtime.binding_inventory_locked()

    def binding_list_snapshot(self, *, refresh_names: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            bindings = [dict(item) for item in self.binding_inventory_locked()]
        return self._enrich_binding_list_snapshot(bindings, refresh_names=refresh_names)

    def binding_list_response(self, *, refresh_names: bool = False) -> dict[str, Any]:
        bindings = self.binding_list_snapshot(refresh_names=refresh_names)
        cache_miss_count = sum(
            1
            for item in bindings
            if not str(item.get("chat_display_name", "") or "").strip()
            and (
                str(item.get("chat_id", "") or "").strip()
                or str(item.get("sender_id", "") or "").strip()
            )
        )
        return {
            "bindings": bindings,
            "refresh_names": bool(refresh_names),
            "chat_display_name_cache_miss_count": cache_miss_count,
        }

    def _enrich_binding_list_snapshot(
        self,
        bindings: list[dict[str, Any]],
        *,
        refresh_names: bool,
    ) -> list[dict[str, Any]]:
        thread_name_by_id: dict[str, str] = {}
        chat_name_by_key: dict[tuple[str, ...], str] = {}
        for item in bindings:
            thread_id = str(item.get("thread_id", "") or "").strip()
            if thread_id:
                if thread_id not in thread_name_by_id:
                    thread_name_by_id[thread_id] = self._binding_list_thread_name(thread_id)
                item["thread_name"] = thread_name_by_id[thread_id]
            else:
                item["thread_name"] = ""

            binding_kind = str(item.get("binding_kind", "") or "").strip()
            sender_id = str(item.get("sender_id", "") or "").strip()
            chat_id = str(item.get("chat_id", "") or "").strip()
            chat_key = self._binding_list_chat_display_name_key(
                binding_kind=binding_kind,
                sender_id=sender_id,
                chat_id=chat_id,
            )
            if chat_key not in chat_name_by_key:
                chat_name_by_key[chat_key] = self._binding_list_chat_display_name(
                    binding_kind=binding_kind,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    refresh_names=refresh_names,
                )
            item["chat_display_name"] = chat_name_by_key[chat_key]
        return bindings

    @staticmethod
    def _binding_list_chat_display_name_key(
        *,
        binding_kind: str,
        sender_id: str,
        chat_id: str,
    ) -> tuple[str, ...]:
        if binding_kind == "group" and chat_id:
            return ("group", chat_id)
        if binding_kind == "p2p" and sender_id:
            return ("p2p", sender_id)
        return ("binding", binding_kind, sender_id, chat_id)

    def _binding_list_thread_name(self, thread_id: str) -> str:
        try:
            summary = self._read_thread(thread_id).summary
        except Exception as exc:
            logger.debug("binding list 读取 thread name 失败: thread=%s, error=%s", thread_id[:12], exc)
            return ""
        return str(getattr(summary, "name", "") or "").strip()

    def _binding_list_chat_display_name(
        self,
        *,
        binding_kind: str,
        sender_id: str,
        chat_id: str,
        refresh_names: bool,
    ) -> str:
        try:
            return str(
                self._resolve_binding_chat_display_name(
                    binding_kind=binding_kind,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    refresh_names=refresh_names,
                )
                or ""
            ).strip()
        except Exception as exc:
            logger.debug(
                "binding list 读取 chat display name 失败: kind=%s chat=%s sender=%s error=%s",
                binding_kind,
                chat_id[:12],
                sender_id[:12],
                exc,
            )
            return ""

    def bound_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        return self._binding_runtime.bound_bindings_for_thread_locked(thread_id)

    def attached_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        return self._binding_runtime.attached_bindings_for_thread_locked(thread_id)

    def interaction_owner_snapshot_locked(
        self,
        thread_id: str,
        *,
        current_binding: ChatBindingKey | None = None,
    ) -> dict[str, str]:
        return self._binding_runtime.interaction_owner_snapshot_locked(
            thread_id,
            current_binding=current_binding,
        )

    def _effective_binding_key(self, sender_id: str, chat_id: str) -> ChatBindingKey:
        with self._lock:
            existing = self._binding_runtime.existing_chat_binding_key_locked(sender_id, chat_id)
        if existing is not None:
            return existing
        return (sender_id, chat_id)

    def read_thread_summary_for_status(self, thread_id: str) -> tuple[ThreadSummary | None, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None, ""
        try:
            summary = self._read_thread(normalized_thread_id).summary
        except Exception as exc:
            if self._is_thread_not_found_error(exc):
                return None, BACKEND_THREAD_LOOKUP_MISSING
            if self._is_thread_not_loaded_error(exc):
                return None, BACKEND_THREAD_STATUS_NOT_LOADED
            logger.exception("读取线程状态失败: thread=%s", normalized_thread_id[:12])
            return None, BACKEND_THREAD_LOOKUP_ERROR
        return summary, str(summary.status or BACKEND_THREAD_STATUS_UNKNOWN).strip() or BACKEND_THREAD_STATUS_UNKNOWN

    def _classify_thread_for_stale_binding_cleanup(self, thread_id: str) -> tuple[str, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return "skip", "empty_thread_id"
        try:
            self._read_thread_for_stale_cleanup(normalized_thread_id)
        except Exception as exc:
            if self._is_thread_not_found_error(exc) or self._is_thread_not_loaded_error(exc):
                return "stale", str(exc) or "thread is not readable"
            logger.exception("验证 stale binding 线程失败: thread=%s", normalized_thread_id[:12])
            return "unknown", str(exc) or type(exc).__name__
        return "present", ""

    @staticmethod
    def _live_runtime_owner_snapshot(lease: ThreadRuntimeLease | None) -> dict[str, str]:
        if lease is None:
            return {
                "instance_name": "",
                "label": "none",
            }
        instance_name = str(lease.owner_instance or "").strip()
        return {
            "instance_name": instance_name,
            "label": instance_name or "unknown",
        }

    @staticmethod
    def _live_runtime_holder_labels(lease: ThreadRuntimeLease | None) -> list[str]:
        if lease is None:
            return []
        labels: list[str] = []
        for holder in lease.holders:
            holder_type = str(holder.holder_type or "").strip() or "unknown"
            instance_name = str(holder.instance_name or "").strip() or "unknown"
            label = f"{holder_type}@{instance_name}"
            if int(holder.owner_pid or 0) > 0:
                label += f"(pid={int(holder.owner_pid)})"
            labels.append(label)
        return labels

    def detach_thread_check_locked(self, thread_id: str) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_NO_THREAD,
                "当前没有绑定线程。",
            )
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if not attached_bindings:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_ALREADY_DETACHED,
                "当前 thread 的飞书推送原本就已是 `detached`。",
            )
        for binding in attached_bindings:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
            if snapshot is None:
                continue
            if snapshot.has_inflight_turn:
                return ReasonedCheck.deny(
                    DETACH_BLOCKED_BY_INFLIGHT_TURN,
                    "当前有飞书侧 turn 正在运行，不能 detach 当前 thread。",
                )
        if self._interaction_requests.thread_has_pending_request_locked(normalized_thread_id):
            return ReasonedCheck.deny(
                DETACH_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能 detach 当前 thread。",
            )
        return ReasonedCheck.allow()

    def detach_check_locked(self, binding: ChatBindingKey) -> ReasonedCheck:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_NO_BINDING,
                f"未找到 binding：{format_binding_id(binding)}",
            )
        if not snapshot.thread_id:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_NO_THREAD,
                "当前没有绑定线程。",
            )
        if snapshot.feishu_runtime_state != FEISHU_RUNTIME_ATTACHED:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_ALREADY_DETACHED,
                "当前 binding 的飞书推送原本就已是 `detached`。",
            )
        if snapshot.has_inflight_turn:
            return ReasonedCheck.deny(
                DETACH_BLOCKED_BY_INFLIGHT_TURN,
                "当前有飞书侧 turn 正在运行，不能 detach 当前会话。",
            )
        if self.binding_has_pending_request_locked(binding):
            return ReasonedCheck.deny(
                DETACH_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能 detach 当前会话。",
            )
        return ReasonedCheck.allow()

    def detach_thread_availability_locked(self, thread_id: str) -> tuple[bool, str]:
        check = self.detach_thread_check_locked(thread_id)
        return check.allowed, check.reason_text

    def preview_detach_thread_locked(self, thread_id: str) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        if not self.bound_bindings_for_thread_locked(normalized_thread_id):
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        check = self.detach_thread_check_locked(normalized_thread_id)
        if not check.allowed and check.reason_code != DETACH_NOT_APPLICABLE_ALREADY_DETACHED:
            raise ValueError(check.reason_text)
        return bool(self.attached_bindings_for_thread_locked(normalized_thread_id))

    def binding_has_pending_request_locked(self, binding: ChatBindingKey) -> bool:
        return self._interaction_requests.binding_has_pending_request_locked(binding)

    def binding_clear_check_locked(self, binding: ChatBindingKey) -> ReasonedCheck:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND,
                f"未找到绑定：{format_binding_id(binding)}",
            )
        if snapshot.has_inflight_turn:
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN,
                "当前有飞书侧 turn 正在运行，不能清除 binding。",
            )
        if self.binding_has_pending_request_locked(binding):
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能清除 binding。",
            )
        return ReasonedCheck.allow()

    def binding_clear_availability_locked(self, binding: ChatBindingKey) -> tuple[bool, str]:
        check = self.binding_clear_check_locked(binding)
        return check.allowed, check.reason_text

    def binding_prompt_check(self, binding: ChatBindingKey) -> ReasonedCheck:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        return self._binding_prompt_check_from_snapshot(binding, snapshot)

    def binding_prompt_check_locked(self, binding: ChatBindingKey) -> ReasonedCheck:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        return self._binding_prompt_check_from_snapshot(binding, snapshot)

    def submit_binding_prompt_for_control(
        self,
        binding: ChatBindingKey,
        *,
        text: str,
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
    ) -> dict[str, Any]:
        prompt_text = str(text or "").strip()
        normalized_input_items = list(input_items or [])
        if not prompt_text and not normalized_input_items:
            raise ValueError("binding/submit-prompt 需要 `text` 或 `input_items`。")
        normalized_display_mode = str(display_mode or "silent").strip().lower() or "silent"
        if normalized_display_mode not in {"silent", "announce"}:
            raise ValueError("binding/submit-prompt 的 display_mode 只支持 `silent` 或 `announce`。")
        check = self.binding_prompt_check(binding)
        if not check.allowed and check.reason_code != PROMPT_DENIED_BY_RUNNING_TURN:
            return {
                "binding_id": format_binding_id(binding),
                "thread_id": "",
                "started": False,
                "queued": False,
                "queue_position": 0,
                "turn_id": "",
                "reason_code": check.reason_code,
                "reason": check.reason_text,
                "synthetic_source": str(synthetic_source or "").strip(),
                "display_mode": normalized_display_mode,
            }
        return self._submit_prompt_for_control(
            binding,
            text=prompt_text,
            actor_open_id=str(actor_open_id or "").strip(),
            input_items=normalized_input_items or None,
            synthetic_source=str(synthetic_source or "").strip(),
            display_mode=normalized_display_mode,
        )

    def _binding_prompt_check_from_snapshot(
        self,
        binding: ChatBindingKey,
        snapshot: Any,
    ) -> ReasonedCheck:
        if snapshot is None:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BINDING_NOT_FOUND,
                f"未找到 binding：{format_binding_id(binding)}",
            )
        has_inflight_turn = bool(
            snapshot.has_inflight_turn
            if hasattr(snapshot, "has_inflight_turn")
            else snapshot.get("running_turn", False)
        )
        thread_id = str(
            snapshot.thread_id
            if hasattr(snapshot, "thread_id")
            else snapshot.get("thread_id", "")
        ).strip()
        feishu_runtime_state = str(
            snapshot.feishu_runtime_state
            if hasattr(snapshot, "feishu_runtime_state")
            else snapshot.get("feishu_runtime_state", "")
        ).strip()
        if has_inflight_turn:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_RUNNING_TURN,
                "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            )
        if not thread_id:
            return ReasonedCheck.allow()
        denial = self._prompt_write_denial_check(
            binding,
            binding[1],
            thread_id,
            message_id="",
        )
        if not denial.allowed:
            return denial
        if feishu_runtime_state == FEISHU_RUNTIME_DETACHED:
            return self._detached_runtime_attach_check(thread_id)
        return ReasonedCheck.allow()

    def clear_binding_for_control(self, binding: ChatBindingKey) -> dict[str, Any]:
        unsubscribe_thread_id = ""
        binding_id = format_binding_id(binding)
        thread_id = ""
        thread_title = ""
        with self._lock:
            allowed, reason = self.binding_clear_availability_locked(binding)
            if not allowed:
                raise ValueError(reason)
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
            assert snapshot is not None
            thread_id = snapshot.thread_id
            thread_title = snapshot.thread_title
            unsubscribe_thread_id = self._deactivate_binding_locked(binding)
        if unsubscribe_thread_id:
            self._unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
        return {
            "binding_id": binding_id,
            "thread_id": thread_id,
            "thread_title": thread_title,
            "cleared": True,
        }

    def clear_all_bindings_for_control(self) -> dict[str, Any]:
        unsubscribe_thread_ids: list[str] = []
        cleared_binding_ids: list[str] = []
        with self._lock:
            self._binding_runtime.hydrate_missing_stored_bindings_locked()
            bindings = list(self._binding_runtime.binding_keys_locked())
            if not bindings:
                self._clear_all_stored_bindings()
                return {
                    "cleared_binding_ids": [],
                    "already_empty": True,
                }
            blockers: list[str] = []
            for binding in bindings:
                allowed, reason = self.binding_clear_availability_locked(binding)
                if not allowed:
                    blockers.append(f"{format_binding_id(binding)}: {reason}")
            if blockers:
                raise ValueError("以下 binding 当前不能清除：\n" + "\n".join(blockers))
            unsubscribe_thread_ids.extend(self._binding_runtime.deactivate_bindings_locked(bindings))
            cleared_binding_ids.extend(format_binding_id(binding) for binding in bindings)
        for unsubscribe_thread_id in sorted(set(unsubscribe_thread_ids)):
            self._unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
        return {
            "cleared_binding_ids": cleared_binding_ids,
            "already_empty": False,
        }

    def binding_status_snapshot(self, binding: ChatBindingKey) -> dict[str, Any]:
        with self._lock:
            snapshot = self._binding_runtime.binding_status_state_snapshot_locked(binding)
            detach_check = self.detach_check_locked(binding)
        prompt_check = self._binding_prompt_check_from_snapshot(binding, snapshot)
        thread_id = str(snapshot["thread_id"] or "").strip()
        summary, backend_thread_status = self.read_thread_summary_for_status(thread_id)
        lease = self._load_thread_runtime_lease(thread_id)
        if summary is not None:
            snapshot["thread_title"] = summary.title or str(snapshot["thread_title"] or "").strip()
            snapshot["working_dir"] = summary.cwd or str(snapshot["working_dir"] or "").strip()
        snapshot["backend_thread_status"] = backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
        snapshot["backend_running_turn"] = backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
        snapshot["live_runtime_owner"] = self._live_runtime_owner_snapshot(lease)
        snapshot["live_runtime_holder_labels"] = self._live_runtime_holder_labels(lease)
        snapshot["detach_available"] = bool(thread_id and detach_check.allowed)
        snapshot["detach_reason_code"] = detach_check.reason_code
        snapshot["detach_reason"] = detach_check.reason_text
        snapshot["next_prompt_allowed"] = prompt_check.allowed
        snapshot["next_prompt_reason_code"] = prompt_check.reason_code
        snapshot["next_prompt_reason"] = prompt_check.reason_text
        return snapshot

    def render_binding_status_markdown(
        self,
        snapshot: dict[str, Any],
        *,
        include_profile_lines: bool,
    ) -> tuple[str, str]:
        thread_id = snapshot["thread_id"]
        if thread_id:
            thread_line = f"当前线程：`{thread_id[:8]}…` {snapshot['thread_title'] or '（无标题）'}"
        else:
            thread_line = "当前线程：-"
        lines = [
            f"目录：`{display_path(snapshot['working_dir'])}`",
            thread_line,
        ]
        lines.extend(
            format_goal_summary_markdown(
                objective=str(snapshot.get("goal_objective", "") or ""),
                status=str(snapshot.get("goal_status", "") or ""),
                token_budget=snapshot.get("goal_token_budget"),
                tokens_used=int(snapshot.get("goal_tokens_used") or 0),
                time_used_seconds=int(snapshot.get("goal_time_used_seconds") or 0),
            )
        )
        if include_profile_lines:
            lines.extend(
                [
                    f"权限基线：`{self._render_permissions_summary(snapshot)}`",
                    f"审批策略：`{snapshot['approval_policy']}`",
                    f"Codex model override：`{snapshot['model'] or 'auto'}`",
                    f"Codex effort override：`{snapshot.get('reasoning_effort', '') or 'auto'}`",
                ]
            )
        template = "turquoise" if snapshot["running_turn"] else "blue"
        return "\n".join(lines), template

    @staticmethod
    def _thread_goal_snapshot(goal: ThreadGoalSummary | None) -> dict[str, Any] | None:
        if goal is None:
            return None
        return {
            "thread_id": goal.thread_id,
            "objective": goal.objective,
            "status": goal.status,
            "token_budget": goal.token_budget,
            "tokens_used": goal.tokens_used,
            "time_used_seconds": goal.time_used_seconds,
            "created_at": goal.created_at,
            "updated_at": goal.updated_at,
        }

    @staticmethod
    def _thread_identity_snapshot(summary: ThreadSummary) -> dict[str, Any]:
        return {
            "thread_id": summary.thread_id,
            "thread_title": summary.title,
            "working_dir": summary.cwd,
        }

    def thread_goal_snapshot(self, thread_id: str, *, summary: ThreadSummary) -> dict[str, Any]:
        goal = self._get_thread_goal(thread_id)
        return {
            **self._thread_identity_snapshot(summary),
            "goal": self._thread_goal_snapshot(goal),
        }

    def set_thread_goal_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary,
        objective: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        normalized_objective = None
        if objective is not None:
            normalized_objective = str(objective or "").strip()
            if not normalized_objective:
                raise ValueError("thread/goal/set 的 objective 不能为空。")
        normalized_status = None
        if status is not None:
            normalized_status = str(status or "").strip()
            if not normalized_status:
                raise ValueError("thread/goal/set 的 status 不能为空。")
        if normalized_objective is None and normalized_status is None:
            raise ValueError("thread/goal/set 至少需要 `objective` 或 `status`。")
        goal = self._set_thread_goal(
            thread_id,
            objective=normalized_objective,
            status=normalized_status,
        )
        return {
            **self._thread_identity_snapshot(summary),
            "goal": self._thread_goal_snapshot(goal),
        }

    def clear_thread_goal_for_control(self, thread_id: str, *, summary: ThreadSummary) -> dict[str, Any]:
        cleared = self._clear_thread_goal(thread_id)
        return {
            **self._thread_identity_snapshot(summary),
            "goal": None,
            "cleared": bool(cleared),
        }

    def handle_status_command(self, binding: ChatBindingKey) -> CommandResult:
        snapshot = self.binding_status_snapshot(binding)
        content, template = self.render_binding_status_markdown(snapshot, include_profile_lines=True)
        return CommandResult(card=build_markdown_card("Codex 当前状态", content, template=template))

    @staticmethod
    def _next_prompt_preflight_line(snapshot: dict[str, Any]) -> str:
        if not snapshot["next_prompt_allowed"]:
            return (
                "下一条普通消息："
                f"`blocked` (`{snapshot['next_prompt_reason_code']}`) {snapshot['next_prompt_reason']}"
            )
        if not str(snapshot.get("thread_id") or "").strip():
            return "下一条普通消息：`accepted`，会在当前目录新建 thread 后启动 turn。"
        if snapshot["feishu_runtime_state"] == FEISHU_RUNTIME_DETACHED:
            return "下一条普通消息：`accepted`，会先按当前 binding 重新 attach / resume，再启动 turn。"
        return "下一条普通消息：`accepted`，会写入当前绑定 thread。"

    @staticmethod
    def _detach_preflight_line(snapshot: dict[str, Any]) -> str:
        if not snapshot["thread_id"]:
            return "detach：`not-applicable`，当前没有绑定 thread。"
        if snapshot["detach_available"]:
            return "detach：`available`"
        return (
            "detach："
            f"`blocked` (`{snapshot['detach_reason_code']}`) "
            f"{snapshot['detach_reason']}"
        )

    def render_binding_preflight_markdown(
        self,
        snapshot: dict[str, Any],
        *,
        include_profile_lines: bool,
    ) -> tuple[str, str]:
        thread_id = str(snapshot["thread_id"] or "").strip()
        if thread_id:
            thread_line = f"当前线程：`{thread_id[:8]}…` {snapshot['thread_title'] or '（无标题）'}"
        else:
            thread_line = "当前线程：-"
        lines = [
            "作用对象：当前 chat binding；这是 dry-run，不会启动 turn，也不会改变 binding。",
            f"目录：`{display_path(snapshot['working_dir'])}`",
            thread_line,
            f"binding：`{snapshot['binding_state']}`",
            f"飞书推送：`{snapshot['feishu_runtime_state']}`",
            f"backend thread status：`{snapshot['backend_thread_status']}`",
            "",
            self._next_prompt_preflight_line(snapshot),
            self._detach_preflight_line(snapshot),
        ]
        if include_profile_lines:
            lines.extend(
                [
                    "",
                    f"权限基线：`{self._render_permissions_summary(snapshot)}`",
                    f"审批策略：`{snapshot['approval_policy']}`",
                    f"model override：`{snapshot['model'] or 'auto'}`",
                    f"effort override：`{snapshot.get('reasoning_effort', '') or 'auto'}`",
                ]
            )
        if thread_id and snapshot["feishu_runtime_state"] == FEISHU_RUNTIME_DETACHED:
            lines.extend(
                [
                    "",
                    "说明：`detached` 状态下，只有 preflight accepted 才允许重新 attach；blocked 必须保持 pure reject。",
                ]
            )
        template = "green" if snapshot["next_prompt_allowed"] else "yellow"
        return "\n".join(lines), template

    def handle_preflight_command(self, binding: ChatBindingKey, arg: str) -> CommandResult:
        if str(arg or "").strip():
            return CommandResult(text="用法：`/preflight`")
        snapshot = self.binding_status_snapshot(binding)
        content, template = self.render_binding_preflight_markdown(snapshot, include_profile_lines=True)
        return CommandResult(card=build_markdown_card("Codex Preflight", content, template=template))

    @staticmethod
    def _short_thread_ids(thread_ids: tuple[str, ...] | list[str]) -> str:
        normalized = [str(thread_id or "").strip() for thread_id in thread_ids if str(thread_id or "").strip()]
        if not normalized:
            return "（无）"
        return ", ".join(f"`{thread_id[:8]}…`" for thread_id in normalized)

    @staticmethod
    def _format_binding_ids(binding_ids: tuple[str, ...] | list[str]) -> str:
        normalized = [str(binding_id or "").strip() for binding_id in binding_ids if str(binding_id or "").strip()]
        if not normalized:
            return "（无）"
        return ", ".join(f"`{binding_id}`" for binding_id in normalized)

    @staticmethod
    def _preview_thread_ids(
        thread_ids: tuple[str, ...] | list[str],
        *,
        limit: int = 3,
    ) -> tuple[str, ...]:
        normalized = [str(thread_id or "").strip() for thread_id in thread_ids if str(thread_id or "").strip()]
        if limit <= 0:
            return ()
        return tuple(normalized[:limit])

    @staticmethod
    def _format_holder_labels(holder_labels: tuple[str, ...] | list[str]) -> str:
        normalized = [str(label or "").strip() for label in holder_labels if str(label or "").strip()]
        if not normalized:
            return "（无）"
        return ", ".join(f"`{label}`" for label in normalized)

    def _backend_reset_hard_blocker_lines(self, preview: BackendResetPreview) -> list[str]:
        lines: list[str] = []
        if preview.blocking_active_turn_count:
            line = f"backend active threads：`{preview.blocking_active_turn_count}`"
            if preview.active_loaded_thread_preview:
                line += f" ({self._short_thread_ids(preview.active_loaded_thread_preview)})"
            lines.append(line)
        if preview.blocking_pending_request_count:
            lines.append(f"待处理审批/输入请求：`{preview.blocking_pending_request_count}`")
        if preview.running_binding_ids:
            lines.append("运行中的 Feishu bindings：" + self._format_binding_ids(preview.running_binding_ids))
        if preview.runtime_verification_failed:
            lines.append("backend loaded thread 校验：`unverified`")
        return lines

    def _backend_reset_collateral_lines(self, preview: BackendResetPreview) -> list[str]:
        if (
            preview.status == BACKEND_RESET_STATUS_BLOCKED
            and not preview.collateral_loaded_thread_count
            and not preview.collateral_active_loaded_thread_count
            and not preview.loaded_thread_preview
            and not preview.runtime_verification_failed
        ):
            return []
        lines = [
            f"当前实例 loaded threads：`{preview.collateral_loaded_thread_count}`",
        ]
        if preview.attached_binding_ids:
            lines.append("attached Feishu bindings：" + self._format_binding_ids(preview.attached_binding_ids))
        if preview.blocking_holder_labels:
            lines.append("live runtime holders：" + self._format_holder_labels(preview.blocking_holder_labels))
        if preview.collateral_active_loaded_thread_count:
            lines.append(f"其中 active threads：`{preview.collateral_active_loaded_thread_count}`")
        if preview.loaded_thread_preview:
            lines.append("preview：" + self._short_thread_ids(preview.loaded_thread_preview))
        return lines

    def _backend_reset_flat_diagnostics(self, preview: BackendResetPreview) -> tuple[str, ...]:
        lines = [
            f"当前实例：`{self._instance_name()}`",
            f"当前 backend 模式：`{self._app_server_mode() or 'unknown'}`",
        ]
        for item in self._backend_reset_hard_blocker_lines(preview):
            lines.append(f"hard blocker：{item}")
        for item in self._backend_reset_collateral_lines(preview):
            lines.append(f"collateral impact：{item}")
        return tuple(lines)

    @staticmethod
    def _attach_action_rows(*, include_thread: bool, include_service: bool, thread_id: str = "") -> list[dict]:
        actions: list[dict] = []
        if include_thread and str(thread_id or "").strip():
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "附着当前线程"},
                    "type": "primary",
                    "value": {
                        "action": "attach_runtime",
                        "scope": "thread",
                        "thread_id": str(thread_id or "").strip(),
                    },
                }
            )
        if include_service:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "附着当前实例"},
                    "type": "default",
                    "value": {
                        "action": "attach_runtime",
                        "scope": "service",
                    },
                }
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "保持 detached"},
                "type": "default",
                "value": {
                    "action": "dismiss_attach",
                },
            }
        )
        return [
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "如需继续收到本地 `fcodex` / backend 的推送，可选择 attach 范围：",
            },
            {
                "tag": "action",
                "actions": actions,
            },
        ]

    @staticmethod
    def _format_blocked_attach_entries(items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            thread_id = str(item.get("thread_id", "") or "").strip()
            binding_ids = [str(value or "").strip() for value in (item.get("binding_ids") or []) if str(value or "").strip()]
            reason = str(item.get("reason", "") or "").strip() or "（无原因）"
            label = f"`{thread_id[:8]}…`" if thread_id else "（未知 thread）"
            if binding_ids:
                label += " " + ", ".join(f"`{binding_id}`" for binding_id in binding_ids)
            lines.append(f"- {label}: {reason}")
        return lines

    def _build_backend_reset_preview_card(
        self,
        preview: BackendResetPreview,
        *,
        leading_lines: list[str] | None = None,
    ) -> dict:
        lines = list(leading_lines or [])
        lines.extend(
            [
                "作用对象：当前实例 backend；这是实例级管理动作，不是当前线程命令。",
                "不会覆盖 binding bookmark、其他用户配置或数据。",
                "",
                f"当前结论：{preview.reason_text}",
            ]
        )
        if preview.status == BACKEND_RESET_STATUS_FORCE_ONLY:
            lines.append("当前只能显式确认强制重置；这会打断当前实例内尚未完成的工作。")
        elif preview.status == BACKEND_RESET_STATUS_BLOCKED:
            lines.append("当前不能在本实例执行 backend reset。")
        hard_blockers = self._backend_reset_hard_blocker_lines(preview)
        collateral = self._backend_reset_collateral_lines(preview)
        if hard_blockers:
            lines.extend(["", "**Hard Blockers**"])
            lines.extend(f"- {line}" for line in hard_blockers)
        if collateral:
            lines.extend(["", "**Collateral Impact**"])
            lines.extend(f"- {line}" for line in collateral)
        if preview.status == BACKEND_RESET_STATUS_BLOCKED and preview.diagnostics:
            lines.extend(["", "**诊断**"])
            lines.extend(f"- {line}" for line in preview.diagnostics)
        template = {
            BACKEND_RESET_STATUS_AVAILABLE: "green",
            BACKEND_RESET_STATUS_FORCE_ONLY: "yellow",
            BACKEND_RESET_STATUS_BLOCKED: "red",
        }.get(preview.status, "blue")
        force = None
        if preview.status == BACKEND_RESET_STATUS_AVAILABLE:
            force = False
        elif preview.status == BACKEND_RESET_STATUS_FORCE_ONLY:
            force = True
        return build_backend_reset_card(
            content="\n".join(lines),
            force=force,
            template=template,
        )

    def _build_backend_reset_result_card(self, result: dict[str, Any], *, forced: bool) -> dict:
        lines = [
            "已重置当前实例 backend。",
            f"当前实例：`{self._instance_name()}`",
            f"执行方式：`{'force' if forced else 'safe'}`",
            f"已中断运行中的 binding：{self._format_binding_ids(result.get('interrupted_binding_ids') or [])}",
            f"已 detach 的 binding：{self._format_binding_ids(result.get('detached_binding_ids') or [])}",
            f"已结束待处理审批/输入请求：`{int(result.get('fail_closed_request_count') or 0)}`",
            f"已清理 live runtime lease thread：{self._short_thread_ids(result.get('purged_thread_ids') or [])}",
            f"当前 backend 地址：`{str(result.get('app_server_url') or '').strip() or '（未知）'}`",
            "",
            "不会覆盖 binding bookmark、其他用户配置或数据。",
        ]
        current_thread_id = str(result.get("current_thread_id", "") or "").strip()
        if result.get("detached_binding_ids"):
            lines.extend(
                [
                    "",
                    "当前所有相关 Feishu binding 已变为 `detached`；若要继续接收推送，可直接在此卡片选择 attach 范围。",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "如需确认飞书侧继续接收本地 `fcodex` / backend 推送，可直接在此卡片选择 attach 范围。",
                ]
            )
        return build_backend_reset_card(
            content="\n".join(lines),
            force=None,
            extra_action_rows=self._attach_action_rows(
                include_thread=bool(current_thread_id),
                include_service=True,
                thread_id=current_thread_id,
            ),
            template="green",
        )

    def handle_reset_backend_command(self, arg: str) -> CommandResult:
        if str(arg or "").strip():
            return CommandResult(text="用法：`/reset-backend`")
        preview = self.backend_reset_preview()
        return CommandResult(card=self._build_backend_reset_preview_card(preview))

    def handle_reset_backend_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        binding = self._effective_binding_key(sender_id, chat_id)
        current_thread_id = ""
        try:
            snapshot = self.binding_status_snapshot(binding)
        except ValueError:
            snapshot = {}
        current_thread_id = str(snapshot.get("thread_id", "") or "").strip()
        force = bool(action_value.get("force"))
        try:
            result = self._reset_current_instance_backend(force)
        except Exception as exc:
            preview = self.backend_reset_preview()
            return make_card_response(
                card=self._build_backend_reset_preview_card(
                    preview,
                    leading_lines=[f"reset backend 失败：{exc}", ""],
                ),
                toast=str(exc) or "reset backend 失败",
                toast_type="warning",
            )
        result = {
            **result,
            "current_thread_id": current_thread_id,
        }
        return make_card_response(
            card=self._build_backend_reset_result_card(result, forced=force),
            toast="已重置当前实例 backend。",
            toast_type="success",
        )

    def _binding_thread_id_or_raise(self, binding: ChatBindingKey) -> str:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            raise ValueError(f"未找到 binding：{format_binding_id(binding)}")
        thread_id = str(snapshot.thread_id or "").strip()
        if not thread_id:
            raise ValueError("当前 binding 没有绑定 thread。")
        return thread_id

    def detach_binding(self, binding: ChatBindingKey) -> dict[str, Any]:
        with self._lock:
            check = self.detach_check_locked(binding)
            if not check.allowed and check.reason_code != DETACH_NOT_APPLICABLE_ALREADY_DETACHED:
                raise ValueError(check.reason_text)
            result = self._binding_runtime.detach_binding_locked(
                binding,
                on_detach_binding_state=self._detach_binding_runtime_state_locked,
            )
        if result.unsubscribe_thread_id:
            self._unsubscribe_thread(result.unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(result.unsubscribe_thread_id)
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(result.thread_id)
        thread_title = str(resolved_summary.title if resolved_summary is not None else result.thread_title or "").strip()
        working_dir = str(resolved_summary.cwd if resolved_summary is not None else result.working_dir or "").strip()
        return {
            "binding_id": result.binding_id,
            "thread_id": result.thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "changed": result.changed,
            "already_detached": result.already_detached,
            "backend_thread_status": backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN,
            "backend_still_loaded": backend_thread_status in LOADED_BACKEND_THREAD_STATUSES,
        }

    def attach_binding(self, binding: ChatBindingKey) -> dict[str, Any]:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            raise ValueError(f"未找到 binding：{format_binding_id(binding)}")
        thread_id = str(snapshot.thread_id or "").strip()
        if not thread_id:
            raise ValueError("当前 binding 没有绑定 thread。")
        binding_id = format_binding_id(binding)
        if snapshot.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED:
            return {
                "binding_id": binding_id,
                "thread_id": thread_id,
                "thread_title": snapshot.thread_title,
                "working_dir": snapshot.working_dir,
                "changed": False,
                "already_attached": True,
            }
        check = self._detached_runtime_attach_check(thread_id)
        if not check.allowed:
            raise ValueError(check.reason_text)
        summary = self._attach_binding(binding, thread_id)
        return {
            "binding_id": binding_id,
            "thread_id": thread_id,
            "thread_title": str(summary.title or snapshot.thread_title or "").strip(),
            "working_dir": str(summary.cwd or snapshot.working_dir or "").strip(),
            "changed": True,
            "already_attached": False,
        }

    def attach_thread(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            bound_bindings = self.bound_bindings_for_thread_locked(normalized_thread_id)
            attached_bindings = set(self.attached_bindings_for_thread_locked(normalized_thread_id))
        if not bound_bindings:
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        attach_check = self._detached_runtime_attach_check(normalized_thread_id)
        if not attach_check.allowed:
            raise ValueError(attach_check.reason_text)
        attached_binding_ids: list[str] = []
        already_attached_binding_ids: list[str] = []
        effective_title = ""
        effective_working_dir = ""
        for binding in bound_bindings:
            binding_id = format_binding_id(binding)
            if binding in attached_bindings:
                already_attached_binding_ids.append(binding_id)
                continue
            result = self.attach_binding(binding)
            attached_binding_ids.append(binding_id)
            effective_title = str(result.get("thread_title", "") or "").strip() or effective_title
            effective_working_dir = str(result.get("working_dir", "") or "").strip() or effective_working_dir
        if not effective_title or not effective_working_dir:
            summary, _backend_status = self.read_thread_summary_for_status(normalized_thread_id)
            if summary is not None:
                effective_title = str(summary.title or "").strip() or effective_title
                effective_working_dir = str(summary.cwd or "").strip() or effective_working_dir
        return {
            "thread_id": normalized_thread_id,
            "thread_title": effective_title,
            "working_dir": effective_working_dir,
            "attached_binding_ids": attached_binding_ids,
            "already_attached_binding_ids": already_attached_binding_ids,
            "changed": bool(attached_binding_ids),
        }

    def attach_service(self) -> dict[str, Any]:
        with self._lock:
            inventory = self.binding_inventory_locked()
        detached_by_thread: dict[str, list[str]] = {}
        for item in inventory:
            if item["binding_state"] != "bound" or item["feishu_runtime_state"] != FEISHU_RUNTIME_DETACHED:
                continue
            thread_id = str(item["thread_id"] or "").strip()
            binding_id = str(item["binding_id"] or "").strip()
            if not thread_id or not binding_id:
                continue
            detached_by_thread.setdefault(thread_id, []).append(binding_id)

        attached_binding_ids: list[str] = []
        attached_thread_ids: list[str] = []
        already_attached_thread_ids: list[str] = []
        blocked_threads: list[dict[str, Any]] = []
        for thread_id in sorted(detached_by_thread):
            try:
                result = self.attach_thread(thread_id)
            except Exception as exc:
                blocked_threads.append(
                    {
                        "thread_id": thread_id,
                        "binding_ids": detached_by_thread[thread_id],
                        "reason": str(exc) or "附着失败",
                    }
                )
                continue
            if result["changed"]:
                attached_thread_ids.append(thread_id)
                attached_binding_ids.extend(result["attached_binding_ids"])
            else:
                already_attached_thread_ids.append(thread_id)
        return {
            "instance_name": self._instance_name(),
            "attached_binding_ids": sorted(set(attached_binding_ids)),
            "attached_thread_ids": sorted(set(attached_thread_ids)),
            "already_attached_thread_ids": sorted(set(already_attached_thread_ids)),
            "blocked_threads": blocked_threads,
        }

    def _build_thread_attach_result_card(self, result: dict[str, Any]) -> dict:
        lines = [
            f"线程：`{result['thread_id'][:8]}…` {result.get('thread_title', '') or '（无标题）'}",
            f"目录：`{display_path(str(result.get('working_dir', '') or ''))}`",
            f"已附着 binding：{self._format_binding_ids(result.get('attached_binding_ids') or [])}",
        ]
        if result.get("already_attached_binding_ids"):
            lines.append(
                f"原本已附着：{self._format_binding_ids(result.get('already_attached_binding_ids') or [])}"
            )
        if not result.get("changed"):
            lines.append("说明：当前 thread 相关 binding 原本就没有需要恢复的 detached 推送。")
        return build_markdown_card("Codex 已附着飞书推送", "\n".join(lines), template="green")

    def _build_service_attach_result_card(self, result: dict[str, Any]) -> dict:
        lines = [
            f"当前实例：`{result.get('instance_name') or self._instance_name()}`",
            f"已附着 threads：{self._short_thread_ids(result.get('attached_thread_ids') or [])}",
            f"已附着 bindings：{self._format_binding_ids(result.get('attached_binding_ids') or [])}",
        ]
        if result.get("already_attached_thread_ids"):
            lines.append(
                f"原本已附着 threads：{self._short_thread_ids(result.get('already_attached_thread_ids') or [])}"
            )
        blocked_threads = result.get("blocked_threads") or []
        template = "green"
        if blocked_threads:
            template = "yellow"
            lines.extend(["", "**未恢复项**"])
            lines.extend(self._format_blocked_attach_entries(blocked_threads))
        elif not result.get("attached_binding_ids"):
            lines.append("说明：当前实例没有需要恢复的 detached 推送。")
        return build_markdown_card("Codex 已附着飞书推送", "\n".join(lines), template=template)

    def handle_attach_command(self, binding: ChatBindingKey, arg: str) -> CommandResult:
        normalized = str(arg or "").strip().lower()
        scope = normalized or "binding"
        if scope not in {"binding", "thread", "service"}:
            return CommandResult(text="用法：`/attach [binding|thread|service]`")
        try:
            if scope == "binding":
                result = self.attach_binding(binding)
                body = [
                    f"binding：`{result['binding_id']}`",
                    f"线程：`{result['thread_id'][:8]}…` {result.get('thread_title', '') or '（无标题）'}",
                    f"目录：`{display_path(str(result.get('working_dir', '') or ''))}`",
                ]
                if result["already_attached"]:
                    body.append("说明：当前 binding 原本就已是 `attached`。")
                    template = "blue"
                else:
                    body.append("说明：当前 binding 已恢复为 `attached`，后续可继续接收该 thread 的推送。")
                    template = "green"
                return CommandResult(card=build_markdown_card("Codex 已附着飞书推送", "\n".join(body), template=template))
            if scope == "thread":
                thread_id = self._binding_thread_id_or_raise(binding)
                return CommandResult(card=self._build_thread_attach_result_card(self.attach_thread(thread_id)))
            return CommandResult(card=self._build_service_attach_result_card(self.attach_service()))
        except Exception as exc:
            return CommandResult(text=f"attach 失败：{exc}")

    def handle_attach_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        binding = self._effective_binding_key(sender_id, chat_id)
        scope = str(action_value.get("scope", "") or "").strip().lower()
        thread_id = str(action_value.get("thread_id", "") or "").strip()
        self._submit_to_runtime(
            self._run_attach_action_on_runtime,
            binding,
            scope,
            message_id=message_id,
            thread_id=thread_id,
        )
        if scope == "service":
            ack_card = build_markdown_card(
                "Codex 正在恢复飞书推送",
                "正在恢复当前实例推送；完成后会自动回复结果。",
            )
        elif scope == "thread":
            ack_card = build_markdown_card(
                "Codex 正在恢复飞书推送",
                "正在恢复当前线程推送；完成后会自动回复结果。",
            )
        else:
            ack_card = build_markdown_card(
                "Codex 正在恢复飞书推送",
                "正在恢复当前会话推送；完成后会自动回复结果。",
            )
        return make_card_response(card=ack_card)

    def _run_attach_action_on_runtime(
        self,
        binding: ChatBindingKey,
        scope: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> None:
        chat_id = binding[1]
        try:
            if scope == "service":
                card = self._build_service_attach_result_card(self.attach_service())
            elif scope == "thread":
                target_thread_id = thread_id or self._binding_thread_id_or_raise(binding)
                card = self._build_thread_attach_result_card(self.attach_thread(target_thread_id))
            else:
                result = self.attach_binding(binding)
                description = (
                    "说明：当前会话原本就已是 `attached`。"
                    if result["already_attached"]
                    else "说明：当前会话已恢复接收该 thread 的飞书推送。"
                )
                template = "blue" if result["already_attached"] else "green"
                card = build_markdown_card(
                    "Codex 已附着飞书推送",
                    "\n".join(
                        [
                            f"binding：`{format_binding_id(binding)}`",
                            description,
                        ]
                    ),
                    template=template,
                )
        except Exception as exc:
            self._reply_card(
                chat_id,
                build_markdown_card("Codex 飞书推送附着失败", str(exc) or "attach 失败", template="red"),
                message_id=message_id,
            )
            return
        self._reply_card(chat_id, card, message_id=message_id)

    def handle_dismiss_attach_action(self) -> P2CardActionTriggerResponse:
        return make_card_response(
            card=build_markdown_card(
                "Codex Backend Reset",
                "已保持 `detached` 状态。\n如需稍后恢复推送，可发送 `/attach`、`/resume`，或直接发送下一条普通消息。",
                template="blue",
            ),
            toast="已保持 detached。",
            toast_type="info",
        )

    def handle_detach_command(self, binding: ChatBindingKey, arg: str) -> CommandResult:
        if str(arg or "").strip():
            return CommandResult(text="用法：`/detach`")
        try:
            result = self.detach_binding(binding)
        except ValueError as exc:
            return CommandResult(text=str(exc))
        body = [
            f"binding：`{result['binding_id']}`",
            f"线程：`{result['thread_id'][:8]}…` {result['thread_title'] or '（无标题）'}",
            f"目录：`{display_path(result['working_dir'])}`",
            f"飞书推送：`{'detached' if result['changed'] or result['already_detached'] else 'attached'}`",
            f"backend thread status：`{result['backend_thread_status']}`",
        ]
        if result["already_detached"]:
            body.append("说明：当前会话原本就已是 `detached`。")
            template = "blue"
        elif result["backend_still_loaded"]:
            body.append("说明：当前会话已 detach；backend 仍保持 loaded，通常是还有本地 `fcodex` 或其他外部订阅者。")
            template = "green"
        else:
            body.append("说明：当前会话已 detach；如果这是最后一个 attached 的 Feishu binding，服务已自动停止该 thread 的 Feishu 订阅。")
            template = "green"
        return CommandResult(card=build_markdown_card("Codex 已暂停飞书推送", "\n".join(body), template=template))

    def _detach_binding_runtime_state_locked(self, state: RuntimeState) -> None:
        self._cancel_patch_timer_locked(state)
        self._cancel_mirror_watchdog_locked(state)

    def fail_close_service_attached_runtime(self) -> dict[str, Any]:
        detached_binding_ids: list[str] = []
        detached_thread_ids: list[str] = []
        release_thread_ids: list[str] = []
        with self._lock:
            attached_thread_ids = sorted(
                {
                    snapshot.thread_id
                    for binding in self._binding_runtime.binding_keys_locked()
                    for snapshot in [self._binding_runtime.binding_runtime_snapshot_locked(binding)]
                    if snapshot is not None
                    and snapshot.thread_id
                    and snapshot.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED
                }
            )
            for thread_id in attached_thread_ids:
                result = self._binding_runtime.detach_thread_bindings_locked(
                    thread_id,
                    detach_availability=lambda _thread_id: (True, ""),
                    on_release_binding_state=self._detach_binding_runtime_state_locked,
                )
                if result.detached_binding_ids:
                    detached_binding_ids.extend(result.detached_binding_ids)
                    detached_thread_ids.append(thread_id)
                if result.unsubscribe_thread_id:
                    release_thread_ids.append(result.unsubscribe_thread_id)
        for thread_id in sorted(set(release_thread_ids)):
            self._release_service_thread_runtime_lease(thread_id)
        return {
            "detached_binding_ids": sorted(set(detached_binding_ids)),
            "detached_thread_ids": sorted(set(detached_thread_ids)),
            "released_thread_ids": sorted(set(release_thread_ids)),
        }

    def detach_thread(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            needs_backend_unsubscribe = self.preview_detach_thread_locked(normalized_thread_id)
        if needs_backend_unsubscribe:
            self._unsubscribe_thread(normalized_thread_id)
        with self._lock:
            result = self._binding_runtime.detach_thread_bindings_locked(
                normalized_thread_id,
                detach_availability=self.detach_thread_availability_locked,
                on_release_binding_state=self._detach_binding_runtime_state_locked,
            )
        if result.unsubscribe_thread_id:
            self._release_service_thread_runtime_lease(result.unsubscribe_thread_id)
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        thread_title = result.thread_title
        working_dir = result.working_dir
        if resolved_summary is not None:
            thread_title = resolved_summary.title or thread_title
            working_dir = resolved_summary.cwd or working_dir
        detach_check = self.detach_thread_check_locked(normalized_thread_id)
        return {
            "thread_id": result.thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "bound_binding_ids": result.bound_binding_ids,
            "detached_binding_ids": result.detached_binding_ids,
            "changed": result.changed,
            "already_detached": result.already_detached,
            "backend_thread_status": backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN,
            "backend_still_loaded": backend_thread_status in LOADED_BACKEND_THREAD_STATUSES,
            "detach_reason_code": "" if result.changed else detach_check.reason_code,
        }

    def archive_thread_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        effective_summary = summary
        if effective_summary is None:
            resolved_summary, _backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
            effective_summary = resolved_summary
        lease = self._load_thread_runtime_lease(normalized_thread_id)
        live_runtime_owner = self._live_runtime_owner_snapshot(lease)
        owner_instance = str(live_runtime_owner.get("instance_name", "") or "").strip()
        if owner_instance and owner_instance != self._instance_name():
            raise ValueError(
                f"当前 thread 的 live runtime 由实例 `{owner_instance}` 持有；"
                "请改在该实例执行 archive。"
            )
        with self._lock:
            self._binding_runtime.hydrate_missing_stored_bindings_locked()
            snapshot = self._binding_runtime.thread_binding_snapshot_locked(
                normalized_thread_id,
                detach_availability=self.detach_thread_availability_locked,
            )
            bound_bindings, running_binding_ids, pending_binding_ids = (
                self._local_thread_binding_clear_plan_locked(normalized_thread_id)
            )
        self._raise_local_thread_binding_clear_blockers(
            running_binding_ids,
            pending_binding_ids,
            action_text="archive 该 thread",
        )
        self._archive_thread(normalized_thread_id)
        cleared_binding_ids: list[str] = []
        unsubscribe_thread_ids: list[str] = []
        with self._lock:
            existing_bindings = [
                binding
                for binding in bound_bindings
                if self._binding_runtime.binding_runtime_snapshot_locked(binding) is not None
            ]
            unsubscribe_thread_ids.extend(self._binding_runtime.deactivate_bindings_locked(existing_bindings))
            cleared_binding_ids.extend(format_binding_id(binding) for binding in existing_bindings)
        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        for unsubscribe_thread_id in unique_unsubscribe_thread_ids:
            self._unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
        if normalized_thread_id not in unique_unsubscribe_thread_ids:
            self._release_service_thread_runtime_lease(normalized_thread_id)
        return {
            "thread_id": normalized_thread_id,
            "thread_title": effective_summary.title if effective_summary is not None else "",
            "working_dir": effective_summary.cwd if effective_summary is not None else "",
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "detached_binding_ids": snapshot["detached_binding_ids"],
            "cleared_binding_ids": cleared_binding_ids,
            "live_runtime_owner": live_runtime_owner,
        }

    def _local_thread_binding_clear_plan_locked(
        self,
        normalized_thread_id: str,
    ) -> tuple[list[ChatBindingKey], list[str], list[str]]:
        bound_bindings = list(self.bound_bindings_for_thread_locked(normalized_thread_id))
        running_binding_ids = [
            format_binding_id(binding)
            for binding in bound_bindings
            if (
                runtime_snapshot := self._binding_runtime.binding_runtime_snapshot_locked(binding)
            ) is not None
            and runtime_snapshot.has_inflight_turn
        ]
        pending_binding_ids = [
            format_binding_id(binding)
            for binding in bound_bindings
            if self.binding_has_pending_request_locked(binding)
        ]
        return bound_bindings, running_binding_ids, pending_binding_ids

    @staticmethod
    def _raise_local_thread_binding_clear_blockers(
        running_binding_ids: list[str],
        pending_binding_ids: list[str],
        *,
        action_text: str,
    ) -> None:
        if running_binding_ids:
            raise ValueError(
                f"当前实例仍有飞书侧 turn 正在运行，不能 {action_text}："
                + ", ".join(f"`{binding_id}`" for binding_id in running_binding_ids)
            )
        if pending_binding_ids:
            raise ValueError(
                f"当前实例仍有待处理审批或补充输入，不能 {action_text}："
                + ", ".join(f"`{binding_id}`" for binding_id in pending_binding_ids)
            )

    def clear_archived_thread_bindings_for_control(
        self,
        thread_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")

        unsubscribe_thread_ids: list[str] = []
        cleared_binding_ids: list[str] = []
        with self._lock:
            self._binding_runtime.hydrate_missing_stored_bindings_locked()
            bound_bindings, running_binding_ids, pending_binding_ids = (
                self._local_thread_binding_clear_plan_locked(normalized_thread_id)
            )
            self._raise_local_thread_binding_clear_blockers(
                running_binding_ids,
                pending_binding_ids,
                action_text="清理该已归档 thread 的本地 bindings",
            )

            existing_bindings = [
                binding
                for binding in bound_bindings
                if self._binding_runtime.binding_runtime_snapshot_locked(binding) is not None
            ]
            if dry_run:
                return {
                    "thread_id": normalized_thread_id,
                    "would_clear_binding_ids": [
                        format_binding_id(binding)
                        for binding in existing_bindings
                    ],
                    "dry_run": True,
                }
            unsubscribe_thread_ids.extend(self._binding_runtime.deactivate_bindings_locked(existing_bindings))
            cleared_binding_ids.extend(format_binding_id(binding) for binding in existing_bindings)

        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        for unsubscribe_thread_id in unique_unsubscribe_thread_ids:
            self._unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
        if cleared_binding_ids and normalized_thread_id not in unique_unsubscribe_thread_ids:
            self._release_service_thread_runtime_lease(normalized_thread_id)
        return {
            "thread_id": normalized_thread_id,
            "cleared_binding_ids": cleared_binding_ids,
            "cleared": bool(cleared_binding_ids),
        }

    def clear_stale_bindings_for_control(self, *, dry_run: bool = False) -> dict[str, Any]:
        with self._lock:
            self._binding_runtime.hydrate_missing_stored_bindings_locked()
            snapshots = [
                snapshot
                for binding in self._binding_runtime.binding_keys_locked()
                for snapshot in [self._binding_runtime.binding_runtime_snapshot_locked(binding)]
                if snapshot is not None and snapshot.thread_id
            ]
        thread_ids = sorted({snapshot.thread_id for snapshot in snapshots})
        stale_thread_ids: list[str] = []
        unknown_threads: list[dict[str, str]] = []
        retained_thread_ids: list[str] = []
        for thread_id in thread_ids:
            presence, reason = self._classify_thread_for_stale_binding_cleanup(thread_id)
            if presence == "stale":
                stale_thread_ids.append(thread_id)
            elif presence == "unknown":
                unknown_threads.append(
                    {
                        "thread_id": thread_id,
                        "reason": reason,
                    }
                )
            else:
                retained_thread_ids.append(thread_id)

        stale_bindings: list[ChatBindingKey] = []
        running_binding_ids: list[str] = []
        pending_binding_ids: list[str] = []
        with self._lock:
            for thread_id in stale_thread_ids:
                bound_bindings, thread_running_binding_ids, thread_pending_binding_ids = (
                    self._local_thread_binding_clear_plan_locked(thread_id)
                )
                stale_bindings.extend(bound_bindings)
                running_binding_ids.extend(thread_running_binding_ids)
                pending_binding_ids.extend(thread_pending_binding_ids)
            self._raise_local_thread_binding_clear_blockers(
                running_binding_ids,
                pending_binding_ids,
                action_text="清理 stale bindings",
            )
            existing_bindings = [
                binding
                for binding in stale_bindings
                if self._binding_runtime.binding_runtime_snapshot_locked(binding) is not None
            ]
            existing_binding_thread_ids = {
                binding: (
                    snapshot.thread_id
                    if (
                        snapshot := self._binding_runtime.binding_runtime_snapshot_locked(binding)
                    ) is not None
                    else ""
                )
                for binding in existing_bindings
            }
            if dry_run:
                return {
                    "would_clear_binding_ids": [
                        format_binding_id(binding)
                        for binding in existing_bindings
                    ],
                    "stale_thread_ids": sorted(stale_thread_ids),
                    "unknown_threads": unknown_threads,
                    "retained_thread_ids": retained_thread_ids,
                    "dry_run": True,
                }
            unsubscribe_thread_ids = self._binding_runtime.deactivate_bindings_locked(existing_bindings)
            cleared_binding_ids = [format_binding_id(binding) for binding in existing_bindings]

        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        for unsubscribe_thread_id in unique_unsubscribe_thread_ids:
            self._unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
        for thread_id in sorted(set(existing_binding_thread_ids.values()) - set(unique_unsubscribe_thread_ids)):
            if thread_id:
                self._release_service_thread_runtime_lease(thread_id)
        return {
            "cleared_binding_ids": cleared_binding_ids,
            "stale_thread_ids": sorted(stale_thread_ids),
            "unknown_threads": unknown_threads,
            "retained_thread_ids": retained_thread_ids,
            "cleared": bool(cleared_binding_ids),
        }

    def send_image_to_thread_attached_bindings(
        self,
        thread_id: str,
        *,
        local_path: str,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            attached_bindings = tuple(self.attached_bindings_for_thread_locked(normalized_thread_id))
        result = self._thread_image_delivery.deliver_local_image(
            thread_id=normalized_thread_id,
            local_path=local_path,
            attached_bindings=attached_bindings,
        )
        effective_summary = summary
        if effective_summary is None:
            resolved_summary, _backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
            effective_summary = resolved_summary
        return {
            "thread_id": result.thread_id,
            "thread_title": effective_summary.title if effective_summary is not None else "",
            "working_dir": effective_summary.cwd if effective_summary is not None else "",
            "local_path": result.local_path,
            "attached_binding_ids": [item.binding_id for item in (*result.delivered, *result.failed)],
            "delivered_binding_ids": [item.binding_id for item in result.delivered],
            "failed_binding_ids": [item.binding_id for item in result.failed],
            "delivered_message_ids": {
                item.binding_id: item.message_id
                for item in result.delivered
            },
            "fully_delivered": result.fully_delivered,
        }

    def thread_status_snapshot(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            snapshot = self._binding_runtime.thread_binding_snapshot_locked(
                normalized_thread_id,
                detach_availability=self.detach_thread_availability_locked,
            )
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        lease = self._load_thread_runtime_lease(normalized_thread_id)
        effective_summary = resolved_summary or summary
        effective_summary_title = ""
        if effective_summary is not None:
            effective_summary_title = str(effective_summary.name or effective_summary.preview or "").strip()
        effective_title = (
            effective_summary_title or str(snapshot.get("thread_title", "") or "").strip()
        )
        effective_working_dir = (
            effective_summary.cwd
            if effective_summary is not None and str(effective_summary.cwd or "").strip()
            else str(snapshot.get("working_dir", "") or "").strip()
        )
        detach_reason_code = self.detach_thread_check_locked(normalized_thread_id).reason_code
        if not snapshot["bound_binding_ids"]:
            detach_reason_code = DETACH_NOT_APPLICABLE_NO_BINDING
        return {
            "thread_id": snapshot["thread_id"],
            "thread_title": effective_title,
            "working_dir": effective_working_dir,
            "backend_thread_status": backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN,
            "backend_running_turn": backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE,
            "live_runtime_owner": self._live_runtime_owner_snapshot(lease),
            "live_runtime_holder_labels": self._live_runtime_holder_labels(lease),
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "detached_binding_ids": snapshot["detached_binding_ids"],
            "interaction_owner": snapshot["interaction_owner"],
            "detach_available": snapshot["detach_available"],
            "detach_reason_code": detach_reason_code,
            "detach_reason": snapshot["detach_reason"],
        }

    def _backend_reset_preview(self) -> BackendResetPreview:
        if self._app_server_mode() != "managed":
            return BackendResetPreview(
                status=BACKEND_RESET_STATUS_BLOCKED,
                reason_code=BACKEND_RESET_UNSUPPORTED_REMOTE,
                reason_text="当前实例是 remote app-server 模式，不拥有 backend 进程，不能执行 reset backend。",
                diagnostics=(
                    f"当前实例：`{self._instance_name()}`",
                    f"当前 backend 模式：`{self._app_server_mode() or 'unknown'}`",
                ),
            )

        pending_requests = self._list_pending_interaction_requests()
        pending_request_count = len(pending_requests)
        with self._lock:
            inventory = self.binding_inventory_locked()
        running_binding_ids = tuple(item["binding_id"] for item in inventory if item["running_turn"])
        attached_binding_ids = tuple(
            sorted(
                str(item["binding_id"] or "").strip()
                for item in inventory
                if str(item.get("binding_id") or "").strip()
                and str(item.get("binding_state") or "").strip() == "bound"
                and str(item.get("feishu_runtime_state") or "").strip() == FEISHU_RUNTIME_ATTACHED
            )
        )

        loaded_thread_ids: tuple[str, ...] = ()
        active_loaded_thread_ids: tuple[str, ...] = ()
        holder_labels: set[str] = set()
        runtime_verification_failed = False
        try:
            loaded_thread_ids = tuple(
                sorted(
                    str(thread_id or "").strip()
                    for thread_id in self._list_loaded_thread_ids()
                    if str(thread_id or "").strip()
                )
            )
            active_loaded: list[str] = []
            for thread_id in loaded_thread_ids:
                holder_labels.update(self._live_runtime_holder_labels(self._load_thread_runtime_lease(thread_id)))
                _summary, backend_status = self.read_thread_summary_for_status(thread_id)
                if backend_status in {
                    BACKEND_THREAD_LOOKUP_ERROR,
                    BACKEND_THREAD_LOOKUP_MISSING,
                    BACKEND_THREAD_STATUS_UNKNOWN,
                }:
                    runtime_verification_failed = True
                    continue
                if backend_status == BACKEND_THREAD_STATUS_ACTIVE:
                    active_loaded.append(thread_id)
            active_loaded_thread_ids = tuple(active_loaded)
        except Exception:
            logger.exception("构造 backend reset preview 时读取 loaded thread 失败")
            runtime_verification_failed = True

        common_kwargs = {
            "pending_request_count": pending_request_count,
            "running_binding_ids": running_binding_ids,
            "active_loaded_thread_ids": active_loaded_thread_ids,
            "loaded_thread_ids": loaded_thread_ids,
            "runtime_verification_failed": runtime_verification_failed,
            "blocking_holder_labels": tuple(sorted(holder_labels)),
            "attached_binding_ids": attached_binding_ids,
            "loaded_thread_preview": self._preview_thread_ids(loaded_thread_ids),
            "active_loaded_thread_preview": self._preview_thread_ids(active_loaded_thread_ids),
            "blocking_active_turn_count": len(active_loaded_thread_ids),
            "blocking_pending_request_count": pending_request_count,
            "collateral_loaded_thread_count": len(loaded_thread_ids),
            "collateral_active_loaded_thread_count": len(active_loaded_thread_ids),
        }

        if pending_request_count:
            preview = BackendResetPreview(
                status=BACKEND_RESET_STATUS_FORCE_ONLY,
                reason_code=BACKEND_RESET_FORCE_ONLY_BY_PENDING_REQUEST,
                reason_text="当前实例还有待处理审批或输入请求；如确认可打断，可执行 force reset。",
                **common_kwargs,
            )
            return replace(preview, diagnostics=self._backend_reset_flat_diagnostics(preview))
        if running_binding_ids:
            preview = BackendResetPreview(
                status=BACKEND_RESET_STATUS_FORCE_ONLY,
                reason_code=BACKEND_RESET_FORCE_ONLY_BY_RUNNING_BINDING,
                reason_text="当前实例仍有运行中的 Feishu turn；如确认可打断，可执行 force reset。",
                **common_kwargs,
            )
            return replace(preview, diagnostics=self._backend_reset_flat_diagnostics(preview))
        if active_loaded_thread_ids:
            preview = BackendResetPreview(
                status=BACKEND_RESET_STATUS_FORCE_ONLY,
                reason_code=BACKEND_RESET_FORCE_ONLY_BY_ACTIVE_LOADED_THREAD,
                reason_text="当前 backend 仍有 active thread；如确认可打断，可执行 force reset。",
                **common_kwargs,
            )
            return replace(preview, diagnostics=self._backend_reset_flat_diagnostics(preview))
        if runtime_verification_failed:
            preview = BackendResetPreview(
                status=BACKEND_RESET_STATUS_FORCE_ONLY,
                reason_code=BACKEND_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED,
                reason_text="当前无法完整确认 backend 是否仍有运行中的 thread；如确认可打断，可执行 force reset。",
                **common_kwargs,
            )
            return replace(preview, diagnostics=self._backend_reset_flat_diagnostics(preview))
        preview = BackendResetPreview(
                status=BACKEND_RESET_STATUS_AVAILABLE,
            reason_code="",
            reason_text="当前实例 backend 可安全重置。",
            **common_kwargs,
        )
        return replace(preview, diagnostics=self._backend_reset_flat_diagnostics(preview))

    def backend_reset_preview(self) -> BackendResetPreview:
        return self._backend_reset_preview()

    def handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "service/status":
            with self._lock:
                bindings = self.binding_inventory_locked()
            reset_preview = self.backend_reset_preview()
            bound_thread_ids = {item["thread_id"] for item in bindings if item["thread_id"]}
            attached_thread_ids = {
                item["thread_id"]
                for item in bindings
                if item["thread_id"] and item["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED
            }
            try:
                loaded_thread_ids = self._list_loaded_thread_ids()
            except Exception:
                logger.exception("读取 loaded thread 列表失败")
                loaded_thread_ids = []
            return {
                "instance_name": self._instance_name(),
                "pid": os.getpid(),
                "control_endpoint": self._service_control_endpoint(),
                "app_server_url": self._current_app_server_url(),
                "app_server_mode": self._app_server_mode(),
                "binding_count": len(bindings),
                "bound_binding_count": sum(1 for item in bindings if item["binding_state"] == "bound"),
                "attached_binding_count": sum(
                    1 for item in bindings if item["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED
                ),
                "thread_count": len(bound_thread_ids),
                "attached_thread_count": len(attached_thread_ids),
                "loaded_thread_count": len(loaded_thread_ids),
                "loaded_thread_ids": loaded_thread_ids,
                "running_binding_ids": [item["binding_id"] for item in bindings if item["running_turn"]],
                "backend_reset_status": reset_preview.status,
                "backend_reset_reason_code": reset_preview.reason_code,
                "backend_reset_reason": reset_preview.reason_text,
            }
        if method == "service/reset-backend":
            force = bool(params.get("force"))
            return self._reset_current_instance_backend(force)
        if method == "service/attach":
            return self.attach_service()
        if method == "binding/list":
            return self.binding_list_response(refresh_names=bool(params.get("refresh_names")))
        if method == "binding/status":
            binding_id = str(params.get("binding_id", "") or "").strip()
            binding = parse_binding_id(binding_id)
            return self.binding_status_snapshot(binding)
        if method == "binding/attach":
            binding_id = str(params.get("binding_id", "") or "").strip()
            if not binding_id:
                raise ValueError(f"{method} 缺少 binding_id。")
            binding = parse_binding_id(binding_id)
            return self.attach_binding(binding)
        if method == "binding/submit-prompt":
            binding_id = str(params.get("binding_id", "") or "").strip()
            if not binding_id:
                raise ValueError("binding/submit-prompt 缺少 binding_id。")
            binding = parse_binding_id(binding_id)
            raw_input_items = params.get("input_items")
            input_items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None
            if raw_input_items is not None:
                if not isinstance(raw_input_items, list):
                    raise ValueError("binding/submit-prompt 的 input_items 必须是数组。")
                input_items = []
                for item in raw_input_items:
                    if not isinstance(item, dict):
                        raise ValueError("binding/submit-prompt 的 input_items 元素必须是对象。")
                    input_items.append(dict(item))
            return self.submit_binding_prompt_for_control(
                binding,
                text=str(params.get("text", "") or ""),
                actor_open_id=str(params.get("actor_open_id", "") or ""),
                input_items=input_items,
                synthetic_source=str(params.get("synthetic_source", "") or ""),
                display_mode=str(params.get("display_mode", "silent") or "silent"),
            )
        if method == "binding/detach":
            binding_id = str(params.get("binding_id", "") or "").strip()
            if not binding_id:
                raise ValueError("binding/detach 缺少 binding_id。")
            binding = parse_binding_id(binding_id)
            return self.detach_binding(binding)
        if method == "binding/clear":
            binding_id = str(params.get("binding_id", "") or "").strip()
            if not binding_id:
                raise ValueError("binding/clear 缺少 binding_id。")
            binding = parse_binding_id(binding_id)
            return self.clear_binding_for_control(binding)
        if method == "binding/clear-all":
            return self.clear_all_bindings_for_control()
        if method == "binding/clear-stale":
            return self.clear_stale_bindings_for_control(dry_run=bool(params.get("dry_run")))
        if method == "thread/clear-archived-bindings":
            thread_id = str(params.get("thread_id", "") or "").strip()
            if not thread_id:
                raise ValueError("thread/clear-archived-bindings 缺少 thread_id。")
            return self.clear_archived_thread_bindings_for_control(
                thread_id,
                dry_run=bool(params.get("dry_run")),
            )
        if method in {
            "thread/status",
            "thread/bindings",
            "thread/goal",
            "thread/goal/set",
            "thread/goal/clear",
            "thread/detach",
            "thread/send-image",
            "thread/attach",
            "thread/archive",
        }:
            thread_id = str(params.get("thread_id", "") or "").strip()
            thread_name = str(params.get("thread_name", "") or "").strip()
            if method in {"thread/status", "thread/bindings"} and thread_id and not thread_name:
                thread = ThreadSummary(
                    thread_id=thread_id,
                    cwd="",
                    name="",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="appServer",
                    status=BACKEND_THREAD_STATUS_UNKNOWN,
                )
            else:
                thread = self._resolve_thread_target_for_control_params(params)
            if method == "thread/status":
                return self.thread_status_snapshot(thread.thread_id, summary=thread)
            if method == "thread/bindings":
                snapshot = self.thread_status_snapshot(thread.thread_id, summary=thread)
                return {
                    "thread_id": snapshot["thread_id"],
                    "thread_title": snapshot["thread_title"],
                    "working_dir": snapshot["working_dir"],
                    "bindings": [
                        {
                            "binding_id": binding_id,
                            "feishu_runtime_state": (
                                FEISHU_RUNTIME_ATTACHED
                                if binding_id in set(snapshot["attached_binding_ids"])
                                else FEISHU_RUNTIME_DETACHED
                            ),
                        }
                        for binding_id in snapshot["bound_binding_ids"]
                    ],
                }
            if method == "thread/goal":
                return self.thread_goal_snapshot(thread.thread_id, summary=thread)
            if method == "thread/goal/set":
                objective = params.get("objective")
                status = params.get("status")
                return self.set_thread_goal_for_control(
                    thread.thread_id,
                    summary=thread,
                    objective=None if objective is None else str(objective),
                    status=None if status is None else str(status),
                )
            if method == "thread/goal/clear":
                return self.clear_thread_goal_for_control(thread.thread_id, summary=thread)
            if method == "thread/send-image":
                local_path = str(params.get("local_path", "") or "").strip()
                if not local_path:
                    raise ValueError("thread/send-image 缺少 local_path。")
                return self.send_image_to_thread_attached_bindings(
                    thread.thread_id,
                    local_path=local_path,
                    summary=thread,
                )
            if method == "thread/attach":
                return self.attach_thread(thread.thread_id)
            if method == "thread/archive":
                return self.archive_thread_for_control(thread.thread_id, summary=thread)
            return self.detach_thread(thread.thread_id)
        raise ValueError(f"未知控制面方法：{method}")
