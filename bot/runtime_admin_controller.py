from __future__ import annotations

import logging
import os
from typing import Any, Callable, MutableMapping, TypeAlias

from bot.adapters.base import RuntimeConfigSummary, ThreadSummary
from bot.binding_identity import format_binding_id, parse_binding_id
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.cards import CommandResult, build_markdown_card
from bot.constants import display_path

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = MutableMapping[str, Any]


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
        service_control_socket_path: Callable[[], str],
        safe_read_runtime_config: Callable[[], RuntimeConfigSummary | None],
        current_default_profile_resolution: Callable[[RuntimeConfigSummary | None], Any],
        permissions_summary: Callable[[str, str], str],
        resolve_thread_target_for_control_params: Callable[[dict[str, Any]], ThreadSummary],
        cancel_patch_timer_locked: Callable[[RuntimeState], None],
        cancel_mirror_watchdog_locked: Callable[[RuntimeState], None],
        is_thread_not_found_error: Callable[[Exception], bool],
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
        self._service_control_socket_path = service_control_socket_path
        self._safe_read_runtime_config = safe_read_runtime_config
        self._current_default_profile_resolution = current_default_profile_resolution
        self._permissions_summary = permissions_summary
        self._resolve_thread_target_for_control_params = resolve_thread_target_for_control_params
        self._cancel_patch_timer_locked = cancel_patch_timer_locked
        self._cancel_mirror_watchdog_locked = cancel_mirror_watchdog_locked
        self._is_thread_not_found_error = is_thread_not_found_error

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
                return None, "missing"
            logger.exception("读取线程状态失败: thread=%s", normalized_thread_id[:12])
            return None, "error"
        return summary, str(summary.status or "unknown").strip() or "unknown"

    def release_feishu_runtime_availability_locked(self, thread_id: str) -> tuple[bool, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False, "当前没有绑定线程。"
        attached_bindings = self.attached_bindings_for_thread_locked(normalized_thread_id)
        if not attached_bindings:
            return False, "当前 thread 的 Feishu runtime 已经是 `released`。"
        for binding in attached_bindings:
            snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
            if snapshot is None:
                continue
            if snapshot.has_inflight_turn:
                return False, "当前有飞书侧 turn 正在运行，不能释放 runtime。"
        if self._interaction_requests.thread_has_pending_request_locked(normalized_thread_id):
            return False, "当前还有飞书侧审批或输入请求未处理，不能释放 runtime。"
        return True, ""

    def binding_has_pending_request_locked(self, binding: ChatBindingKey) -> bool:
        return self._interaction_requests.binding_has_pending_request_locked(binding)

    def binding_clear_availability_locked(self, binding: ChatBindingKey) -> tuple[bool, str]:
        snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
        if snapshot is None:
            return False, f"未找到绑定：{format_binding_id(binding)}"
        if snapshot.has_inflight_turn:
            return False, "当前有飞书侧 turn 正在运行，不能清除 binding。"
        if self.binding_has_pending_request_locked(binding):
            return False, "当前还有飞书侧审批或输入请求未处理，不能清除 binding。"
        return True, ""

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
        return {
            "cleared_binding_ids": cleared_binding_ids,
            "already_empty": False,
        }

    def binding_status_snapshot(self, binding: ChatBindingKey) -> dict[str, Any]:
        return self._binding_runtime.binding_status_snapshot(
            binding,
            read_thread_summary_for_status=self.read_thread_summary_for_status,
            release_feishu_runtime_availability=self.release_feishu_runtime_availability_locked,
        )

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
            f"Feishu 写入 owner：`{snapshot['feishu_write_owner_binding_id'] or snapshot['feishu_write_owner_relation']}`",
            f"交互 owner：`{snapshot['interaction_owner']['label']}`",
            f"re-profile possible：`{'yes' if snapshot['reprofile_possible'] else 'no'}`",
            (
                "release-feishu-runtime：`available`"
                if snapshot["release_feishu_runtime_available"]
                else f"release-feishu-runtime：`blocked` {snapshot['release_feishu_runtime_reason']}"
                if thread_id
                else "release-feishu-runtime：`not-applicable`"
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
                    f"默认 profile：`{local_profile or '（未设置）'}`",
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
                    f"注意：之前保存的默认 profile `{profile_resolution.stale_profile}` 已不存在，已自动回退到 Codex 原生默认。"
                )
        if snapshot["running_turn"]:
            next_step = "如需停止当前执行，可点当前执行卡片上的停止按钮。"
        elif binding_state == "unbound":
            next_step = "直接发送普通文本，会在当前目录自动新建线程。"
        else:
            next_step = "发送 `/help session` 查看线程恢复、释放 runtime 与本地继续规则。"
        lines.extend(["", next_step])
        template = "turquoise" if snapshot["running_turn"] else "blue"
        return "\n".join(lines), template

    def handle_status_command(self, binding: ChatBindingKey) -> CommandResult:
        snapshot = self.binding_status_snapshot(binding)
        content, template = self.render_binding_status_markdown(snapshot, include_profile_lines=True)
        return CommandResult(card=build_markdown_card("Codex 当前状态", content, template=template))

    def handle_release_feishu_runtime_command(self, binding: ChatBindingKey, arg: str) -> CommandResult:
        if str(arg or "").strip():
            return CommandResult(text="用法：`/release-feishu-runtime`")
        snapshot = self.binding_status_snapshot(binding)
        thread_id = str(snapshot["thread_id"] or "").strip()
        if not thread_id:
            return CommandResult(text="当前没有绑定线程，无需释放 Feishu runtime。")
        try:
            result = self.release_feishu_runtime_by_thread_id(thread_id)
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
                "Codex Feishu Runtime 已释放",
                "\n".join(body),
                template="green" if result["changed"] else "blue",
            )
        )

    def _release_binding_runtime_state_locked(self, state: RuntimeState) -> None:
        self._cancel_patch_timer_locked(state)
        self._cancel_mirror_watchdog_locked(state)

    def release_feishu_runtime_by_thread_id(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = str(thread_id or "").strip()
        with self._lock:
            result = self._binding_runtime.release_feishu_runtime_by_thread_id_locked(
                normalized_thread_id,
                release_feishu_runtime_availability=self.release_feishu_runtime_availability_locked,
                on_release_binding_state=self._release_binding_runtime_state_locked,
            )
        if result.unsubscribe_thread_id:
            self._unsubscribe_thread(result.unsubscribe_thread_id)
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        thread_title = result.thread_title
        working_dir = result.working_dir
        if resolved_summary is not None:
            thread_title = resolved_summary.title or thread_title
            working_dir = resolved_summary.cwd or working_dir
        return {
            "thread_id": result.thread_id,
            "thread_title": thread_title,
            "working_dir": working_dir,
            "bound_binding_ids": result.bound_binding_ids,
            "released_binding_ids": result.released_binding_ids,
            "changed": result.changed,
            "already_released": result.already_released,
            "backend_thread_status": backend_thread_status or "unknown",
            "backend_still_loaded": backend_thread_status in {"idle", "active", "systemError"},
            "reprofile_possible": backend_thread_status == "notLoaded",
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
                release_feishu_runtime_availability=self.release_feishu_runtime_availability_locked,
            )
        resolved_summary, backend_thread_status = self.read_thread_summary_for_status(normalized_thread_id)
        effective_summary = resolved_summary or summary
        return {
            "thread_id": snapshot["thread_id"],
            "thread_title": effective_summary.title if effective_summary is not None else "",
            "working_dir": effective_summary.cwd if effective_summary is not None else "",
            "backend_thread_status": backend_thread_status or "unknown",
            "backend_running_turn": backend_thread_status == "active",
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "released_binding_ids": snapshot["released_binding_ids"],
            "feishu_write_owner_binding_id": snapshot["feishu_write_owner_binding_id"],
            "interaction_owner": snapshot["interaction_owner"],
            "reprofile_possible": backend_thread_status == "notLoaded",
            "release_feishu_runtime_available": snapshot["release_feishu_runtime_available"],
            "release_feishu_runtime_reason": snapshot["release_feishu_runtime_reason"],
        }

    def handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "service/status":
            with self._lock:
                bindings = self.binding_inventory_locked()
            bound_thread_ids = {item["thread_id"] for item in bindings if item["thread_id"]}
            attached_thread_ids = {
                item["thread_id"] for item in bindings if item["thread_id"] and item["feishu_runtime_state"] == "attached"
            }
            try:
                loaded_thread_ids = self._list_loaded_thread_ids()
            except Exception:
                logger.exception("读取 loaded thread 列表失败")
                loaded_thread_ids = []
            return {
                "pid": os.getpid(),
                "control_socket_path": self._service_control_socket_path(),
                "app_server_url": self._current_app_server_url(),
                "binding_count": len(bindings),
                "bound_binding_count": sum(1 for item in bindings if item["binding_state"] == "bound"),
                "attached_binding_count": sum(1 for item in bindings if item["feishu_runtime_state"] == "attached"),
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
        if method in {"thread/status", "thread/bindings", "thread/release-feishu-runtime"}:
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
                                "attached" if binding_id in set(snapshot["attached_binding_ids"]) else "released"
                            ),
                        }
                        for binding_id in snapshot["bound_binding_ids"]
                    ],
                }
            return self.release_feishu_runtime_by_thread_id(thread.thread_id)
        raise ValueError(f"未知控制面方法：{method}")
