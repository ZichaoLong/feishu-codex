"""Local admin CLI for the running feishu-codex service."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from dataclasses import replace
from typing import Any

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.config import load_config_file
from bot.constants import display_path
from bot.env_file import load_env_file
from bot.instance_resolution import list_running_instances, resolve_cli_instance_target, resolve_running_instance_app_server_url
from bot.platform_paths import default_data_root
from bot.thread_resolution import list_current_dir_threads, list_global_threads
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.stores.service_instance_lease import ServiceInstanceLease


class _HelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    pass


def _data_dir() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root()


def _resolve_target_data_dir(explicit_instance: str | None) -> pathlib.Path:
    return resolve_cli_instance_target(explicit_instance).data_dir


def _request(data_dir: pathlib.Path, method: str, params: dict[str, Any] | None = None) -> Any:
    return control_request(data_dir, method, params)


def _remote_adapter(data_dir: pathlib.Path) -> tuple[CodexAppServerAdapter, dict[str, Any], str]:
    cfg = load_config_file("codex")
    configured_url = str(cfg.get("app_server_url", "ws://127.0.0.1:8765")).strip() or "ws://127.0.0.1:8765"
    app_server_url = resolve_effective_app_server_url(configured_url, data_dir=data_dir)
    config = replace(
        CodexAppServerConfig.from_dict(cfg),
        app_server_mode="remote",
        app_server_url=app_server_url,
    )
    return CodexAppServerAdapter(config), cfg, app_server_url


def _thread_target_params(args: argparse.Namespace) -> dict[str, str]:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    thread_name = str(getattr(args, "thread_name", "") or "").strip()
    if bool(thread_id) == bool(thread_name):
        raise ValueError("必须且只能提供 --thread-id 或 --thread-name。")
    if thread_id:
        return {"thread_id": thread_id}
    return {"thread_name": thread_name}


def _print_service_status(data_dir: pathlib.Path) -> int:
    metadata = ServiceInstanceLease(data_dir).load_metadata()
    published_endpoint = metadata.control_endpoint if metadata is not None else ""
    try:
        result = _request(data_dir, "service/status")
    except ServiceControlError as exc:
        print("service: stopped")
        print(f"control endpoint: {published_endpoint or 'unavailable'}")
        runtime = AppServerRuntimeStore(data_dir).load_managed_runtime()
        if runtime is not None:
            print(f"last known app server: {runtime.active_url}")
        print(f"reason: {exc}")
        return 3
    if result.get("instance_name"):
        print(f"instance: {result['instance_name']}")
    print("service: running")
    print(f"pid: {result['pid']}")
    print(f"control endpoint: {result['control_endpoint']}")
    print(f"app server: {result['app_server_url']}")
    print(f"app server mode: {result.get('app_server_mode', '-')}")
    print(f"bindings: total={result['binding_count']} bound={result['bound_binding_count']} attached={result['attached_binding_count']}")
    print(f"threads: bound={result['thread_count']} feishu-attached={result['attached_thread_count']} loaded={result['loaded_thread_count']}")
    print(f"running bindings: {', '.join(result['running_binding_ids']) or '（无）'}")
    print(f"backend reset: {result.get('backend_reset_status', '-')}")
    if result.get("backend_reset_reason_code"):
        print(f"backend reset reason code: {result['backend_reset_reason_code']}")
    if result.get("backend_reset_reason"):
        print(f"backend reset reason: {result['backend_reset_reason']}")
    return 0


def _reset_service_backend(data_dir: pathlib.Path, *, force: bool) -> int:
    result = control_request(
        data_dir,
        "service/reset-backend",
        {"force": bool(force)},
        timeout_seconds=30.0,
    )
    print("backend reset: ok")
    print(f"force: {'yes' if result.get('force') else 'no'}")
    print(f"app server: {result.get('app_server_url', '-')}")
    print(f"released bindings: {', '.join(result.get('released_binding_ids') or []) or '（无）'}")
    print(f"interrupted bindings: {', '.join(result.get('interrupted_binding_ids') or []) or '（无）'}")
    print(f"fail-closed requests: {int(result.get('fail_closed_request_count') or 0)}")
    print(f"purged runtime leases: {', '.join(result.get('purged_thread_ids') or []) or '（无）'}")
    return 0


def _print_binding_list(data_dir: pathlib.Path) -> int:
    result = _request(data_dir, "binding/list")
    bindings = result.get("bindings") or []
    if not bindings:
        print("当前没有可见 binding。")
        return 0
    print("BINDING_ID\tKIND\tSTATE\tRUNTIME\tTHREAD\tCWD")
    for item in bindings:
        thread = item["thread_id"][:8] + "…" if item["thread_id"] else "-"
        cwd = display_path(str(item["working_dir"] or ""))
        print(
            "\t".join(
                [
                    item["binding_id"],
                    item["binding_kind"],
                    item["binding_state"],
                    item["feishu_runtime_state"],
                    thread,
                    cwd,
                ]
            )
        )
    return 0


def _print_binding_status(data_dir: pathlib.Path, binding_id: str) -> int:
    snapshot = _request(data_dir, "binding/status", {"binding_id": binding_id})
    print(f"binding: {snapshot['binding_id']}")
    print(f"kind: {snapshot['binding_kind']}")
    print(f"chat_id: {snapshot['chat_id']}")
    if snapshot["binding_kind"] == "p2p":
        print(f"sender_id: {snapshot['sender_id']}")
    print(f"working_dir: {display_path(snapshot['working_dir'])}")
    print(f"binding: {snapshot['binding_state']}")
    print(f"thread: {snapshot['thread_id'] or '-'} {snapshot['thread_title'] or ''}".rstrip())
    print(f"feishu runtime: {snapshot['feishu_runtime_state']}")
    print(f"backend thread status: {snapshot['backend_thread_status']}")
    print(f"backend running turn: {'yes' if snapshot['backend_running_turn'] else 'no'}")
    print(f"interaction owner: {snapshot['interaction_owner']['label']}")
    if snapshot["next_prompt_allowed"]:
        print("next prompt: accepted")
    else:
        print(f"next prompt: blocked ({snapshot['next_prompt_reason_code']})")
        print(f"next prompt reason: {snapshot['next_prompt_reason']}")
    print(f"re-profile possible: {'yes' if snapshot['reprofile_possible'] else 'no'}")
    if snapshot["thread_id"]:
        availability = "available" if snapshot["unsubscribe_available"] else "blocked"
        print(f"unsubscribe: {availability}")
        if snapshot["unsubscribe_reason_code"]:
            print(f"unsubscribe reason code: {snapshot['unsubscribe_reason_code']}")
        if snapshot["unsubscribe_reason"]:
            print(f"unsubscribe reason: {snapshot['unsubscribe_reason']}")
    print(f"approval_policy: {snapshot['approval_policy']}")
    print(f"sandbox: {snapshot['sandbox']}")
    print(f"collaboration_mode: {snapshot['collaboration_mode']}")
    return 0


def _clear_binding(data_dir: pathlib.Path, binding_id: str) -> int:
    result = _request(data_dir, "binding/clear", {"binding_id": binding_id})
    print(f"cleared binding: {result['binding_id']}")
    print(f"thread: {result['thread_id'] or '-'} {result['thread_title'] or ''}".rstrip())
    return 0


def _clear_all_bindings(data_dir: pathlib.Path) -> int:
    result = _request(data_dir, "binding/clear-all")
    cleared_binding_ids = result.get("cleared_binding_ids") or []
    if result.get("already_empty"):
        print("当前没有可清除的 binding。")
        return 0
    print(f"cleared bindings: {', '.join(cleared_binding_ids) or '（无）'}")
    return 0


def _print_thread_status(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    snapshot = _request(data_dir, "thread/status", target_params)
    print(f"thread: {snapshot['thread_id']} {snapshot['thread_title'] or ''}".rstrip())
    print(f"working_dir: {display_path(snapshot['working_dir'])}")
    print(f"backend thread status: {snapshot['backend_thread_status']}")
    print(f"backend running turn: {'yes' if snapshot['backend_running_turn'] else 'no'}")
    print(f"bound bindings: {', '.join(snapshot['bound_binding_ids']) or '（无）'}")
    print(f"attached bindings: {', '.join(snapshot['attached_binding_ids']) or '（无）'}")
    print(f"released bindings: {', '.join(snapshot['released_binding_ids']) or '（无）'}")
    print(f"interaction owner: {snapshot['interaction_owner']['label']}")
    print(f"re-profile possible: {'yes' if snapshot['reprofile_possible'] else 'no'}")
    availability = "available" if snapshot["unsubscribe_available"] else "blocked"
    print(f"unsubscribe: {availability}")
    if snapshot["unsubscribe_reason_code"]:
        print(f"unsubscribe reason code: {snapshot['unsubscribe_reason_code']}")
    if snapshot["unsubscribe_reason"]:
        print(f"unsubscribe reason: {snapshot['unsubscribe_reason']}")
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


def _print_thread_list(data_dir: pathlib.Path, *, scope: str, cwd: str) -> int:
    adapter, cfg, app_server_url = _remote_adapter(data_dir)
    del app_server_url
    try:
        limit = int(cfg.get("thread_list_query_limit", 100))
        threads = (
            list_current_dir_threads(adapter, cwd=cwd, limit=limit)
            if scope == "cwd"
            else list_global_threads(adapter, limit=limit)
        )
    finally:
        adapter.stop()
    if not threads:
        print("当前没有可见线程。")
        return 0
    print("THREAD_ID\tPROVIDER\tCWD\tTITLE")
    for item in threads:
        print(
            "\t".join(
                [
                    item.thread_id,
                    str(item.model_provider or "-"),
                    display_path(item.cwd),
                    item.title,
                ]
            )
        )
    return 0


def _unsubscribe_thread(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/unsubscribe", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"released bindings: {', '.join(result['released_binding_ids']) or '（无）'}")
    print(f"backend thread status: {result['backend_thread_status']}")
    print(f"re-profile possible: {'yes' if result['reprofile_possible'] else 'no'}")
    if result.get("unsubscribe_reason_code"):
        print(f"unsubscribe reason code: {result['unsubscribe_reason_code']}")
    if result["already_released"]:
        print("note: Feishu runtime was already released.")
    elif result["backend_still_loaded"]:
        print("note: backend is still loaded; external subscribers are still attached, typically local fcodex.")
    else:
        print("note: Feishu has released its runtime residency for this thread while keeping bindings intact.")
    return 0


def _list_running_instances() -> int:
    instances = list_running_instances()
    if not instances:
        print("当前没有运行中的实例。")
        return 0
    print("INSTANCE\tPID\tCONTROL\tAPP_SERVER")
    for item in instances:
        control = item.control_endpoint
        app_server = resolve_running_instance_app_server_url(item) or "-"
        print(f"{item.instance_name}\t{item.owner_pid}\t{control}\t{app_server}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feishu-codexctl",
        description=(
            "本地查看 / 管理面：查看运行中的 feishu-codex service、binding、thread 与实例。\n\n"
            "说明：\n"
            "- `feishu-codexctl` 是本地查看 / 管理面，不是第二个 Codex 前端\n"
            "- 除 `instance list` 外，其余命令默认作用于 `default` 实例；多实例时请显式加 `--instance <name>`\n"
            "- `binding clear` / `clear-all` 清的是 Feishu 本地 bookmark，不删除 thread，也不等于 `unsubscribe`\n"
            "- `thread list` 默认列当前目录线程，也支持 `--scope global`\n"
        ),
        epilog=(
            "常用命令:\n"
            "  feishu-codexctl service status\n"
            "  feishu-codexctl service reset-backend\n"
            "  feishu-codexctl instance list\n"
            "  feishu-codexctl binding list\n"
            "  feishu-codexctl binding status <binding_id>\n"
            "  feishu-codexctl thread list --scope cwd\n"
            "  feishu-codexctl thread status --thread-id <id>\n"
            "  feishu-codexctl thread unsubscribe --thread-name <name>\n"
            "\n"
            "多实例:\n"
            "  feishu-codexctl --instance corp-a service status\n"
            "  feishu-codexctl --instance corp-a thread status --thread-name demo\n"
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--instance",
        help="目标实例；省略时默认连 `default`。仅 `instance list` 不使用这个参数。",
    )
    subparsers = parser.add_subparsers(dest="resource", required=True, title="resources", metavar="resource")

    instance = subparsers.add_parser(
        "instance",
        help="查看运行中的实例注册表。",
        description="实例发现面。当前只提供 `list`，用于查看本机运行中的实例及其控制面地址。",
        formatter_class=_HelpFormatter,
    )
    instance_sub = instance.add_subparsers(dest="action", required=True, title="instance commands", metavar="instance-command")
    instance_sub.add_parser(
        "list",
        help="列出运行中的实例。",
        description="列出本机运行中的实例、owner pid、control endpoint 与 app-server 地址。",
        formatter_class=_HelpFormatter,
    )

    service = subparsers.add_parser(
        "service",
        help="查看目标实例的服务状态。",
        description="服务查看面。用于确认目标实例是否在运行，以及当前 control plane / app-server 发现状态。",
        formatter_class=_HelpFormatter,
    )
    service_sub = service.add_subparsers(dest="action", required=True, title="service commands", metavar="service-command")
    service_sub.add_parser(
        "status",
        help="查看服务运行态。",
        description="查看目标实例当前服务运行态、control endpoint、app-server 地址以及 binding / thread 统计。",
        formatter_class=_HelpFormatter,
    )
    service_reset = service_sub.add_parser(
        "reset-backend",
        help="重置当前实例 backend，不重启 feishu-codex service。",
        description=(
            "重置当前实例 backend，不重启 feishu-codex service 进程。\n"
            "普通 reset 只在确认当前实例没有待处理工作时允许；如需打断当前实例里的运行中 turn / 审批 / 输入请求，可加 `--force`。"
        ),
        formatter_class=_HelpFormatter,
    )
    service_reset.add_argument(
        "--force",
        action="store_true",
        help="强制重置 backend，允许打断当前实例里正在进行的工作。",
    )

    binding = subparsers.add_parser(
        "binding",
        help="查看或清理目标实例里的 Feishu binding。",
        description=(
            "Binding 管理面。\n"
            "`clear` / `clear-all` 清的是 Feishu 本地 bookmark，不删除 thread，也不等于 `unsubscribe`。"
        ),
        formatter_class=_HelpFormatter,
    )
    binding_sub = binding.add_subparsers(dest="action", required=True, title="binding commands", metavar="binding-command")
    binding_sub.add_parser(
        "list",
        help="列出当前实例可见 binding。",
        description="列出当前实例可见的 binding、运行态、关联 thread 与 cwd。",
        formatter_class=_HelpFormatter,
    )
    binding_status = binding_sub.add_parser(
        "status",
        help="查看单个 binding 详情。",
        description="查看单个 binding 的 chat、thread、runtime 与下一次发言可否被接受。",
        formatter_class=_HelpFormatter,
    )
    binding_status.add_argument("binding_id", help="目标 binding id。")
    binding_clear = binding_sub.add_parser(
        "clear",
        help="清除单个 binding bookmark。",
        description="清除单个 Feishu binding bookmark；不会删除 thread，也不会执行 unsubscribe。",
        formatter_class=_HelpFormatter,
    )
    binding_clear.add_argument("binding_id", help="要清除的 binding id。")
    binding_sub.add_parser(
        "clear-all",
        help="清除当前实例下全部 binding bookmark。",
        description="清除当前实例下全部 Feishu binding bookmark；不会删除 thread，也不会执行 unsubscribe。",
        formatter_class=_HelpFormatter,
    )

    thread = subparsers.add_parser(
        "thread",
        help="查看或管理 thread。",
        description=(
            "Thread 管理面。\n"
            "- `list` 默认列当前目录线程；也支持 `--scope global`\n"
            "- 其他 thread 子命令必须显式指定 `--thread-id` 或 `--thread-name`\n"
            "- 所有实例共享同一套 persisted thread 发现面；实例差异主要体现在 live runtime 持有"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_sub = thread.add_subparsers(dest="action", required=True, title="thread commands", metavar="thread-command")
    thread_list = thread_sub.add_parser(
        "list",
        help="列出可见 thread。",
        description="列出 persisted thread。默认按当前目录过滤，也支持 `--scope global` 查看全局线程。",
        formatter_class=_HelpFormatter,
    )
    thread_list.add_argument("--scope", choices=("cwd", "global"), default="cwd", help="列线程时使用的作用域。")
    thread_list.add_argument("--cwd", default="", help="当 `--scope cwd` 时使用的目录；省略时取当前 shell 目录。")
    thread_status = thread_sub.add_parser(
        "status",
        help="查看单个 thread 详情。",
        description="查看单个 thread 的 backend 状态、绑定关系与 unsubscribe 可用性。",
        formatter_class=_HelpFormatter,
    )
    thread_status_target = thread_status.add_mutually_exclusive_group(required=True)
    thread_status_target.add_argument("--thread-id", help="目标 thread id。")
    thread_status_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_bindings = thread_sub.add_parser(
        "bindings",
        help="查看某个 thread 关联的 binding。",
        description="查看某个 thread 当前关联的 binding 列表。",
        formatter_class=_HelpFormatter,
    )
    thread_bindings_target = thread_bindings.add_mutually_exclusive_group(required=True)
    thread_bindings_target.add_argument("--thread-id", help="目标 thread id。")
    thread_bindings_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_unsubscribe = thread_sub.add_parser(
        "unsubscribe",
        help="让 Feishu 释放某个 thread 的 runtime residency。",
        description="让 Feishu 释放某个 thread 的 runtime residency，同时保留 thread 与 binding 关系。",
        formatter_class=_HelpFormatter,
    )
    thread_unsubscribe_target = thread_unsubscribe.add_mutually_exclusive_group(required=True)
    thread_unsubscribe_target.add_argument("--thread-id", help="目标 thread id。")
    thread_unsubscribe_target.add_argument("--thread-name", help="目标 thread 名称。")
    return parser


def main() -> None:
    load_env_file()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.resource == "instance" and args.action == "list":
            raise SystemExit(_list_running_instances())
        data_dir = _resolve_target_data_dir(args.instance)
        if args.resource == "service" and args.action == "status":
            raise SystemExit(_print_service_status(data_dir))
        if args.resource == "service" and args.action == "reset-backend":
            raise SystemExit(_reset_service_backend(data_dir, force=bool(args.force)))
        if args.resource == "binding" and args.action == "list":
            raise SystemExit(_print_binding_list(data_dir))
        if args.resource == "binding" and args.action == "status":
            raise SystemExit(_print_binding_status(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear":
            raise SystemExit(_clear_binding(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear-all":
            raise SystemExit(_clear_all_bindings(data_dir))
        if args.resource == "thread" and args.action == "list":
            cwd = str(args.cwd or "").strip() or os.getcwd()
            raise SystemExit(_print_thread_list(data_dir, scope=args.scope, cwd=cwd))
        if args.resource == "thread" and args.action == "status":
            raise SystemExit(_print_thread_status(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "bindings":
            raise SystemExit(_print_thread_bindings(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "unsubscribe":
            raise SystemExit(_unsubscribe_thread(data_dir, _thread_target_params(args)))
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
