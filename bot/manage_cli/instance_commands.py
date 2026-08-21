"""Instance lifecycle and instance-status Manage CLI commands."""

from __future__ import annotations

import pathlib
import shutil

from bot.cli_table import render_table as _render_table
from bot.instance_layout import (
    DEFAULT_INSTANCE_NAME,
    list_known_instance_names,
    resolve_instance_paths,
    validate_instance_name,
)
from bot.instance_resolution import list_running_instances
from bot.platform_paths import default_config_root, default_data_root
from bot.service_manager import ServiceManagerError, current_service_manager
from bot.stores.service_instance_lease import (
    ServiceInstanceMaintenanceLease,
    ServiceInstanceMaintenanceLeaseError,
)

from .errors import InstallLifecycleError
from .provisioning import (
    _ensure_instance_scaffold,
    _install_wrappers,
    _service_definition,
)
from .service_commands import (
    _instance_runtime_cells,
    _service_state_label,
    _should_probe_instance_runtime,
)


def _handle_instance_create(instance_name: str) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    _install_wrappers()
    current_service_manager().ensure_service(_service_definition(normalized))
    paths = resolve_instance_paths(normalized)
    print(f"已初始化实例: {normalized}")
    print(f"config dir: {paths.config_dir}")
    print(f"data dir: {paths.data_dir}")
    print(f"shared env: {default_config_root() / 'focus.env'}")
    return 0


def _handle_instance_list() -> int:
    running_entries = {entry.instance_name: entry for entry in list_running_instances()}
    instance_names = sorted(set(list_known_instance_names()) | set(running_entries))
    manager = current_service_manager()
    rows: list[list[str]] = []
    for instance_name in instance_names:
        paths = resolve_instance_paths(instance_name)
        running_entry = running_entries.get(instance_name)
        try:
            service_status = manager.status(_service_definition(instance_name))
            service_state = _service_state_label(service_status)
        except (ServiceManagerError, ValueError) as exc:
            service_status = None
            service_state = "unknown"
            service_error = str(exc)
        else:
            service_error = ""

        runtime_state = "-"
        pid = "-"
        app_server = "-"
        service_running = service_status is not None and service_status.running
        if _should_probe_instance_runtime(
            paths.data_dir, service_running=service_running, running_entry=running_entry
        ):
            runtime_state, pid, app_server = _instance_runtime_cells(
                paths.data_dir, running_entry=running_entry
            )
        elif service_error:
            runtime_state = "unknown"

        rows.append(
            [
                instance_name,
                service_state,
                runtime_state,
                pid,
                app_server,
                str(paths.config_dir),
                str(paths.data_dir),
            ]
        )
    for line in _render_table(
        [
            "INSTANCE",
            "SERVICE",
            "RUNTIME",
            "PID",
            "APP_SERVER",
            "CONFIG_DIR",
            "DATA_DIR",
        ],
        rows,
    ):
        print(line)
    return 0


def _remove_empty_parent(path: pathlib.Path, *, stop_at: pathlib.Path) -> None:
    current = pathlib.Path(path)
    boundary = pathlib.Path(stop_at)
    while True:
        if current == boundary:
            return
        try:
            current.rmdir()
        except FileNotFoundError:
            return
        except OSError:
            return
        parent = current.parent
        if parent == current:
            return
        current = parent


def _remove_instance_tree(path: pathlib.Path, *, label: str) -> bool:
    if path.is_symlink():
        raise InstallLifecycleError(
            f"拒绝删除符号链接形式的 instance {label} 目录：{path}"
        )
    if not path.exists():
        return False
    if not path.is_dir():
        raise InstallLifecycleError(f"instance {label} 目标不是目录：{path}")
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InstallLifecycleError(
            f"删除 instance {label} 目录失败：{path}: {exc}；不会报告成功。"
        ) from exc
    return True


def _handle_instance_remove(instance_name: str) -> int:
    normalized = validate_instance_name(instance_name)
    if normalized == DEFAULT_INSTANCE_NAME:
        raise ValueError(
            "不能删除 `default` 实例；如需整体清理，请用 `focusctl uninstall` 或 `purge`。"
        )

    paths = resolve_instance_paths(normalized)

    definition = _service_definition(normalized)
    try:
        manager = current_service_manager()
    except ServiceManagerError as exc:
        raise InstallLifecycleError(
            f"无法取得 service manager；不会删除 instance 配置或数据：{exc}"
        ) from exc
    try:
        manager.uninstall(definition)
    except (ServiceManagerError, OSError) as exc:
        raise InstallLifecycleError(
            f"instance service uninstall 失败；不会删除配置或数据。 instance={normalized}: {exc}"
        ) from exc

    status_method = getattr(manager, "status", None)
    completion_method = getattr(manager, "is_instance_uninstalled", None)
    if not callable(status_method) or not callable(completion_method):
        raise InstallLifecycleError(
            "service manager 不支持卸载后状态验证；不会删除 instance 配置或数据。"
            f" instance={normalized}"
        )
    try:
        status = status_method(definition)
    except (ServiceManagerError, OSError) as exc:
        raise InstallLifecycleError(
            f"无法验证 instance service 已卸载；不会删除配置或数据。 instance={normalized}: {exc}"
        ) from exc
    if not completion_method(definition, status):
        raise InstallLifecycleError(
            "instance service uninstall 返回后注册或进程仍然存在；不会删除配置或数据。"
            f" instance={normalized} installed={getattr(status, 'installed', 'unknown')}"
            f" running={getattr(status, 'running', 'unknown')}"
        )

    maintenance_lease: ServiceInstanceMaintenanceLease | None = None
    if paths.data_dir.exists():
        maintenance_lease = ServiceInstanceMaintenanceLease(paths.data_dir)
        try:
            maintenance_lease.acquire()
        except (ServiceInstanceMaintenanceLeaseError, OSError) as exc:
            raise InstallLifecycleError(
                "service 卸载后仍无法取得离线 maintenance 所有权；"
                "不会删除 instance 配置或数据。"
                f" instance={normalized} data_dir={paths.data_dir}: {exc}"
            ) from exc

    removed: list[str] = []
    try:
        if _remove_instance_tree(paths.config_dir, label="config"):
            removed.append("config")
        if maintenance_lease is not None:
            # See the purge path above: release the in-tree lock only after
            # service registration and config have both been removed.
            maintenance_lease.release()
            maintenance_lease = None
        if _remove_instance_tree(paths.data_dir, label="data"):
            removed.append("data")
    except InstallLifecycleError as exc:
        removed_summary = ", ".join(removed) or "无"
        raise InstallLifecycleError(f"{exc} 本次已删除：{removed_summary}。") from exc
    finally:
        if maintenance_lease is not None:
            maintenance_lease.release()

    _remove_empty_parent(paths.config_dir.parent, stop_at=default_config_root())
    _remove_empty_parent(paths.data_dir.parent, stop_at=default_data_root())
    print(f"已删除实例: {normalized}")
    print(f"config dir: {paths.config_dir}")
    print(f"data dir: {paths.data_dir}")
    return 0
