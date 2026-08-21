"""Unified local management CLI for FOCUS."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from bot.cli_command_schema import (
    CommandSchema,
    argparse_subparser,
    argparse_subcommand_names,
    command_schema_from_argparse,
)
from bot.version import __version__


_FOCUSCTL_HELP_FLAGS = ("-h", "--help")
_FOCUSCTL_VERSION_FLAG = "--version"
_FOCUSCTL_INSTANCE_OPTION = "--instance"


@dataclass(frozen=True, slots=True)
class FocusctlResourceSpec:
    name: str
    route: Literal["manage", "runtime", "service"]
    description: str


@dataclass(frozen=True, slots=True)
class FocusctlServiceActionSpec:
    name: str
    route: Literal["manage", "runtime"]


# Top-level routing and `focusctl --help` are projections of this one registry.
# Subcommand semantics remain owned by each routed argparse surface.
FOCUSCTL_RESOURCE_SPECS: tuple[FocusctlResourceSpec, ...] = (
    FocusctlResourceSpec("config", "manage", "查看或打开 system/codex/env/init-token 配置"),
    FocusctlResourceSpec("instance", "manage", "管理本机实例；list 提供 service/runtime 总览"),
    FocusctlResourceSpec("service", "service", "管理后台服务、日志、自启动与 backend 恢复动作"),
    FocusctlResourceSpec("binding", "runtime", "查看、恢复、暂停或清理 Feishu binding"),
    FocusctlResourceSpec("prompt", "runtime", "向既有 binding 合成提交 prompt"),
    FocusctlResourceSpec("thread", "runtime", "查看或管理 Codex thread"),
    FocusctlResourceSpec("image", "runtime", "向 thread attached bindings 发送本地图片"),
    FocusctlResourceSpec("web", "runtime", "打开目标实例的 Focus Web 前端"),
    FocusctlResourceSpec("skill", "manage", "安装或卸载 FOCUS 提供的 workspace skills"),
    FocusctlResourceSpec("migrate", "manage", "一次性迁移旧 feishu-codex 本地安装"),
    FocusctlResourceSpec("uninstall", "manage", "移除程序安装面并保留配置与数据"),
    FocusctlResourceSpec("purge", "manage", "经安全预检后移除程序、配置与数据"),
)
_RESOURCE_ROUTES = {spec.name: spec.route for spec in FOCUSCTL_RESOURCE_SPECS}
_HIDDEN_MANAGE_RESOURCES = {"bootstrap-install"}
_INTERNAL_MANAGE_COMMANDS = {*_HIDDEN_MANAGE_RESOURCES, "run"}
FOCUSCTL_SERVICE_ACTION_SPECS: tuple[FocusctlServiceActionSpec, ...] = (
    FocusctlServiceActionSpec("start", "manage"),
    FocusctlServiceActionSpec("stop", "manage"),
    FocusctlServiceActionSpec("restart", "manage"),
    FocusctlServiceActionSpec("status", "manage"),
    FocusctlServiceActionSpec("autostart", "manage"),
    FocusctlServiceActionSpec("log", "manage"),
    FocusctlServiceActionSpec("reset-backend", "runtime"),
    FocusctlServiceActionSpec("attach", "runtime"),
)
_SERVICE_MANAGER_ACTIONS = {
    spec.name for spec in FOCUSCTL_SERVICE_ACTION_SPECS if spec.route == "manage"
}
_SERVICE_RUNTIME_ACTIONS = {
    spec.name for spec in FOCUSCTL_SERVICE_ACTION_SPECS if spec.route == "runtime"
}


@lru_cache(maxsize=1)
def focusctl_command_schema() -> CommandSchema:
    """Export the public command tree from the parsers used for execution.

    ``focusctl`` routes into two argparse surfaces.  This function composes
    those production parsers through the same resource/action registries used
    by the router, and rejects an unclassified parser command instead of
    allowing completion to maintain a divergent command tree.
    """

    from bot.manage_cli.entrypoint import _build_parser as build_manage_parser
    from bot.runtime_admin.cli_inputs import build_runtime_admin_parser

    manage_parser = build_manage_parser()
    runtime_parser = build_runtime_admin_parser()
    manage_schema = command_schema_from_argparse(manage_parser, name="focusctl-manage")
    runtime_schema = command_schema_from_argparse(runtime_parser, name="focusctl-runtime")

    manage_resource_names = {
        spec.name for spec in FOCUSCTL_RESOURCE_SPECS if spec.route == "manage"
    }
    runtime_resource_names = {
        spec.name for spec in FOCUSCTL_RESOURCE_SPECS if spec.route == "runtime"
    }
    expected_manage_commands = {
        *manage_resource_names,
        *_SERVICE_MANAGER_ACTIONS,
        *_INTERNAL_MANAGE_COMMANDS,
    }
    actual_manage_commands = set(
        argparse_subcommand_names(manage_parser, visible_only=False)
    )
    if actual_manage_commands != expected_manage_commands:
        raise RuntimeError(
            "focusctl manage command routing drift: "
            f"parser={sorted(actual_manage_commands)!r} "
            f"classified={sorted(expected_manage_commands)!r}"
        )

    expected_runtime_resources = {*runtime_resource_names, "service"}
    actual_runtime_resources = set(
        argparse_subcommand_names(runtime_parser, visible_only=False)
    )
    if actual_runtime_resources != expected_runtime_resources:
        raise RuntimeError(
            "focusctl runtime resource routing drift: "
            f"parser={sorted(actual_runtime_resources)!r} "
            f"classified={sorted(expected_runtime_resources)!r}"
        )

    runtime_service = runtime_schema.subcommand("service")
    if runtime_service is None:
        raise RuntimeError("focusctl runtime parser is missing service")
    runtime_service_parser = argparse_subparser(runtime_parser, ("service",))
    actual_runtime_service_actions = set(
        argparse_subcommand_names(runtime_service_parser, visible_only=False)
    )
    if actual_runtime_service_actions != _SERVICE_RUNTIME_ACTIONS:
        raise RuntimeError(
            "focusctl runtime service routing drift: "
            f"parser={sorted(actual_runtime_service_actions)!r} "
            f"classified={sorted(_SERVICE_RUNTIME_ACTIONS)!r}"
        )

    global_option_names = {
        *_FOCUSCTL_HELP_FLAGS,
        _FOCUSCTL_VERSION_FLAG,
        _FOCUSCTL_INSTANCE_OPTION,
    }
    for label, schema in (("manage", manage_schema), ("runtime", runtime_schema)):
        parser_option_names = {
            option_name
            for option in schema.options
            for option_name in option.names
        }
        if parser_option_names != global_option_names:
            raise RuntimeError(
                f"focusctl {label} global option drift: "
                f"parser={sorted(parser_option_names)!r} "
                f"router={sorted(global_option_names)!r}"
            )

    service_subcommands: list[CommandSchema] = []
    for spec in FOCUSCTL_SERVICE_ACTION_SPECS:
        source = manage_schema if spec.route == "manage" else runtime_service
        command = source.subcommand(spec.name)
        if command is None:
            raise RuntimeError(
                f"focusctl {spec.route} parser is missing service action {spec.name!r}"
            )
        service_subcommands.append(command)
    service_schema = CommandSchema(
        name="service",
        options=runtime_service.options,
        subcommands=tuple(service_subcommands),
    )

    public_resources: list[CommandSchema] = []
    for spec in FOCUSCTL_RESOURCE_SPECS:
        if spec.route == "service":
            public_resources.append(service_schema)
            continue
        source = manage_schema if spec.route == "manage" else runtime_schema
        command = source.subcommand(spec.name)
        if command is None:
            raise RuntimeError(
                f"focusctl {spec.route} parser is missing resource {spec.name!r}"
            )
        public_resources.append(command)
    return CommandSchema(
        name="focusctl",
        options=manage_schema.options,
        subcommands=tuple(public_resources),
    )


def _print_help() -> None:
    resources = "\n".join(
        f"  {spec.name:<12}{spec.description}"
        for spec in FOCUSCTL_RESOURCE_SPECS
    )
    print(
        "focusctl 管理 FOCUS 本地系统。\n\n"
        "用法:\n"
        "  focusctl [--instance <name>] <resource> <command> [args ...]\n\n"
        "资源:\n"
        f"{resources}\n\n"
        "常用命令:\n"
        "  focusctl config system --open\n"
        "  focusctl config env --open\n"
        "  focusctl instance create explorer\n"
        "  focusctl instance list\n"
        "  focusctl service start\n"
        "  focusctl service status\n"
        "  focusctl service autostart enable\n"
        "  focusctl binding list\n"
        "  focusctl binding clear-stale --dry-run\n"
        "  focusctl thread list --scope cwd\n"
        "  focusctl thread list --archived --scope global\n"
        "  focusctl thread archive --thread-id <id>\n"
        "  focusctl thread unarchive --thread-id <id-1> --thread-id <id-2>\n"
        "  focusctl thread delete --thread-id <id> --force\n"
        "  focusctl image send --thread-id <id> --path ./diagram.png\n\n"
        "  focusctl web open\n\n"
        "  focusctl migrate from-feishu-codex\n\n"
        "工作入口:\n"
        "  focus / fcodex 是 Codex TUI thin wrapper；focusctl 不进入 TUI。\n"
    )


def _print_service_action_help(action: str) -> bool:
    if action in {"start", "stop", "restart"}:
        descriptions = {
            "start": "启动目标实例后台 service，不改变登录后自动启动设置。",
            "stop": "停止目标实例后台 service，不改变登录后自动启动设置。",
            "restart": "重启目标实例后台 service，不改变登录后自动启动设置。",
        }
        print(f"usage: focusctl [--instance <name>] service {action} [-h]\n")
        print(descriptions[action])
        print("\noptions:\n  -h, --help  show this help message and exit")
        return True
    if action == "status":
        print("usage: focusctl [--instance <name>] service status [-h]\n")
        print(
            "查看目标实例 service manager 状态。\n"
            "service 正在运行时会附带 best-effort runtime 诊断；"
            "登录后自动启动状态请使用 `focusctl service autostart status`。"
        )
        print("\noptions:\n  -h, --help  show this help message and exit")
        return True
    if action == "log":
        print("usage: focusctl [--instance <name>] service log [-h] [--lines LINES]\n")
        print("查看目标实例日志文件并持续跟随。")
        print("\noptions:\n  -h, --help     show this help message and exit\n  --lines LINES  启动时先输出的历史日志行数。")
        return True
    if action == "autostart":
        print("usage: focusctl [--instance <name>] service autostart [-h] {enable,disable,status}\n")
        print("管理目标实例“登录后自动启动”设置，不直接改动当前运行态。")
        print(
            "\nautostart commands:\n"
            "  enable   开启登录后自动启动。\n"
            "  disable  关闭登录后自动启动。\n"
            "  status   查看登录后自动启动是否开启。\n"
            "\noptions:\n"
            "  -h, --help  show this help message and exit"
        )
        return True
    return False


def _service_command_summary() -> str:
    rendered_actions = (
        "autostart <enable|disable|status>"
        if spec.name == "autostart"
        else spec.name
        for spec in FOCUSCTL_SERVICE_ACTION_SPECS
    )
    return "focusctl service commands:\n  " + " | ".join(rendered_actions)


def _consume_global_options(argv: list[str]) -> tuple[list[str], list[str]]:
    global_args: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            rest.extend(argv[index + 1 :])
            break
        if item in _FOCUSCTL_HELP_FLAGS:
            global_args.append(item)
            index += 1
            continue
        if item == _FOCUSCTL_VERSION_FLAG:
            global_args.append(item)
            index += 1
            continue
        if item == _FOCUSCTL_INSTANCE_OPTION:
            if index + 1 >= len(argv):
                rest.extend(argv[index:])
                break
            global_args.extend([item, argv[index + 1]])
            index += 2
            continue
        if item.startswith(f"{_FOCUSCTL_INSTANCE_OPTION}="):
            global_args.append(item)
            index += 1
            continue
        rest.extend(argv[index:])
        break
    return global_args, rest


def _single_instance_args(global_args: list[str]) -> list[str]:
    instance_values = [
        arg for arg in global_args if arg.startswith(f"{_FOCUSCTL_INSTANCE_OPTION}=")
    ]
    index = 0
    while index < len(global_args):
        if (
            global_args[index] == _FOCUSCTL_INSTANCE_OPTION
            and index + 1 < len(global_args)
        ):
            instance_values.append(global_args[index + 1])
            index += 2
            continue
        index += 1
    if len(instance_values) > 1:
        raise ValueError("当前命令只接受一个 `--instance`；批量操作请使用支持批量的命令或分别执行。")
    return list(global_args)


def _run_manage(args: list[str]) -> None:
    from bot.manage_cli.entrypoint import main as manage_main

    manage_main(args)


def _run_runtime(args: list[str]) -> None:
    from bot.runtime_admin.cli import main as runtime_main

    runtime_main(args)


def main(argv: list[str] | None = None) -> None:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args or (
        len(raw_args) == 1 and raw_args[0] in _FOCUSCTL_HELP_FLAGS
    ):
        _print_help()
        raise SystemExit(0)
    if raw_args == [_FOCUSCTL_VERSION_FLAG]:
        print(f"focusctl {__version__}")
        raise SystemExit(0)

    global_args, rest = _consume_global_options(raw_args)
    if _FOCUSCTL_VERSION_FLAG in global_args:
        print(f"focusctl {__version__}")
        raise SystemExit(0)
    if any(flag in global_args for flag in _FOCUSCTL_HELP_FLAGS):
        _print_help()
        raise SystemExit(0)
    if not rest:
        _print_help()
        raise SystemExit(0)
    if rest[0] in _FOCUSCTL_HELP_FLAGS:
        _print_help()
        raise SystemExit(0)

    resource = rest[0]
    try:
        route = _RESOURCE_ROUTES.get(resource)
        if route == "manage" or resource in _HIDDEN_MANAGE_RESOURCES:
            _run_manage([*global_args, *rest])
            return
        if route == "runtime":
            _run_runtime([*_single_instance_args(global_args), *rest])
            return
        if route == "service":
            if len(rest) < 2 or rest[1] in _FOCUSCTL_HELP_FLAGS:
                print(_service_command_summary())
                raise SystemExit(0)
            action = rest[1]
            action_args = rest[2:]
            if action in _SERVICE_MANAGER_ACTIONS and any(
                item in _FOCUSCTL_HELP_FLAGS for item in action_args
            ):
                if _print_service_action_help(action):
                    raise SystemExit(0)
            if action in _SERVICE_MANAGER_ACTIONS:
                _run_manage([*global_args, action, *action_args])
                return
            if action == "list":
                raise ValueError("`focusctl service list` 已删除；请使用 `focusctl instance list`。")
            if action in _SERVICE_RUNTIME_ACTIONS:
                _run_runtime([*_single_instance_args(global_args), "service", action, *action_args])
                return
            raise ValueError(f"未知 service 命令：{action}")
        raise ValueError(f"未知资源：{resource}")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
