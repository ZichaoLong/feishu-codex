"""
Cross-platform management CLI for local feishu-codex installation.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import secrets
import shlex
import shutil
import subprocess
import sys
import time

from bot import __main__ as daemon_entry
from bot.env_file import ensure_env_template
from bot.file_permissions import ensure_private_file_permissions
from bot.instance_layout import DEFAULT_INSTANCE_NAME, apply_instance_environment, resolve_instance_paths, validate_instance_name
from bot.install_templates import CODEX_YAML_TEMPLATE, SYSTEM_YAML_TEMPLATE
from bot.instance_resolution import list_running_instances
from bot.platform_paths import default_config_root, default_data_root, default_log_file, default_user_bin_dir, is_windows
from bot.service_manager import ServiceManagerError, build_service_definition, current_service_manager
from bot.stores.service_instance_lease import ServiceInstanceLease


class _HelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "argument command: invalid choice: 'install'" in message:
            self.exit(
                2,
                (
                    f"{self.prog}: error: 公开命令中已无 `install`；"
                    "首次安装或修复请从仓库根目录运行 `bash install.sh`"
                    " 或 `./install.ps1`。\n"
                ),
            )
        sanitized = message.replace("bootstrap-install, ", "").replace(", bootstrap-install", "")
        super().error(sanitized)


def _hide_subcommand_from_help(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str
) -> None:
    subparsers._choices_actions = [
        action
        for action in subparsers._choices_actions
        if getattr(action, "dest", None) != name
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="feishu-codex",
        description=(
            "跨平台本地管理 CLI：负责安装、service 生命周期、配置入口和实例管理。\n\n"
            "说明：\n"
            "- 首次安装与修复都请从仓库根目录执行 `bash install.sh` 或 `./install.ps1`\n"
            "- `feishu-codex` 是唯一公开管理面；底层会调用原生 service manager\n"
            "  管理后台进程与“登录后自动启动”：Linux=systemd、macOS=LaunchAgent、Windows=Task Scheduler\n"
            "- 安装脚本会重建 shared wrapper，并重建所有已知实例的 service 定义/注册材料\n"
            "- `start|stop|restart|status` 只管理当前运行态；`autostart` 单独管理登录后自动启动\n"
            "- `run` 是跨平台单一 daemon 入口，通常由底层 service manager 调用\n"
        ),
        epilog=(
            "常见流程:\n"
            "  首次安装 / 修复:\n"
            "    bash install.sh\n"
            "    # Windows PowerShell: .\\install.ps1\n"
            "\n"
            "  默认实例启动:\n"
            "    feishu-codex config system --open\n"
            "    feishu-codex start\n"
            "\n"
            "  多实例:\n"
            "    feishu-codex instance create corp-a\n"
            "    feishu-codex --instance corp-a config system --open\n"
            "    feishu-codex --instance corp-a autostart enable\n"
            "    feishu-codex --instance corp-a start\n"
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--instance",
        default=DEFAULT_INSTANCE_NAME,
        help="目标实例；默认是 `default`。对 `instance ...` 子命令无效。",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="command",
    )

    subparsers.add_parser(
        "bootstrap-install",
        help="内部安装入口；一般不手动调用。",
        description="内部安装入口；通常由 `install.py` 调用。",
        formatter_class=_HelpFormatter,
    )
    _hide_subcommand_from_help(subparsers, "bootstrap-install")
    subparsers.add_parser(
        "start",
        help="启动目标实例后台 service。",
        description="启动目标实例后台 service，不改变登录后自动启动设置。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "stop",
        help="停止目标实例后台 service。",
        description="停止目标实例后台 service，不改变登录后自动启动设置。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "restart",
        help="重启目标实例后台 service。",
        description="重启目标实例后台 service，不改变登录后自动启动设置。service 定义缺失时会直接报错。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "status",
        help="查看目标实例当前运行态。",
        description=(
            "查看目标实例当前运行态。\n"
            "这描述的是后台进程当前是否在运行，而不是登录后自动启动是否开启。"
        ),
        formatter_class=_HelpFormatter,
    )

    autostart_parser = subparsers.add_parser(
        "autostart",
        help="管理目标实例“登录后自动启动”设置。",
        description=(
            "管理目标实例“登录后自动启动”设置。\n"
            "底层会调用当前平台原生 service manager 完成设置；不会直接改动当前运行态。"
        ),
        formatter_class=_HelpFormatter,
    )
    autostart_subparsers = autostart_parser.add_subparsers(
        dest="autostart_command",
        required=True,
        title="autostart commands",
        metavar="autostart-command",
    )
    autostart_subparsers.add_parser(
        "enable",
        help="开启登录后自动启动。",
        description="开启目标实例登录后自动启动，不会立即启动它。",
        formatter_class=_HelpFormatter,
    )
    autostart_subparsers.add_parser(
        "disable",
        help="关闭登录后自动启动。",
        description="关闭目标实例登录后自动启动，不会立即停止它。",
        formatter_class=_HelpFormatter,
    )
    autostart_subparsers.add_parser(
        "status",
        help="查看登录后自动启动是否开启。",
        description="查看目标实例登录后自动启动是否开启。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "run",
        help="以前台方式运行目标实例 daemon；通常由 service manager 调用。",
        description="以前台方式运行目标实例 daemon；通常由 systemd/launchd/Task Scheduler 调用。",
        formatter_class=_HelpFormatter,
    )

    log_parser = subparsers.add_parser(
        "log",
        help="查看目标实例日志文件并持续跟随。",
        description="查看目标实例日志文件并持续跟随。",
        formatter_class=_HelpFormatter,
    )
    log_parser.add_argument("--lines", type=int, default=40, help="启动时先输出的历史日志行数。")

    config_parser = subparsers.add_parser(
        "config",
        help="查看或打开当前实例相关配置文件。",
        description=(
            "查看或打开当前实例相关配置文件。\n"
            "可用目标：`system`、`codex`、`env`、`init-token`。"
        ),
        formatter_class=_HelpFormatter,
    )
    config_parser.add_argument(
        "target",
        nargs="?",
        choices=["system", "codex", "env", "init-token"],
        help="要查看的配置目标；省略时打印各配置文件路径。",
    )
    config_parser.add_argument("--open", action="store_true", help="用本地编辑器打开目标文件。")

    instance_parser = subparsers.add_parser(
        "instance",
        help="创建、列出、删除命名实例。",
        description=(
            "实例管理。\n"
            "注意：`feishu-codex instance ...` 不接受顶层 `--instance`；目标实例名写在子命令参数里。"
        ),
        formatter_class=_HelpFormatter,
    )
    instance_subparsers = instance_parser.add_subparsers(
        dest="instance_command",
        required=True,
        title="instance commands",
        metavar="instance-command",
    )
    instance_create_parser = instance_subparsers.add_parser(
        "create",
        help="创建命名实例，并准备对应后台 service 定义/注册材料。",
        description="创建命名实例，并准备对应后台 service 定义/注册材料；不会自动启动，也不会自动开启登录后自动启动。",
        formatter_class=_HelpFormatter,
    )
    instance_create_parser.add_argument("name", help="要创建的实例名，例如 `corp-a`。")
    instance_subparsers.add_parser(
        "list",
        help="列出本机已知实例及其本地目录。",
        description="列出本机已知实例及其本地目录。",
        formatter_class=_HelpFormatter,
    )
    instance_remove_parser = instance_subparsers.add_parser(
        "remove",
        help="删除命名实例及其实例级 service 注册材料。",
        description="删除命名实例及其实例级 service 注册材料；不会删除 `default` 实例。",
        formatter_class=_HelpFormatter,
    )
    instance_remove_parser.add_argument("name", help="要删除的实例名，例如 `corp-a`。")

    subparsers.add_parser(
        "uninstall",
        help="卸载所有 service 定义 / 自启动注册与 wrapper，保留配置与数据。",
        description="卸载所有 service 定义 / 自启动注册与 wrapper，保留配置与数据。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "purge",
        help="卸载所有 service 定义 / 自启动注册与 wrapper，并删除配置与数据。",
        description="卸载所有 service 定义 / 自启动注册与 wrapper，并删除配置与数据。",
        formatter_class=_HelpFormatter,
    )
    return parser


def _managed_venv_dir() -> pathlib.Path:
    return default_data_root() / ".venv"


def _venv_python() -> pathlib.Path:
    venv_dir = _managed_venv_dir()
    if is_windows():
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def _ensure_text_file(path: pathlib.Path, contents: str, *, overwrite: bool, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return
    path.write_text(contents, encoding="utf-8")
    if private:
        ensure_private_file_permissions(path)


def _ensure_init_token(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return
    path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
    ensure_private_file_permissions(path)


def _ensure_instance_scaffold(instance_name: str) -> None:
    paths = apply_instance_environment(instance_name)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.global_data_dir.mkdir(parents=True, exist_ok=True)
    _ensure_text_file(paths.config_dir / "system.yaml.example", SYSTEM_YAML_TEMPLATE, overwrite=True)
    _ensure_text_file(paths.config_dir / "codex.yaml.example", CODEX_YAML_TEMPLATE, overwrite=True)
    _ensure_text_file(paths.config_dir / "system.yaml", SYSTEM_YAML_TEMPLATE, overwrite=False, private=True)
    _ensure_text_file(paths.config_dir / "codex.yaml", CODEX_YAML_TEMPLATE, overwrite=False)
    ensure_env_template()
    _ensure_init_token(paths.config_dir / "init.token")


def _module_command(module_name: str, *args: str) -> tuple[str, ...]:
    return (str(_venv_python()), "-m", module_name, *args)


def _wrapper_path(command_name: str) -> pathlib.Path:
    bin_dir = default_user_bin_dir()
    if is_windows():
        return bin_dir / f"{command_name}.cmd"
    return bin_dir / command_name


def _service_daemon_command(instance_name: str) -> tuple[str, ...]:
    return (
        str(_wrapper_path("feishu-codex")),
        "--instance",
        validate_instance_name(instance_name),
        "run",
    )


def _write_wrapper(path: pathlib.Path, module_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_windows():
        wrapper_path = path.with_suffix(".cmd")
        wrapper_path.write_text(
            "\r\n".join(
                [
                    "@echo off",
                    f'"{_venv_python()}" -m {module_name} %*',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env sh",
                f'exec "{_venv_python()}" -m {module_name} "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _install_wrappers() -> pathlib.Path:
    bin_dir = default_user_bin_dir()
    _write_wrapper(bin_dir / "feishu-codex", "bot.manage_cli")
    _write_wrapper(bin_dir / "feishu-codexd", "bot.__main__")
    _write_wrapper(bin_dir / "feishu-codexctl", "bot.feishu_codexctl")
    _write_wrapper(bin_dir / "fcodex", "bot.fcodex")
    return bin_dir


def _open_in_editor(path: pathlib.Path) -> int:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if is_windows() else "nano"
    argv = [*shlex.split(editor), str(path)]
    return subprocess.call(argv)


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


def _service_definition(instance_name: str):
    normalized = validate_instance_name(instance_name)
    paths = resolve_instance_paths(normalized)
    return build_service_definition(
        instance_name=normalized,
        paths=paths,
        daemon_command=_service_daemon_command(normalized),
    )


def _known_instance_names() -> list[str]:
    names = {DEFAULT_INSTANCE_NAME}
    config_root = default_config_root()
    data_root = default_data_root()
    for root in (config_root / "instances", data_root / "instances"):
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir():
                try:
                    names.add(validate_instance_name(child.name))
                except ValueError:
                    continue
    return sorted(names)


def _print_install_summary(bin_dir: pathlib.Path, rebuilt_instances: list[str]) -> None:
    print("安装完成。")
    print(f"配置根目录: {default_config_root()}")
    print(f"数据根目录: {default_data_root()}")
    print(f"命令目录: {bin_dir}")
    print(f"已重建实例: {', '.join(rebuilt_instances)}")
    if not shutil.which("codex"):
        print("警告: 未检测到 `codex` 命令，请先安装 Codex CLI。")
    print("")
    print("下一步:")
    print(f"  1. 编辑配置: {resolve_instance_paths(DEFAULT_INSTANCE_NAME).config_dir / 'system.yaml'}")
    print(f"  2. 按需写入 provider 环境变量: {default_config_root() / 'feishu-codex.env'}")
    print("  3. 按需开启登录后自动启动: feishu-codex autostart enable")
    print("  4. 启动服务: feishu-codex start")
    print("  5. 查看初始化口令: feishu-codex config init-token")
    print("  6. 新建命名实例: feishu-codex instance create corp-a")


def _handle_bootstrap_install() -> int:
    instance_names = _known_instance_names()
    for instance_name in instance_names:
        _ensure_instance_scaffold(instance_name)
    bin_dir = _install_wrappers()
    manager = current_service_manager()
    for instance_name in instance_names:
        manager.ensure_service(_service_definition(instance_name))
    _print_install_summary(bin_dir, instance_names)
    return 0


def _handle_service_action(instance_name: str, action: str) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    definition = _service_definition(normalized)
    manager = current_service_manager()
    display_name = manager.display_name(definition)
    if action == "start":
        manager.start(definition)
        print(f"started service: {display_name}")
        return 0
    if action == "stop":
        manager.stop(definition)
        print(f"stopped service: {display_name}")
        return 0
    if action == "restart":
        manager.restart(definition)
        print(f"restarted service: {display_name}")
        return 0
    if action == "status":
        status = manager.status(definition)
        print(f"service: {'installed' if status.installed else 'missing'}")
        print(f"running: {'yes' if status.running else 'no'}")
        if status.detail:
            print(f"detail: {status.detail}")
        return 0 if status.running else 3
    raise ValueError(f"unknown service action: {action}")


def _handle_autostart_action(instance_name: str, action: str) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    definition = _service_definition(normalized)
    manager = current_service_manager()
    display_name = manager.display_name(definition)
    if action == "enable":
        manager.autostart_enable(definition)
        print(f"autostart enabled: {display_name}")
        return 0
    if action == "disable":
        manager.autostart_disable(definition)
        print(f"autostart disabled: {display_name}")
        return 0
    if action == "status":
        status = manager.autostart_status(definition)
        print(f"autostart: {'enabled' if status.enabled else 'disabled'}")
        if status.detail:
            print(f"detail: {status.detail}")
        return 0 if status.enabled else 3
    raise ValueError(f"unknown autostart action: {action}")


def _handle_run(instance_name: str) -> int:
    daemon_entry.main(["--instance", validate_instance_name(instance_name)])
    return 0


def _handle_config(instance_name: str, target: str | None, *, open_editor: bool) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    paths = resolve_instance_paths(normalized)
    candidates = {
        "system": paths.config_dir / "system.yaml",
        "codex": paths.config_dir / "codex.yaml",
        "env": default_config_root() / "feishu-codex.env",
        "init-token": paths.config_dir / "init.token",
    }
    if target is None:
        print(f"instance: {normalized}")
        for key, path in candidates.items():
            print(f"{key}: {path}")
        return 0
    resolved = candidates[target]
    print(resolved)
    if open_editor:
        return _open_in_editor(resolved)
    return 0


def _remove_wrappers() -> None:
    bin_dir = default_user_bin_dir()
    if is_windows():
        for name in ("feishu-codex", "feishu-codexd", "feishu-codexctl", "fcodex"):
            try:
                (bin_dir / f"{name}.cmd").unlink()
            except FileNotFoundError:
                pass
        return
    for name in ("feishu-codex", "feishu-codexd", "feishu-codexctl", "fcodex"):
        try:
            (bin_dir / name).unlink()
        except FileNotFoundError:
            pass


def _handle_uninstall(*, purge: bool) -> int:
    try:
        manager = current_service_manager()
    except ServiceManagerError:
        manager = None
    for instance_name in _known_instance_names():
        definition = _service_definition(instance_name)
        if manager is not None:
            try:
                manager.uninstall(definition)
            except ServiceManagerError:
                pass
    if manager is not None and hasattr(manager, "uninstall_shared"):
        try:
            manager.uninstall_shared()
        except ServiceManagerError:
            pass
    _remove_wrappers()
    if purge:
        shutil.rmtree(default_config_root(), ignore_errors=True)
        shutil.rmtree(default_data_root(), ignore_errors=True)
        print("已删除配置、数据、service 定义与命令包装器。")
    else:
        print("已删除 service 定义与命令包装器，配置和数据保留。")
    return 0


def _handle_instance_create(instance_name: str) -> int:
    normalized = validate_instance_name(instance_name)
    _ensure_instance_scaffold(normalized)
    _install_wrappers()
    current_service_manager().ensure_service(_service_definition(normalized))
    paths = resolve_instance_paths(normalized)
    print(f"已初始化实例: {normalized}")
    print(f"config dir: {paths.config_dir}")
    print(f"data dir: {paths.data_dir}")
    print(f"shared env: {default_config_root() / 'feishu-codex.env'}")
    return 0


def _handle_instance_list() -> int:
    running_entries = {entry.instance_name: entry for entry in list_running_instances()}
    instance_names = sorted(set(_known_instance_names()) | set(running_entries))
    print("instance\tstate\tconfig_dir\tdata_dir")
    for instance_name in instance_names:
        paths = resolve_instance_paths(instance_name)
        state = "running" if instance_name in running_entries else "stopped"
        print(f"{instance_name}\t{state}\t{paths.config_dir}\t{paths.data_dir}")
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


def _handle_instance_remove(instance_name: str) -> int:
    normalized = validate_instance_name(instance_name)
    if normalized == DEFAULT_INSTANCE_NAME:
        raise ValueError("不能删除 `default` 实例；如需整体清理，请用 `feishu-codex uninstall` 或 `purge`。")

    paths = resolve_instance_paths(normalized)

    try:
        manager = current_service_manager()
    except ServiceManagerError:
        manager = None

    if manager is not None:
        try:
            manager.uninstall(_service_definition(normalized))
        except ServiceManagerError:
            pass

    metadata = ServiceInstanceLease(paths.data_dir).load_metadata()
    if metadata is not None:
        raise ValueError(
            "目标实例仍有运行中的 service owner；请先确认该实例已经停止。"
            f" instance={normalized} owner_pid={metadata.owner_pid or 'unknown'}"
        )

    shutil.rmtree(paths.config_dir, ignore_errors=True)
    shutil.rmtree(paths.data_dir, ignore_errors=True)
    _remove_empty_parent(paths.config_dir.parent, stop_at=default_config_root())
    _remove_empty_parent(paths.data_dir.parent, stop_at=default_data_root())
    print(f"已删除实例: {normalized}")
    print(f"config dir: {paths.config_dir}")
    print(f"data dir: {paths.data_dir}")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "bootstrap-install":
            raise SystemExit(_handle_bootstrap_install())
        if args.command in {"start", "stop", "restart", "status"}:
            raise SystemExit(_handle_service_action(args.instance, args.command))
        if args.command == "autostart":
            raise SystemExit(_handle_autostart_action(args.instance, args.autostart_command))
        if args.command == "run":
            raise SystemExit(_handle_run(args.instance))
        if args.command == "log":
            raise SystemExit(_tail_log(default_log_file(resolve_instance_paths(validate_instance_name(args.instance)).data_dir), lines=args.lines))
        if args.command == "config":
            raise SystemExit(_handle_config(args.instance, args.target, open_editor=args.open))
        if args.command == "instance":
            if validate_instance_name(args.instance) != DEFAULT_INSTANCE_NAME:
                raise ValueError("`feishu-codex instance ...` 不接受顶层 `--instance`；请把目标实例写在子命令参数里。")
            if args.instance_command == "create":
                raise SystemExit(_handle_instance_create(args.name))
            if args.instance_command == "list":
                raise SystemExit(_handle_instance_list())
            if args.instance_command == "remove":
                raise SystemExit(_handle_instance_remove(args.name))
        if args.command == "uninstall":
            raise SystemExit(_handle_uninstall(purge=False))
        if args.command == "purge":
            raise SystemExit(_handle_uninstall(purge=True))
    except ServiceManagerError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
