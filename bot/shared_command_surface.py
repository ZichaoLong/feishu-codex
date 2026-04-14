"""
Feishu 与 fcodex wrapper 共享的命令事实源。

这里只定义明确纳入“共享 surface”的命令：

- /help
- /profile
- /rm
- /session
- /resume

upstream Codex TUI 内的原生命令不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SharedCommandSpec:
    key: str
    slash_name: str
    feishu_usage: str
    wrapper_usage: str
    feishu_summary: str
    wrapper_summary: str


_SHARED_COMMAND_SPECS = (
    SharedCommandSpec(
        key="help",
        slash_name="/help",
        feishu_usage="/help [session|settings|group|local]",
        wrapper_usage="fcodex /help",
        feishu_summary="查看帮助概览与主题入口。",
        wrapper_summary="查看 wrapper 自命令说明与 shared surface 边界。",
    ),
    SharedCommandSpec(
        key="profile",
        slash_name="/profile",
        feishu_usage="/profile [name]",
        wrapper_usage="fcodex /profile [name]",
        feishu_summary="查看或切换默认 profile。",
        wrapper_summary="查看或切换 feishu-codex / 默认 fcodex 的本地默认 profile。",
    ),
    SharedCommandSpec(
        key="rm",
        slash_name="/rm",
        feishu_usage="/rm [thread_id|thread_name]",
        wrapper_usage="fcodex /rm <thread_id|thread_name>",
        feishu_summary="归档当前线程或指定线程。",
        wrapper_summary="归档线程（archive），从常规列表中隐藏。",
    ),
    SharedCommandSpec(
        key="session",
        slash_name="/session",
        feishu_usage="/session",
        wrapper_usage="fcodex /session [cwd|global]",
        feishu_summary="查看当前目录线程。",
        wrapper_summary="列出共享后端线程；默认当前目录，也支持 global。",
    ),
    SharedCommandSpec(
        key="resume",
        slash_name="/resume",
        feishu_usage="/resume <thread_id|thread_name>",
        wrapper_usage="fcodex /resume <thread_id|thread_name>",
        feishu_summary="恢复指定线程。",
        wrapper_summary="恢复线程；name 走全局跨 provider 精确匹配。",
    ),
)

_SHARED_COMMANDS_BY_KEY = {spec.key: spec for spec in _SHARED_COMMAND_SPECS}


def iter_shared_commands() -> tuple[SharedCommandSpec, ...]:
    return _SHARED_COMMAND_SPECS


def get_shared_command(key: str) -> SharedCommandSpec:
    return _SHARED_COMMANDS_BY_KEY[key]


def shared_wrapper_commands() -> frozenset[str]:
    return frozenset(spec.slash_name for spec in _SHARED_COMMAND_SPECS)


def format_shared_wrapper_command_names() -> str:
    return "、".join(f"`{spec.slash_name}`" for spec in _SHARED_COMMAND_SPECS)


def shared_wrapper_usage_lines() -> tuple[str, ...]:
    return tuple(spec.wrapper_usage for spec in _SHARED_COMMAND_SPECS)


def shared_wrapper_help_lines() -> tuple[str, ...]:
    return tuple(f"{spec.wrapper_usage:<32} {spec.wrapper_summary}" for spec in _SHARED_COMMAND_SPECS)
