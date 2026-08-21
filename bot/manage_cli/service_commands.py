"""Service, autostart, daemon-run, and log Manage CLI commands."""

from __future__ import annotations

import importlib
import os
import pathlib
import sys
import time
from dataclasses import dataclass

from bot.service_control_plane import ServiceControlError, control_request
from bot.service_manager import ServiceManagerError, current_service_manager
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.stores.service_instance_lease import ServiceInstanceLease

from .provisioning import (
    _normalize_requested_instances,
    _prepare_cli_instance,
    _service_definition,
)


@dataclass(frozen=True, slots=True)
class _RuntimeStatusSummary:
    available: bool
    result: dict[str, object]
    reason: str = ""
    control_endpoint: str = ""
    last_known_app_server: str = ""
    owner_pid: int = 0


def _tail_log(path: pathlib.Path, *, lines: int) -> int:
    if not path.exists():
        print(f"log file not found: {path}", file=sys.stderr)
        return 2
    buffer = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in buffer[-max(lines, 0) :]:
        print(line)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        try:
            while True:
                line = handle.readline()
                if line:
                    print(line, end="")
                    continue
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def _load_daemon_entry():
    return importlib.import_module("bot.__main__")


def _service_state_label(status) -> str:
    if status.running:
        return "running"
    if not status.installed:
        return "missing"
    return "stopped"


def _last_known_app_server(data_dir: pathlib.Path) -> str:
    runtime = AppServerRuntimeStore(data_dir).load_owned_runtime()
    if runtime is None:
        return ""
    return str(runtime.active_url or "").strip()


def _load_runtime_status_summary(data_dir: pathlib.Path) -> _RuntimeStatusSummary:
    metadata = ServiceInstanceLease(data_dir).load_metadata()
    published_endpoint = metadata.control_endpoint if metadata is not None else ""
    owner_pid = metadata.owner_pid if metadata is not None else 0
    try:
        result = control_request(data_dir, "service/status")
    except ServiceControlError as exc:
        return _RuntimeStatusSummary(
            available=False,
            result={},
            reason=str(exc),
            control_endpoint=published_endpoint,
            last_known_app_server=_last_known_app_server(data_dir),
            owner_pid=owner_pid,
        )
    if not isinstance(result, dict):
        return _RuntimeStatusSummary(
            available=False,
            result={},
            reason="control plane returned non-object service status",
            control_endpoint=published_endpoint,
            last_known_app_server=_last_known_app_server(data_dir),
            owner_pid=owner_pid,
        )
    return _RuntimeStatusSummary(
        available=True,
        result=result,
        control_endpoint=str(
            result.get("control_endpoint") or published_endpoint or ""
        ).strip(),
        last_known_app_server=str(
            result.get("app_server_url") or _last_known_app_server(data_dir) or ""
        ).strip(),
        owner_pid=int(result.get("pid") or owner_pid or 0),
    )


def _print_service_runtime_summary(summary: _RuntimeStatusSummary) -> None:
    if not summary.available:
        print("runtime: unavailable")
        print(f"control endpoint: {summary.control_endpoint or 'unavailable'}")
        print("web gateway: unavailable")
        if summary.last_known_app_server:
            print(f"last known app server: {summary.last_known_app_server}")
        if summary.owner_pid:
            print(f"last known pid: {summary.owner_pid}")
        print(f"reason: {summary.reason}")
        return

    result = summary.result
    print("runtime: available")
    print(f"pid: {result.get('pid', '-')}")
    print(
        f"control endpoint: {result.get('control_endpoint', summary.control_endpoint or '-')}"
    )
    print(
        f"app server: {result.get('app_server_url', summary.last_known_app_server or '-')}"
    )
    web_gateway_url = str(result.get("web_gateway_url") or "").strip()
    if web_gateway_url:
        web_gateway_status = web_gateway_url
    elif result.get("web_gateway_enabled") is False:
        web_gateway_status = "disabled"
    else:
        web_gateway_status = "unavailable"
    print(f"web gateway: {web_gateway_status}")
    print(
        "bindings: "
        f"total={result.get('binding_count', '-')} "
        f"bound={result.get('bound_binding_count', '-')} "
        f"attached={result.get('attached_binding_count', '-')}"
    )
    print(
        "threads: "
        f"bound={result.get('thread_count', '-')} "
        f"feishu-attached={result.get('attached_thread_count', '-')} "
        f"loaded={result.get('loaded_thread_count', '-')}"
    )
    running_bindings = result.get("running_binding_ids") or []
    print(
        f"running bindings: {', '.join(str(item) for item in running_bindings) or '（无）'}"
    )
    print(f"backend reset: {result.get('backend_reset_status', '-')}")
    if result.get("backend_reset_reason_code"):
        print(f"backend reset reason code: {result['backend_reset_reason_code']}")
    if result.get("backend_reset_reason"):
        print(f"backend reset reason: {result['backend_reset_reason']}")


def _should_probe_instance_runtime(
    data_dir: pathlib.Path,
    *,
    service_running: bool,
    running_entry,
) -> bool:
    if service_running or running_entry is not None:
        return True
    return ServiceInstanceLease(data_dir).load_metadata() is not None


def _instance_runtime_cells(
    data_dir: pathlib.Path, *, running_entry
) -> tuple[str, str, str]:
    runtime_summary = _load_runtime_status_summary(data_dir)
    if runtime_summary.available:
        return (
            "available",
            str(runtime_summary.result.get("pid") or runtime_summary.owner_pid or "-"),
            str(
                runtime_summary.result.get("app_server_url")
                or runtime_summary.last_known_app_server
                or "-"
            ),
        )
    pid = str(
        runtime_summary.owner_pid
        or (running_entry.owner_pid if running_entry is not None else 0)
        or "-"
    )
    app_server = (
        runtime_summary.last_known_app_server
        or (
            str(running_entry.app_server_url or "").strip()
            if running_entry is not None
            else ""
        )
        or "-"
    )
    return ("unavailable", pid, app_server)


def _handle_service_action(instance_name: str, action: str) -> int:
    normalized = _prepare_cli_instance(instance_name)
    definition = _service_definition(normalized)
    manager = current_service_manager()
    if action == "start":
        display_name = manager.display_name(definition)
        manager.start(definition)
        print(f"started service: {display_name}")
        return 0
    if action == "stop":
        display_name = manager.display_name(definition)
        manager.stop(definition)
        print(f"stopped service: {display_name}")
        return 0
    if action == "restart":
        display_name = manager.display_name(definition)
        manager.restart(definition)
        print(f"restarted service: {display_name}")
        return 0
    if action == "status":
        status = manager.status(definition)
        print(f"service: {_service_state_label(status)}")
        if status.source and status.detail:
            print(f"service source: {status.source}: {status.detail}")
        elif status.detail:
            print(f"service detail: {status.detail}")
        if status.running:
            _print_service_runtime_summary(
                _load_runtime_status_summary(definition.paths.data_dir)
            )
        return 0 if status.running else 3
    raise ValueError(f"unknown service action: {action}")


def _merge_batch_exit_codes(exit_codes: list[int]) -> int:
    if not exit_codes:
        return 0
    if any(code == 2 for code in exit_codes):
        return 2
    non_zero_codes = [code for code in exit_codes if code != 0]
    if non_zero_codes:
        return max(non_zero_codes)
    return 0


def _run_instance_batch(
    instance_names: list[str] | tuple[str, ...] | None,
    *,
    runner,
) -> int:
    normalized_values = _normalize_requested_instances(instance_names)
    if len(normalized_values) == 1:
        return int(runner(normalized_values[0]))

    exit_codes: list[int] = []
    for index, instance_name in enumerate(normalized_values):
        if index:
            print("")
        print(f"instance: {instance_name}")
        try:
            exit_codes.append(int(runner(instance_name)))
        except (ServiceManagerError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            exit_codes.append(2)
    return _merge_batch_exit_codes(exit_codes)


def _handle_service_actions(
    instance_names: list[str] | tuple[str, ...] | None, action: str
) -> int:
    return _run_instance_batch(
        instance_names,
        runner=lambda instance_name: _handle_service_action(instance_name, action),
    )


def _handle_autostart_action(instance_name: str, action: str) -> int:
    normalized = _prepare_cli_instance(instance_name)
    definition = _service_definition(normalized)
    manager = current_service_manager()
    if action == "enable":
        display_name = manager.display_name(definition)
        manager.autostart_enable(definition)
        print(f"autostart enabled: {display_name}")
        return 0
    if action == "disable":
        display_name = manager.display_name(definition)
        manager.autostart_disable(definition)
        print(f"autostart disabled: {display_name}")
        return 0
    if action == "status":
        status = manager.autostart_status(definition)
        print(f"autostart: {'enabled' if status.enabled else 'disabled'}")
        if status.source and status.detail:
            print(f"{status.source}: {status.detail}")
        elif status.detail:
            print(f"detail: {status.detail}")
        return 0 if status.enabled else 3
    raise ValueError(f"unknown autostart action: {action}")


def _handle_autostart_actions(
    instance_names: list[str] | tuple[str, ...] | None, action: str
) -> int:
    return _run_instance_batch(
        instance_names,
        runner=lambda instance_name: _handle_autostart_action(instance_name, action),
    )


def _handle_run(instance_name: str) -> int:
    daemon_entry = _load_daemon_entry()
    daemon_entry.main(["--instance", _prepare_cli_instance(instance_name)])
    return 0
