"""Canonical method schema and dispatch owner for the runtime control plane."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeAlias

from bot.adapters.base import ThreadSummary
from bot.backend_reset.contract import (
    BackendResetPreview,
    decode_backend_reset_force,
)
from bot.binding_identity import parse_binding_id
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
)


logger = logging.getLogger(__name__)
ChatBindingKey: TypeAlias = tuple[str, str]


def loaded_thread_inventory_control_response(
    params: dict[str, Any],
    *,
    instance_name: object,
    list_loaded_thread_ids: Callable[[], Iterable[object]],
) -> dict[str, Any]:
    """Validate and read the strict process-local loaded inventory method."""

    if params:
        raise ValueError("thread/loaded/list 不接受参数。")
    normalized_instance_name = str(instance_name or "").strip().lower()
    if not normalized_instance_name:
        raise ValueError("thread/loaded/list 无法确认当前 instance。")
    loaded_thread_ids: list[str] = []
    seen: set[str] = set()
    for raw_thread_id in list_loaded_thread_ids():
        if type(raw_thread_id) is not str:
            raise ValueError("thread/loaded/list 返回了无效 thread id。")
        thread_id = raw_thread_id.strip()
        if not thread_id or thread_id in seen:
            raise ValueError("thread/loaded/list 返回了无效 thread id。")
        seen.add(thread_id)
        loaded_thread_ids.append(thread_id)
    return {
        "instance_name": normalized_instance_name,
        "loaded_thread_ids": sorted(loaded_thread_ids),
    }


@dataclass(frozen=True, slots=True)
class RuntimeAdminServiceControlPorts:
    binding_inventory_snapshot: Callable[[], list[dict[str, Any]]]
    backend_reset_preview: Callable[[], BackendResetPreview]
    list_loaded_thread_ids: Callable[[], list[str]]
    instance_name: Callable[[], str]
    service_control_endpoint: Callable[[], str]
    current_app_server_url: Callable[[], str]
    web_gateway_enabled: Callable[[], bool]
    current_web_gateway_url: Callable[[], str]
    operational_status: Callable[[], dict[str, Any]]
    reset_backend: Callable[[bool], dict[str, Any]]
    attach_service: Callable[[], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RuntimeAdminBindingControlPorts:
    list_response: Callable[..., dict[str, Any]]
    status_snapshot: Callable[[ChatBindingKey], dict[str, Any]]
    attach: Callable[[ChatBindingKey], dict[str, Any]]
    submit_prompt: Callable[..., dict[str, Any]]
    detach: Callable[[ChatBindingKey], dict[str, Any]]
    clear: Callable[[ChatBindingKey], dict[str, Any]]
    clear_all: Callable[[], dict[str, Any]]
    clear_stale: Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RuntimeAdminThreadControlPorts:
    resolve_target: Callable[[dict[str, Any]], ThreadSummary]
    status_snapshot: Callable[..., dict[str, Any]]
    goal_snapshot: Callable[..., dict[str, Any]]
    set_goal: Callable[..., dict[str, Any]]
    clear_goal: Callable[..., dict[str, Any]]
    clear_archived_bindings: Callable[..., dict[str, Any]]
    local_bindings: Callable[[str], dict[str, Any]]
    loaded_status: Callable[[str], dict[str, str]]
    archive: Callable[[str], dict[str, Any]]
    unarchive: Callable[[str], dict[str, Any]]
    delete: Callable[[str], dict[str, Any]]
    send_image: Callable[..., dict[str, Any]]
    attach: Callable[[str], dict[str, Any]]
    detach: Callable[[str], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RuntimeAdminControlRouterPorts:
    service: RuntimeAdminServiceControlPorts
    binding: RuntimeAdminBindingControlPorts
    thread: RuntimeAdminThreadControlPorts


class RuntimeAdminControlRouter:
    """Validate and dispatch one exact local control method.

    The router owns the control-plane method vocabulary and wire parameter
    normalization. Domain facts and transactions remain behind the named
    service, binding, and thread ports.
    """

    def __init__(self, ports: RuntimeAdminControlRouterPorts) -> None:
        if type(ports) is not RuntimeAdminControlRouterPorts:
            raise TypeError("runtime admin control router requires typed ports")
        self._ports = ports

    @staticmethod
    def _required_text(
        params: dict[str, Any],
        key: str,
        *,
        method: str,
    ) -> str:
        value = str(params.get(key, "") or "").strip()
        if not value:
            raise ValueError(f"{method} 缺少 {key}。")
        return value

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method == "service/status":
            return self._service_status()
        if method == "thread/loaded/list":
            ports = self._ports.service
            return loaded_thread_inventory_control_response(
                params,
                instance_name=ports.instance_name(),
                list_loaded_thread_ids=ports.list_loaded_thread_ids,
            )
        if method == "service/reset-backend":
            return self._ports.service.reset_backend(
                decode_backend_reset_force(params)
            )
        if method == "service/attach":
            # A local control socket belongs to the deployment trust domain,
            # but carries no Feishu writer identity.
            return self._ports.service.attach_service()
        if method == "binding/list":
            return self._ports.binding.list_response(
                refresh_names=bool(params.get("refresh_names"))
            )
        if method == "binding/status":
            binding_id = str(params.get("binding_id", "") or "").strip()
            return self._ports.binding.status_snapshot(
                parse_binding_id(binding_id)
            )
        if method == "binding/attach":
            binding_id = self._required_text(
                params,
                "binding_id",
                method=method,
            )
            return self._ports.binding.attach(parse_binding_id(binding_id))
        if method == "binding/submit-prompt":
            return self._submit_binding_prompt(params)
        if method == "binding/detach":
            binding_id = self._required_text(
                params,
                "binding_id",
                method=method,
            )
            return self._ports.binding.detach(parse_binding_id(binding_id))
        if method == "binding/clear":
            binding_id = self._required_text(
                params,
                "binding_id",
                method=method,
            )
            return self._ports.binding.clear(parse_binding_id(binding_id))
        if method == "binding/clear-all":
            return self._ports.binding.clear_all()
        if method == "binding/clear-stale":
            return self._ports.binding.clear_stale(
                dry_run=bool(params.get("dry_run"))
            )
        if method == "thread/clear-archived-bindings":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.clear_archived_bindings(
                thread_id,
                dry_run=bool(params.get("dry_run")),
            )
        if method == "thread/local-bindings":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.local_bindings(thread_id)
        if method == "thread/loaded-status":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.loaded_status(thread_id)
        if method == "thread/archive":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.archive(thread_id)
        if method == "thread/unarchive":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.unarchive(thread_id)
        if method == "thread/delete":
            thread_id = self._required_text(
                params,
                "thread_id",
                method=method,
            )
            return self._ports.thread.delete(thread_id)
        if method in {
            "thread/status",
            "thread/bindings",
            "thread/goal",
            "thread/goal/set",
            "thread/goal/clear",
            "thread/detach",
            "thread/send-image",
            "thread/attach",
        }:
            return self._handle_resolved_thread(method, params)
        raise ValueError(f"未知控制面方法：{method}")

    def _service_status(self) -> dict[str, Any]:
        ports = self._ports.service
        bindings = ports.binding_inventory_snapshot()
        reset_preview = ports.backend_reset_preview()
        bound_thread_ids = {
            item["thread_id"] for item in bindings if item["thread_id"]
        }
        attached_thread_ids = {
            item["thread_id"]
            for item in bindings
            if item["thread_id"]
            and item["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED
        }
        try:
            loaded_thread_ids = ports.list_loaded_thread_ids()
        except Exception:
            logger.exception("读取 loaded thread 列表失败")
            loaded_thread_ids = []
        return {
            "instance_name": ports.instance_name(),
            "pid": os.getpid(),
            "control_endpoint": ports.service_control_endpoint(),
            "app_server_url": ports.current_app_server_url(),
            "web_gateway_enabled": bool(ports.web_gateway_enabled()),
            "web_gateway_url": ports.current_web_gateway_url(),
            "binding_count": len(bindings),
            "bound_binding_count": sum(
                1 for item in bindings if item["binding_state"] == "bound"
            ),
            "attached_binding_count": sum(
                1
                for item in bindings
                if item["feishu_runtime_state"] == FEISHU_RUNTIME_ATTACHED
            ),
            "thread_count": len(bound_thread_ids),
            "attached_thread_count": len(attached_thread_ids),
            "loaded_thread_count": len(loaded_thread_ids),
            "loaded_thread_ids": loaded_thread_ids,
            "running_binding_ids": [
                item["binding_id"] for item in bindings if item["running_turn"]
            ],
            "backend_reset_status": reset_preview.status,
            "backend_reset_reason_code": reset_preview.reason_code,
            "backend_reset_reason": reset_preview.reason_text,
            "operator_status": ports.operational_status(),
        }

    def _submit_binding_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        method = "binding/submit-prompt"
        binding_id = self._required_text(
            params,
            "binding_id",
            method=method,
        )
        raw_input_items = params.get("input_items")
        input_items: list[dict[str, Any]] | None = None
        if raw_input_items is not None:
            if not isinstance(raw_input_items, list):
                raise ValueError(
                    "binding/submit-prompt 的 input_items 必须是数组。"
                )
            input_items = []
            for item in raw_input_items:
                if not isinstance(item, dict):
                    raise ValueError(
                        "binding/submit-prompt 的 input_items 元素必须是对象。"
                    )
                input_items.append(dict(item))
        return self._ports.binding.submit_prompt(
            parse_binding_id(binding_id),
            text=str(params.get("text", "") or ""),
            actor_open_id=str(params.get("actor_open_id", "") or ""),
            input_items=input_items,
            synthetic_source=str(params.get("synthetic_source", "") or ""),
            display_mode=str(
                params.get("display_mode", "silent") or "silent"
            ),
        )

    def _handle_resolved_thread(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        ports = self._ports.thread
        thread_id = str(params.get("thread_id", "") or "").strip()
        thread_name = str(params.get("thread_name", "") or "").strip()
        if (
            method in {"thread/status", "thread/bindings"}
            and thread_id
            and not thread_name
        ):
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
            thread = ports.resolve_target(params)
        if method == "thread/status":
            return ports.status_snapshot(thread.thread_id, summary=thread)
        if method == "thread/bindings":
            snapshot = ports.status_snapshot(thread.thread_id, summary=thread)
            attached_binding_ids = set(snapshot["attached_binding_ids"])
            return {
                "thread_id": snapshot["thread_id"],
                "thread_title": snapshot["thread_title"],
                "working_dir": snapshot["working_dir"],
                "bindings": [
                    {
                        "binding_id": binding_id,
                        "feishu_runtime_state": (
                            FEISHU_RUNTIME_ATTACHED
                            if binding_id in attached_binding_ids
                            else FEISHU_RUNTIME_DETACHED
                        ),
                    }
                    for binding_id in snapshot["bound_binding_ids"]
                ],
            }
        if method == "thread/goal":
            return ports.goal_snapshot(thread.thread_id, summary=thread)
        if method == "thread/goal/set":
            objective = params.get("objective")
            status = params.get("status")
            return ports.set_goal(
                thread.thread_id,
                summary=thread,
                objective=None if objective is None else str(objective),
                status=None if status is None else str(status),
            )
        if method == "thread/goal/clear":
            return ports.clear_goal(thread.thread_id, summary=thread)
        if method == "thread/send-image":
            local_path = self._required_text(
                params,
                "local_path",
                method=method,
            )
            return ports.send_image(
                thread.thread_id,
                local_path=local_path,
                summary=thread,
            )
        if method == "thread/attach":
            return ports.attach(thread.thread_id)
        return ports.detach(thread.thread_id)
