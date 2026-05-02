"""
Feishu 侧一等 slash 命令事实源。

这里只定义仓库明确维护的 Feishu slash surface，供 help / cards / tests 复用。
upstream Codex TUI 内的原生命令不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SharedCommandSpec:
    key: str
    slash_name: str
    feishu_usage: str
    feishu_summary: str


_SHARED_COMMAND_SPECS = (
    SharedCommandSpec(
        key="help",
        slash_name="/help",
        feishu_usage="/help [chat|group|thread|runtime|identity]",
        feishu_summary="查看帮助概览与主题入口。",
    ),
    SharedCommandSpec(
        key="profile",
        slash_name="/profile",
        feishu_usage="/profile [name]",
        feishu_summary="查看或切换当前绑定 thread 的 resume profile。",
    ),
    SharedCommandSpec(
        key="reset-backend",
        slash_name="/reset-backend",
        feishu_usage="/reset-backend",
        feishu_summary="预览并重置当前实例 backend。",
    ),
    SharedCommandSpec(
        key="archive",
        slash_name="/archive",
        feishu_usage="/archive [thread_id|thread_name]",
        feishu_summary="归档当前线程或指定线程。",
    ),
    SharedCommandSpec(
        key="threads",
        slash_name="/threads",
        feishu_usage="/threads",
        feishu_summary="查看当前目录线程。",
    ),
    SharedCommandSpec(
        key="preflight",
        slash_name="/preflight",
        feishu_usage="/preflight",
        feishu_summary="预检当前 chat 下一条普通消息与 release 可用性。",
    ),
    SharedCommandSpec(
        key="resume",
        slash_name="/resume",
        feishu_usage="/resume <thread_id|thread_name>",
        feishu_summary="恢复指定线程。",
    ),
    SharedCommandSpec(
        key="release-runtime",
        slash_name="/release-runtime",
        feishu_usage="/release-runtime",
        feishu_summary="释放当前绑定 thread 的 Feishu runtime 附着。",
    ),
)

_SHARED_COMMANDS_BY_KEY = {spec.key: spec for spec in _SHARED_COMMAND_SPECS}


def iter_shared_commands() -> tuple[SharedCommandSpec, ...]:
    return _SHARED_COMMAND_SPECS


def get_shared_command(key: str) -> SharedCommandSpec:
    return _SHARED_COMMANDS_BY_KEY[key]
