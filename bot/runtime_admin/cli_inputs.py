"""Runtime Admin CLI grammar and input normalization."""

from __future__ import annotations

import argparse
import os
import pathlib

from bot.constants import display_path
from bot.version import __version__

_CODEX_THREAD_ID_ENV_VAR = "CODEX_THREAD_ID"


class _HelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    pass


def thread_target_params(args: argparse.Namespace) -> dict[str, str]:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    thread_name = str(getattr(args, "thread_name", "") or "").strip()
    if bool(thread_id) == bool(thread_name):
        raise ValueError("必须且只能提供 --thread-id 或 --thread-name。")
    if thread_id:
        return {"thread_id": thread_id}
    return {"thread_name": thread_name}


def image_send_target_params(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    thread_name = str(getattr(args, "thread_name", "") or "").strip()
    if thread_id and thread_name:
        raise ValueError("不能同时提供 --thread-id 和 --thread-name。")
    if thread_id:
        return {"thread_id": thread_id}, thread_id
    if thread_name:
        return {"thread_name": thread_name}, ""
    env_thread_id = str(os.environ.get(_CODEX_THREAD_ID_ENV_VAR, "") or "").strip()
    if env_thread_id:
        return {"thread_id": env_thread_id}, env_thread_id
    raise ValueError(
        "必须提供 --thread-id 或 --thread-name；若在 Codex turn 内调用，也可依赖环境变量 `CODEX_THREAD_ID`。"
    )


def thread_archive_inputs(args: argparse.Namespace) -> tuple[list[str], str]:
    raw_thread_ids = list(getattr(args, "thread_ids", []) or [])
    thread_ids = list(dict.fromkeys(str(item or "").strip() for item in raw_thread_ids if str(item or "").strip()))
    raw_thread_names = list(getattr(args, "thread_names", []) or [])
    if len(raw_thread_names) > 1:
        raise ValueError(
            "thread archive 只允许提供一个 `--thread-name`；批量归档请重复提供 `--thread-id`。"
        )
    thread_name = str(raw_thread_names[0] if raw_thread_names else "").strip()
    if thread_ids and thread_name:
        raise ValueError("thread archive 不能同时提供 `--thread-id` 和 `--thread-name`。")
    if not thread_ids and not thread_name:
        raise ValueError("thread archive 必须提供至少一个 `--thread-id`；单线程也可改用 `--thread-name`。")
    return thread_ids, thread_name


def thread_unarchive_inputs(args: argparse.Namespace) -> list[str]:
    raw_thread_ids = list(getattr(args, "thread_ids", []) or [])
    if not raw_thread_ids:
        raise ValueError("thread unarchive 必须提供至少一个 `--thread-id`。")
    thread_ids = [str(item or "").strip() for item in raw_thread_ids]
    if any(not thread_id for thread_id in thread_ids):
        raise ValueError("thread unarchive 的 `--thread-id` 不能为空。")
    return list(dict.fromkeys(thread_ids))


def thread_delete_input(args: argparse.Namespace) -> str:
    raw_thread_ids = list(getattr(args, "thread_ids", []) or [])
    if not raw_thread_ids:
        raise ValueError("thread delete 必须提供 `--thread-id`。")
    if len(raw_thread_ids) > 1:
        raise ValueError("thread delete 只允许提供一个 `--thread-id`；请逐个确认并删除。")
    thread_id = str(raw_thread_ids[0] or "").strip()
    if not thread_id:
        raise ValueError("thread delete 的 `--thread-id` 不能为空。")
    return thread_id


def prompt_text_from_args(args: argparse.Namespace) -> str:
    inline_text = str(getattr(args, "text", "") or "")
    text_file = str(getattr(args, "text_file", "") or "").strip()
    if bool(inline_text.strip()) == bool(text_file):
        raise ValueError("必须且只能提供 --text 或 --text-file。")
    if text_file:
        path = pathlib.Path(text_file).expanduser()
        if not path.exists():
            raise ValueError(f"prompt 文本文件不存在：{display_path(str(path))}")
        if not path.is_file():
            raise ValueError(f"prompt 文本文件不是普通文件：{display_path(str(path))}")
        return path.read_text(encoding="utf-8")
    return inline_text


def build_runtime_admin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="focusctl",
        description=(
            "本地查看 / 管理面：查看运行中的 FOCUS service、binding、thread 与实例。\n\n"
            "说明：\n"
            "- `focusctl` 是本地查看 / 管理面，不是第二个 Codex 前端\n"
            "- 命名实例必须先显式 `focusctl instance create <name>`；这里不会隐式创建\n"
            "- 命令都可加 `--instance <name>`；显式值优先\n"
            "- 若未显式指定，则按 preferred-running（若有）/ unique-running / default-running / current-instance-paths 规则解析；多实例仍有歧义时必须显式指定\n"
            "- `binding clear` / `clear-all` 删除的是 Feishu 本地 binding 记录，不删除 thread，也不等于 `detach`\n"
            "- `thread list` 默认列当前目录线程，也支持 `--scope global`\n"
        ),
        epilog=(
            "常用命令:\n"
            "  focusctl service reset-backend\n"
            "  focusctl service attach\n"
            "  focusctl binding list\n"
            "  focusctl binding status <binding_id>\n"
            "  focusctl binding attach <binding_id>\n"
            "  focusctl binding detach <binding_id>\n"
            "  focusctl binding clear-stale --dry-run\n"
            "  focusctl prompt send --binding-id <binding_id> --text '继续执行'\n"
            "  focusctl thread list --scope cwd\n"
            "  focusctl thread status --thread-id <id>\n"
            "  focusctl thread goal --thread-id <id>\n"
            "  focusctl thread archive --thread-name demo\n"
            "  focusctl thread archive --thread-id <id-1> --thread-id <id-2>\n"
            "  focusctl thread list --archived --scope global\n"
            "  focusctl thread unarchive --thread-id <id-1> --thread-id <id-2>\n"
            "  focusctl thread delete --thread-id <id> --force\n"
            "  focusctl thread clear-archived-bindings --thread-id <id> --dry-run\n"
            "  focusctl thread clear-archived-bindings --all --dry-run\n"
            "  focusctl thread attach --thread-id <id>\n"
            "  focusctl thread detach --thread-name <name>\n"
            "  focusctl image send --thread-id <id> --path ./diagram.png\n"
            "  focusctl web open\n"
            "\n"
            "多实例:\n"
            "  focusctl --instance corp-a thread status --thread-name demo\n"
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"focusctl {__version__}")
    parser.add_argument(
        "--instance",
        help=(
            "目标实例；显式值优先。命名实例必须先 `focusctl instance create <name>`。"
            "省略时按运行中实例解析，必要时必须显式指定。"
        ),
    )
    subparsers = parser.add_subparsers(dest="resource", required=True, title="resources", metavar="resource")

    service = subparsers.add_parser(
        "service",
        help="修复目标实例的运行中服务。",
        description="运行中服务修复面。service 状态查看由 `focusctl service status` 的本机 service manager 路径负责。",
        formatter_class=_HelpFormatter,
    )
    service_sub = service.add_subparsers(dest="action", required=True, title="service commands", metavar="service-command")
    service_reset = service_sub.add_parser(
        "reset-backend",
        help="重置当前实例 backend，不重启 FOCUS service。",
        description=(
            "重置当前实例 backend，不重启 FOCUS service 进程。\n"
            "普通 reset 只在确认当前实例没有待处理工作时允许；如需打断当前实例里的运行中 turn / 审批 / 输入请求，可加 `--force`。"
        ),
        formatter_class=_HelpFormatter,
    )
    service_reset.add_argument(
        "--force",
        action="store_true",
        help="强制重置 backend，允许打断当前实例里正在进行的工作。",
    )
    service_sub.add_parser(
        "attach",
        help="恢复当前实例下 detached 的 Feishu 推送。",
        description="恢复当前实例下全部 detached 的 Feishu binding 推送；若部分 thread 被其他实例占用，会逐项报告 blocked 原因。",
        formatter_class=_HelpFormatter,
    )

    binding = subparsers.add_parser(
        "binding",
        help="查看或清理目标实例里的 Feishu binding。",
        description=(
            "Binding 管理面。\n"
            "`clear` / `clear-all` / `clear-stale` 删除的是 Feishu 本地 binding 记录，"
            "包括其中保存的 thread 指向和 binding-local 设置；不删除 thread，也不等于 `detach`。"
        ),
        formatter_class=_HelpFormatter,
    )
    binding_sub = binding.add_subparsers(dest="action", required=True, title="binding commands", metavar="binding-command")
    binding_list = binding_sub.add_parser(
        "list",
        help="列出当前实例可见 binding。",
        description=(
            "列出当前实例可见的 binding、运行态、关联 thread 与 cwd。\n"
            "默认只使用本地缓存显示 CHAT 名称；如需同步查询飞书 / 联系人 API，可加 `--refresh-names`。"
        ),
        formatter_class=_HelpFormatter,
    )
    binding_list.add_argument(
        "--refresh-names",
        action="store_true",
        help="显式刷新 CHAT 显示名缓存；可能访问飞书 / 联系人 API，耗时取决于 binding 数量和飞书请求超时。",
    )
    binding_status = binding_sub.add_parser(
        "status",
        help="查看单个 binding 详情。",
        description="查看单个 binding 的 chat、thread、runtime 与下一次发言可否被接受。",
        formatter_class=_HelpFormatter,
    )
    binding_status.add_argument("binding_id", help="目标 binding id。")
    binding_clear = binding_sub.add_parser(
        "clear",
        help="删除单个本地 binding 记录。",
        description="删除单个 Feishu 本地 binding 记录；不会删除 thread，也不会执行 detach。",
        formatter_class=_HelpFormatter,
    )
    binding_clear.add_argument("binding_id", help="要清除的 binding id。")
    binding_attach = binding_sub.add_parser(
        "attach",
        help="恢复单个 binding 的飞书推送。",
        description="让目标 binding 从 detached 恢复到 attached；不启动 turn，只恢复推送接收能力。",
        formatter_class=_HelpFormatter,
    )
    binding_attach.add_argument("binding_id", help="要恢复的 binding id。")
    binding_detach = binding_sub.add_parser(
        "detach",
        help="暂停单个 binding 的飞书推送。",
        description="让目标 binding 从 attached 变为 detached；保留本地 binding 记录，不删除 thread。",
        formatter_class=_HelpFormatter,
    )
    binding_detach.add_argument("binding_id", help="要暂停的 binding id。")
    binding_sub.add_parser(
        "clear-all",
        help="删除当前实例下全部本地 binding 记录。",
        description="删除当前实例下全部 Feishu 本地 binding 记录；不会删除 thread，也不会执行 detach。",
        formatter_class=_HelpFormatter,
    )
    binding_clear_stale = binding_sub.add_parser(
        "clear-stale",
        help="删除指向已不可读取 thread 的 stale binding 记录。",
        description=(
            "扫描本项目本地 binding，并通过运行中的 app-server 验证其 thread 是否仍可读取。\n"
            "明确不可读取的 thread 视为 stale 并删除对应本地 binding 记录；查询失败或无法判断时 fail-closed 保留。\n"
            "默认扫描所有运行中实例和已知非运行实例；传全局 `--instance <name>` 时只作用于该实例。"
        ),
        formatter_class=_HelpFormatter,
    )
    binding_clear_stale.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览会清理哪些 binding，不修改本地数据。",
    )

    prompt = subparsers.add_parser(
        "prompt",
        help="向某个 binding 合成提交一条新 prompt。",
        description=(
            "Prompt 注入管理面。\n"
            "当前只提供 `send`：直接通过正在运行的 FOCUS service，"
            "向目标 binding 对应的 thread 合成发起一轮新 prompt。"
        ),
        formatter_class=_HelpFormatter,
    )
    prompt_sub = prompt.add_subparsers(dest="action", required=True, title="prompt commands", metavar="prompt-command")
    prompt_send = prompt_sub.add_parser(
        "send",
        help="向目标 binding 发起一轮 synthetic prompt。",
        description=(
            "向目标 binding 发起一轮 synthetic prompt。\n"
            "这是 binding-scoped 动作；真正执行仍会经过 running-turn / attach / interaction 等保护，"
            "不可写时 fail-closed 返回拒绝原因。"
        ),
        formatter_class=_HelpFormatter,
    )
    prompt_send.add_argument("--binding-id", required=True, help="目标 binding id。")
    prompt_text_group = prompt_send.add_mutually_exclusive_group(required=True)
    prompt_text_group.add_argument("--text", help="要提交的 prompt 文本。")
    prompt_text_group.add_argument("--text-file", help="从本地 UTF-8 文本文件读取 prompt。")
    prompt_send.add_argument(
        "--synthetic-source",
        default="",
        help="可选 synthetic source 标签，例如 `schedule`。",
    )
    prompt_send.add_argument(
        "--display-mode",
        choices=("silent", "announce"),
        default="silent",
        help="是否仅在 synthetic prompt 成功启动后向目标聊天发送一条触发说明。",
    )
    prompt_send.add_argument(
        "--actor-open-id",
        default="",
        help="可选 actor_open_id；主要供 group/shared binding 的高级场景使用。",
    )

    thread = subparsers.add_parser(
        "thread",
        help="查看或管理 thread。",
        description=(
            "Thread 管理面。\n"
            "- `list` 默认列当前目录线程；也支持 `--scope global`\n"
            "- 其他 thread 子命令必须按各自帮助显式指定目标 thread\n"
            "- `goal` 是 thread-scoped 的本地调试 / 运维面，默认查看，也支持 set/clear\n"
            "- 所有实例共享同一套 persisted thread 发现面；实例差异主要体现在 live runtime 持有"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_sub = thread.add_subparsers(dest="action", required=True, title="thread commands", metavar="thread-command")
    thread_list = thread_sub.add_parser(
        "list",
        help="列出可见 thread。",
        description="列出 persisted thread。默认按当前目录过滤，也支持 `--scope global` 查看全局线程。",
        formatter_class=_HelpFormatter,
    )
    thread_list.add_argument("--scope", choices=("cwd", "global"), default="cwd", help="列线程时使用的作用域。")
    thread_list.add_argument("--cwd", default="", help="当 `--scope cwd` 时使用的目录；省略时取当前 shell 目录。")
    thread_list.add_argument(
        "--archived",
        action="store_true",
        help="列出 archived thread；仍沿用当前 scope 与默认 source 可见性。",
    )
    thread_status = thread_sub.add_parser(
        "status",
        help="查看单个 thread 详情。",
        description="查看单个 thread 的 backend 状态、绑定关系与 detach 可用性。",
        formatter_class=_HelpFormatter,
    )
    thread_status_target = thread_status.add_mutually_exclusive_group(required=True)
    thread_status_target.add_argument("--thread-id", help="目标 thread id。")
    thread_status_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_bindings = thread_sub.add_parser(
        "bindings",
        help="查看某个 thread 关联的 binding。",
        description="查看某个 thread 当前关联的 binding 列表。",
        formatter_class=_HelpFormatter,
    )
    thread_bindings_target = thread_bindings.add_mutually_exclusive_group(required=True)
    thread_bindings_target.add_argument("--thread-id", help="目标 thread id。")
    thread_bindings_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_goal = thread_sub.add_parser(
        "goal",
        help="查看或调试某个 thread 的 goal。",
        description=(
            "Thread goal 调试面。\n"
            "默认直接查看当前 goal，也支持 `show` / `set` / `clear`。\n"
            "其中 `set --status active|paused` 只是 thread-scoped 的 persisted goal 改写，"
            "不是 runtime resume / pause 命令；是否立即继续运行，仍取决于 thread 当前是否 loaded"
            " 以及 loaded 后是否 idle。\n"
            "这是本地 CLI 调试 / 运维面，直接经由 service control plane 调用 goal RPC。"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_goal_target = thread_goal.add_mutually_exclusive_group(required=False)
    thread_goal_target.add_argument("--thread-id", help="目标 thread id。")
    thread_goal_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_goal_sub = thread_goal.add_subparsers(dest="goal_action", required=False, title="goal commands", metavar="goal-command")
    thread_goal.set_defaults(goal_action="show")
    thread_goal_show = thread_goal_sub.add_parser(
        "show",
        help="查看某个 thread 当前 goal。",
        description="查看某个 thread 当前 goal；等价于省略 `show` 直接执行 `thread goal`。",
        formatter_class=_HelpFormatter,
    )
    thread_goal_show_target = thread_goal_show.add_mutually_exclusive_group(required=True)
    thread_goal_show_target.add_argument("--thread-id", help="目标 thread id。")
    thread_goal_show_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_goal_set = thread_goal_sub.add_parser(
        "set",
        help="设置或调试某个 thread 的 goal。",
        description="设置或调试某个 thread 的 goal；至少提供 `--objective` 或 `--status` 之一。",
        formatter_class=_HelpFormatter,
    )
    thread_goal_set_target = thread_goal_set.add_mutually_exclusive_group(required=True)
    thread_goal_set_target.add_argument("--thread-id", help="目标 thread id。")
    thread_goal_set_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_goal_set.add_argument("--objective", default="", help="新的 goal objective。")
    thread_goal_set.add_argument(
        "--status",
        choices=("active", "paused"),
        default="",
        help="可选 persisted goal 状态；当前只暴露 `active|paused` 这两个本地调试用状态改写。",
    )
    thread_goal_clear = thread_goal_sub.add_parser(
        "clear",
        help="清除某个 thread 当前 goal。",
        description="清除某个 thread 当前 goal。",
        formatter_class=_HelpFormatter,
    )
    thread_goal_clear_target = thread_goal_clear.add_mutually_exclusive_group(required=True)
    thread_goal_clear_target.add_argument("--thread-id", help="目标 thread id。")
    thread_goal_clear_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_archive = thread_sub.add_parser(
        "archive",
        help="归档一个或多个 thread，并清理指向它们的本地 bindings。",
        description=(
            "归档目标 thread，使其从常规列表中隐藏，而不是硬删除。\n"
            "可重复提供 `--thread-id` 做批量归档；批量时每个 thread 都独立按当前单线程语义路由并执行。\n"
            "归档成功后，会清理当前目标实例、其他可达运行实例，以及已知非运行实例里指向该 thread 的 bindings。\n"
            "Focus 会 fail-closed 检查本机已知实例的 loaded 状态，但不协调裸 Codex、IDE 或其他机器。"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_archive.add_argument(
        "--thread-id",
        dest="thread_ids",
        action="append",
        default=[],
        help="目标 thread id。可重复提供以批量归档。",
    )
    thread_archive.add_argument(
        "--thread-name",
        dest="thread_names",
        action="append",
        default=[],
        help="目标 thread 名称。仅单线程归档时可用，不能与 `--thread-id` 连用。",
    )
    thread_unarchive = thread_sub.add_parser(
        "unarchive",
        help="按 thread id 恢复一个或多个 archived thread。",
        description=(
            "逐项调用上游 Codex thread/unarchive，把 archived thread 恢复为未归档状态并放回常规列表。\n"
            "可重复提供 `--thread-id` 批量恢复；各项独立执行，结果 unknown 时停止，已成功项不回滚。\n"
            "可先运行 `focusctl thread list --archived --scope global` 查询归档线程及其 ID。\n"
            "执行前要求本机所有已知 Focus 实例都不再保留该 thread 的 binding 或 loaded runtime；"
            "成功后不会自动创建 binding。\n"
            "Focus 不协调裸 Codex、IDE 或其他机器。"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_unarchive.add_argument(
        "--thread-id",
        dest="thread_ids",
        action="append",
        required=True,
        help="目标 archived thread id。可重复提供以批量恢复。",
    )
    thread_delete = thread_sub.add_parser(
        "delete",
        help="按 thread id 永久删除一个 thread。",
        description=(
            "调用上游 Codex thread/delete 永久删除目标 thread。\n"
            "上游可能同时级联删除 spawned descendants；Focus 不把不完整查询包装成确认范围。\n"
            "执行前会检查本机已知 Focus runtime；请自行停止裸 Codex、IDE 或其他机器对同一 thread 的使用。"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_delete.add_argument(
        "--thread-id",
        dest="thread_ids",
        action="append",
        required=True,
        help="要永久删除的 thread id。只允许提供一次。",
    )
    thread_delete.add_argument(
        "--force",
        action="store_true",
        help="只跳过交互确认，不绕过 loaded/running/unknown 等安全检查；非交互环境必须提供。",
    )
    thread_clear_archived = thread_sub.add_parser(
        "clear-archived-bindings",
        help="删除已归档 thread 残留的本地 bindings。",
        description=(
            "只删除本项目本地 binding 记录，不调用上游 Codex archive。\n"
            "匹配的 binding 会整条删除，包括其中保存的 thread 指向和 binding-local 设置。\n"
            "必须显式选择 `--thread-id <id>` 或 `--all`；`--all` 会先通过运行中的实例查询上游 archived thread 列表。\n"
            "默认扫描所有运行中实例和已知非运行实例；传全局 `--instance <name>` 时只作用于该实例。\n"
            "适合补救旧版本 archive、外部归档，或服务重启后无 live owner 导致的跨实例残留。"
        ),
        formatter_class=_HelpFormatter,
    )
    thread_clear_archived_target = thread_clear_archived.add_mutually_exclusive_group(required=True)
    thread_clear_archived_target.add_argument("--thread-id", help="要删除本地 binding 的 archived thread id。")
    thread_clear_archived_target.add_argument(
        "--all",
        dest="all_archived",
        action="store_true",
        help="查询上游 archived thread 列表，并删除命中的本地 binding 记录。",
    )
    thread_clear_archived.add_argument(
        "--dry-run",
        action="store_true",
        help="只预览会清理哪些 binding，不修改本地数据。",
    )
    thread_detach = thread_sub.add_parser(
        "detach",
        help="暂停某个 thread 的飞书推送。",
        description="让 Feishu 服务暂停该 thread 当前 attached bindings 的推送，同时保留 thread 与 binding 关系。",
        formatter_class=_HelpFormatter,
    )
    thread_detach_target = thread_detach.add_mutually_exclusive_group(required=True)
    thread_detach_target.add_argument("--thread-id", help="目标 thread id。")
    thread_detach_target.add_argument("--thread-name", help="目标 thread 名称。")
    thread_attach = thread_sub.add_parser(
        "attach",
        help="恢复某个 thread 下 detached 的飞书推送。",
        description="把目标 thread 当前所有 detached 的 Feishu bindings 恢复到 attached；不启动 turn。",
        formatter_class=_HelpFormatter,
    )
    thread_attach_target = thread_attach.add_mutually_exclusive_group(required=True)
    thread_attach_target.add_argument("--thread-id", help="目标 thread id。")
    thread_attach_target.add_argument("--thread-name", help="目标 thread 名称。")

    image = subparsers.add_parser(
        "image",
        help="向某个 thread 的 attached Feishu bindings 发送图片。",
        description=(
            "图片出站管理面。\n"
            "当前只提供 `send`：把一张本地图片发送到目标 thread 当前所有 attached 的 Feishu bindings。\n"
            "如果省略 `--thread-id/--thread-name`，会尝试读取当前环境变量 `CODEX_THREAD_ID`。"
        ),
        formatter_class=_HelpFormatter,
    )
    image_sub = image.add_subparsers(dest="action", required=True, title="image commands", metavar="image-command")
    image_send = image_sub.add_parser(
        "send",
        help="把本地图片发送到目标 thread 的所有 attached bindings。",
        description=(
            "把一张本地图片发送到目标 thread 当前所有 attached 的 Feishu bindings。\n"
            "这是 thread-scoped 动作，不会扫描工作区，也不会自动推断任意图片文件。"
        ),
        formatter_class=_HelpFormatter,
    )
    image_send.add_argument("--path", required=True, help="本地图片路径。")
    image_send_target = image_send.add_mutually_exclusive_group(required=False)
    image_send_target.add_argument("--thread-id", help="目标 thread id。省略时可回落到 `CODEX_THREAD_ID`。")
    image_send_target.add_argument("--thread-name", help="目标 thread 名称。")

    web_resource = subparsers.add_parser(
        "web",
        help="打开目标实例的 local/SSH Focus Web 前端。",
        description=(
            "读取运行中目标实例发布的一次性 loopback URL，并在默认浏览器中打开。\n"
            "该入口只用于本机或 SSH local forwarding；configured trusted HTTPS proxy 的"
            "外部用户应直接打开其 HTTPS origin，`web open` 不会输出 external URL。"
        ),
        formatter_class=_HelpFormatter,
    )
    web_sub = web_resource.add_subparsers(dest="action", required=True, title="web commands", metavar="web-command")
    web_open = web_sub.add_parser(
        "open",
        help="生成 local/SSH 一次性引导 URL 并打开浏览器。",
        description=(
            "为本机或 SSH local forwarding 生成一次性 loopback 引导 URL，并默认打开浏览器。\n"
            "configured trusted HTTPS proxy 的外部用户应直接打开其 HTTPS origin；"
            "本命令不会输出 external URL。"
        ),
        formatter_class=_HelpFormatter,
    )
    web_open.add_argument(
        "--no-browser",
        action="store_true",
        help="只输出一次性 URL，不调用系统默认浏览器。",
    )
    return parser
