"""Managed installation, migration, and removal command surface."""

from __future__ import annotations

import json
import ntpath
import os
import pathlib
import shlex
import shutil
import sys
from dataclasses import dataclass

from bot.codex_command_resolver import detect_stable_codex_command
from bot.instance_layout import list_known_instance_names, resolve_instance_paths
from bot.install_lifecycle import (
    ManagedInstallLifecycleError,
    ManagedInstallLifecyclePorts,
    ManagedInstallLock,
    ManagedInstallTransaction,
    ManagedRemovalTarget,
    managed_install_lock_path,
    remove_managed_trees,
)
from bot.platform_paths import (
    default_config_root,
    default_data_root,
    default_user_bin_dir,
    is_windows,
)
from bot.service_control_plane import control_request
from bot.service_manager import (
    ServiceManager,
    ServiceManagerError,
    current_service_manager,
)
from bot.shell_completion_install import (
    CompletionInstallResult,
    install_shell_completion_files,
    remove_shell_completion_files,
)
from bot.stores.service_instance_lease import ServiceInstanceMaintenanceLease
from bot.windows_removal_handoff import (
    WindowsRemovalHandoff,
    WindowsRemovalHandoffError,
    WindowsRemovalHandoffReceipt,
    WindowsRemovalTarget,
    prepare_windows_removal_handoff,
)

from .errors import InstallLifecycleError
from .provisioning import (
    _MANAGED_ROOT_MARKER,
    _canonical_path,
    _ensure_instance_scaffold,
    _install_wrappers,
    _managed_root_marker_matches,
    _managed_root_marker_payload,
    _read_managed_root_marker,
    _service_definition,
    _venv_python,
)


_WINDOWS_USER_PATH_METADATA_FILE = "windows-user-path.json"


@dataclass(frozen=True, slots=True)
class _PurgeRoot:
    role: str
    path: pathlib.Path


def _windows_user_path_metadata_path() -> pathlib.Path:
    return default_config_root() / "install-state" / _WINDOWS_USER_PATH_METADATA_FILE


def _read_windows_user_path_metadata() -> tuple[pathlib.Path | None, bool]:
    path = _windows_user_path_metadata_path()
    if not path.exists():
        return None, False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, False
    if not isinstance(raw, dict):
        return None, False
    bin_dir_raw = str(raw.get("bin_dir", "") or "").strip()
    bin_dir = pathlib.Path(bin_dir_raw).expanduser() if bin_dir_raw else None
    return bin_dir, bool(raw.get("added_to_user_path", False))


def _write_windows_user_path_metadata(
    *, bin_dir: pathlib.Path, added_to_user_path: bool
) -> None:
    path = _windows_user_path_metadata_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "bin_dir": str(pathlib.Path(bin_dir)),
                "added_to_user_path": bool(added_to_user_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _remove_windows_user_path_metadata() -> None:
    path = _windows_user_path_metadata_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _normalize_windows_path_entry(value: pathlib.Path | str) -> str:
    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    return ntpath.normcase(ntpath.normpath(text))


def _split_windows_path_entries(raw_path: str) -> list[str]:
    return [entry.strip() for entry in str(raw_path or "").split(";") if entry.strip()]


def _windows_path_contains_entry(entries: list[str], entry: pathlib.Path | str) -> bool:
    target = _normalize_windows_path_entry(entry)
    if not target:
        return False
    return any(_normalize_windows_path_entry(item) == target for item in entries)


def _append_windows_path_entry(
    raw_path: str, entry: pathlib.Path | str
) -> tuple[str, bool]:
    entries = _split_windows_path_entries(raw_path)
    rendered_entry = str(pathlib.Path(entry))
    if _windows_path_contains_entry(entries, rendered_entry):
        return ";".join(entries), False
    entries.append(rendered_entry)
    return ";".join(entries), True


def _remove_windows_path_entry(
    raw_path: str, entry: pathlib.Path | str
) -> tuple[str, bool]:
    entries = _split_windows_path_entries(raw_path)
    target = _normalize_windows_path_entry(entry)
    if not target:
        return ";".join(entries), False
    kept_entries: list[str] = []
    removed = False
    for item in entries:
        if not removed and _normalize_windows_path_entry(item) == target:
            removed = True
            continue
        kept_entries.append(item)
    return ";".join(kept_entries), removed


def _read_windows_user_path_value() -> tuple[str, int | None]:
    if not is_windows():
        return "", None
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
        try:
            value, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return str(value or ""), int(value_type)


def _notify_windows_environment_changed() -> None:
    if not is_windows():
        return
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_void_p()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            5000,
            ctypes.byref(result),
        )
    except Exception:
        return


def _write_windows_user_path_value(raw_path: str, *, value_type: int | None) -> None:
    if not is_windows():
        return
    import winreg

    normalized_type = (
        value_type
        if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
        else winreg.REG_EXPAND_SZ
    )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
        if raw_path:
            winreg.SetValueEx(key, "Path", 0, normalized_type, str(raw_path))
        else:
            try:
                winreg.DeleteValue(key, "Path")
            except FileNotFoundError:
                pass
    _notify_windows_environment_changed()


def _ensure_windows_user_path(bin_dir: pathlib.Path) -> None:
    if not is_windows():
        return
    recorded_bin_dir, recorded_added = _read_windows_user_path_metadata()
    same_recorded_bin = recorded_bin_dir is not None and _normalize_windows_path_entry(
        recorded_bin_dir
    ) == _normalize_windows_path_entry(bin_dir)
    raw_user_path, value_type = _read_windows_user_path_value()
    updated_user_path = raw_user_path
    changed = False
    if recorded_added and recorded_bin_dir is not None and not same_recorded_bin:
        updated_user_path, removed = _remove_windows_path_entry(
            updated_user_path, recorded_bin_dir
        )
        changed = changed or removed
    updated_user_path, added = _append_windows_path_entry(updated_user_path, bin_dir)
    changed = changed or added
    if changed:
        _write_windows_user_path_value(updated_user_path, value_type=value_type)
    _write_windows_user_path_metadata(
        bin_dir=bin_dir,
        added_to_user_path=bool(added or (recorded_added and same_recorded_bin)),
    )


def _remove_windows_user_path() -> None:
    if not is_windows():
        return
    recorded_bin_dir, recorded_added = _read_windows_user_path_metadata()
    try:
        if recorded_added and recorded_bin_dir is not None:
            raw_user_path, value_type = _read_windows_user_path_value()
            updated_user_path, removed = _remove_windows_path_entry(
                raw_user_path, recorded_bin_dir
            )
            if removed:
                _write_windows_user_path_value(updated_user_path, value_type=value_type)
    finally:
        _remove_windows_user_path_metadata()


def create_managed_install_transaction(
    *,
    operation: str = "install",
    restore_running_on_success: bool = True,
    service_manager: ServiceManager | None = None,
) -> ManagedInstallTransaction:
    """Compose the shared install lifecycle from existing platform owners."""

    instance_names = list_known_instance_names()
    manager = service_manager or current_service_manager()

    def definition(instance_name: str):
        return _service_definition(instance_name)

    def prepare(instance_name: str):
        paths = resolve_instance_paths(instance_name)
        return control_request(
            paths.data_dir,
            "service/prepare-offline-maintenance",
        )

    def cancel(instance_name: str) -> None:
        paths = resolve_instance_paths(instance_name)
        result = control_request(
            paths.data_dir,
            "service/cancel-offline-maintenance",
        )
        if not isinstance(result, dict):
            raise InstallLifecycleError(
                f"实例 {instance_name} 返回了无效 maintenance cancel 结果。"
            )
        result_instance = str(result.get("instance_name", "") or "").strip()
        status = str(result.get("status", "") or "").strip()
        if result_instance != instance_name or status != "cancelled":
            raise InstallLifecycleError(
                f"实例 {instance_name} 未返回 matching maintenance cancel proof。"
            )

    return ManagedInstallTransaction(
        operation=operation,
        instance_names=instance_names,
        lock=ManagedInstallLock(managed_install_lock_path(default_data_root())),
        ports=ManagedInstallLifecyclePorts(
            service_status=lambda instance_name: manager.status(
                definition(instance_name)
            ),
            prepare_offline_maintenance=prepare,
            cancel_offline_maintenance=cancel,
            stop_service=lambda instance_name: manager.stop(definition(instance_name)),
            start_service=lambda instance_name: manager.start(
                definition(instance_name)
            ),
            maintenance_lease=lambda instance_name: (
                ServiceInstanceMaintenanceLease(
                    resolve_instance_paths(instance_name).data_dir
                )
            ),
        ),
        restore_running_on_success=restore_running_on_success,
    )


def _print_install_summary(
    bin_dir: pathlib.Path,
    rebuilt_instances: list[str],
    *,
    completion_result: CompletionInstallResult,
) -> None:
    print("安装面已刷新；正在由安装事务收口 service 状态。")
    print(f"配置根目录: {default_config_root()}")
    print(f"数据根目录: {default_data_root()}")
    print(f"命令目录: {bin_dir}")
    if completion_result.bash_dir is not None:
        print(f"Bash completion: {completion_result.bash_dir}")
    if completion_result.zsh_script_path is not None:
        print(f"zsh completion: {completion_result.zsh_script_path}")
    if completion_result.powershell_script_path is not None:
        print(f"PowerShell completion: {completion_result.powershell_script_path}")
    print("  - Codex TUI 工作入口 focus --help 或 fcodex --help")
    print("  - 本地管理 focusctl --help")
    print(f"已重建实例: {', '.join(rebuilt_instances)}。不覆盖各实例现有用户配置")
    if not (shutil.which("codex") or detect_stable_codex_command()):
        print("警告: 未检测到 `codex` 命令，请先安装 Codex CLI。")
    if is_windows():
        print(
            "Windows 用户 PATH: 已确保包含命令目录；新开 PowerShell / cmd 后应可直接发现命令。"
        )
    elif not _path_contains_directory(os.environ.get("PATH", ""), bin_dir):
        rendered_bin_dir = shlex.quote(str(bin_dir))
        print(f"警告: 命令目录尚不在当前 PATH：{bin_dir}")
        print(f'  - 当前 shell 可执行: export PATH={rendered_bin_dir}:"$PATH"')
        print("  - 如需长期生效，请把同一目录加入所用 shell 的启动配置。")
    print("")
    print("下一步:")
    print("  1. 配置飞书应用、provider 环境变量")
    print("    - focusctl config system --open")
    print("    - focusctl config env --open（按需）")
    print("  2. 启动服务并设置登陆后自动启动")
    print("    - focusctl service start")
    print("    - focusctl service autostart enable")
    print("  3. 飞书侧初始化")
    print("    - 查看初始化口令 focusctl config init-token")
    print("    - 在飞书侧发送 /init <token>")
    print("  4. 新建并配置命名实例")
    print("    - focusctl instance create corp-a")
    print("    - focusctl --instance corp-a service start")
    print("  5. 如需在某个目录下启用 FOCUS 附带 skills（可选）")
    print("    - 先 cd 到目标目录，再执行 focusctl skill install")
    print("    - 如需移除，回到同一目录执行 focusctl skill uninstall")
    print("    - 注意：focusctl uninstall/purge 不会删除各工作区中的 .agents/skills")
    if (
        completion_result.bash_dir is not None
        or completion_result.zsh_script_path is not None
        or completion_result.powershell_script_path is not None
    ):
        print("  6. Shell completion")
        if completion_result.bash_dir is not None:
            print("    - Bash：新开一个 Bash shell 通常会自动生效")
            print(
                f"    - 当前 shell 也可手动执行 source {completion_result.bash_dir / 'focusctl'}"
            )
        if completion_result.zsh_script_path is not None:
            if completion_result.zsh_rc_path is not None:
                print(
                    f"    - zsh：已写入自动加载钩子 {completion_result.zsh_rc_path}；新开 shell 即可生效"
                )
            print(
                f"    - zsh：当前 shell 也可手动执行 source {completion_result.zsh_script_path}"
            )
        if completion_result.powershell_script_path is not None:
            if completion_result.powershell_profile_path is not None:
                print(
                    "    - PowerShell：已写入自动加载 profile "
                    f"{completion_result.powershell_profile_path}；重开 PowerShell 即可生效"
                )
            else:
                print(
                    "    - PowerShell：当前执行策略禁止自动加载本地 profile 脚本；未写入自动加载钩子"
                )
                print(
                    "    - PowerShell：如需自动生效，可先执行 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
                )
            print(
                f"    - PowerShell：当前 shell 也可手动执行 . '{completion_result.powershell_script_path}'"
            )


def _path_contains_directory(raw_path: str, directory: pathlib.Path) -> bool:
    """Compare PATH entries without requiring every entry to exist."""

    target = os.path.normcase(os.path.abspath(os.path.expanduser(str(directory))))
    for raw_entry in str(raw_path or "").split(os.pathsep):
        if not raw_entry:
            continue
        candidate = os.path.normcase(os.path.abspath(os.path.expanduser(raw_entry)))
        if candidate == target:
            return True
    return False


def _handle_bootstrap_install() -> int:
    instance_names = list_known_instance_names()
    for instance_name in instance_names:
        _ensure_instance_scaffold(instance_name)
    bin_dir = _install_wrappers()
    _ensure_windows_user_path(bin_dir)
    if is_windows():
        remove_shell_completion_files()
        completion_result = CompletionInstallResult()
    else:
        completion_result = install_shell_completion_files(venv_python=_venv_python())
    manager = current_service_manager()
    for instance_name in instance_names:
        manager.ensure_service(_service_definition(instance_name))
    _print_install_summary(
        bin_dir,
        instance_names,
        completion_result=completion_result,
    )
    return 0


def _handle_migrate_from_feishu_codex() -> int:
    from bot.legacy_migration import LegacyMigrationError, migrate_from_feishu_codex

    try:
        summary = migrate_from_feishu_codex(
            install_new_surface=_handle_bootstrap_install
        )
    except LegacyMigrationError as exc:
        print(f"迁移失败（stage: {exc.stage}）：{exc}", file=sys.stderr)
        return 2
    print("迁移完成。")
    print(f"迁移实例: {', '.join(summary.instances) if summary.instances else '-'}")
    print(f"配置文件: {summary.config_files}")
    print(f"数据文件: {summary.data_files}")
    print(f"scheduled timers: {summary.scheduled_tasks}")
    print(f"移除旧 wrapper: {summary.removed_wrappers}")
    if summary.warnings:
        print("迁移警告:")
        for warning in summary.warnings:
            print(f"- {warning}")
    if summary.backup_dir is not None:
        print(f"备份目录: {summary.backup_dir}")
    return 0


def _remove_wrappers() -> None:
    bin_dir = default_user_bin_dir()
    if is_windows():
        for name in ("focus", "focusd", "focusctl", "fcodex"):
            try:
                (bin_dir / f"{name}.cmd").unlink()
            except FileNotFoundError:
                pass
        _remove_windows_user_path()
    else:
        for name in ("focus", "focusd", "focusctl", "fcodex"):
            try:
                (bin_dir / name).unlink()
            except FileNotFoundError:
                pass
    remove_shell_completion_files()


def _path_contains(parent: pathlib.Path, child: pathlib.Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _source_checkout_root() -> pathlib.Path | None:
    candidate = pathlib.Path(__file__).resolve().parents[2]
    if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def _resolve_purge_root(
    path: pathlib.Path | str,
    *,
    role: str,
    operation: str = "purge",
) -> _PurgeRoot:
    operation_label = str(operation or "").strip() or "purge"
    raw_path = pathlib.Path(path).expanduser()
    if raw_path.is_symlink():
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {role} 根目录本身不能是符号链接：{raw_path}。"
            "请修正 FOCUS_CONFIG_ROOT / FOCUS_DATA_ROOT 后重试。"
        )
    resolved = _canonical_path(raw_path, label=f"FOCUS {role} purge 根目录")
    anchor = pathlib.Path(resolved.anchor)
    relative_depth = max(len(resolved.parts) - len(anchor.parts), 0)
    if resolved == anchor:
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {role} 根目录过宽：{resolved}"
        )

    protected_paths: list[tuple[str, pathlib.Path]] = [
        ("用户主目录", _canonical_path(pathlib.Path.home(), label="用户主目录")),
        ("当前工作目录", _canonical_path(pathlib.Path.cwd(), label="当前工作目录")),
    ]
    checkout_root = _source_checkout_root()
    if checkout_root is not None:
        protected_paths.append(("FOCUS 源码仓库", checkout_root))
    for protected_label, protected_path in protected_paths:
        if _path_contains(resolved, protected_path):
            raise InstallLifecycleError(
                f"拒绝 {operation_label}：FOCUS {role} 根目录 {resolved} "
                f"是{protected_label} {protected_path} "
                "本身或其父目录。"
            )
    if relative_depth < 2:
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {role} 根目录过宽：{resolved}"
        )
    if relative_depth == 2 and "focus" not in resolved.name.casefold():
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {role} 根目录不是可识别的受管叶目录：{resolved}。"
            "浅层自定义根目录的叶目录名必须明确包含 `focus`。"
        )
    return _PurgeRoot(role=role, path=resolved)


def _verify_purge_root_marker(
    target: _PurgeRoot,
    *,
    operation: str = "purge",
) -> None:
    operation_label = str(operation or "").strip() or "purge"
    root = target.path
    if root.is_symlink():
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {target.role} 根目录不能是符号链接：{root}"
        )
    if not root.exists():
        return
    if not root.is_dir():
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {target.role} 根目标不是目录：{root}"
        )
    expected = _managed_root_marker_payload(root, role=target.role)
    actual = _read_managed_root_marker(root, role=target.role)
    if not _managed_root_marker_matches(actual, expected):
        raise InstallLifecycleError(
            f"拒绝 {operation_label}：FOCUS {target.role} 根目录 marker "
            "与 role/canonical target 不一致："
            f"{root / _MANAGED_ROOT_MARKER}。请确认根目录，移除错误 marker，"
            "再运行 `bash install.sh` 或 `./install.ps1` repair。"
        )


def _resolve_purge_roots() -> tuple[_PurgeRoot, _PurgeRoot]:
    config_root = _resolve_purge_root(default_config_root(), role="config")
    data_root = _resolve_purge_root(default_data_root(), role="data")
    if _path_contains(config_root.path, data_root.path) or _path_contains(
        data_root.path, config_root.path
    ):
        raise InstallLifecycleError(
            "拒绝 purge：FOCUS config/data 根目录必须是彼此独立的受管叶目录，"
            f"不能相同或互为父子目录：config={config_root.path} data={data_root.path}"
        )
    _verify_purge_root_marker(config_root)
    _verify_purge_root_marker(data_root)
    return config_root, data_root


def _resolve_managed_venv_target() -> pathlib.Path:
    data_root = _resolve_purge_root(
        default_data_root(),
        role="data",
        operation="uninstall",
    )
    _verify_purge_root_marker(data_root, operation="uninstall")
    target = data_root.path / ".venv"
    if target.is_symlink():
        raise InstallLifecycleError(
            f"拒绝 uninstall：受管 .venv 不能是符号链接：{target}"
        )
    if target.exists() and not target.is_dir():
        raise InstallLifecycleError(
            f"拒绝 uninstall：受管 .venv 目标不是目录：{target}"
        )
    return target


def _uninstall_service_definitions(
    instance_names: list[str],
    *,
    manager: ServiceManager,
) -> None:
    for instance_name in instance_names:
        try:
            manager.uninstall(_service_definition(instance_name))
        except (ServiceManagerError, OSError) as exc:
            raise InstallLifecycleError(
                "service uninstall 失败；不会继续删除 wrapper、配置或数据。"
                f" instance={instance_name}: {exc}"
            ) from exc
    if hasattr(manager, "uninstall_shared"):
        try:
            manager.uninstall_shared()
        except (ServiceManagerError, OSError) as exc:
            raise InstallLifecycleError(
                f"共享 service 定义卸载失败；不会继续删除 wrapper、配置或数据：{exc}"
            ) from exc
    for instance_name in instance_names:
        definition = _service_definition(instance_name)
        try:
            status = manager.status(definition)
        except (ServiceManagerError, OSError) as exc:
            raise InstallLifecycleError(
                "无法验证 service 已卸载；不会继续删除 wrapper、配置或数据。"
                f" instance={instance_name}: {exc}"
            ) from exc
        completion_method = getattr(manager, "is_instance_uninstalled", None)
        complete = (
            bool(completion_method(definition, status))
            if callable(completion_method)
            else not status.installed and not status.running
        )
        if not complete:
            raise InstallLifecycleError(
                "service uninstall 返回后定义或进程仍然存在；"
                "不会继续删除 wrapper、配置或数据。"
                f" instance={instance_name} installed={status.installed}"
                f" running={status.running}"
            )


def _prepare_windows_uninstall_handoff(
    *,
    purge: bool,
    purge_roots: tuple[_PurgeRoot, ...],
    managed_venv: pathlib.Path | None,
) -> WindowsRemovalHandoff:
    targets = (
        tuple(
            WindowsRemovalTarget(role=target.role, path=target.path)
            for target in purge_roots
        )
        if purge
        else (
            WindowsRemovalTarget(
                role="managed_venv",
                path=managed_venv or _resolve_managed_venv_target(),
            ),
        )
    )
    try:
        return prepare_windows_removal_handoff(
            operation="purge" if purge else "uninstall",
            parent_pid=os.getpid(),
            machine_lock_path=managed_install_lock_path(default_data_root()),
            targets=targets,
        )
    except WindowsRemovalHandoffError as exc:
        raise InstallLifecycleError(str(exc)) from exc


def _handle_uninstall(*, purge: bool) -> int:
    purge_roots = _resolve_purge_roots() if purge else ()
    managed_venv = None if purge else _resolve_managed_venv_target()
    windows_handoff = (
        _prepare_windows_uninstall_handoff(
            purge=purge,
            purge_roots=purge_roots,
            managed_venv=managed_venv,
        )
        if is_windows()
        else None
    )
    handoff_receipt: WindowsRemovalHandoffReceipt | None = None
    try:
        try:
            manager = current_service_manager()
        except ServiceManagerError as exc:
            raise InstallLifecycleError(
                f"无法取得 service manager；不会删除 wrapper、配置或数据：{exc}"
            ) from exc
        transaction = create_managed_install_transaction(
            operation="purge" if purge else "uninstall",
            restore_running_on_success=False,
            service_manager=manager,
        )
        try:
            with transaction:
                instance_names = list(transaction.instance_names)
                _uninstall_service_definitions(instance_names, manager=manager)
                try:
                    _remove_wrappers()
                except OSError as exc:
                    raise InstallLifecycleError(
                        f"删除命令包装器或 completion 失败；未开始删除配置或数据：{exc}"
                    ) from exc
                if purge:
                    for target in purge_roots:
                        _verify_purge_root_marker(target)
                elif managed_venv is not None:
                    _verify_purge_root_marker(
                        _PurgeRoot("data", managed_venv.parent), operation="uninstall"
                    )
                if windows_handoff is not None:
                    try:
                        transaction.yield_handoff_barrier()
                        handoff_receipt = windows_handoff.launch()
                    except WindowsRemovalHandoffError as exc:
                        raise InstallLifecycleError(str(exc)) from exc
                elif purge:
                    remove_managed_trees(
                        tuple(
                            ManagedRemovalTarget(item.role, item.path)
                            for item in purge_roots
                        ),
                        operation="purge",
                    )
                elif managed_venv is not None:
                    remove_managed_trees(
                        (ManagedRemovalTarget("managed_venv", managed_venv),),
                        operation="uninstall",
                    )
        except ManagedInstallLifecycleError as exc:
            suffix = ""
            if windows_handoff is not None and windows_handoff.armed:
                suffix = (
                    " Windows 删除 handoff 已提交，最终结果以该文件为准："
                    f"{handoff_receipt.result_path}"
                )
            raise InstallLifecycleError(f"{exc}{suffix}") from exc

        if handoff_receipt is not None:
            print("service 定义与命令包装器已删除。")
            print(
                "Windows 删除 helper 已接受退出后任务；当前命令只报告 handoff，"
                "不把它写成删除完成。"
            )
            print(f"handoff id: {handoff_receipt.handoff_id}")
            print(f"helper pid: {handoff_receipt.helper_pid}")
            print(f"最终删除结果: {handoff_receipt.result_path}")
        elif purge:
            print("已删除配置、数据、service 定义与命令包装器。")
        else:
            print("已删除 service 定义、命令包装器与受管 .venv，配置和其他数据保留。")
        return 0
    finally:
        if windows_handoff is not None and not windows_handoff.armed:
            windows_handoff.discard()
