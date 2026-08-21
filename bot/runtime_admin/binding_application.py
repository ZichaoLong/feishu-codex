"""Surface-neutral Runtime Admin binding application owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeAlias

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.binding_identity import format_binding_id
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import CodexRpcError
from bot.direct_thread_target_policy import (
    DirectThreadTargetPolicyError,
    read_direct_thread_target,
)
from bot.goal_continuation_policy import is_reviewed_non_continuing_goal_status
from bot.reason_codes import (
    BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND,
    BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN,
    BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST,
    DETACH_BLOCKED_BY_INFLIGHT_TURN,
    DETACH_BLOCKED_BY_PENDING_REQUEST,
    DETACH_NOT_APPLICABLE_ALREADY_DETACHED,
    DETACH_NOT_APPLICABLE_NO_BINDING,
    DETACH_NOT_APPLICABLE_NO_THREAD,
    PROMPT_DENIED_BINDING_NOT_FOUND,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    PROMPT_DENIED_BY_RUNNING_TURN,
    ReasonedCheck,
)
from bot.runtime_admin.binding_clear import (
    RuntimeBindingBatchDeactivationReceipt,
    RuntimeBindingClearPorts,
    RuntimeBindingClearService,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    LOADED_BACKEND_THREAD_STATUSES,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.thread_lifecycle_service import (
    ThreadLifecyclePolicyError,
    ThreadLifecycleService,
)

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]


class RuntimeAdminAttachBinding(Protocol):
    def __call__(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        *,
        active_observer: bool = False,
    ) -> ThreadSummary: ...


@dataclass(frozen=True, slots=True)
class RuntimeAdminBindingApplicationPorts:
    read_thread: Callable[[str], Any]
    read_thread_for_stale_cleanup: Callable[[str], Any]
    unsubscribe_thread: Callable[[str], None]
    attach_binding: RuntimeAdminAttachBinding
    get_thread_goal: Callable[[str], ThreadGoalSummary | None]
    clear_all_stored_bindings: Callable[[], None]
    deactivate_binding_and_invalidate_queue_locked: Callable[
        [ChatBindingKey], RuntimeBindingBatchDeactivationReceipt
    ]
    deactivate_bindings_and_invalidate_queues_locked: Callable[
        [tuple[ChatBindingKey, ...], list[str]],
        RuntimeBindingBatchDeactivationReceipt,
    ]
    release_service_thread_runtime_lease: Callable[[str], None]
    instance_name: Callable[[], str]
    load_thread_runtime_lease: Callable[[str], ThreadRuntimeLease | None]
    submit_prompt_for_control: Callable[..., dict[str, Any]]
    prompt_write_denial_check: Callable[..., ReasonedCheck]
    external_control_write_denial_check: Callable[..., ReasonedCheck]
    all_mode_thread_exclusivity_check: Callable[
        [str, str], ReasonedCheck
    ]
    detached_runtime_attach_check: Callable[[str], ReasonedCheck]
    resolve_binding_chat_display_name: Callable[..., str]
    is_thread_not_found_error: Callable[[Exception], bool]
    is_thread_not_loaded_error: Callable[[Exception], bool]
    invalidate_feishu_execution_queue_locked: Callable[[ChatBindingKey], None]
    invalidate_all_feishu_execution_queues_locked: Callable[[], int]
    operational_status: Callable[[], dict[str, Any]]
    thread_has_pending_request_locked: Callable[[str], bool]
    binding_has_pending_request_locked: Callable[[ChatBindingKey], bool]


class RuntimeAdminBindingApplication:
    """Own Runtime Admin binding queries and complete application transactions.

    Binding facts remain in ``BindingRuntimeManager``. Root lifecycle facts and
    clear settlement remain in their existing owners; this class owns only the
    application ordering that spans those owners.
    """

    def __init__(
        self,
        *,
        lock: Any,
        binding_runtime: BindingRuntimeManager,
        thread_lifecycle: ThreadLifecycleService,
        ports: RuntimeAdminBindingApplicationPorts,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._thread_lifecycle = thread_lifecycle

        self._read_thread = ports.read_thread
        self._read_thread_for_stale_cleanup = ports.read_thread_for_stale_cleanup
        self._unsubscribe_thread = ports.unsubscribe_thread
        self._attach_binding = ports.attach_binding
        self._get_thread_goal = ports.get_thread_goal
        self._release_service_thread_runtime_lease = (
            ports.release_service_thread_runtime_lease
        )
        self._instance_name = ports.instance_name
        self._load_thread_runtime_lease = ports.load_thread_runtime_lease
        self._submit_prompt_for_control = ports.submit_prompt_for_control
        self._prompt_write_denial_check = ports.prompt_write_denial_check
        self._external_control_write_denial_check = (
            ports.external_control_write_denial_check
        )
        self._all_mode_thread_exclusivity_check = (
            ports.all_mode_thread_exclusivity_check
        )
        self._detached_runtime_attach_check = ports.detached_runtime_attach_check
        self._resolve_binding_chat_display_name = (
            ports.resolve_binding_chat_display_name
        )
        self._is_thread_not_found_error = ports.is_thread_not_found_error
        self._is_thread_not_loaded_error = ports.is_thread_not_loaded_error
        self._invalidate_feishu_execution_queue_locked = (
            ports.invalidate_feishu_execution_queue_locked
        )
        self._operational_status = ports.operational_status
        self._thread_has_pending_request_locked = (
            ports.thread_has_pending_request_locked
        )
        self._binding_has_pending_request_locked = (
            ports.binding_has_pending_request_locked
        )
        self._binding_clear = RuntimeBindingClearService(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            ports=RuntimeBindingClearPorts(
                binding_clear_availability_locked=(
                    self.binding_clear_availability_locked
                ),
                require_direct_thread_target=self._require_direct_thread_target,
                deactivate_binding_and_invalidate_queue_locked=(
                    ports.deactivate_binding_and_invalidate_queue_locked
                ),
                deactivate_bindings_and_invalidate_queues_locked=(
                    ports.deactivate_bindings_and_invalidate_queues_locked
                ),
                clear_all_stored_bindings=ports.clear_all_stored_bindings,
                invalidate_all_execution_queues_locked=(
                    ports.invalidate_all_feishu_execution_queues_locked
                ),
                finalize_deactivated_thread_runtime=(
                    self._finalize_deactivated_thread_runtime
                ),
                raise_binding_cleanup_errors=self._raise_binding_cleanup_errors,
            ),
        )

    def binding_inventory_locked(self) -> list[dict[str, Any]]:
        return self._binding_runtime.binding_inventory_locked()

    def binding_inventory_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return self.binding_inventory_locked()

    def binding_list_snapshot(
        self,
        *,
        refresh_names: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            bindings = [dict(item) for item in self.binding_inventory_locked()]
        return self._enrich_binding_list_snapshot(
            bindings,
            refresh_names=refresh_names,
        )

    def binding_list_response(
        self,
        *,
        refresh_names: bool = False,
    ) -> dict[str, Any]:
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
                    thread_name_by_id[thread_id] = self._binding_list_thread_name(
                        thread_id
                    )
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
                chat_name_by_key[chat_key] = (
                    self._binding_list_chat_display_name(
                        binding_kind=binding_kind,
                        sender_id=sender_id,
                        chat_id=chat_id,
                        refresh_names=refresh_names,
                    )
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
            logger.debug(
                "binding list 读取 thread name 失败: thread=%s, error=%s",
                thread_id[:12],
                exc,
            )
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
                "binding list 读取 chat display name 失败: "
                "kind=%s chat=%s sender=%s error=%s",
                binding_kind,
                chat_id[:12],
                sender_id[:12],
                exc,
            )
            return ""

    def bound_bindings_for_thread_locked(
        self,
        thread_id: str,
    ) -> list[ChatBindingKey]:
        return self._binding_runtime.bound_bindings_for_thread_locked(thread_id)

    def attached_bindings_for_thread_locked(
        self,
        thread_id: str,
    ) -> list[ChatBindingKey]:
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

    def effective_binding_key(
        self,
        sender_id: str,
        chat_id: str,
    ) -> ChatBindingKey:
        with self._lock:
            existing = self._binding_runtime.existing_chat_binding_key_locked(
                sender_id,
                chat_id,
            )
        if existing is not None:
            return existing
        return (sender_id, chat_id)

    def _require_direct_thread_target(
        self,
        thread_id: str,
        *,
        operation: str,
    ) -> ThreadSummary:
        normalized_thread_id = str(thread_id or "").strip()
        try:
            return read_direct_thread_target(
                normalized_thread_id,
                read_thread=self._read_thread,
                operation=operation,
            )
        except DirectThreadTargetPolicyError as exc:
            raise ThreadLifecyclePolicyError(str(exc)) from exc
        except Exception as exc:
            raise ThreadLifecyclePolicyError(
                "无法确认 root thread 是否可直接操作；已按 fail-closed 拒绝"
                f" {operation}。"
            ) from exc

    def _classify_thread_for_stale_binding_cleanup(
        self,
        thread_id: str,
    ) -> tuple[str, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return "skip", "empty_thread_id"
        try:
            self._read_thread_for_stale_cleanup(normalized_thread_id)
        except Exception as exc:
            if self._is_thread_not_found_error(
                exc
            ) or self._is_thread_not_loaded_error(exc):
                return "stale", str(exc) or "thread is not readable"
            logger.exception(
                "验证 stale binding 线程失败: thread=%s",
                normalized_thread_id[:12],
            )
            return "unknown", str(exc) or type(exc).__name__
        return "present", ""

    @staticmethod
    def live_runtime_owner_snapshot(
        lease: ThreadRuntimeLease | None,
    ) -> dict[str, str]:
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
    def live_runtime_holder_labels(
        lease: ThreadRuntimeLease | None,
    ) -> list[str]:
        if lease is None:
            return []
        labels: list[str] = []
        for holder in lease.holders:
            holder_type = str(holder.holder_type or "").strip() or "unknown"
            instance_name = (
                str(holder.instance_name or "").strip() or "unknown"
            )
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
        attached_bindings = self.attached_bindings_for_thread_locked(
            normalized_thread_id
        )
        if not attached_bindings:
            return ReasonedCheck.deny(
                DETACH_NOT_APPLICABLE_ALREADY_DETACHED,
                "当前 thread 的飞书推送原本就已是 `detached`。",
            )
        for binding in attached_bindings:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(
                binding
            )
            if snapshot is not None and snapshot.has_inflight_turn:
                return ReasonedCheck.deny(
                    DETACH_BLOCKED_BY_INFLIGHT_TURN,
                    "当前有飞书侧 turn 正在运行，不能 detach 当前 thread。",
                )
        if self._thread_has_pending_request_locked(normalized_thread_id):
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
        if self._binding_has_pending_request_locked(binding):
            return ReasonedCheck.deny(
                DETACH_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能 detach 当前会话。",
            )
        return ReasonedCheck.allow()

    def detach_thread_availability_locked(
        self,
        thread_id: str,
    ) -> tuple[bool, str]:
        check = self.detach_thread_check_locked(thread_id)
        return check.allowed, check.reason_text

    def preview_detach_thread_locked(self, thread_id: str) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        if not self.bound_bindings_for_thread_locked(normalized_thread_id):
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        check = self.detach_thread_check_locked(normalized_thread_id)
        if (
            not check.allowed
            and check.reason_code != DETACH_NOT_APPLICABLE_ALREADY_DETACHED
        ):
            raise ValueError(check.reason_text)
        return bool(
            self.attached_bindings_for_thread_locked(normalized_thread_id)
        )

    def binding_clear_check_locked(
        self,
        binding: ChatBindingKey,
    ) -> ReasonedCheck:
        record = self._binding_runtime.binding_record_snapshot_locked(binding)
        if record is None:
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND,
                f"未找到绑定：{format_binding_id(binding)}",
            )
        if record.has_inflight_turn:
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN,
                "当前有飞书侧 turn 正在运行，不能清除 binding。",
            )
        if self._binding_has_pending_request_locked(binding):
            return ReasonedCheck.deny(
                BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能清除 binding。",
            )
        return ReasonedCheck.allow()

    def binding_clear_availability_locked(
        self,
        binding: ChatBindingKey,
    ) -> tuple[bool, str]:
        check = self.binding_clear_check_locked(binding)
        return check.allowed, check.reason_text

    def binding_prompt_check(self, binding: ChatBindingKey) -> ReasonedCheck:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(
                binding
            )
        return self._binding_prompt_check_from_snapshot(binding, snapshot)

    def binding_prompt_check_locked(
        self,
        binding: ChatBindingKey,
    ) -> ReasonedCheck:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        return self._binding_prompt_check_from_snapshot(binding, snapshot)

    def submit_binding_prompt_for_control(
        self,
        binding: ChatBindingKey,
        *,
        text: str,
        actor_open_id: str = "",
        input_items: list[dict[str, Any]]
        | tuple[dict[str, Any], ...]
        | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
    ) -> dict[str, Any]:
        prompt_text = str(text or "").strip()
        normalized_input_items = list(input_items or [])
        if not prompt_text and not normalized_input_items:
            raise ValueError(
                "binding/submit-prompt 需要 `text` 或 `input_items`。"
            )
        normalized_display_mode = (
            str(display_mode or "silent").strip().lower() or "silent"
        )
        if normalized_display_mode not in {"silent", "announce"}:
            raise ValueError(
                "binding/submit-prompt 的 display_mode 只支持 "
                "`silent` 或 `announce`。"
            )
        check = self.binding_prompt_check(binding)
        if (
            not check.allowed
            and check.reason_code != PROMPT_DENIED_BY_RUNNING_TURN
        ):
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
        if not thread_id:
            if has_inflight_turn:
                return ReasonedCheck.deny(
                    PROMPT_DENIED_BY_RUNNING_TURN,
                    "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
                )
            return ReasonedCheck.allow()
        denial = self._prompt_write_denial_check(
            binding,
            binding[1],
            thread_id,
            message_id="",
        )
        if not denial.allowed:
            return denial
        if has_inflight_turn:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_RUNNING_TURN,
                "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            )
        if feishu_runtime_state == FEISHU_RUNTIME_DETACHED:
            return self._detached_runtime_attach_check(thread_id)
        return ReasonedCheck.allow()

    def clear_binding_for_control(
        self,
        binding: ChatBindingKey,
    ) -> dict[str, Any]:
        return self._binding_clear.clear_one(binding)

    def clear_all_bindings_for_control(self) -> dict[str, Any]:
        return self._binding_clear.clear_all()

    def _finalize_deactivated_thread_runtime(
        self,
        unsubscribe_thread_ids: list[str] | tuple[str, ...],
        *,
        release_only_thread_ids: set[str] | None = None,
        cleanup_errors: list[str],
    ) -> None:
        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        for thread_id in unique_unsubscribe_thread_ids:
            try:
                self._unsubscribe_thread(thread_id)
            except Exception as exc:
                cleanup_errors.append(
                    f"取消 thread 订阅失败: {thread_id}: {exc}"
                )
                continue
            try:
                self._release_service_thread_runtime_lease(thread_id)
            except Exception as exc:
                cleanup_errors.append(
                    f"释放 runtime lease 失败: {thread_id}: {exc}"
                )
        release_only = set(release_only_thread_ids or ()) - set(
            unique_unsubscribe_thread_ids
        )
        for thread_id in sorted(release_only):
            try:
                self._release_service_thread_runtime_lease(thread_id)
            except Exception as exc:
                cleanup_errors.append(
                    f"释放 runtime lease 失败: {thread_id}: {exc}"
                )

    @staticmethod
    def _raise_binding_cleanup_errors(cleanup_errors: list[str]) -> None:
        if cleanup_errors:
            raise RuntimeError(
                "binding 清理不完整：" + "；".join(cleanup_errors)
            )

    def binding_status_snapshot(
        self,
        binding: ChatBindingKey,
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = (
                self._binding_runtime.binding_status_state_snapshot_locked(
                    binding
                )
            )
            detach_check = self.detach_check_locked(binding)
        prompt_check = self._binding_prompt_check_from_snapshot(
            binding,
            snapshot,
        )
        thread_id = str(snapshot["thread_id"] or "").strip()
        summary, backend_thread_status = (
            self._thread_lifecycle.read_thread_summary_for_status(thread_id)
        )
        lease = self._load_thread_runtime_lease(thread_id)
        if summary is not None:
            snapshot["thread_title"] = summary.title or str(
                snapshot["thread_title"] or ""
            ).strip()
            snapshot["working_dir"] = summary.cwd or str(
                snapshot["working_dir"] or ""
            ).strip()
        snapshot["backend_thread_status"] = (
            backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
        )
        snapshot["backend_running_turn"] = (
            backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
        )
        snapshot["live_runtime_owner"] = self.live_runtime_owner_snapshot(lease)
        snapshot["live_runtime_holder_labels"] = (
            self.live_runtime_holder_labels(lease)
        )
        snapshot["detach_available"] = bool(
            thread_id and detach_check.allowed
        )
        snapshot["detach_reason_code"] = detach_check.reason_code
        snapshot["detach_reason"] = detach_check.reason_text
        snapshot["next_prompt_allowed"] = prompt_check.allowed
        snapshot["next_prompt_reason_code"] = prompt_check.reason_code
        snapshot["next_prompt_reason"] = prompt_check.reason_text
        snapshot["operator_status"] = self._operational_status()
        return snapshot

    def binding_thread_id_or_raise(self, binding: ChatBindingKey) -> str:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(
                binding
            )
        if snapshot is None:
            raise ValueError(
                f"未找到 binding：{format_binding_id(binding)}"
            )
        thread_id = str(snapshot.thread_id or "").strip()
        if not thread_id:
            raise ValueError("当前 binding 没有绑定 thread。")
        return thread_id

    def detach_binding(self, binding: ChatBindingKey) -> dict[str, Any]:
        with self._lock:
            initial_snapshot = (
                self._binding_runtime.binding_runtime_snapshot_locked(binding)
            )
        if initial_snapshot is None:
            raise ValueError(
                f"当前 binding 不存在：{format_binding_id(binding)}"
            )
        initial_thread_id = str(initial_snapshot.thread_id or "").strip()
        if initial_thread_id:
            self._require_direct_thread_target(
                initial_thread_id,
                operation="detach 飞书 binding",
            )
        with self._lock:
            check = self.detach_check_locked(binding)
            if (
                not check.allowed
                and check.reason_code
                != DETACH_NOT_APPLICABLE_ALREADY_DETACHED
            ):
                raise ValueError(check.reason_text)
            current_snapshot = (
                self._binding_runtime.binding_runtime_snapshot_locked(binding)
            )
            if (
                current_snapshot is None
                or str(current_snapshot.thread_id or "").strip()
                != initial_thread_id
            ):
                raise ValueError(
                    "binding 在线程核验期间发生变化；请重试。"
                )
            result = self._binding_runtime.detach_binding_locked(binding)
            self._invalidate_feishu_execution_queue_locked(binding)
        cancel_runtime_timer_effects(result.timer_cancellations)
        if result.unsubscribe_thread_id:
            self._unsubscribe_thread(result.unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(
                result.unsubscribe_thread_id
            )
        resolved_summary, backend_thread_status = (
            self._thread_lifecycle.read_thread_summary_for_status(
                result.thread_id
            )
        )
        thread_title = str(
            resolved_summary.title
            if resolved_summary is not None
            else result.thread_title or ""
        ).strip()
        working_dir = str(
            resolved_summary.cwd
            if resolved_summary is not None
            else result.working_dir or ""
        ).strip()
        return {
            "binding_id": result.binding_id,
            "thread_id": result.thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "changed": result.changed,
            "already_detached": result.already_detached,
            "backend_thread_status": (
                backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
            ),
            "backend_still_loaded": (
                backend_thread_status in LOADED_BACKEND_THREAD_STATUSES
            ),
        }

    def _attach_writer_admission_check(
        self,
        thread_id: str,
        *,
        writer_binding: ChatBindingKey | None,
    ) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        if writer_binding is None:
            admission = self._external_control_write_denial_check(
                normalized_thread_id
            )
            if not admission.allowed:
                return admission
        else:
            admission = self._prompt_write_denial_check(
                writer_binding,
                writer_binding[1],
                normalized_thread_id,
                message_id="",
            )
            if not admission.allowed:
                return admission
        return self._attach_goal_resume_admission_check(
            normalized_thread_id,
            writer_binding=writer_binding,
        )

    @staticmethod
    def _goals_feature_is_disabled_error(exc: Exception) -> bool:
        return (
            isinstance(exc, CodexRpcError)
            and str(exc.error.get("message", "") or "").strip().lower()
            == "goals feature is disabled"
        )

    def _attach_goal_resume_admission_check(
        self,
        thread_id: str,
        *,
        writer_binding: ChatBindingKey | None,
    ) -> ReasonedCheck:
        try:
            goal = self._get_thread_goal(thread_id)
        except Exception as exc:
            if self._goals_feature_is_disabled_error(exc):
                return ReasonedCheck.allow()
            return self._active_or_unknown_goal_attach_check(
                writer_binding=writer_binding,
                active=False,
            )
        if goal is None:
            return ReasonedCheck.allow()

        status = str(goal.status or "").strip()
        if is_reviewed_non_continuing_goal_status(status):
            return ReasonedCheck.allow()
        return self._active_or_unknown_goal_attach_check(
            writer_binding=writer_binding,
            active=status == "active",
        )

    @staticmethod
    def _active_or_unknown_goal_attach_check(
        *,
        writer_binding: ChatBindingKey | None,
        active: bool,
    ) -> ReasonedCheck:
        if writer_binding is not None:
            return ReasonedCheck.allow()
        if active:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_INTERACTION_OWNER,
                "该 thread 的 persisted goal 为 active；本地控制面没有 writer，"
                "不能通过 attach / resume 启动无人可处理交互的 main turn。",
            )
        return ReasonedCheck.deny(
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            "无法确认该 thread 的 persisted goal 是否会继续执行；"
            "本地控制面不能通过 attach / resume 冒险启动无人可处理交互的 main turn。",
        )

    def attach_binding(
        self,
        binding: ChatBindingKey,
        *,
        writer_binding: ChatBindingKey | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(
                binding
            )
        if snapshot is None:
            raise ValueError(
                f"未找到 binding：{format_binding_id(binding)}"
            )
        thread_id = str(snapshot.thread_id or "").strip()
        if not thread_id:
            raise ValueError("当前 binding 没有绑定 thread。")
        verified = self._require_direct_thread_target(
            thread_id,
            operation="attach 飞书 binding",
        )
        active_observer = (
            verified.status == BACKEND_THREAD_STATUS_ACTIVE
        )
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
        if active_observer and writer_binding is not None:
            exclusivity = self._all_mode_thread_exclusivity_check(
                writer_binding[1],
                thread_id,
            )
            if not exclusivity.allowed:
                raise ThreadLifecyclePolicyError(
                    exclusivity.reason_text
                    or "当前群聊模式不允许共享该 thread。"
                )
        if not active_observer:
            admission = self._attach_writer_admission_check(
                thread_id,
                writer_binding=writer_binding,
            )
            if not admission.allowed:
                raise ThreadLifecyclePolicyError(
                    admission.reason_text
                    or "当前 main-turn 准入不允许恢复飞书订阅。"
                )
        check = self._detached_runtime_attach_check(thread_id)
        if not check.allowed:
            raise ValueError(check.reason_text)
        effect_check = self._detached_runtime_attach_check(thread_id)
        if not effect_check.allowed:
            raise ValueError(effect_check.reason_text)
        summary = (
            self._attach_binding(
                binding,
                thread_id,
                active_observer=True,
            )
            if active_observer
            else self._attach_binding(binding, thread_id)
        )
        active_observer_attached = (
            active_observer
            and summary.status == BACKEND_THREAD_STATUS_ACTIVE
        )
        return {
            "binding_id": binding_id,
            "thread_id": thread_id,
            "thread_title": str(
                summary.title or snapshot.thread_title or ""
            ).strip(),
            "working_dir": str(
                summary.cwd or snapshot.working_dir or ""
            ).strip(),
            "changed": True,
            "already_attached": False,
            "active_observer": active_observer_attached,
        }

    def attach_thread(
        self,
        thread_id: str,
        *,
        writer_binding: ChatBindingKey | None = None,
    ) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        verified = self._require_direct_thread_target(
            normalized_thread_id,
            operation="attach 飞书 thread",
        )
        active_observer = (
            verified.status == BACKEND_THREAD_STATUS_ACTIVE
        )
        with self._lock:
            bound_bindings = self.bound_bindings_for_thread_locked(
                normalized_thread_id
            )
            attached_bindings = set(
                self.attached_bindings_for_thread_locked(normalized_thread_id)
            )
        if not bound_bindings:
            raise ValueError("当前没有 Feishu 绑定指向该线程。")
        needs_resume = any(
            binding not in attached_bindings for binding in bound_bindings
        )
        if needs_resume and not active_observer:
            admission = self._attach_writer_admission_check(
                normalized_thread_id,
                writer_binding=writer_binding,
            )
            if not admission.allowed:
                raise ThreadLifecyclePolicyError(
                    admission.reason_text
                    or "当前 main-turn 准入不允许恢复飞书订阅。"
                )
            attach_check = self._detached_runtime_attach_check(
                normalized_thread_id
            )
            if not attach_check.allowed:
                raise ValueError(attach_check.reason_text)
        attached_binding_ids: list[str] = []
        already_attached_binding_ids: list[str] = []
        active_observer_binding_ids: list[str] = []
        effective_title = ""
        effective_working_dir = ""
        for binding in bound_bindings:
            binding_id = format_binding_id(binding)
            if binding in attached_bindings:
                already_attached_binding_ids.append(binding_id)
                continue
            result = self.attach_binding(
                binding,
                writer_binding=writer_binding,
            )
            attached_binding_ids.append(binding_id)
            if bool(result.get("active_observer")):
                active_observer_binding_ids.append(binding_id)
            effective_title = (
                str(result.get("thread_title", "") or "").strip()
                or effective_title
            )
            effective_working_dir = (
                str(result.get("working_dir", "") or "").strip()
                or effective_working_dir
            )
        if not effective_title or not effective_working_dir:
            summary, _backend_status = (
                self._thread_lifecycle.read_thread_summary_for_status(
                    normalized_thread_id
                )
            )
            if summary is not None:
                effective_title = (
                    str(summary.title or "").strip() or effective_title
                )
                effective_working_dir = (
                    str(summary.cwd or "").strip() or effective_working_dir
                )
        return {
            "thread_id": normalized_thread_id,
            "thread_title": effective_title,
            "working_dir": effective_working_dir,
            "attached_binding_ids": attached_binding_ids,
            "already_attached_binding_ids": already_attached_binding_ids,
            "active_observer_binding_ids": active_observer_binding_ids,
            "changed": bool(attached_binding_ids),
        }

    def attach_service(
        self,
        *,
        writer_binding: ChatBindingKey | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            inventory = self.binding_inventory_locked()
        detached_by_thread: dict[str, list[str]] = {}
        for item in inventory:
            if (
                item["binding_state"] != "bound"
                or item["feishu_runtime_state"]
                != FEISHU_RUNTIME_DETACHED
            ):
                continue
            thread_id = str(item["thread_id"] or "").strip()
            binding_id = str(item["binding_id"] or "").strip()
            if not thread_id or not binding_id:
                continue
            detached_by_thread.setdefault(thread_id, []).append(binding_id)

        attached_binding_ids: list[str] = []
        attached_thread_ids: list[str] = []
        active_observer_binding_ids: list[str] = []
        active_observer_thread_ids: list[str] = []
        already_attached_thread_ids: list[str] = []
        blocked_threads: list[dict[str, Any]] = []
        for thread_id in sorted(detached_by_thread):
            try:
                result = self.attach_thread(
                    thread_id,
                    writer_binding=writer_binding,
                )
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
                if result.get("active_observer_binding_ids"):
                    active_observer_thread_ids.append(thread_id)
                    active_observer_binding_ids.extend(
                        result["active_observer_binding_ids"]
                    )
            else:
                already_attached_thread_ids.append(thread_id)
        return {
            "instance_name": self._instance_name(),
            "attached_binding_ids": sorted(set(attached_binding_ids)),
            "attached_thread_ids": sorted(set(attached_thread_ids)),
            "active_observer_binding_ids": sorted(
                set(active_observer_binding_ids)
            ),
            "active_observer_thread_ids": sorted(
                set(active_observer_thread_ids)
            ),
            "already_attached_thread_ids": sorted(
                set(already_attached_thread_ids)
            ),
            "blocked_threads": blocked_threads,
        }

    def fail_close_service_attached_runtime(self) -> dict[str, Any]:
        detached_binding_ids: list[str] = []
        detached_thread_ids: list[str] = []
        release_thread_ids: list[str] = []
        timer_cancellations = []
        with self._lock:
            attached_thread_ids = sorted(
                {
                    snapshot.thread_id
                    for binding in self._binding_runtime.binding_keys_locked()
                    for snapshot in [
                        self._binding_runtime.binding_runtime_snapshot_locked(
                            binding
                        )
                    ]
                    if snapshot is not None
                    and snapshot.thread_id
                    and snapshot.feishu_runtime_state
                    == FEISHU_RUNTIME_ATTACHED
                }
            )
            for thread_id in attached_thread_ids:
                attached_bindings = (
                    self._binding_runtime.attached_bindings_for_thread_locked(
                        thread_id
                    )
                )
                result = self._binding_runtime.detach_thread_bindings_locked(
                    thread_id,
                    detach_availability=lambda _thread_id: (True, ""),
                )
                timer_cancellations.extend(result.timer_cancellations)
                if result.detached_binding_ids:
                    for binding in attached_bindings:
                        self._invalidate_feishu_execution_queue_locked(binding)
                    detached_binding_ids.extend(result.detached_binding_ids)
                    detached_thread_ids.append(thread_id)
                if result.unsubscribe_thread_id:
                    release_thread_ids.append(result.unsubscribe_thread_id)
        cancel_runtime_timer_effects(tuple(timer_cancellations))
        for thread_id in sorted(set(release_thread_ids)):
            self._release_service_thread_runtime_lease(thread_id)
        return {
            "detached_binding_ids": sorted(set(detached_binding_ids)),
            "detached_thread_ids": sorted(set(detached_thread_ids)),
            "released_thread_ids": sorted(set(release_thread_ids)),
        }

    def detach_thread(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        self._require_direct_thread_target(
            normalized_thread_id,
            operation="detach 飞书 thread",
        )
        with self._lock:
            owner_loss_receipt = (
                self._binding_runtime.preflight_detach_thread_bindings_locked(
                    normalized_thread_id,
                    detach_availability=(
                        self.detach_thread_availability_locked
                    ),
                )
            )
        if owner_loss_receipt is not None:
            try:
                self._unsubscribe_thread(normalized_thread_id)
            except Exception:
                self._binding_runtime.discard_detach_owner_loss_receipt(
                    owner_loss_receipt
                )
                raise
        with self._lock:
            attached_bindings = (
                self._binding_runtime.attached_bindings_for_thread_locked(
                    normalized_thread_id
                )
            )
            result = self._binding_runtime.detach_thread_bindings_locked(
                normalized_thread_id,
                detach_availability=self.detach_thread_availability_locked,
                owner_loss_receipt=owner_loss_receipt,
            )
            if result.detached_binding_ids:
                for binding in attached_bindings:
                    self._invalidate_feishu_execution_queue_locked(binding)
        cancel_runtime_timer_effects(result.timer_cancellations)
        if result.unsubscribe_thread_id:
            self._release_service_thread_runtime_lease(
                result.unsubscribe_thread_id
            )
        resolved_summary, backend_thread_status = (
            self._thread_lifecycle.read_thread_summary_for_status(
                normalized_thread_id
            )
        )
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
            "backend_thread_status": (
                backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
            ),
            "backend_still_loaded": (
                backend_thread_status in LOADED_BACKEND_THREAD_STATUSES
            ),
            "detach_reason_code": (
                "" if result.changed else detach_check.reason_code
            ),
        }

    def clear_stale_bindings_for_control(
        self,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            records = [
                record
                for record in (
                    self._binding_runtime.binding_record_inventory_locked()
                )
                if record.thread_id
            ]
        thread_ids = sorted({record.thread_id for record in records})
        stale_thread_ids: list[str] = []
        unknown_threads: list[dict[str, str]] = []
        retained_thread_ids: list[str] = []
        for thread_id in thread_ids:
            presence, reason = (
                self._classify_thread_for_stale_binding_cleanup(thread_id)
            )
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
        planned_bindings_by_thread: dict[
            str,
            tuple[ChatBindingKey, ...],
        ] = {}
        with self._lock:
            for thread_id in stale_thread_ids:
                plan = self._thread_lifecycle.local_binding_clear_plan_locked(
                    thread_id
                )
                self._thread_lifecycle.require_binding_cleanup_allowed(
                    plan,
                    action_text="清理 stale bindings",
                )
                planned_bindings_by_thread[thread_id] = tuple(plan.bindings)
                stale_bindings.extend(plan.bindings)
            existing_bindings = [
                binding
                for binding in stale_bindings
                if self._binding_runtime.binding_exists_locked(binding)
            ]
            existing_binding_thread_ids = {
                binding: (
                    self._binding_runtime.binding_owner_thread_id_locked(
                        binding
                    )
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

        cleanup_errors: list[str] = []
        with self._lock:
            for thread_id, planned_bindings in (
                planned_bindings_by_thread.items()
            ):
                current_plan = (
                    self._thread_lifecycle.local_binding_clear_plan_locked(
                        thread_id
                    )
                )
                self._thread_lifecycle.require_binding_cleanup_allowed(
                    current_plan,
                    action_text="清理 stale bindings",
                )
                if set(current_plan.bindings) != set(planned_bindings):
                    raise ValueError(
                        "binding 在 stale cleanup quarantine 核验期间发生变化；"
                        "请重试。"
                    )
            for binding, expected_thread_id in (
                existing_binding_thread_ids.items()
            ):
                current_thread_id = (
                    self._binding_runtime.binding_owner_thread_id_locked(
                        binding
                    )
                )
                if current_thread_id != expected_thread_id:
                    raise ValueError(
                        "binding 在 stale cleanup quarantine 核验期间发生变化；"
                        "请重试。"
                    )
            deactivation_receipts = (
                self._binding_runtime.deactivate_bindings_with_receipts_locked(
                    existing_bindings,
                    cleanup_errors=cleanup_errors,
                )
            )
            cleared_bindings = [
                binding
                for binding in existing_bindings
                if not self._binding_runtime.binding_exists_locked(binding)
            ]
            for binding in cleared_bindings:
                self._invalidate_feishu_execution_queue_locked(binding)
            retained_bindings = [
                binding
                for binding in existing_bindings
                if self._binding_runtime.binding_exists_locked(binding)
            ]
            cleared_binding_ids = [
                format_binding_id(binding) for binding in cleared_bindings
            ]
        cancel_runtime_timer_effects(
            tuple(
                effect
                for receipt in deactivation_receipts
                for effect in receipt.timer_cancellations
            )
        )

        cleared_thread_ids = {
            existing_binding_thread_ids[binding]
            for binding in cleared_bindings
            if existing_binding_thread_ids[binding]
        }
        failed_cleanup_thread_ids = {
            existing_binding_thread_ids[binding]
            for binding in retained_bindings
            if existing_binding_thread_ids[binding]
        }
        self._finalize_deactivated_thread_runtime(
            (),
            release_only_thread_ids=(
                cleared_thread_ids - failed_cleanup_thread_ids
            ),
            cleanup_errors=cleanup_errors,
        )
        self._raise_binding_cleanup_errors(cleanup_errors)
        return {
            "cleared_binding_ids": cleared_binding_ids,
            "stale_thread_ids": sorted(stale_thread_ids),
            "unknown_threads": unknown_threads,
            "retained_thread_ids": retained_thread_ids,
            "cleared": bool(cleared_binding_ids),
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
        resolved_summary, backend_thread_status = (
            self._thread_lifecycle.read_thread_summary_for_status(
                normalized_thread_id
            )
        )
        lease = self._load_thread_runtime_lease(normalized_thread_id)
        effective_summary = resolved_summary or summary
        effective_summary_title = ""
        if effective_summary is not None:
            effective_summary_title = str(
                effective_summary.name or effective_summary.preview or ""
            ).strip()
        effective_title = effective_summary_title or str(
            snapshot.get("thread_title", "") or ""
        ).strip()
        effective_working_dir = (
            effective_summary.cwd
            if effective_summary is not None
            and str(effective_summary.cwd or "").strip()
            else str(snapshot.get("working_dir", "") or "").strip()
        )
        detach_reason_code = self.detach_thread_check_locked(
            normalized_thread_id
        ).reason_code
        if not snapshot["bound_binding_ids"]:
            detach_reason_code = DETACH_NOT_APPLICABLE_NO_BINDING
        return {
            "thread_id": snapshot["thread_id"],
            "thread_title": effective_title,
            "working_dir": effective_working_dir,
            "backend_thread_status": (
                backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
            ),
            "backend_running_turn": (
                backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
            ),
            "live_runtime_owner": self.live_runtime_owner_snapshot(lease),
            "live_runtime_holder_labels": self.live_runtime_holder_labels(
                lease
            ),
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "detached_binding_ids": snapshot["detached_binding_ids"],
            "interaction_owner": snapshot["interaction_owner"],
            "detach_available": snapshot["detach_available"],
            "detach_reason_code": detach_reason_code,
            "detach_reason": snapshot["detach_reason"],
        }
