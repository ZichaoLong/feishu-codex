"""Runtime Admin CLI presentation."""

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import sys
import webbrowser
from typing import Any

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.backend_reset.cli import reset_service_backend
from bot.cli_table import render_table as _render_table
from bot.cli_table import terminal_display_width as _terminal_display_width
from bot.codex_config import CodexConfig
from bot.config import load_config, load_config_file
from bot.constants import display_path, format_timestamp
from bot.env_file import load_env_file
from bot.instance_layout import (
    global_data_dir,
    infer_instance_name_from_data_dir,
    list_known_instance_names,
    resolve_instance_paths,
)
from bot.instance_resolution import (
    list_running_instances,
    resolve_cli_instance_target,
    resolve_running_instance_app_server_url,
)
from bot.platform_paths import default_data_root
from bot.runtime_admin import cli_inputs
from bot.runtime_admin.offline_lifecycle import (
    RuntimeAdminOfflineLifecycle,
    RuntimeAdminOfflineLifecyclePorts,
)
from bot.service_control_plane import (
    ServiceControlError,
    ServiceControlOutcomeUnknownError,
    ServiceControlResponseTimeoutError,
    control_request,
)
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore
from bot.stores.web_gateway_runtime_store import WebGatewayRuntimeStore
from bot.thread_resolution import (
    list_current_dir_threads,
    list_global_threads,
)

_THREAD_LIST_TITLE_MAX_WIDTH = 80
_BINDING_LIST_CHAT_MAX_WIDTH = 32
_BINDING_LIST_THREAD_MAX_WIDTH = 64
_BINDING_LIST_CONTROL_TIMEOUT_SECONDS = 3.0
_BINDING_LIST_REFRESH_TIMEOUT_MARGIN_SECONDS = 3.0
_CODEX_LIFECYCLE_TIMEOUT_MARGIN_SECONDS = 5.0


def _data_dir() -> pathlib.Path:
    raw = os.environ.get("FOCUS_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root()


def _resolve_target_instance(
    explicit_instance: str | None,
    *,
    preferred_running_instance: str = "",
):
    return resolve_cli_instance_target(
        explicit_instance,
        preferred_running_instance=preferred_running_instance,
    )


def _request(
    data_dir: pathlib.Path,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 3.0,
) -> Any:
    return control_request(data_dir, method, params, timeout_seconds=timeout_seconds)


def _open_web(data_dir: pathlib.Path, *, no_browser: bool) -> int:
    runtime = WebGatewayRuntimeStore(data_dir).load()
    if runtime is None:
        raise ValueError(
            "目标实例尚未发布 Focus Web Gateway。请确认 service 正在运行，"
            "并在该实例 codex.yaml 中设置 `web_enabled: true` 后重启 service。"
        )
    from urllib.parse import quote

    url = f"{runtime.endpoint}/#token={quote(runtime.bootstrap_token, safe='')}"
    print(url)
    if no_browser:
        return 0
    if not webbrowser.open(url, new=2):
        print("未能自动打开浏览器；请使用上方一次性 URL。", file=sys.stderr)
    return 0


def _attached_endpoint_adapter(
    data_dir: pathlib.Path,
    *,
    running_entry: InstanceRegistryEntry | None = None,
) -> tuple[CodexAppServerAdapter, CodexConfig, str]:
    if running_entry is None:
        raise ValueError(
            "此操作需要目标 Focus 实例正在运行并发布 app-server；"
            "请先启动该实例的 service 后再试。"
        )
    cfg = _target_codex_config(data_dir, running_entry=running_entry)
    parsed_config = CodexConfig.from_dict(cfg)
    app_server_url = resolve_running_instance_app_server_url(running_entry)
    if not app_server_url:
        raise ValueError(
            f"运行中的实例 `{running_entry.instance_name}` 未发布可用的 app-server 地址；请重启该实例后再试。"
        )
    config = CodexAppServerConfig.from_config(parsed_config).with_attached_endpoint(
        app_server_url=app_server_url,
        app_server_data_dir=str(data_dir),
    )
    return CodexAppServerAdapter(config), parsed_config, app_server_url


def _target_codex_config(
    data_dir: pathlib.Path,
    *,
    running_entry: InstanceRegistryEntry | None = None,
) -> dict[str, Any]:
    target_config_dir: pathlib.Path | None = None
    if running_entry is not None and str(running_entry.config_dir or "").strip():
        target_config_dir = pathlib.Path(running_entry.config_dir)
    else:
        inferred_instance = infer_instance_name_from_data_dir(data_dir)
        if inferred_instance:
            target_config_dir = resolve_instance_paths(inferred_instance).config_dir
    return load_config_file("codex", directory=target_config_dir)


def _lifecycle_control_timeout_seconds(
    data_dir: pathlib.Path,
    *,
    operation: str,
    running_entry: InstanceRegistryEntry | None = None,
) -> float:
    cfg = CodexConfig.from_dict(
        _target_codex_config(pathlib.Path(data_dir), running_entry=running_entry)
    )
    request_timeout = max(cfg.request_timeout_seconds, 0.1)
    connect_timeout = max(cfg.connect_timeout_seconds, 0.1)
    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation == "archive":
        request_count = 3
    elif normalized_operation == "delete":
        request_count = 3
    elif normalized_operation == "unarchive":
        request_count = 2
    else:
        raise ValueError(f"未知 lifecycle operation：{operation}")
    startup_budget = connect_timeout * 2
    return max(
        startup_budget
        + request_timeout * request_count
        + _CODEX_LIFECYCLE_TIMEOUT_MARGIN_SECONDS,
        10.0,
    )


def _lease_owner_instance(thread_id: str) -> str:
    lease = ThreadRuntimeLeaseStore(global_data_dir()).load(thread_id)
    if lease is None:
        return ""
    return str(lease.owner_instance or "").strip()


def _offline_lifecycle() -> RuntimeAdminOfflineLifecycle:
    return RuntimeAdminOfflineLifecycle(
        RuntimeAdminOfflineLifecyclePorts(
            resolve_target_instance=_resolve_target_instance,
            request=_request,
            attached_endpoint_adapter=_attached_endpoint_adapter,
            lifecycle_control_timeout_seconds=_lifecycle_control_timeout_seconds,
            lease_owner_instance=_lease_owner_instance,
            list_running_instances=list_running_instances,
            list_known_instance_names=list_known_instance_names,
            resolve_instance_paths=resolve_instance_paths,
        )
    )


def _resolve_thread_archive_target(args: argparse.Namespace):
    targets = _resolve_thread_archive_targets(args)
    if len(targets) != 1:
        raise ValueError("thread archive 批量模式请改用 _resolve_thread_archive_targets().")
    return targets[0]


def _resolve_thread_archive_targets(args: argparse.Namespace):
    thread_ids, thread_name = cli_inputs.thread_archive_inputs(args)
    return _offline_lifecycle().resolve_archive_targets(
        thread_ids,
        thread_name=thread_name,
        explicit_instance=str(getattr(args, "instance", "") or "").strip(),
    )


def _live_runtime_summary(snapshot: dict[str, Any]) -> tuple[str, list[str]]:
    owner = snapshot.get("live_runtime_owner")
    holder_labels = snapshot.get("live_runtime_holder_labels")
    if isinstance(owner, dict) and isinstance(holder_labels, list):
        label = str(owner.get("label", "") or "").strip() or "none"
        normalized_holders = [str(item or "").strip() for item in holder_labels if str(item or "").strip()]
        return label, normalized_holders
    return "none", []


def _format_goal_ts_seconds(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return "-"
    if timestamp <= 0:
        return "-"
    return format_timestamp(timestamp)


def _goal_status_label(status: str) -> str:
    return {
        "active": "进行中",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "usageLimited": "触发 usage 限制",
        "budgetLimited": "触发预算限制",
        "complete": "已完成",
    }.get(str(status or "").strip(), "未知")


def _single_line_display_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _truncate_display_text(value: Any, *, max_width: int) -> str:
    text = _single_line_display_text(value)
    if max_width <= 0:
        return ""
    if _terminal_display_width(text) <= max_width:
        return text
    ellipsis = "…"
    budget = max(max_width - _terminal_display_width(ellipsis), 0)
    used = 0
    chars: list[str] = []
    for char in text:
        char_width = _terminal_display_width(char)
        if used + char_width > budget:
            break
        chars.append(char)
        used += char_width
    return "".join(chars).rstrip() + ellipsis


def _short_display_id(value: Any, *, prefix_width: int = 8) -> str:
    text = _single_line_display_text(value)
    if not text:
        return "-"
    if _terminal_display_width(text) <= prefix_width:
        return text
    return _truncate_display_text(text, max_width=prefix_width + 1)


def _binding_list_chat_display(item: dict[str, Any]) -> str:
    display_name = _single_line_display_text(item.get("chat_display_name", ""))
    if display_name:
        return _truncate_display_text(display_name, max_width=_BINDING_LIST_CHAT_MAX_WIDTH)
    fallback_id = item.get("chat_id") or item.get("sender_id") or ""
    return _short_display_id(fallback_id)


def _binding_list_thread_display(item: dict[str, Any]) -> str:
    thread_id = _single_line_display_text(item.get("thread_id", ""))
    if not thread_id:
        return "-"
    short_thread_id = _short_display_id(thread_id)
    thread_name = _single_line_display_text(item.get("thread_name", ""))
    if not thread_name:
        return short_thread_id
    return _truncate_display_text(
        f"{short_thread_id} {thread_name}",
        max_width=_BINDING_LIST_THREAD_MAX_WIDTH,
    )


def _binding_list_refresh_target_key(item: dict[str, Any]) -> tuple[str, str] | None:
    binding_kind = _single_line_display_text(item.get("binding_kind", ""))
    if binding_kind == "group":
        chat_id = _single_line_display_text(item.get("chat_id", ""))
        if chat_id:
            return ("group", chat_id)
    if binding_kind == "p2p":
        sender_id = _single_line_display_text(item.get("sender_id", ""))
        if sender_id:
            return ("p2p", sender_id)
    return None


def _binding_list_refresh_target_count(bindings: list[dict[str, Any]]) -> int:
    return len({key for item in bindings if (key := _binding_list_refresh_target_key(item)) is not None})


def _binding_list_refresh_target_resolution_counts(bindings: list[dict[str, Any]]) -> tuple[int, int]:
    resolved_by_target: dict[tuple[str, str], bool] = {}
    for item in bindings:
        target_key = _binding_list_refresh_target_key(item)
        if target_key is None:
            continue
        resolved = bool(_single_line_display_text(item.get("chat_display_name", "")))
        resolved_by_target[target_key] = resolved_by_target.get(target_key, False) or resolved
    resolved_count = sum(1 for resolved in resolved_by_target.values() if resolved)
    return resolved_count, len(resolved_by_target) - resolved_count


def _configured_feishu_request_timeout_seconds() -> float:
    return load_config().request_timeout_seconds


def _binding_list_refresh_timeout_seconds(bindings: list[dict[str, Any]]) -> float:
    target_count = _binding_list_refresh_target_count(bindings)
    return max(
        _BINDING_LIST_CONTROL_TIMEOUT_SECONDS,
        _BINDING_LIST_REFRESH_TIMEOUT_MARGIN_SECONDS
        + target_count * _configured_feishu_request_timeout_seconds(),
    )


def _binding_list_refresh_command(*, instance_name: str = "") -> str:
    normalized_instance = str(instance_name or "").strip()
    if normalized_instance and normalized_instance.lower() != "default":
        return f"focusctl --instance {shlex.quote(normalized_instance)} binding list --refresh-names"
    return "focusctl binding list --refresh-names"


def _attach_service(data_dir: pathlib.Path) -> int:
    try:
        result = _request(data_dir, "service/attach", timeout_seconds=30.0)
    except ServiceControlResponseTimeoutError as exc:
        print("runtime attach: accepted")
        print("status: waiting for result timed out")
        print("note: attach 请求已送达运行中的 FOCUS service，后台可能仍在继续恢复推送。")
        print("next:")
        print("  - wait a moment, then run: focusctl binding list")
        print("  - or check runtime overview: focusctl instance list")
        print(f"detail: {exc}")
        return 0
    print("runtime attach: ok")
    print(f"instance: {result.get('instance_name', '-')}")
    print(f"attached threads: {', '.join(result.get('attached_thread_ids') or []) or '（无）'}")
    print(f"attached bindings: {', '.join(result.get('attached_binding_ids') or []) or '（无）'}")
    blocked_threads = result.get("blocked_threads") or []
    if blocked_threads:
        print("blocked threads:")
        for item in blocked_threads:
            binding_ids = ", ".join(item.get("binding_ids") or []) or "（无 binding）"
            print(f"- {item.get('thread_id', '-')}: {binding_ids} -> {item.get('reason', '（无原因）')}")
        return 1
    if not result.get("attached_binding_ids"):
        print("note: 当前实例没有需要恢复的 detached 推送。")
    return 0


def _print_binding_list(data_dir: pathlib.Path, *, refresh_names: bool = False, instance_name: str = "") -> int:
    if refresh_names:
        preview_result = _request(
            data_dir,
            "binding/list",
            timeout_seconds=_BINDING_LIST_CONTROL_TIMEOUT_SECONDS,
        )
        preview_bindings = preview_result.get("bindings") or []
        if not preview_bindings:
            print("当前没有可见 binding。")
            return 0
        result = _request(
            data_dir,
            "binding/list",
            {"refresh_names": True},
            timeout_seconds=_binding_list_refresh_timeout_seconds(preview_bindings),
        )
    else:
        result = _request(
            data_dir,
            "binding/list",
            timeout_seconds=_BINDING_LIST_CONTROL_TIMEOUT_SECONDS,
        )
    bindings = result.get("bindings") or []
    if not bindings:
        print("当前没有可见 binding。")
        return 0
    rows: list[list[str]] = []
    for item in bindings:
        cwd = display_path(str(item["working_dir"] or ""))
        rows.append(
            [
                item["binding_id"],
                item["binding_kind"],
                _binding_list_chat_display(item),
                item["binding_state"],
                item["feishu_runtime_state"],
                _binding_list_thread_display(item),
                cwd,
            ]
    )
    for line in _render_table(["BINDING_ID", "KIND", "CHAT", "STATE", "RUNTIME", "THREAD", "CWD"], rows):
        print(line)
    cache_miss_count = int(result.get("chat_display_name_cache_miss_count") or 0)
    if cache_miss_count and not refresh_names:
        refresh_command = _binding_list_refresh_command(instance_name=instance_name)
        print(
            f"note: {cache_miss_count} 个 CHAT 名称未命中缓存；"
            f"如需刷新可执行 `{refresh_command}`。"
        )
    if refresh_names:
        resolved_count, unresolved_count = _binding_list_refresh_target_resolution_counts(bindings)
        print(f"name refresh targets: resolved={resolved_count} unresolved={unresolved_count}")
    return 0


def _print_binding_status(data_dir: pathlib.Path, binding_id: str, *, instance_name: str = "") -> int:
    snapshot = _request(data_dir, "binding/status", {"binding_id": binding_id})
    live_runtime_owner, live_runtime_holders = _live_runtime_summary(snapshot)
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"binding: {snapshot['binding_id']}")
    print(f"kind: {snapshot['binding_kind']}")
    print(f"chat_id: {snapshot['chat_id']}")
    if snapshot["binding_kind"] == "p2p":
        print(f"sender_id: {snapshot['sender_id']}")
    print(f"working_dir: {display_path(snapshot['working_dir'])}")
    print(f"binding: {snapshot['binding_state']}")
    print(f"thread: {snapshot['thread_id'] or '-'} {snapshot['thread_title'] or ''}".rstrip())
    print(f"feishu push: {snapshot['feishu_runtime_state']}")
    print(f"current instance backend thread status: {snapshot['backend_thread_status']}")
    print(f"backend running turn: {'yes' if snapshot['backend_running_turn'] else 'no'}")
    print(f"live runtime owner: {live_runtime_owner}")
    print(f"live runtime holders: {', '.join(live_runtime_holders) or '（无）'}")
    print(f"current-instance interaction owner: {snapshot['interaction_owner']['label']}")
    if snapshot["next_prompt_allowed"]:
        print("next prompt: accepted")
    else:
        print(f"next prompt: blocked ({snapshot['next_prompt_reason_code']})")
        print(f"next prompt reason: {snapshot['next_prompt_reason']}")
    if snapshot["thread_id"]:
        availability = "available" if snapshot["detach_available"] else "blocked"
        print(f"detach: {availability}")
        if snapshot["detach_reason_code"]:
            print(f"detach reason code: {snapshot['detach_reason_code']}")
        if snapshot["detach_reason"]:
            print(f"detach reason: {snapshot['detach_reason']}")
    print(f"approval_policy: {snapshot['approval_policy']}")
    print(f"permissions_profile_id: {snapshot['permissions_profile_id']}")
    return 0


def _clear_binding(data_dir: pathlib.Path, binding_id: str) -> int:
    result = _request(data_dir, "binding/clear", {"binding_id": binding_id})
    print(f"cleared binding: {result['binding_id']}")
    print(f"thread: {result['thread_id'] or '-'} {result['thread_title'] or ''}".rstrip())
    return 0


def _attach_binding(data_dir: pathlib.Path, binding_id: str) -> int:
    result = _request(data_dir, "binding/attach", {"binding_id": binding_id}, timeout_seconds=30.0)
    print(f"binding: {result['binding_id']}")
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    if result.get("already_attached"):
        print("note: 该 binding 原本就已 attached。")
    else:
        print("note: 该 binding 已恢复 attached，可继续接收推送。")
    return 0


def _detach_binding(data_dir: pathlib.Path, binding_id: str) -> int:
    result = _request(data_dir, "binding/detach", {"binding_id": binding_id})
    print(f"binding: {result['binding_id']}")
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    print(f"backend thread status: {result['backend_thread_status']}")
    if result.get("already_detached"):
        print("note: 该 binding 原本就已 detached。")
    elif result.get("backend_still_loaded"):
        print("note: backend 仍保持 loaded；通常还有本地 fcodex 或其他外部订阅者。")
    else:
        print("note: 该 binding 已 detached；如果它是最后一个 attached 的 Feishu binding，服务已自动停止该 thread 的 Feishu 订阅。")
    return 0


def _clear_all_bindings(data_dir: pathlib.Path) -> int:
    result = _request(data_dir, "binding/clear-all")
    cleared_binding_ids = result.get("cleared_binding_ids") or []
    if result.get("already_empty"):
        print("当前没有可清除的 binding。")
        return 0
    print(f"cleared bindings: {', '.join(cleared_binding_ids) or '（无）'}")
    return 0


def _clear_stale_bindings(
    *,
    explicit_instance: str = "",
    dry_run: bool = False,
) -> int:
    normalized_explicit_instance = str(explicit_instance or "").strip()
    cleanup_results, cleanup_failures = (
        _offline_lifecycle().clear_stale_bindings(
            explicit_instance=normalized_explicit_instance,
            dry_run=dry_run,
        )
    )
    _print_stale_binding_cleanup_results(
        cleanup_results,
        cleanup_failures,
        dry_run=dry_run,
        scope_label=normalized_explicit_instance or "all known instances",
    )
    unknown_count = sum(
        len(item.get("unknown_threads") or []) for item in cleanup_results
    )
    return 1 if cleanup_failures or unknown_count else 0
def _print_stale_binding_cleanup_results(
    cleanup_results: list[dict[str, Any]],
    cleanup_failures: list[dict[str, str]],
    *,
    dry_run: bool,
    scope_label: str,
) -> None:
    action_key = "would_clear_binding_ids" if dry_run else "cleared_binding_ids"
    action_label = "would clear stale bindings" if dry_run else "cleared stale bindings"
    total_stale = 0
    total_unknown = 0
    print(f"scope: {scope_label}")
    if dry_run:
        print("mode: dry-run")
    if not cleanup_results and not cleanup_failures:
        print("instances: （无）")
        return
    for item in cleanup_results:
        binding_ids = list(item.get(action_key) or [])
        stale_thread_ids = list(item.get("stale_thread_ids") or [])
        unknown_threads = list(item.get("unknown_threads") or [])
        total_stale += len(binding_ids)
        total_unknown += len(unknown_threads)
        print(f"- {item.get('instance_name', '-')} ({item.get('mode', '-')}):")
        query_instance = str(item.get("query_instance_name", "") or "").strip()
        if query_instance:
            print(f"  query instance: {query_instance}")
        print(f"  {action_label}: {', '.join(binding_ids) or '（无）'}")
        if stale_thread_ids:
            print(f"  stale threads: {', '.join(stale_thread_ids)}")
        if unknown_threads:
            print("  unknown threads:")
            for unknown in unknown_threads:
                print(f"  - {unknown.get('thread_id', '-')}: {unknown.get('reason', '')}")
    if cleanup_failures:
        print("cleanup warnings:")
        for item in cleanup_failures:
            print(
                f"- {item.get('instance_name', '-')}"
                f" ({item.get('mode', '-')}): {item.get('reason', 'unknown error')}"
            )
    print(
        "summary: "
        f"instances={len(cleanup_results)} "
        f"{'would_clear' if dry_run else 'cleared'}={total_stale} "
        f"unknown_threads={total_unknown} "
        f"cleanup_failed={len(cleanup_failures)}"
    )


def _send_binding_prompt(
    data_dir: pathlib.Path,
    *,
    binding_id: str,
    text: str,
    actor_open_id: str = "",
    synthetic_source: str = "",
    display_mode: str = "silent",
    instance_name: str = "",
) -> int:
    result = _request(
        data_dir,
        "binding/submit-prompt",
        {
            "binding_id": binding_id,
            "text": text,
            "actor_open_id": actor_open_id,
            "synthetic_source": synthetic_source,
            "display_mode": display_mode,
        },
    )
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"binding: {result['binding_id']}")
    print(f"thread: {result.get('thread_id') or '-'}")
    print(f"display_mode: {result.get('display_mode') or 'silent'}")
    if result.get("synthetic_source"):
        print(f"synthetic_source: {result['synthetic_source']}")
    if result.get("started"):
        print("started: yes")
        print(f"turn_id: {result.get('turn_id') or '-'}")
        return 0
    if result.get("queued"):
        print("started: no")
        print("queued: yes")
        print(f"queue_position: {int(result.get('queue_position') or 0)}")
        return 0
    print("started: no")
    if result.get("reason_code"):
        print(f"reason code: {result['reason_code']}")
    if result.get("reason"):
        print(f"reason: {result['reason']}")
    return 1


def _print_thread_status(data_dir: pathlib.Path, target_params: dict[str, str], *, instance_name: str = "") -> int:
    snapshot = _request(data_dir, "thread/status", target_params)
    live_runtime_owner, live_runtime_holders = _live_runtime_summary(snapshot)
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"thread: {snapshot['thread_id']} {snapshot['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(snapshot['working_dir'])}")
    print(f"current instance backend thread status: {snapshot['backend_thread_status']}")
    print(f"backend running turn: {'yes' if snapshot['backend_running_turn'] else 'no'}")
    print(f"live runtime owner: {live_runtime_owner}")
    print(f"live runtime holders: {', '.join(live_runtime_holders) or '（无）'}")
    print(f"bound bindings: {', '.join(snapshot['bound_binding_ids']) or '（无）'}")
    print(f"attached bindings: {', '.join(snapshot['attached_binding_ids']) or '（无）'}")
    print(f"detached bindings: {', '.join(snapshot['detached_binding_ids']) or '（无）'}")
    print(f"current-instance interaction owner: {snapshot['interaction_owner']['label']}")
    availability = "available" if snapshot["detach_available"] else "blocked"
    print(f"detach: {availability}")
    if snapshot["detach_reason_code"]:
        print(f"detach reason code: {snapshot['detach_reason_code']}")
    if snapshot["detach_reason"]:
        print(f"detach reason: {snapshot['detach_reason']}")
    return 0


def _print_thread_bindings(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/bindings", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    bindings = result.get("bindings") or []
    if not bindings:
        print("bindings: （无）")
        return 0
    print("bindings:")
    for item in bindings:
        print(f"- {item['binding_id']} [{item['feishu_runtime_state']}]")
    return 0


def _print_thread_goal_result(result: dict[str, Any], *, instance_name: str = "", note: str = "") -> int:
    goal = result.get("goal")
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    if note:
        print(f"note: {note}")
    if not isinstance(goal, dict):
        print("goal: （无）")
        return 0
    objective = str(goal.get("objective", "") or "").strip()
    status = str(goal.get("status", "") or "").strip()
    print(f"objective: {objective or '-'}")
    print(f"status: {status or '-'} ({_goal_status_label(status)})")
    token_budget = goal.get("token_budget")
    print(f"token budget: {token_budget if token_budget is not None else '-'}")
    print(f"tokens used: {int(goal.get('tokens_used') or 0)}")
    print(f"time used: {int(goal.get('time_used_seconds') or 0)}s")
    print(f"created_at: {_format_goal_ts_seconds(goal.get('created_at'))}")
    print(f"updated_at: {_format_goal_ts_seconds(goal.get('updated_at'))}")
    return 0


def _print_thread_goal(data_dir: pathlib.Path, target_params: dict[str, str], *, instance_name: str = "") -> int:
    result = _request(data_dir, "thread/goal", target_params)
    return _print_thread_goal_result(result, instance_name=instance_name)


def _set_thread_goal(
    data_dir: pathlib.Path,
    target_params: dict[str, str],
    *,
    objective: str = "",
    status: str = "",
    instance_name: str = "",
) -> int:
    normalized_objective = str(objective or "").strip()
    normalized_status = str(status or "").strip()
    if not normalized_objective and not normalized_status:
        raise ValueError("thread goal set 至少需要 `--objective` 或 `--status`。")
    params: dict[str, Any] = dict(target_params)
    if normalized_objective:
        params["objective"] = normalized_objective
    if normalized_status:
        params["status"] = normalized_status
    result = _request(
        data_dir,
        "thread/goal/set",
        params,
    )
    return _print_thread_goal_result(result, instance_name=instance_name, note="当前 thread goal 已更新。")


def _clear_thread_goal(data_dir: pathlib.Path, target_params: dict[str, str], *, instance_name: str = "") -> int:
    result = _request(data_dir, "thread/goal/clear", target_params)
    note = "当前 thread goal 已清除。" if result.get("cleared") else "当前 thread 原本就没有 goal。"
    return _print_thread_goal_result(result, instance_name=instance_name, note=note)


def _print_thread_list(
    data_dir: pathlib.Path,
    *,
    scope: str,
    cwd: str,
    running_entry: InstanceRegistryEntry | None = None,
    archived: bool = False,
) -> int:
    adapter, cfg, app_server_url = _attached_endpoint_adapter(
        data_dir,
        running_entry=running_entry,
    )
    del app_server_url
    try:
        limit = cfg.thread_list_query_limit
        threads = (
            list_current_dir_threads(adapter, cwd=cwd, limit=limit, archived=archived)
            if scope == "cwd"
            else list_global_threads(adapter, limit=limit, archived=archived)
        )
    finally:
        adapter.stop()
    if not threads:
        print("当前没有可见的已归档线程。" if archived else "当前没有可见线程。")
        return 0
    rows: list[list[str]] = []
    for item in threads:
        rows.append(
            [
                item.thread_id,
                str(item.model_provider or "-"),
                display_path(item.cwd),
                _truncate_display_text(item.title, max_width=_THREAD_LIST_TITLE_MAX_WIDTH),
            ]
        )
    for line in _render_table(["THREAD_ID", "PROVIDER", "CWD", "TITLE"], rows):
        print(line)
    return 0


def _detach_thread(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/detach", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"detached bindings: {', '.join(result['detached_binding_ids']) or '（无）'}")
    print(f"backend thread status: {result['backend_thread_status']}")
    if result.get("detach_reason_code"):
        print(f"detach reason code: {result['detach_reason_code']}")
    if result["already_detached"]:
        print("note: Feishu push for this thread was already detached.")
    elif result["backend_still_loaded"]:
        print("note: backend is still loaded; external subscribers are still attached, typically local fcodex.")
    else:
        print("note: Feishu push for this thread has been detached while keeping bindings intact.")
    return 0


def _attach_thread(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/attach", target_params, timeout_seconds=30.0)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    print(f"attached bindings: {', '.join(result.get('attached_binding_ids') or []) or '（无）'}")
    if result.get("already_attached_binding_ids"):
        print(f"already attached bindings: {', '.join(result.get('already_attached_binding_ids') or [])}")
    if not result.get("changed"):
        print("note: 当前 thread 没有需要恢复的 detached 推送。")
    return 0


def _print_archive_cleanup_results(
    cleanup_results: list[dict[str, Any]],
    cleanup_failures: list[dict[str, str]],
    *,
    dry_run: bool = False,
    scope_label: str = "in other instances",
) -> None:
    non_empty_results = [item for item in cleanup_results if item.get("cleared_binding_ids")]
    action = "would clear bindings" if dry_run else "cleared bindings"
    header = f"{action} {scope_label}:" if scope_label else f"{action}:"
    if non_empty_results:
        print(header)
        for item in non_empty_results:
            print(
                f"- {item.get('instance_name', '-')}"
                f" ({item.get('mode', '-')}): "
                + (", ".join(item.get("cleared_binding_ids") or []) or "（无）")
            )
    elif cleanup_results:
        print(f"{header} （无）")
    if cleanup_failures:
        print("cleanup warnings:")
        for item in cleanup_failures:
            print(
                f"- {item.get('instance_name', '-')}"
                f" ({item.get('mode', '-')}): {item.get('reason', 'unknown error')}"
            )


def _clear_all_archived_thread_bindings(
    *,
    explicit_instance: str = "",
    dry_run: bool = False,
) -> int:
    lifecycle = _offline_lifecycle()
    query_instance_name, query_data_dir, query_running_entry = (
        lifecycle.resolve_archived_thread_listing_target(explicit_instance)
    )
    archived_thread_ids = lifecycle.list_archived_thread_ids_from_running_instance(
        query_data_dir,
        running_entry=query_running_entry,
    )
    print(f"archived query instance: {query_instance_name}")
    print(f"archived threads: {len(archived_thread_ids)}")
    print(f"scope: {explicit_instance or 'all known instances'}")
    if dry_run:
        print("mode: dry-run")
    if not archived_thread_ids:
        print("bindings: （无）")
        return 0

    changed_thread_count = 0
    cleared_binding_count = 0
    cleanup_failure_count = 0
    for thread_id in archived_thread_ids:
        cleanup_results, cleanup_failures = (
            lifecycle.cleanup_archived_thread_bindings_in_scope(
            thread_id,
            explicit_instance=explicit_instance,
            dry_run=dry_run,
            )
        )
        thread_cleared_count = sum(len(item.get("cleared_binding_ids") or []) for item in cleanup_results)
        if thread_cleared_count or cleanup_failures:
            print()
            print(f"thread: {thread_id}")
            _print_archive_cleanup_results(
                cleanup_results,
                cleanup_failures,
                dry_run=dry_run,
                scope_label="",
            )
        if thread_cleared_count:
            changed_thread_count += 1
            cleared_binding_count += thread_cleared_count
        cleanup_failure_count += len(cleanup_failures)

    action = "would_clear_bindings" if dry_run else "cleared_bindings"
    print()
    print(
        "summary: "
        f"archived_threads={len(archived_thread_ids)} "
        f"threads_with_bindings={changed_thread_count} "
        f"{action}={cleared_binding_count} "
        f"cleanup_failed={cleanup_failure_count}"
    )
    return 1 if cleanup_failure_count else 0


def _clear_archived_thread_bindings(
    thread_id: str = "",
    *,
    all_archived: bool = False,
    explicit_instance: str = "",
    dry_run: bool = False,
) -> int:
    normalized_thread_id = str(thread_id or "").strip()
    if bool(normalized_thread_id) == bool(all_archived):
        raise ValueError("thread clear-archived-bindings 必须且只能提供 --thread-id 或 --all。")
    if all_archived:
        return _clear_all_archived_thread_bindings(
            explicit_instance=explicit_instance,
            dry_run=dry_run,
        )
    cleanup_results, cleanup_failures = (
        _offline_lifecycle().cleanup_archived_thread_bindings_in_scope(
            normalized_thread_id,
            explicit_instance=explicit_instance,
            dry_run=dry_run,
        )
    )
    print(f"thread: {normalized_thread_id}")
    print(f"scope: {explicit_instance or 'all known instances'}")
    if dry_run:
        print("mode: dry-run")
    if not cleanup_results and not cleanup_failures:
        print("instances: （无）")
        return 0
    _print_archive_cleanup_results(
        cleanup_results,
        cleanup_failures,
        dry_run=dry_run,
        scope_label="",
    )
    return 1 if cleanup_failures else 0


def _print_lifecycle_non_success(result: dict[str, Any], *, action: str) -> int:
    outcome = str(result.get("upstream_outcome", "error") or "error")
    print(f"upstream outcome: {outcome}")
    if outcome == "unknown" and result.get("outcome_detail"):
        print(f"diagnostic: {result['outcome_detail']}")
    elif result.get("upstream_error"):
        print(f"upstream error: {result['upstream_error']}")
    print("focus cleanup: skipped")
    if outcome == "unknown":
        print(f"note: {action} 请求可能仍已执行；请先核对 thread 状态，不要自动重试。")
        return 3
    print(f"note: 上游明确返回错误；{action} 仍可能伴随局部副作用。")
    return 1


def _archive_thread(
    data_dir: pathlib.Path,
    target_params: dict[str, str],
    *,
    instance_name: str = "",
    running_entry: InstanceRegistryEntry | None = None,
) -> int:
    thread_id = str(target_params.get("thread_id", "") or "").strip()
    if not thread_id or str(target_params.get("thread_name", "") or "").strip():
        raise ValueError("thread archive mutation 只接受已解析的 thread_id。")
    try:
        receipt = _offline_lifecycle().archive_thread(
            data_dir,
            thread_id,
            instance_name=instance_name,
            running_entry=running_entry,
        )
    except ServiceControlOutcomeUnknownError as exc:
        if instance_name:
            print(f"instance: {instance_name}")
        print("upstream outcome: unknown")
        print(f"reason: {exc}")
        print("focus cleanup: skipped")
        print("note: archive 请求可能仍在 service 中执行；请先核对 archived 列表，不要自动重试。")
        return 3
    result = receipt.result
    if str(result.get("upstream_outcome", "success") or "success") != "success":
        if instance_name:
            print(f"instance: {instance_name}")
        print(f"thread: {result.get('thread_id') or target_params.get('thread_id') or '-'}")
        return _print_lifecycle_non_success(result, action="archive")
    cleanup_results = list(receipt.cleanup_results)
    cleanup_failures = list(receipt.cleanup_failures)
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    print("upstream outcome: success")
    print(f"focus cleanup in this instance: {result.get('focus_cleanup') or 'complete'}")
    print(f"cleared bindings in this instance: {', '.join(result.get('cleared_binding_ids') or []) or '（无）'}")
    for cleanup_error in result.get("cleanup_errors") or []:
        print(f"cleanup warning: {cleanup_error}")
    _print_archive_cleanup_results(cleanup_results, cleanup_failures)
    print("note: 归档完成；该 thread 会从常规列表中隐藏，不是硬删除。")
    print("note: 安全范围仅覆盖本机已知 Focus/fcodex runtime；不包含裸 Codex、IDE 或其他机器。")
    if cleanup_failures:
        print(
            "note: 已尝试清理其他可达运行实例与已知非运行实例里的本地 bindings；"
            "部分实例未完成，见 cleanup warnings。"
        )
    else:
        print("note: 已清理其他可达运行实例与已知非运行实例里指向该 thread 的本地 bindings。")
    return 1 if cleanup_failures or result.get("focus_cleanup") == "incomplete" else 0


def _archive_threads(thread_ids: list[str], *, explicit_instance: str = "") -> int:
    normalized_thread_ids = [str(item or "").strip() for item in thread_ids if str(item or "").strip()]
    if not normalized_thread_ids:
        raise ValueError("thread archive 缺少目标。")
    if len(normalized_thread_ids) == 1:
        thread_id = normalized_thread_ids[0]
        target = _resolve_target_instance(
            explicit_instance or None,
            preferred_running_instance="" if explicit_instance else _lease_owner_instance(thread_id),
        )
        return _archive_thread(
            target.data_dir,
            {"thread_id": thread_id},
            instance_name=target.instance_name,
            running_entry=target.running_entry,
        )

    success_count = 0
    failure_count = 0
    cleanup_failure_count = 0
    resolved_explicit_target = _resolve_target_instance(explicit_instance) if explicit_instance else None
    lifecycle = _offline_lifecycle()
    print(f"batch archive: total={len(normalized_thread_ids)}")
    for index, requested_thread_id in enumerate(normalized_thread_ids, start=1):
        print(f"[{index}/{len(normalized_thread_ids)}] thread: {requested_thread_id or '-'}")
        try:
            target = resolved_explicit_target or _resolve_target_instance(
                None,
                preferred_running_instance=_lease_owner_instance(requested_thread_id),
            )
        except ValueError as exc:
            failure_count += 1
            print("status: failed")
            print(f"reason: {exc}")
            if index != len(normalized_thread_ids):
                print()
            continue
        print(f"instance: {target.instance_name}")
        try:
            receipt = lifecycle.archive_thread(
                target.data_dir,
                requested_thread_id,
                instance_name=target.instance_name,
                running_entry=target.running_entry,
            )
        except ServiceControlOutcomeUnknownError as exc:
            print("status: unknown")
            print(f"reason: {exc}")
            print("note: batch 已停止；该请求可能仍在执行，请先人工核对。")
            print()
            print(
                f"summary: archived={success_count} failed={failure_count} "
                f"unknown=1 cleanup_failed={cleanup_failure_count}"
            )
            return 3
        except ServiceControlError as exc:
            failure_count += 1
            print("status: failed")
            print(f"reason: {exc}")
        else:
            result = receipt.result
            upstream_outcome = str(result.get("upstream_outcome", "success") or "success")
            if upstream_outcome == "unknown":
                print("status: unknown")
                _print_lifecycle_non_success(result, action="archive")
                print("note: batch 已停止，请先人工核对。")
                print()
                print(
                    f"summary: archived={success_count} failed={failure_count} "
                    f"unknown=1 cleanup_failed={cleanup_failure_count}"
                )
                return 3
            if upstream_outcome != "success":
                failure_count += 1
                print("status: failed")
                _print_lifecycle_non_success(result, action="archive")
                if index != len(normalized_thread_ids):
                    print()
                continue
            success_count += 1
            print("status: archived")
            print(f"resolved thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
            print(f"working_dir: {display_path(result['working_dir'])}")
            print(f"focus cleanup in this instance: {result.get('focus_cleanup') or 'complete'}")
            print(
                "cleared bindings in this instance: "
                + (", ".join(result.get("cleared_binding_ids") or []) or "（无）")
            )
            if result.get("focus_cleanup") == "incomplete":
                cleanup_failure_count += 1
            for cleanup_error in result.get("cleanup_errors") or []:
                print(f"cleanup warning: {cleanup_error}")
            cleanup_results = list(receipt.cleanup_results)
            cleanup_failures = list(receipt.cleanup_failures)
            _print_archive_cleanup_results(cleanup_results, cleanup_failures)
            cleanup_failure_count += len(cleanup_failures)
        if index != len(normalized_thread_ids):
            print()
    print()
    print(f"summary: archived={success_count} failed={failure_count} cleanup_failed={cleanup_failure_count}")
    print("note: 每个 thread 都按现有单线程 archive 语义独立路由、独立执行。")
    if cleanup_failure_count:
        print("note: archive 成功项的 Focus cleanup 已尝试执行；部分清理未完成，见各项 warning 和 summary。")
    else:
        print("note: archive 成功项已完成当前可见范围内的 Focus binding 清理。")
    print("note: 安全范围仅覆盖本机已知 Focus/fcodex runtime；不包含裸 Codex、IDE 或其他机器。")
    return 0 if failure_count == 0 and cleanup_failure_count == 0 else 1


def _unarchive_thread(
    data_dir: pathlib.Path,
    thread_id: str,
    *,
    instance_name: str,
    running_entry: InstanceRegistryEntry | None = None,
) -> int:
    try:
        receipt = _offline_lifecycle().unarchive_thread(
            data_dir,
            thread_id,
            running_entry=running_entry,
        )
    except ServiceControlOutcomeUnknownError as exc:
        print(f"instance: {instance_name}")
        print(f"thread: {thread_id}")
        print("upstream outcome: unknown")
        print(f"reason: {exc}")
        print("focus cleanup: skipped")
        print("note: unarchive 可能已经发生；请先查看 active/archived 列表，不要自动重试。")
        return 3
    result = receipt.result
    print(f"instance: {instance_name}")
    resolved_thread_id = str(result.get("thread_id") or thread_id)
    print(f"thread: {resolved_thread_id} {result.get('thread_title') or ''}".rstrip())
    if str(result.get("upstream_outcome", "success") or "success") != "success":
        return _print_lifecycle_non_success(result, action="unarchive")
    print(f"working_dir: {display_path(result.get('working_dir') or '')}")
    print("upstream outcome: success")
    print("focus cleanup: skipped")
    print("note: thread 已恢复为未归档状态并回到常规列表；当前仍未加载，也未创建 binding。")
    print(f"next (本地): focus resume {resolved_thread_id}")
    print(f"next (飞书): /resume {resolved_thread_id}")
    print("note: 安全范围仅覆盖本机已知 Focus/fcodex runtime；不包含裸 Codex、IDE 或其他机器。")
    return 0


def _unarchive_threads(thread_ids: list[str], *, explicit_instance: str = "") -> int:
    normalized_thread_ids = list(
        dict.fromkeys(str(item or "").strip() for item in thread_ids if str(item or "").strip())
    )
    if not normalized_thread_ids:
        raise ValueError("thread unarchive 缺少目标。")
    target = _resolve_target_instance(explicit_instance or None)
    if target.running_entry is None:
        raise ValueError("thread unarchive 需要目标 Focus 实例正在运行。")
    if len(normalized_thread_ids) == 1:
        return _unarchive_thread(
            target.data_dir,
            normalized_thread_ids[0],
            instance_name=target.instance_name,
            running_entry=target.running_entry,
        )

    success_count = 0
    failure_count = 0
    print(f"batch unarchive: total={len(normalized_thread_ids)}")
    for index, thread_id in enumerate(normalized_thread_ids, start=1):
        print(f"[{index}/{len(normalized_thread_ids)}] requested thread: {thread_id}")
        try:
            result = _unarchive_thread(
                target.data_dir,
                thread_id,
                instance_name=target.instance_name,
                running_entry=target.running_entry,
            )
        except ServiceControlOutcomeUnknownError as exc:
            print("status: unknown")
            print(f"reason: {exc}")
            print("note: batch 已停止；该请求可能仍在执行，请先人工核对。")
            print()
            print(f"summary: unarchived={success_count} failed={failure_count} unknown=1")
            return 3
        except (ServiceControlError, ValueError) as exc:
            failure_count += 1
            print(f"instance: {target.instance_name}")
            print("status: failed")
            print(f"reason: {exc}")
        else:
            if result == 3:
                print("status: unknown")
                print("note: batch 已停止；请先人工核对该 thread 的 active/archived 状态。")
                print()
                print(f"summary: unarchived={success_count} failed={failure_count} unknown=1")
                return 3
            if result == 0:
                success_count += 1
                print("status: unarchived")
            else:
                failure_count += 1
                print("status: failed")
        if index != len(normalized_thread_ids):
            print()
    print()
    print(f"summary: unarchived={success_count} failed={failure_count}")
    print("note: 每个 thread 都独立执行；已成功项不会因后续失败而回滚。")
    return 0 if failure_count == 0 else 1


def _confirm_delete_thread(thread_id: str, *, force: bool) -> bool:
    print("安全范围：Focus 仅协调本机已知 Focus/fcodex runtime；请先停止其他客户端对该 thread 的使用。")
    if force:
        return True
    if not sys.stdin.isatty():
        raise ValueError("非交互环境执行 thread delete 必须显式提供 `--force`。")
    print(f"将永久删除 thread `{thread_id}`。")
    print("Codex 可能同时级联删除 spawned descendants；Focus 不声称能完整预览该集合。")
    answer = input("继续？输入 yes 确认: ").strip().lower()
    return answer == "yes"


def _delete_thread(
    data_dir: pathlib.Path,
    thread_id: str,
    *,
    instance_name: str,
    force: bool,
    running_entry: InstanceRegistryEntry | None = None,
) -> int:
    lifecycle = _offline_lifecycle()
    try:
        receipt = lifecycle.delete_thread(
            data_dir,
            thread_id,
            instance_name=instance_name,
            confirm=lambda target_thread_id: _confirm_delete_thread(
                target_thread_id,
                force=force,
            ),
            running_entry=running_entry,
        )
    except ServiceControlOutcomeUnknownError as exc:
        print(f"instance: {instance_name}")
        print(f"thread: {thread_id}")
        print("upstream outcome: unknown")
        print(f"reason: {exc}")
        print("focus cleanup: skipped")
        print("note: delete 可能已经发生；请先核对 thread 状态，不要自动重试。")
        return 3
    if receipt is None:
        print("已取消。")
        return 1
    result = receipt.result
    print(f"instance: {instance_name}")
    print(f"thread: {result.get('thread_id') or thread_id} {result.get('thread_title') or ''}".rstrip())
    if str(result.get("upstream_outcome", "success") or "success") != "success":
        return _print_lifecycle_non_success(result, action="delete")

    cleanup_results = list(receipt.cleanup_results)
    cleanup_failures = list(receipt.cleanup_failures)
    print("upstream outcome: success")
    print(f"focus cleanup in this instance: {result.get('focus_cleanup') or 'complete'}")
    print(f"cleared bindings in this instance: {', '.join(result.get('cleared_binding_ids') or []) or '（无）'}")
    for cleanup_error in result.get("cleanup_errors") or []:
        print(f"cleanup warning: {cleanup_error}")
    _print_archive_cleanup_results(cleanup_results, cleanup_failures)
    print("note: thread 已由上游永久删除；上游可能同时删除 spawned descendants。")
    print("note: 未被 Focus 预先发现的 descendant binding 可用 `binding clear-stale --dry-run` 检查。")
    return 1 if cleanup_failures or result.get("focus_cleanup") == "incomplete" else 0


def _send_thread_image(
    data_dir: pathlib.Path,
    target_params: dict[str, str],
    *,
    local_path: str,
    instance_name: str = "",
) -> int:
    result = _request(
        data_dir,
        "thread/send-image",
        {
            **target_params,
            "local_path": local_path,
        },
    )
    if instance_name:
        print(f"instance: {instance_name}")
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(result['working_dir'])}")
    print(f"local_path: {display_path(result['local_path'])}")
    print(f"delivered bindings: {', '.join(result['delivered_binding_ids']) or '（无）'}")
    if result.get("failed_binding_ids"):
        print(f"failed bindings: {', '.join(result['failed_binding_ids'])}")
        print("note: 图片只完成部分投递；若重试，已成功的 binding 可能会再次收到同一张图片。")
        return 1
    return 0


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = cli_inputs.build_runtime_admin_parser()
    args = parser.parse_args(argv)
    try:
        if args.resource == "image" and args.action == "send":
            target_params, preferred_thread_id = cli_inputs.image_send_target_params(args)
            target = _resolve_target_instance(
                args.instance,
                preferred_running_instance=_lease_owner_instance(preferred_thread_id),
            )
            raise SystemExit(
                _send_thread_image(
                    target.data_dir,
                    target_params,
                    local_path=args.path,
                    instance_name=target.instance_name,
                )
            )
        if args.resource == "thread" and args.action == "archive":
            thread_ids, thread_name = cli_inputs.thread_archive_inputs(args)
            if thread_name:
                target, target_params = _resolve_thread_archive_target(args)
                raise SystemExit(
                    _archive_thread(
                        target.data_dir,
                        target_params,
                        instance_name=target.instance_name,
                        running_entry=target.running_entry,
                    )
                )
            raise SystemExit(_archive_threads(thread_ids, explicit_instance=str(args.instance or "").strip()))
        if args.resource == "thread" and args.action == "unarchive":
            raise SystemExit(
                _unarchive_threads(
                    cli_inputs.thread_unarchive_inputs(args),
                    explicit_instance=str(args.instance or "").strip(),
                )
            )
        if args.resource == "thread" and args.action == "delete":
            thread_id = cli_inputs.thread_delete_input(args)
            target = _resolve_target_instance(
                args.instance,
                preferred_running_instance="" if args.instance else _lease_owner_instance(thread_id),
            )
            if target.running_entry is None:
                raise ValueError("thread delete 需要目标 Focus 实例正在运行。")
            raise SystemExit(
                _delete_thread(
                    target.data_dir,
                    thread_id,
                    instance_name=target.instance_name,
                    force=bool(args.force),
                    running_entry=target.running_entry,
                )
            )
        if args.resource == "thread" and args.action == "clear-archived-bindings":
            raise SystemExit(
                _clear_archived_thread_bindings(
                    getattr(args, "thread_id", "") or "",
                    all_archived=bool(getattr(args, "all_archived", False)),
                    explicit_instance=str(args.instance or "").strip(),
                    dry_run=bool(args.dry_run),
                )
            )
        if args.resource == "binding" and args.action == "clear-stale":
            raise SystemExit(
                _clear_stale_bindings(
                    explicit_instance=str(args.instance or "").strip(),
                    dry_run=bool(args.dry_run),
                )
            )
        target = _resolve_target_instance(args.instance)
        data_dir = target.data_dir
        if args.resource == "web" and args.action == "open":
            raise SystemExit(_open_web(data_dir, no_browser=bool(args.no_browser)))
        if args.resource == "service" and args.action == "reset-backend":
            raise SystemExit(
                reset_service_backend(
                    data_dir,
                    force=bool(args.force),
                    instance_name=target.instance_name,
                )
            )
        if args.resource == "service" and args.action == "attach":
            raise SystemExit(_attach_service(data_dir))
        if args.resource == "binding" and args.action == "list":
            raise SystemExit(
                _print_binding_list(
                    data_dir,
                    refresh_names=bool(args.refresh_names),
                    instance_name=target.instance_name,
                )
            )
        if args.resource == "binding" and args.action == "status":
            raise SystemExit(_print_binding_status(data_dir, args.binding_id, instance_name=target.instance_name))
        if args.resource == "binding" and args.action == "attach":
            raise SystemExit(_attach_binding(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "detach":
            raise SystemExit(_detach_binding(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear":
            raise SystemExit(_clear_binding(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear-all":
            raise SystemExit(_clear_all_bindings(data_dir))
        if args.resource == "prompt" and args.action == "send":
            raise SystemExit(
                _send_binding_prompt(
                    data_dir,
                    binding_id=args.binding_id,
                    text=cli_inputs.prompt_text_from_args(args),
                    actor_open_id=args.actor_open_id,
                    synthetic_source=args.synthetic_source,
                    display_mode=args.display_mode,
                    instance_name=target.instance_name,
                )
            )
        if args.resource == "thread" and args.action == "list":
            cwd = str(args.cwd or "").strip() or os.getcwd()
            raise SystemExit(
                _print_thread_list(
                    data_dir,
                    scope=args.scope,
                    cwd=cwd,
                    running_entry=target.running_entry,
                    archived=bool(args.archived),
                )
            )
        if args.resource == "thread" and args.action == "status":
            raise SystemExit(
                _print_thread_status(
                    data_dir,
                    cli_inputs.thread_target_params(args),
                    instance_name=target.instance_name,
                )
            )
        if args.resource == "thread" and args.action == "goal":
            goal_action = str(getattr(args, "goal_action", "") or "show").strip() or "show"
            if goal_action == "show":
                raise SystemExit(
                    _print_thread_goal(
                        data_dir,
                        cli_inputs.thread_target_params(args),
                        instance_name=target.instance_name,
                    )
                )
            if goal_action == "set":
                raise SystemExit(
                    _set_thread_goal(
                        data_dir,
                        cli_inputs.thread_target_params(args),
                        objective=str(args.objective or ""),
                        status=str(args.status or ""),
                        instance_name=target.instance_name,
                    )
                )
            if goal_action == "clear":
                raise SystemExit(
                    _clear_thread_goal(
                        data_dir,
                        cli_inputs.thread_target_params(args),
                        instance_name=target.instance_name,
                    )
                )
        if args.resource == "thread" and args.action == "bindings":
            raise SystemExit(_print_thread_bindings(data_dir, cli_inputs.thread_target_params(args)))
        if args.resource == "thread" and args.action == "attach":
            raise SystemExit(_attach_thread(data_dir, cli_inputs.thread_target_params(args)))
        if args.resource == "thread" and args.action == "detach":
            raise SystemExit(_detach_thread(data_dir, cli_inputs.thread_target_params(args)))
    except ServiceControlOutcomeUnknownError as exc:
        print(f"控制面请求结果未知：{exc}", file=sys.stderr)
        raise SystemExit(3)
    except ServiceControlError as exc:
        print(f"控制面请求失败：{exc}", file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    parser.print_usage(sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
