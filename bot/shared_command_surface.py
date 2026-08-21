"""
Feishu slash 命令共享片段。

这里只定义 help / cards / tests 复用的那部分命令描述，不是完整命令路由事实源。
完整 Feishu surface 以 `bot/codex_handler.py` 路由表和 `docs/contracts/feishu-command-matrix.*` 为准。
upstream Codex TUI 内的原生命令也不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.feishu_command_syntax import feishu_visible_command_syntax


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
        feishu_usage="/help [overview|start|thread-settings|turn|connection|group|more]",
        feishu_summary="查看帮助概览与主题入口。",
    ),
    SharedCommandSpec(
        key="commands",
        slash_name="/commands",
        feishu_usage="/commands",
        feishu_summary="按帮助分组查看常用命令列表。",
    ),
    SharedCommandSpec(
        key="goal",
        slash_name="/goal",
        feishu_usage=feishu_visible_command_syntax("/goal [show|text|set <objective>|pause|resume|clear]"),
        feishu_summary="查看或管理当前绑定 thread 的 goal。",
    ),
    SharedCommandSpec(
        key="last",
        slash_name="/last",
        feishu_usage="/last text",
        feishu_summary="导出当前会话最近一条权威终态文本；优先终态结果，若没有则回退最近执行卡。",
    ),
    SharedCommandSpec(
        key="steer",
        slash_name="/steer",
        feishu_usage=feishu_visible_command_syntax("/steer <text>"),
        feishu_summary="向当前 exact active turn 显式补充一段文本。",
    ),
    SharedCommandSpec(
        key="model",
        slash_name="/model",
        feishu_usage="/model [name|auto]",
        feishu_summary="查看或切换当前飞书会话后续 turn 的 model override。",
    ),
    SharedCommandSpec(
        key="effort",
        slash_name="/effort",
        feishu_usage="/effort [auto|value]",
        feishu_summary="查看或切换当前飞书会话后续 turn 的 effort override。",
    ),
    SharedCommandSpec(
        key="compact",
        slash_name="/compact",
        feishu_usage="/compact",
        feishu_summary="压缩当前绑定 thread 的上下文历史。",
    ),
    SharedCommandSpec(
        key="reset-backend",
        slash_name="/reset-backend",
        feishu_usage="/reset-backend",
        feishu_summary="预览并重置当前实例 backend，用于恢复或排障。",
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
        feishu_summary="预检当前 chat 下一条普通消息与 detach 可用性。",
    ),
    SharedCommandSpec(
        key="resume",
        slash_name="/resume",
        feishu_usage=feishu_visible_command_syntax("/resume <thread_id|thread_name>"),
        feishu_summary="恢复指定线程。",
    ),
    SharedCommandSpec(
        key="detach",
        slash_name="/detach",
        feishu_usage="/detach",
        feishu_summary="暂停当前会话接收当前线程的飞书推送。",
    ),
    SharedCommandSpec(
        key="attach",
        slash_name="/attach",
        feishu_usage="/attach [binding|thread|service]",
        feishu_summary="恢复当前会话、当前线程或当前实例的飞书推送。",
    ),
)

_SHARED_COMMANDS_BY_KEY = {spec.key: spec for spec in _SHARED_COMMAND_SPECS}


def iter_shared_commands() -> tuple[SharedCommandSpec, ...]:
    return _SHARED_COMMAND_SPECS


def get_shared_command(key: str) -> SharedCommandSpec:
    return _SHARED_COMMANDS_BY_KEY[key]
