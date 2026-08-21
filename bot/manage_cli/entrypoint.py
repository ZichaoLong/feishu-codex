"""FOCUS manage CLI parser, router, and direct presentation commands."""

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import subprocess
import sys

from bot.env_file import ensure_env_template
from bot.instance_layout import resolve_instance_paths, validate_instance_name
from bot.platform_paths import default_config_root, default_log_file, is_windows
from bot.service_manager import ServiceManagerError
from bot.version import __version__
from bot.managed_skills.workspace_lifecycle import (
    _handle_skill_install,
    _handle_skill_uninstall,
)

from .errors import InstallLifecycleError
from .install_surface import (
    _handle_bootstrap_install,
    _handle_migrate_from_feishu_codex,
    _handle_uninstall,
)
from .instance_commands import (
    _handle_instance_create,
    _handle_instance_list,
    _handle_instance_remove,
)
from .provisioning import _normalize_requested_instances, _prepare_cli_instance
from .service_commands import (
    _handle_autostart_actions,
    _handle_run,
    _handle_service_actions,
    _tail_log,
)


class _HelpFormatter(
    argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter
):
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
        sanitized = message.replace("bootstrap-install, ", "").replace(
            ", bootstrap-install", ""
        )
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
        prog="focusctl",
        description=(
            "FOCUS 安装与 service lifecycle 管理内部入口。\n\n"
            "说明：\n"
            "- 首次安装与修复都请从仓库根目录执行 `bash install.sh` 或 `./install.ps1`\n"
            "- 公开管理面是 `focusctl`；底层会调用原生 service manager\n"
            "  管理后台进程与“登录后自动启动”：Linux=systemd、macOS=LaunchAgent、Windows=Task Scheduler\n"
            "- 安装脚本会重建 shared wrapper，并为所有已知实例重建 service 定义/注册材料；\n"
            "  只刷新 `*.example` 并补齐缺失 scaffold，不覆盖现有配置或数据\n"
            "- `start|stop|restart|status` 只管理当前运行态；`autostart` 单独管理登录后自动启动\n"
            "- 命名实例必须先显式 `instance create`；其他命令不会隐式创建命名实例\n"
            "- `uninstall|purge` 只清理本机安装面；不会删除你在各工作区安装的 `.agents/skills`\n"
            "- `run` 是跨平台单一 daemon 入口，通常由底层 service manager 调用\n"
        ),
        epilog=(
            "常见流程:\n"
            "  首次安装 / 修复:\n"
            "    bash install.sh\n"
            "    # Windows PowerShell: .\\install.ps1\n"
            "\n"
            "  默认实例启动:\n"
            "    focusctl config system --open\n"
            "    focusctl service start\n"
            "\n"
            "  多实例:\n"
            "    focusctl instance create corp-a\n"
            "    focusctl --instance corp-a config system --open\n"
            "    focusctl --instance corp-a service autostart enable\n"
            "    focusctl --instance corp-a service start\n"
            "\n"
            "  在目标目录启用发图 skill（可选）:\n"
            "    focusctl skill install\n"
            "\n"
            "  批量查看 / 控制多个实例:\n"
            "    focusctl --instance default --instance corp-a service status\n"
            "    focusctl instance list\n"
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"focusctl {__version__}"
    )
    parser.add_argument(
        "--instance",
        action="append",
        default=argparse.SUPPRESS,
        metavar="NAME",
        help=(
            "目标实例；默认按当前 CLI 实例解析规则选择。可重复传入，仅对 `start|stop|restart|status|autostart ...` "
            "这类天然可批量命令生效。命名实例必须先用 `instance create` 创建。"
            "对 `instance ...` 子命令无效。"
        ),
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
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="一次性迁移旧 feishu-codex 本地安装到 FOCUS。",
        description=(
            "一次性迁移旧 feishu-codex 本地安装到 FOCUS。\n"
            "迁移是 transfer，不是兼容 fallback；成功后主路径只认 focus。"
        ),
        formatter_class=_HelpFormatter,
    )
    migrate_subparsers = migrate_parser.add_subparsers(
        dest="migrate_command",
        required=True,
        title="migrate commands",
        metavar="migrate-command",
    )
    migrate_subparsers.add_parser(
        "from-feishu-codex",
        help="停止旧服务、迁移配置/持久数据/timer，并移除旧安装面。",
        description=(
            "停止旧服务、迁移配置/持久数据/timer，并移除旧安装面。\n"
            "不会迁移 PID、lease、registry、backend URL/token、正在执行的 turn 或内存队列。"
        ),
        formatter_class=_HelpFormatter,
    )
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
        help="查看目标实例 service manager 状态。",
        description=(
            "查看目标实例 service manager 状态。\n"
            "这描述的是平台 service manager 看到的后台进程状态，而不是登录后自动启动是否开启；"
            "service 正在运行时会附带 best-effort runtime 诊断。"
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
    log_parser.add_argument(
        "--lines", type=int, default=40, help="启动时先输出的历史日志行数。"
    )

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
    config_parser.add_argument(
        "--open", action="store_true", help="用本地编辑器打开目标文件。"
    )

    instance_parser = subparsers.add_parser(
        "instance",
        help="创建、列出、删除命名实例。",
        description=(
            "实例管理。\n"
            "注意：`focusctl instance ...` 不接受顶层 `--instance`；目标实例名写在子命令参数里。"
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
        help="列出本机实例、service 状态、runtime 与本地目录总览。",
        description="列出本机实例、service 状态、runtime 可用性、app-server 摘要与本地目录总览。",
        formatter_class=_HelpFormatter,
    )
    instance_remove_parser = instance_subparsers.add_parser(
        "remove",
        help="删除命名实例及其实例级 service 注册材料。",
        description="删除命名实例及其实例级 service 注册材料；不会删除 `default` 实例。",
        formatter_class=_HelpFormatter,
    )
    instance_remove_parser.add_argument("name", help="要删除的实例名，例如 `corp-a`。")

    skill_parser = subparsers.add_parser(
        "skill",
        help="安装或卸载 FOCUS 提供的工作区 skill。",
        description=(
            "Skill 管理。\n"
            "在当前目录 `.agents/skills` 安装或卸载 FOCUS 自带的工作区 skills。\n"
            "在 `~` 下执行时，home 下线程可发现；在仓库目录下执行时，只对该仓库生效。\n"
            "注意：`focusctl skill ...` 不接受顶层 `--instance`。"
        ),
        formatter_class=_HelpFormatter,
    )
    skill_subparsers = skill_parser.add_subparsers(
        dest="skill_command",
        required=True,
        title="skill commands",
        metavar="skill-command",
    )
    skill_subparsers.add_parser(
        "install",
        help="安装 FOCUS 自带的受管 skills 到当前目录。",
        description=(
            "把 FOCUS 自带的受管 skills 安装到当前目录 `.agents/skills`。\n"
            "当前包括：`feishu-send-image`、`feishu-scheduled-prompts`。"
        ),
        formatter_class=_HelpFormatter,
    )
    skill_subparsers.add_parser(
        "uninstall",
        help="卸载当前目录下 FOCUS 受管安装的 skills。",
        description=(
            "删除当前目录 `.agents/skills` 下 FOCUS 受管安装的 skills；"
            "不会删除其他来源的 skills。"
        ),
        formatter_class=_HelpFormatter,
    )

    subparsers.add_parser(
        "uninstall",
        help="卸载 service、wrapper 与受管 .venv，保留配置和其他数据。",
        description="卸载 service、wrapper、completion 与受管 .venv，保留配置和其他数据。",
        formatter_class=_HelpFormatter,
    )
    subparsers.add_parser(
        "purge",
        help="卸载所有 service 定义 / 自启动注册与 wrapper，并删除配置与数据。",
        description="卸载所有 service 定义 / 自启动注册与 wrapper，并删除配置与数据。",
        formatter_class=_HelpFormatter,
    )
    return parser


def _open_in_editor(path: pathlib.Path) -> int:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if is_windows() else "nano"
    argv = [*shlex.split(editor), str(path)]
    return subprocess.call(argv)


def _single_requested_instance(
    instance_names: list[str] | tuple[str, ...] | None,
    *,
    command_label: str,
) -> str:
    normalized_values = _normalize_requested_instances(instance_names)
    if len(normalized_values) != 1:
        raise ValueError(
            f"`{command_label}` 当前只支持单个实例；请只传一个 `--instance`。"
        )
    return normalized_values[0]


def _handle_config(instance_name: str, target: str | None, *, open_editor: bool) -> int:
    normalized = validate_instance_name(instance_name)
    if target == "env":
        ensure_env_template()
    else:
        normalized = _prepare_cli_instance(normalized)
    paths = resolve_instance_paths(normalized)
    candidates = {
        "system": paths.config_dir / "system.yaml",
        "codex": paths.config_dir / "codex.yaml",
        "env": default_config_root() / "focus.env",
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


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    requested_instances = getattr(args, "instance", [])
    try:
        if args.command == "bootstrap-install":
            raise SystemExit(_handle_bootstrap_install())
        if args.command == "migrate":
            if requested_instances:
                raise ValueError("`focusctl migrate ...` 不接受顶层 `--instance`。")
            if args.migrate_command == "from-feishu-codex":
                raise SystemExit(_handle_migrate_from_feishu_codex())
        if args.command in {"start", "stop", "restart", "status"}:
            raise SystemExit(_handle_service_actions(requested_instances, args.command))
        if args.command == "autostart":
            raise SystemExit(
                _handle_autostart_actions(requested_instances, args.autostart_command)
            )
        if args.command == "run":
            raise SystemExit(
                _handle_run(
                    _single_requested_instance(requested_instances, command_label="run")
                )
            )
        if args.command == "log":
            raise SystemExit(
                _tail_log(
                    default_log_file(
                        resolve_instance_paths(
                            _single_requested_instance(
                                requested_instances, command_label="log"
                            )
                        ).data_dir
                    ),
                    lines=args.lines,
                )
            )
        if args.command == "config":
            raise SystemExit(
                _handle_config(
                    _single_requested_instance(
                        requested_instances, command_label="config"
                    ),
                    args.target,
                    open_editor=args.open,
                )
            )
        if args.command == "instance":
            if requested_instances:
                raise ValueError(
                    "`focusctl instance ...` 不接受顶层 `--instance`；请把目标实例写在子命令参数里。"
                )
            if args.instance_command == "create":
                raise SystemExit(_handle_instance_create(args.name))
            if args.instance_command == "list":
                raise SystemExit(_handle_instance_list())
            if args.instance_command == "remove":
                raise SystemExit(_handle_instance_remove(args.name))
        if args.command == "skill":
            if requested_instances:
                raise ValueError("`focusctl skill ...` 不接受顶层 `--instance`。")
            if args.skill_command == "install":
                raise SystemExit(_handle_skill_install())
            if args.skill_command == "uninstall":
                raise SystemExit(_handle_skill_uninstall())
        if args.command == "uninstall":
            raise SystemExit(_handle_uninstall(purge=False))
        if args.command == "purge":
            raise SystemExit(_handle_uninstall(purge=True))
    except (InstallLifecycleError, ServiceManagerError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(2)
