from __future__ import annotations

import logging
import os
from typing import Any, Callable, TypeAlias

from bot.adapters.base import RuntimeConfigSummary, ThreadSummary
from bot.binding_identity import format_binding_id, parse_binding_id
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.cards import CommandResult, build_markdown_card
from bot.constants import display_path
from bot.reason_codes import (
    BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND,
    BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN,
    BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST,
    PROMPT_DENIED_BY_RUNNING_TURN,
    UNSUBSCRIBE_BLOCKED_BY_INFLIGHT_TURN,
    UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST,
    UNSUBSCRIBE_NOT_APPLICABLE_NO_BINDING,
    UNSUBSCRIBE_NOT_APPLICABLE_ALREADY_RELEASED,
    UNSUBSCRIBE_NOT_APPLICABLE_NO_THREAD,
    ReasonedCheck,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_LOOKUP_ERROR,
    BACKEND_THREAD_LOOKUP_MISSING,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_RELEASED,
    LOADED_BACKEND_THREAD_STATUSES,
    RuntimeStateDict,
)

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = RuntimeStateDict


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
        list_loaded_thread_ids: Callable[[], list[str]],
        current_app_server_url: Callable[[], str],
        unsubscribe_thread: Callable[[str], None],
        release_service_thread_runtime_lease: Callable[[str], None],
        service_control_endpoint: Callable[[], str],
        instance_name: Callable[[], str],
        admitted_thread_ids: Callable[[], tuple[str, ...]],
        admit_thread: Callable[[str], bool],
        revoke_thread: Callable[[str], bool],
        safe_read_runtime_config: Callable[[], RuntimeConfigSummary | None],
        current_default_profile_resolution: Callable[[RuntimeConfigSummary | None], Any],
        permissions_summary: Callable[[str, str], str],
        prompt_write_denial_check: Callable[[ChatBindingKey, str, str, str], ReasonedCheck],
        resolve_thread_target_for_control_params: Callable[[dict[str, Any]], ThreadSummary],
        cancel_patch_timer_locked: Callable[[RuntimeState], None],
        cancel_mirror_watchdog_locked: Callable[[RuntimeState], None],
        is_thread_not_found_error: Callable[[Exception], bool],
        reprofile_possible_check: Callable[[str], tuple[bool, str]],
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._interaction_requests = interaction_requests
        self._clear_all_stored_bindings = clear_all_stored_bindings
        self._deactivate_binding_locked = deactivate_binding_locked
        self._read_thread = read_thread
        self._list_loaded_thread_ids = list_loaded_thread_ids
        self._current_app_server_url = current_app_server_url
        self._unsubscribe_thread = unsubscribe_thread
        self._release_service_thread_runtime_lease = release_service_thread_runtime_lease
        self._service_control_endpoint = service_control_endpoint
        self._instance_name = instance_name
        self._admitted_thread_ids = admitted_thread_ids
        self._admit_thread = admit_thread
        self._revoke_thread = revoke_thread
        self._safe_read_runtime_config = safe_read_runtime_config
        self._current_default_profile_resolution = current_default_profile_resolution
        self._permissions_summary = permissions_summary
        self._prompt_write_denial_check = prompt_write_denial_check
        self._resolve_thread_target_for_control_params = resolve_thread_target_for_control_params
        self._cancel_patch_timer_locked = cancel_patch_timer_locked
        self._cancel_mirror_watchdog_locked = cancel_mirror_watchdog_locked
        self._is_thread_not_found_error = is_thread_not_found_error
        self._reprofile_possible_check = reprofile_possible_check

    @staticmethod
    def binding_has_inflight_turn_locked(state: RuntimeState) -> bool:
        return BindingRuntimeManager.binding_has_inflight_turn_locked(state)

    def binding_inventory_locked(self) -> list[dict[str, Any]]:
        return self._binding_runtime.binding_inventory_locked()

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

    def read_thread_summary_for_status(self, thread_id: str) -> tuple[ThreadSummary | None, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None, ""
        try:
            summary = self._read_thread(normalized_thread_id).summary
        except Exception as exc:
            if self._is_thread_not_found_error(exc):
                return None, BACKEND_THREAD_LOOKUP_MISSING
            logger.exception("读取线程状态失败: thread=%s", normalized_thread_id[:12])
            return None, BACKEND_THREAD_LOOKUP_ERROR
        return summary, str(summary.status or BACKEND_THREAD_STATUS_UNKNOWN).strip() or BACKEND_THREAD_STATUS_UNKNOWN

    def unsubscribe_check_locked(self, thread_id: str) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.deny(
                UNSUBSCRIBE_NOT_APPLICABLE_NO_THREAD,
                "当前没有绑定线程。",
            )
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if not attached_bindings:
            return ReasonedCheck.deny(
                UNSUBSCRIBE_NOT_APPLICABLE_ALREADY_RELEASED,
                "当前 thread 的 Feishu runtime 已经是 `released`。",
            )
        for binding in attached_bindings:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
            if snapshot is None:
                continue
            if snapshot.has_inflight_turn:
                return ReasonedCheck.deny(
                    UNSUBSCRIBE_BLOCKED_BY_INFLIGHT_TURN,
                    "当前有飞书侧 turn 正在运行，不能释放 runtime。",
                )
        if self._interaction_requests.thread_has_pending_request_locked(normalized_thread_id):
            return ReasonedCheck.deny(
                UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST,
                "当前还有飞书侧审批或输入请求未处理，不能释放 runtime。",
            )
        return ReasonedCheck.allow()

    def unsubscribe_availability_locked(self, thread_id: str) -> tuple[bool, str]:
        check = self.unsubscribe_check_locked(thread_id)
        return check.allowed, check.reason_text

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
            return self.binding_prompt_check_locked(binding)

    def binding_prompt_check_locked(self, binding: ChatBindingKey) -> ReasonedCheck:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            return ReasonedCheck.allow()
        if snapshot.has_inflight_turn:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_RUNNING_TURN,
                "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            )
        if not snapshot.thread_id:
            return ReasonedCheck.allow()
        return self._prompt_write_denial_check(
            binding,
            binding[1],
            snapshot.thread_id,
            message_id="",
        )

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
            for binding in bindings:
                unsubscribe_thread_id = self._deactivate_binding_locked(binding)
                cleared_binding_ids.append(format_binding_id(binding))
                if unsubscribe_thread_id:
                    unsubscribe_thread_ids.append(unsubscribe_thread_id)
            self._clear_all_stored_bindings()
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
            unsubscribe_check = self.unsubscribe_check_locked(str(snapshot["thread_id"] or "").strip())
            prompt_check = self.binding_prompt_check_locked(binding)
        thread_id = str(snapshot["thread_id"] or "").strip()
        summary, backend_thread_status = self.read_thread_summary_for_status(thread_id)
        if summary is not None:
            snapshot["thread_title"] = summary.title or str(snapshot["thread_title"] or "").strip()
            snapshot["working_dir"] = summary.cwd or str(snapshot["working_dir"] or "").strip()
        snapshot["backend_thread_status"] = backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN
        snapshot["backend_running_turn"] = backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE
        snapshot["reprofile_possible"] = bool(thread_id and self._reprofile_possible_check(thread_id)[0])
        snapshot["unsubscribe_available"] = bool(thread_id and unsubscribe_check.allowed)
        snapshot["unsubscribe_reason_code"] = unsubscribe_check.reason_code
        snapshot["unsubscribe_reason"] = unsubscribe_check.reason_text
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
        binding_state = snapshot["binding_state"]
        thread_id = snapshot["thread_id"]
        if thread_id:
            thread_line = f"当前线程：`{thread_id[:8]}…` {snapshot['thread_title'] or '（无标题）'}"
        else:
            thread_line = "当前线程：-"
        lines = [
            f"目录：`{display_path(snapshot['working_dir'])}`",
            thread_line,
            f"binding：`{binding_state}`",
            f"feishu runtime：`{snapshot['feishu_runtime_state']}`",
            f"backend thread status：`{snapshot['backend_thread_status']}`",
            f"backend running turn：`{'yes' if snapshot['backend_running_turn'] else 'no'}`",
            f"交互 owner：`{snapshot['interaction_owner']['label']}`",
            f"re-profile possible：`{'yes' if snapshot['reprofile_possible'] else 'no'}`",
            (
                "unsubscribe：`available`"
                if snapshot["unsubscribe_available"]
                else (
                    "unsubscribe："
                    f"`blocked` (`{snapshot['unsubscribe_reason_code']}`) {snapshot['unsubscribe_reason']}"
                )
                if thread_id
                else "unsubscribe：`not-applicable`"
            ),
            (
                "当前直接提问：`accepted`"
                if snapshot["next_prompt_allowed"]
                else f"当前直接提问：`blocked` (`{snapshot['next_prompt_reason_code']}`) {snapshot['next_prompt_reason']}"
            ),
        ]
        if snapshot["running_turn"]:
            lines.append(
                f"当前 Feishu turn：`{snapshot['current_turn_id'][:8]}…`"
                if snapshot["current_turn_id"]
                else "当前 Feishu turn：`pending`"
            )
        if include_profile_lines:
            runtime_config = self._safe_read_runtime_config()
            profile_resolution = self._current_default_profile_resolution(runtime_config)
            local_profile = profile_resolution.effective_profile
            lines.extend(
                [
                    f"新 thread seed profile：`{local_profile or '（未设置）'}`",
                    (
                        f"当前 provider：`{runtime_config.current_model_provider or '（未设置）'}`"
                        if runtime_config
                        else "当前 provider：读取失败"
                    ),
                    f"权限预设：`{self._permissions_summary(snapshot['approval_policy'], snapshot['sandbox'])}`",
                    f"审批策略：`{snapshot['approval_policy']}`",
                    f"沙箱策略：`{snapshot['sandbox']}`",
                    f"协作模式：`{snapshot['collaboration_mode']}`",
                ]
            )
            if profile_resolution.stale_profile:
                lines.append(
                    f"注意：之前保存的新 thread seed profile `{profile_resolution.stale_profile}` 已不存在，已自动回退到 Codex 原生默认。"
                )
        if snapshot["running_turn"]:
            next_step = "如需停止当前执行，可点当前执行卡片上的停止按钮。"
        elif binding_state == "unbound":
            next_step = "直接发送普通文本，会在当前目录自动新建线程。"
        else:
            next_step = "发送 `/help session` 查看线程恢复、unsubscribe 与本地继续规则。"
        lines.extend(["", next_step])
        template = "turquoise" if snapshot["running_turn"] else "blue"
        return "\n".join(lines), template

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
        if snapshot["binding_state"] == "unbound":
            return "下一条普通消息：`accepted`，会在当前目录新建 thread 后启动 turn。"
        if snapshot["feishu_runtime_state"] == FEISHU_RUNTIME_RELEASED:
            return "下一条普通消息：`accepted`，会先按当前绑定重新附着 / resume，再启动 turn。"
        return "下一条普通消息：`accepted`，会写入当前绑定 thread。"

    @staticmethod
    def _release_preflight_line(snapshot: dict[str, Any]) -> str:
        if not snapshot["thread_id"]:
            return "unsubscribe：`not-applicable`，当前没有绑定 thread。"
        if snapshot["unsubscribe_available"]:
            return "unsubscribe：`available`"
        return (
            "unsubscribe："
            f"`blocked` (`{snapshot['unsubscribe_reason_code']}`) "
            f"{snapshot['unsubscribe_reason']}"
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
            f"feishu runtime：`{snapshot['feishu_runtime_state']}`",
            f"backend thread status：`{snapshot['backend_thread_status']}`",
            "",
            self._next_prompt_preflight_line(snapshot),
            self._release_preflight_line(snapshot),
        ]
        if include_profile_lines:
            runtime_config = self._safe_read_runtime_config()
            profile_resolution = self._current_default_profile_resolution(runtime_config)
            local_profile = profile_resolution.effective_profile
            lines.extend(
                [
                    "",
                    f"新 thread seed profile：`{local_profile or '（未设置）'}`",
                    f"权限预设：`{self._permissions_summary(snapshot['approval_policy'], snapshot['sandbox'])}`",
                    f"审批策略：`{snapshot['approval_policy']}`",
                    f"沙箱策略：`{snapshot['sandbox']}`",
                    f"协作模式：`{snapshot['collaboration_mode']}`",
                ]
            )
            if profile_resolution.stale_profile:
                lines.append(
                    f"注意：之前保存的新 thread seed profile `{profile_resolution.stale_profile}` 已不存在，实际执行会回退到 Codex 原生默认。"
                )
        if thread_id and snapshot["feishu_runtime_state"] == FEISHU_RUNTIME_RELEASED:
            lines.extend(
                [
                    "",
                    "说明：`released` 状态下，只有 preflight accepted 才允许重新附着；blocked 必须保持 pure reject。",
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

    def handle_unsubscribe_command(self, binding: ChatBindingKey, arg: str) -> CommandResult:
        if str(arg or "").strip():
            return CommandResult(text="用法：`/unsubscribe`")
        snapshot = self.binding_status_snapshot(binding)
        thread_id = str(snapshot["thread_id"] or "").strip()
        if not thread_id:
            return CommandResult(text="当前没有绑定线程，无需释放 Feishu runtime。")
        try:
            result = self.unsubscribe_feishu_runtime_by_thread_id(thread_id)
        except ValueError as exc:
            return CommandResult(text=str(exc))
        body = [
            f"线程：`{thread_id[:8]}…` {result['thread_title'] or '（无标题）'}",
            f"Feishu runtime：`{'released' if result['changed'] or result['already_released'] else 'attached'}`",
            f"受影响绑定：{', '.join(result['released_binding_ids']) or '（无）'}",
            f"backend thread status：`{result['backend_thread_status']}`",
            f"re-profile possible：`{'yes' if result['reprofile_possible'] else 'no'}`",
        ]
        if result["already_released"]:
            body.append("说明：该线程的 Feishu runtime 原本就已是 `released`。")
        elif result["backend_still_loaded"]:
            body.append("说明：backend 仍保持 loaded，说明还有外部订阅者仍附着在这个 thread 上，通常是本地 `fcodex`。")
        else:
            body.append("说明：Feishu 已释放自己对该 thread 的 runtime 持有；绑定关系仍保留，之后可继续 resume。")
        return CommandResult(
            card=build_markdown_card(
                "Codex 已取消 Feishu 订阅",
                "\n".join(body),
                template="green" if result["changed"] else "blue",
            )
        )

    def _release_binding_runtime_state_locked(self, state: RuntimeState) -> None:
        self._cancel_patch_timer_locked(state)
        self._cancel_mirror_watchdog_locked(state)

    def unsubscribe_feishu_runtime_by_thread_id(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            result = self._binding_runtime.unsubscribe_feishu_runtime_by_thread_id_locked(
                normalized_thread_id,
                unsubscribe_availability=self.unsubscribe_availability_locked,
                on_release_binding_state=self._release_binding_runtime_state_locked,
            )
        if result.unsubscribe_thread_id:
            self._unsubscribe_thread(result.unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(result.unsubscribe_thread_id)
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        thread_title = result.thread_title
        working_dir = result.working_dir
        if resolved_summary is not None:
            thread_title = resolved_summary.title or thread_title
            working_dir = resolved_summary.cwd or working_dir
        unsubscribe_check = self.unsubscribe_check_locked(normalized_thread_id)
        return {
            "thread_id": result.thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "bound_binding_ids": result.bound_binding_ids,
            "released_binding_ids": result.released_binding_ids,
            "changed": result.changed,
            "already_released": result.already_released,
            "backend_thread_status": backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN,
            "backend_still_loaded": backend_thread_status in LOADED_BACKEND_THREAD_STATUSES,
            "reprofile_possible": self._reprofile_possible_check(normalized_thread_id)[0],
            "unsubscribe_reason_code": "" if result.changed else unsubscribe_check.reason_code,
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
                unsubscribe_availability=self.unsubscribe_availability_locked,
            )
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        effective_summary = resolved_summary or summary
        unsubscribe_reason_code = self.unsubscribe_check_locked(normalized_thread_id).reason_code
        if not snapshot["bound_binding_ids"]:
            unsubscribe_reason_code = UNSUBSCRIBE_NOT_APPLICABLE_NO_BINDING
        return {
            "thread_id": snapshot["thread_id"],
            "thread_title": effective_summary.title if effective_summary is not None else "",
            "working_dir": effective_summary.cwd if effective_summary is not None else "",
            "backend_thread_status": backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN,
            "backend_running_turn": backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE,
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "released_binding_ids": snapshot["released_binding_ids"],
            "interaction_owner": snapshot["interaction_owner"],
            "reprofile_possible": self._reprofile_possible_check(normalized_thread_id)[0],
            "unsubscribe_available": snapshot["unsubscribe_available"],
            "unsubscribe_reason_code": unsubscribe_reason_code,
            "unsubscribe_reason": snapshot["unsubscribe_reason"],
        }

    def handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "service/status":
            with self._lock:
                bindings = self.binding_inventory_locked()
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
                "admitted_thread_count": len(self._admitted_thread_ids()),
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
            }
        if method == "binding/list":
            with self._lock:
                return {"bindings": self.binding_inventory_locked()}
        if method == "binding/status":
            binding_id = str(params.get("binding_id", "") or "").strip()
            binding = parse_binding_id(binding_id)
            return self.binding_status_snapshot(binding)
        if method == "binding/clear":
            binding_id = str(params.get("binding_id", "") or "").strip()
            if not binding_id:
                raise ValueError("binding/clear 缺少 binding_id。")
            binding = parse_binding_id(binding_id)
            return self.clear_binding_for_control(binding)
        if method == "binding/clear-all":
            return self.clear_all_bindings_for_control()
        if method in {"thread/status", "thread/bindings", "thread/unsubscribe"}:
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
                                else FEISHU_RUNTIME_RELEASED
                            ),
                        }
                        for binding_id in snapshot["bound_binding_ids"]
                    ],
                }
            return self.unsubscribe_feishu_runtime_by_thread_id(thread.thread_id)
        if method == "thread/admissions":
            return {"instance_name": self._instance_name(), "thread_ids": list(self._admitted_thread_ids())}
        if method == "thread/import":
            thread = self._resolve_thread_target_for_control_params(params)
            return {
                "thread_id": thread.thread_id,
                "thread_title": thread.title,
                "imported": self._admit_thread(thread.thread_id),
            }
        if method == "thread/revoke":
            thread = self._resolve_thread_target_for_control_params(params)
            with self._lock:
                if self.bound_bindings_for_thread_locked(thread.thread_id):
                    raise ValueError("当前仍有 binding 指向该线程，不能撤销 admission。")
            return {
                "thread_id": thread.thread_id,
                "thread_title": thread.title,
                "revoked": self._revoke_thread(thread.thread_id),
            }
        raise ValueError(f"未知控制面方法：{method}")
