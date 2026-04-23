"""Local admin CLI for the running feishu-codex service."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Any

from bot.constants import display_path
from bot.env_file import load_env_file
from bot.instance_layout import DEFAULT_INSTANCE_NAME, resolve_instance_paths, validate_instance_name
from bot.instance_resolution import current_cli_instance_name, default_running_instance, list_running_instances, unique_running_instance
from bot.platform_paths import default_data_root
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.stores.service_instance_lease import ServiceInstanceLease


def _data_dir() -> pathlib.Path:
    raw = os.environ.get("FC_DATA_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser()
    return default_data_root()


def _resolve_target_data_dir(explicit_instance: str | None) -> pathlib.Path:
    normalized_instance = str(explicit_instance or "").strip()
    if normalized_instance:
        return resolve_instance_paths(validate_instance_name(normalized_instance)).data_dir
    env_instance = str(os.environ.get("FC_INSTANCE", "") or "").strip()
    if env_instance:
        return resolve_instance_paths(validate_instance_name(env_instance)).data_dir
    running = unique_running_instance()
    if running is not None:
        return pathlib.Path(running.data_dir)
    default_running = default_running_instance()
    if default_running is not None:
        return pathlib.Path(default_running.data_dir)
    running_instances = list_running_instances()
    if len(running_instances) > 1:
        raise ValueError("检测到多个运行中的实例，请显式传 `--instance <name>`。")
    if not running_instances:
        return resolve_instance_paths(current_cli_instance_name()).data_dir
    return pathlib.Path(running_instances[0].data_dir)


def _request(data_dir: pathlib.Path, method: str, params: dict[str, Any] | None = None) -> Any:
    return control_request(data_dir, method, params)


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
    if "admitted_thread_count" in result:
        print(f"admitted threads: {result['admitted_thread_count']}")
    print(f"bindings: total={result['binding_count']} bound={result['bound_binding_count']} attached={result['attached_binding_count']}")
    print(f"threads: bound={result['thread_count']} feishu-attached={result['attached_thread_count']} loaded={result['loaded_thread_count']}")
    print(f"running bindings: {', '.join(result['running_binding_ids']) or '（无）'}")
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
        availability = "available" if snapshot["release_feishu_runtime_available"] else "blocked"
        print(f"release-feishu-runtime: {availability}")
        if snapshot["release_feishu_runtime_reason_code"]:
            print(f"release reason code: {snapshot['release_feishu_runtime_reason_code']}")
        if snapshot["release_feishu_runtime_reason"]:
            print(f"release reason: {snapshot['release_feishu_runtime_reason']}")
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
    availability = "available" if snapshot["release_feishu_runtime_available"] else "blocked"
    print(f"release-feishu-runtime: {availability}")
    if snapshot["release_feishu_runtime_reason_code"]:
        print(f"release reason code: {snapshot['release_feishu_runtime_reason_code']}")
    if snapshot["release_feishu_runtime_reason"]:
        print(f"release reason: {snapshot['release_feishu_runtime_reason']}")
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


def _release_thread_runtime(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/release-feishu-runtime", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print(f"released bindings: {', '.join(result['released_binding_ids']) or '（无）'}")
    print(f"backend thread status: {result['backend_thread_status']}")
    print(f"re-profile possible: {'yes' if result['reprofile_possible'] else 'no'}")
    if result.get("release_feishu_runtime_reason_code"):
        print(f"release reason code: {result['release_feishu_runtime_reason_code']}")
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
        app_server = item.app_server_url or "-"
        print(f"{item.instance_name}\t{item.owner_pid}\t{control}\t{app_server}")
    return 0


def _list_thread_admissions(data_dir: pathlib.Path) -> int:
    result = _request(data_dir, "thread/admissions")
    print(f"instance: {result['instance_name']}")
    thread_ids = result.get("thread_ids") or []
    if not thread_ids:
        print("admitted threads: （无）")
        return 0
    print("admitted threads:")
    for thread_id in thread_ids:
        print(f"- {thread_id}")
    return 0


def _import_thread(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/import", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print("imported: yes" if result["imported"] else "imported: already-admitted")
    return 0


def _revoke_thread(data_dir: pathlib.Path, target_params: dict[str, str]) -> int:
    result = _request(data_dir, "thread/revoke", target_params)
    print(f"thread: {result['thread_id']} {result['thread_title'] or ''}".rstrip())
    print("revoked: yes" if result["revoked"] else "revoked: already-absent")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu-codexctl")
    parser.add_argument("--instance")
    subparsers = parser.add_subparsers(dest="resource", required=True)

    instance = subparsers.add_parser("instance")
    instance_sub = instance.add_subparsers(dest="action", required=True)
    instance_sub.add_parser("list")

    service = subparsers.add_parser("service")
    service_sub = service.add_subparsers(dest="action", required=True)
    service_sub.add_parser("status")

    binding = subparsers.add_parser("binding")
    binding_sub = binding.add_subparsers(dest="action", required=True)
    binding_sub.add_parser("list")
    binding_status = binding_sub.add_parser("status")
    binding_status.add_argument("binding_id")
    binding_clear = binding_sub.add_parser("clear")
    binding_clear.add_argument("binding_id")
    binding_sub.add_parser("clear-all")

    thread = subparsers.add_parser("thread")
    thread_sub = thread.add_subparsers(dest="action", required=True)
    thread_status = thread_sub.add_parser("status")
    thread_status_target = thread_status.add_mutually_exclusive_group(required=True)
    thread_status_target.add_argument("--thread-id")
    thread_status_target.add_argument("--thread-name")
    thread_bindings = thread_sub.add_parser("bindings")
    thread_bindings_target = thread_bindings.add_mutually_exclusive_group(required=True)
    thread_bindings_target.add_argument("--thread-id")
    thread_bindings_target.add_argument("--thread-name")
    thread_release = thread_sub.add_parser("release-feishu-runtime")
    thread_release_target = thread_release.add_mutually_exclusive_group(required=True)
    thread_release_target.add_argument("--thread-id")
    thread_release_target.add_argument("--thread-name")
    thread_admissions = thread_sub.add_parser("admissions")
    thread_import = thread_sub.add_parser("import")
    thread_import_target = thread_import.add_mutually_exclusive_group(required=True)
    thread_import_target.add_argument("--thread-id")
    thread_import_target.add_argument("--thread-name")
    thread_revoke = thread_sub.add_parser("revoke")
    thread_revoke_target = thread_revoke.add_mutually_exclusive_group(required=True)
    thread_revoke_target.add_argument("--thread-id")
    thread_revoke_target.add_argument("--thread-name")
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
        if args.resource == "binding" and args.action == "list":
            raise SystemExit(_print_binding_list(data_dir))
        if args.resource == "binding" and args.action == "status":
            raise SystemExit(_print_binding_status(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear":
            raise SystemExit(_clear_binding(data_dir, args.binding_id))
        if args.resource == "binding" and args.action == "clear-all":
            raise SystemExit(_clear_all_bindings(data_dir))
        if args.resource == "thread" and args.action == "status":
            raise SystemExit(_print_thread_status(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "bindings":
            raise SystemExit(_print_thread_bindings(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "release-feishu-runtime":
            raise SystemExit(_release_thread_runtime(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "admissions":
            raise SystemExit(_list_thread_admissions(data_dir))
        if args.resource == "thread" and args.action == "import":
            raise SystemExit(_import_thread(data_dir, _thread_target_params(args)))
        if args.resource == "thread" and args.action == "revoke":
            raise SystemExit(_revoke_thread(data_dir, _thread_target_params(args)))
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
