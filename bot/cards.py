"""
feishu-codex 飞书卡片构建。
"""

import json

from bot.constants import KEYWORD, display_path, format_timestamp, shorten
from bot.feishu_bot import _MAX_CARD_TABLES, count_card_tables, limit_card_tables

_HISTORY_TEXT_MAX = 300
_PLAN_CONTENT_MAX = 4000


def build_execution_card(
    log_text: str,
    reply_text: str = "",
    *,
    running: bool = False,
    elapsed: int = 0,
    cancelled: bool = False,
) -> dict:
    """构造主执行卡片。"""
    if running:
        template = "turquoise"
        header_content = f"Codex（执行中 {elapsed}s）" if elapsed > 0 else "Codex（执行中）"
    elif cancelled:
        template = "grey"
        header_content = "Codex（已停止）"
    else:
        template = "blue"
        header_content = "Codex"

    panel_icon = {
        "tag": "standard_icon",
        "token": "right-small-ccm_outlined",
        "size": "16px 16px",
    }

    def _panel(title: str, content: str, expanded: bool) -> dict:
        return {
            "tag": "collapsible_panel",
            "expanded": expanded,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "icon": panel_icon,
                "icon_position": "left",
                "icon_expanded_angle": 90,
            },
            "elements": [{"tag": "markdown", "content": content or ""}],
        }

    elements: list[dict] = []
    elements.append(
        {
            "tag": "markdown",
            "content": "*提示：发送 `/help` 查看可用命令列表。*",
        }
    )
    elements.append({"tag": "hr"})
    if log_text and reply_text:
        log_tables = count_card_tables(log_text)
        reply_tables = count_card_tables(reply_text)
        if log_tables + reply_tables > _MAX_CARD_TABLES:
            reply_budget = min(reply_tables, _MAX_CARD_TABLES)
            log_budget = _MAX_CARD_TABLES - reply_budget
            log_text = limit_card_tables(log_text, log_budget)
            reply_text = limit_card_tables(reply_text, reply_budget)
        elements.append(_panel("执行过程", log_text, expanded=running))
        elements.append(_panel("回复", reply_text, expanded=True))
    elif reply_text:
        elements.append(_panel("回复", limit_card_tables(reply_text), expanded=True))
    elif log_text:
        elements.append(_panel("执行过程", limit_card_tables(log_text), expanded=running))
    else:
        elements.append({"tag": "markdown", "content": "*暂无输出*"})

    if running:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消执行"},
                "type": "danger",
                "value": {"action": "cancel_turn", "plugin": KEYWORD},
            }
        )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_content},
            "template": template,
        },
        "body": {"elements": elements},
    }


def build_command_approval_card(
    request_id: str,
    *,
    command: str,
    cwd: str = "",
    reason: str = "",
) -> dict:
    """构造命令审批卡片。"""
    cwd_display = display_path(cwd) if cwd else "-"
    content = [f"**工作目录**: `{cwd_display}`", "**命令**:", f"```bash\n{command or '(空命令)'}\n```"]
    if reason:
        content.append(f"**原因**: {reason}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 命令执行审批"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(content)},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本次"},
                        "type": "primary",
                        "value": {
                            "action": "command_allow_once",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "command_allow_session",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "command_deny",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "中止本轮"},
                        "type": "danger",
                        "value": {
                            "action": "command_abort",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                ],
            },
        ],
    }


def build_file_change_approval_card(
    request_id: str,
    *,
    grant_root: str = "",
    reason: str = "",
) -> dict:
    """构造文件修改审批卡片。"""
    lines = []
    if grant_root:
        lines.append(f"**授权根目录**: `{display_path(grant_root)}`")
    else:
        lines.append("**授权范围**: 当前变更")
    if reason:
        lines.append(f"**原因**: {reason}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 文件修改审批"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本次"},
                        "type": "primary",
                        "value": {
                            "action": "file_change_accept",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "file_change_accept_session",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "file_change_decline",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "中止本轮"},
                        "type": "danger",
                        "value": {
                            "action": "file_change_cancel",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                ],
            },
        ],
    }


def build_permissions_approval_card(
    request_id: str,
    *,
    permissions: dict,
    reason: str = "",
) -> dict:
    """构造额外权限审批卡片。"""
    fs_profile = permissions.get("fileSystem") or {}
    network_profile = permissions.get("network") or {}
    lines: list[str] = []

    read_paths = fs_profile.get("read") or []
    write_paths = fs_profile.get("write") or []
    if read_paths:
        lines.append("**新增读权限**:")
        lines.extend(f"- `{display_path(path)}`" for path in read_paths[:10])
    if write_paths:
        lines.append("**新增写权限**:")
        lines.extend(f"- `{display_path(path)}`" for path in write_paths[:10])
    if network_profile.get("enabled"):
        lines.append("**新增网络权限**: 已启用")
    if reason:
        lines.append(f"**原因**: {reason}")
    if not lines:
        lines.append("*未提供具体权限详情*")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 额外权限审批"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本次"},
                        "type": "primary",
                        "value": {
                            "action": "permissions_allow_once",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "permissions_allow_session",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "permissions_deny",
                            "plugin": KEYWORD,
                            "request_id": request_id,
                        },
                    },
                ],
            },
        ],
    }


def build_approval_handled_card(title: str, decision: str, detail: str = "") -> dict:
    """构造已处理审批卡片。"""
    content = f"已{decision}。"
    if detail:
        content = f"{content}\n{detail}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "grey",
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def build_approval_policy_card(current_policy: str) -> dict:
    """构造原生审批策略选择卡片。"""
    labels = {
        "untrusted": "untrusted",
        "on-failure": "on-failure",
        "on-request": "on-request",
        "never": "never",
    }
    descs = {
        "untrusted": "偏保守，更多操作会要求审批。",
        "on-failure": "仅在受限操作失败后请求审批。",
        "on-request": "仅在模型显式请求时审批。",
        "never": "不请求审批，自动执行。",
    }

    buttons = []
    elements = [
        {
            "tag": "markdown",
            "content": f"当前审批策略：**{labels[current_policy]}**\n{descs[current_policy]}",
        },
        {"tag": "hr"},
    ]
    for policy, label in labels.items():
        elements.append({"tag": "markdown", "content": f"**{label}**\n{descs[policy]}"})
        buttons.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"{'✓ ' if policy == current_policy else ''}{label}",
                },
                "type": "primary" if policy == current_policy else "default",
                "value": {
                    "action": "set_approval_policy",
                    "plugin": KEYWORD,
                    "policy": policy,
                },
            }
        )
    elements.append({"tag": "action", "actions": buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 审批策略"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_collaboration_mode_card(current_mode: str, *, running: bool = False) -> dict:
    """构造协作模式选择卡片。"""
    labels = {
        "default": "default",
        "plan": "plan",
    }
    descs = {
        "default": "普通协作模式，更接近直接执行；不保证触发原生 requestUserInput 或计划通知。",
        "plan": "规划式协作模式；可启用原生 requestUserInput，并可能产出计划通知。",
    }

    current_desc = descs[current_mode]
    if running:
        current_desc += "\n\n当前若有执行中的 turn，切换仅对下一轮生效。"

    elements = [
        {
            "tag": "markdown",
            "content": f"当前协作模式：**{labels[current_mode]}**\n{current_desc}",
        },
        {"tag": "hr"},
    ]
    buttons = []
    for mode, label in labels.items():
        elements.append({"tag": "markdown", "content": f"**{label}**\n{descs[mode]}"})
        buttons.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"{'✓ ' if mode == current_mode else ''}{label}",
                },
                "type": "primary" if mode == current_mode else "default",
                "value": {
                    "action": "set_collaboration_mode",
                    "plugin": KEYWORD,
                    "mode": mode,
                },
            }
        )
    elements.append({"tag": "action", "actions": buttons})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 协作模式"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_ask_user_card(
    request_id: str,
    questions: list[dict],
    answers: dict[str, str] | None = None,
) -> dict:
    """构造 requestUserInput 卡片。"""
    answers = answers or {}
    elements: list[dict] = []

    pending_ids = [q.get("id", "") for q in questions if q.get("id", "") not in answers]
    current_id = pending_ids[0] if pending_ids else ""

    for index, question in enumerate(questions, start=1):
        qid = question.get("id", "")
        header = question.get("header") or f"问题 {index}"
        question_text = question.get("question", "")
        options = question.get("options") or []
        allow_custom = bool(question.get("isOther", False)) or not options
        is_secret = bool(question.get("isSecret", False))

        if qid in answers:
            answer_text = "（已提交隐藏内容）" if is_secret else answers[qid]
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"**{header}**\n~~已回答：{answer_text}~~",
                }
            )
            elements.append({"tag": "hr"})
            continue

        if qid != current_id:
            elements.append({"tag": "markdown", "content": f"**{header}**\n*待回答*"})
            elements.append({"tag": "hr"})
            continue

        elements.append(
            {
                "tag": "markdown",
                "content": f"**{header}**\n\n{question_text}",
            }
        )

        if options:
            option_lines = []
            for opt in options:
                label = opt.get("label", "")
                desc = opt.get("description", "")
                option_lines.append(f"**{label}**: {desc}" if desc else f"**{label}**")
            elements.append({"tag": "markdown", "content": "\n".join(option_lines)})
            elements.append({"tag": "hr"})
            elements.append(
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": opt.get("label", "选项")},
                            "type": "primary" if idx == 0 else "default",
                            "value": {
                                "action": "answer_user_input_option",
                                "plugin": KEYWORD,
                                "request_id": request_id,
                                "question_id": qid,
                                "answer": opt.get("label", ""),
                            },
                        }
                        for idx, opt in enumerate(options)
                    ],
                }
            )

        if allow_custom:
            elements.append(
                {
                    "tag": "form",
                    "name": f"user_input_form_{qid}",
                    "elements": [
                        {
                            "tag": "input",
                            "name": f"user_input_{qid}",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "输入自定义回答…",
                            },
                        },
                        {
                            "tag": "button",
                            "name": f"submit_{qid}",
                            "text": {"tag": "plain_text", "content": "提交"},
                            "type": "default",
                            "form_action_type": "submit",
                            "value": {
                                "action": "answer_user_input_custom",
                                "plugin": KEYWORD,
                                "request_id": request_id,
                                "question_id": qid,
                            },
                        },
                    ],
                }
            )

        if is_secret and allow_custom:
            elements.append(
                {
                    "tag": "markdown",
                    "content": "*注意：飞书卡片输入框本身不是保密控件，敏感信息请谨慎输入。*",
                }
            )

        elements.append({"tag": "hr"})

    pending_count = len(pending_ids)
    title = "Codex 需要你的输入" if pending_count <= 1 else f"Codex 需要你的输入（剩余 {pending_count} 题）"

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements or [{"tag": "markdown", "content": "已全部回答。"}],
    }


def build_ask_user_answered_card(
    questions: list[dict],
    answers: dict[str, str],
) -> dict:
    """构造问答已完成卡片。"""
    lines = []
    for question in questions:
        qid = question.get("id", "")
        header = question.get("header") or qid or "问题"
        answer = answers.get(qid, "（未回答）")
        if question.get("isSecret", False) and qid in answers:
            answer = "（已提交隐藏内容）"
        lines.append(f"**{header}**\n{answer}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 用户输入 - 已提交"},
            "template": "grey",
        },
        "elements": [{"tag": "markdown", "content": "\n\n".join(lines) or "已提交。"}],
    }


def _thread_origin_text(source: str, service_name: str | None) -> str:
    source_text = source or "unknown"
    if service_name:
        return f"`{source_text}` / `{service_name}`"
    return f"`{source_text}`"


def build_resume_guard_card(
    thread_id: str,
    *,
    title: str,
    cwd: str,
    updated_at: int,
    source: str,
    service_name: str | None,
) -> dict:
    """构造外部线程恢复风险卡片。"""
    origin_text = _thread_origin_text(source, service_name)
    content = (
        f"**{shorten(title, 160)}**\n"
        f"thread: `{thread_id[:8]}…`\n"
        f"目录：`{display_path(cwd)}`\n"
        f"更新时间：`{format_timestamp(updated_at)}`\n"
        f"来源：{origin_text}\n\n"
        "该线程当前**未加载在 feishu-codex 的 backend 中**。\n"
        "如果直接恢复并继续写入，feishu-codex 会在自己的 backend 里再恢复一份 live thread。\n"
        "若本地裸 `codex` 也继续写同一线程，历史可能分叉或错乱。\n\n"
        "如果只是想先看内容，请选“查看快照”；如果希望本地与飞书安全共用同一线程，请改用 `fcodex`。"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "恢复线程前确认"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": content},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看快照"},
                        "type": "primary",
                        "value": {
                            "action": "preview_thread_snapshot",
                            "plugin": KEYWORD,
                            "thread_id": thread_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "恢复并继续写入"},
                        "type": "danger",
                        "value": {
                            "action": "resume_thread_write",
                            "plugin": KEYWORD,
                            "thread_id": thread_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {
                            "action": "cancel_resume_guard",
                            "plugin": KEYWORD,
                            "thread_id": thread_id,
                        },
                    },
                ],
            },
        ],
    }


def build_thread_snapshot_card(
    thread_id: str,
    *,
    title: str,
    cwd: str,
    updated_at: int,
    source: str,
    service_name: str | None,
    rounds: list[tuple[str, str]],
) -> dict:
    """构造线程快照卡片。"""
    origin_text = _thread_origin_text(source, service_name)
    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": (
                f"**{shorten(title, 160)}**\n"
                f"thread: `{thread_id[:8]}…`\n"
                f"目录：`{display_path(cwd)}`\n"
                f"更新时间：`{format_timestamp(updated_at)}`\n"
                f"来源：{origin_text}\n\n"
                "*当前仅查看快照，尚未在 feishu-codex backend 中恢复此线程。*"
            ),
        },
        {"tag": "hr"},
    ]

    if rounds:
        for user_text, assistant_text in rounds:
            elements.append({"tag": "markdown", "content": f"👤 **你**\n{shorten(user_text, _HISTORY_TEXT_MAX)}"})
            elements.append({"tag": "markdown", "content": f"🤖 **Codex**\n{shorten(assistant_text, _HISTORY_TEXT_MAX)}"})
            elements.append({"tag": "hr"})
    else:
        elements.append({"tag": "markdown", "content": "*暂无可展示历史。*"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"线程 {thread_id[:8]}… 快照"},
            "template": "green",
        },
        "elements": elements,
    }


def build_session_row(session: dict, current_thread_id: str) -> list[dict]:
    """构造单个线程行。"""
    thread_id = session["thread_id"]
    current = thread_id == current_thread_id
    title = session.get("title", "（无标题）")
    if session.get("starred"):
        title = f"⭐ {title}"

    summary_parts = [f"**{thread_id[:8]}…**", f"`{display_path(session.get('cwd', ''))}`"]
    if session.get("model_provider"):
        summary_parts.append(f"`{session['model_provider']}`")
    summary_parts.append(format_timestamp(session.get("updated_at")))
    line = " | ".join(summary_parts) + f"\n{shorten(title, 120)}"

    return [
        {"tag": "markdown", "content": line},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": f"{'✓ 当前  ' if current else ''}恢复",
                    },
                    "type": "primary" if current else "default",
                    "value": {
                        "action": "resume_thread",
                        "plugin": KEYWORD,
                        "thread_id": thread_id,
                    },
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "取消收藏" if session.get("starred") else "收藏",
                    },
                    "type": "default",
                    "value": {
                        "action": "toggle_star_thread",
                        "plugin": KEYWORD,
                        "thread_id": thread_id,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "重命名"},
                    "type": "default",
                    "value": {
                        "action": "show_rename_form",
                        "plugin": KEYWORD,
                        "thread_id": thread_id,
                    },
                },
            ],
        },
        {"tag": "hr"},
    ]


def build_sessions_card(
    sessions: list[dict],
    current_thread_id: str,
    working_dir: str,
    total_count: int,
    *,
    shown_starred_count: int,
    total_starred_count: int,
    shown_unstarred_count: int,
    total_unstarred_count: int,
) -> dict:
    """构造线程列表卡片。"""
    working_dir_display = display_path(working_dir) or working_dir or "."
    summary_parts: list[str] = []
    if total_starred_count:
        summary = f"收藏 {shown_starred_count} / {total_starred_count} 个"
        summary_parts.append(summary)
    if total_unstarred_count:
        summary = f"未收藏 {shown_unstarred_count} / {total_unstarred_count} 个"
        summary_parts.append(summary)
    if not summary_parts:
        summary_parts.append(f"共 {total_count} 个线程")

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": (
                f"当前目录：`{working_dir_display}`\n"
                "已按当前目录跨 provider 汇总显示线程。\n"
                f"收藏优先，其余按最近更新时间排序。\n"
                f"当前显示：{'，'.join(summary_parts)}。\n"
                "全局恢复请用 `/resume <thread_id|thread_name>`。\n"
                "wrapper 级 `fcodex /resume <thread_name>` 与飞书 `/resume` 使用同一套跨 provider 精确匹配；"
                "本地若想先看线程，可用 `fcodex /session`；"
                "`fcodex /help`、`/profile`、`/rm`、`/session`、`/resume` 这些 shell wrapper 自命令必须单独使用；"
                "`fcodex` TUI 内置 `/resume` 仍保持 upstream 原样。"
            ),
        },
        {"tag": "hr"},
    ]

    for session in sessions:
        elements.extend(build_session_row(session, current_thread_id))

    if not sessions:
        elements.append({"tag": "markdown", "content": "*当前目录下暂无可恢复线程。*"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 当前目录线程"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_rename_card(session: dict) -> dict:
    """构造重命名卡片。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "重命名线程"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**{session['thread_id'][:8]}…** | `{display_path(session.get('cwd', ''))}`\n"
                    f"当前标题：{session.get('title', '（无标题）')}"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "form",
                "name": "rename_thread_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "rename_title",
                        "placeholder": {
                            "tag": "plain_text",
                            "content": "输入新标题…",
                        },
                        "default_value": session.get("title", ""),
                    },
                    {
                        "tag": "button",
                        "name": "submit_rename",
                        "text": {"tag": "plain_text", "content": "确认"},
                        "type": "primary",
                        "form_action_type": "submit",
                        "value": {
                            "action": "rename_thread",
                            "plugin": KEYWORD,
                            "thread_id": session["thread_id"],
                        },
                    },
                ],
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {
                            "action": "cancel_rename",
                            "plugin": KEYWORD,
                        },
                    }
                ],
            },
        ],
    }


def build_history_preview_card(thread_id: str, rounds: list[tuple[str, str]]) -> dict:
    """构造历史预览卡片。"""
    elements: list[dict] = []
    for user_text, assistant_text in rounds:
        elements.append({"tag": "markdown", "content": f"👤 **你**\n{shorten(user_text, _HISTORY_TEXT_MAX)}"})
        elements.append({"tag": "markdown", "content": f"🤖 **Codex**\n{shorten(assistant_text, _HISTORY_TEXT_MAX)}"})
        elements.append({"tag": "hr"})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"线程 {thread_id[:8]}… 最近对话"},
            "template": "green",
        },
        "elements": elements or [{"tag": "markdown", "content": "*暂无可展示历史。*"}],
    }


def build_plan_card(
    turn_id: str,
    *,
    explanation: str = "",
    plan_steps: list[dict] | None = None,
    plan_text: str = "",
) -> dict:
    """构造计划卡片。"""
    plan_steps = plan_steps or []
    elements: list[dict] = []

    if explanation:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**说明**\n{shorten(explanation, _PLAN_CONTENT_MAX)}",
            }
        )
        elements.append({"tag": "hr"})

    if plan_steps:
        status_labels = {
            "pending": "[ ]",
            "inProgress": "[~]",
            "completed": "[x]",
        }
        lines = [
            f"{status_labels.get(step.get('status', ''), '[ ]')} {shorten(step.get('step', ''), 240)}"
            for step in plan_steps
            if step.get("step")
        ]
        if lines:
            elements.append(
                {
                    "tag": "markdown",
                    "content": "**计划步骤**\n" + "\n".join(lines),
                }
            )
            elements.append({"tag": "hr"})

    if plan_text:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**计划正文**\n{shorten(plan_text, _PLAN_CONTENT_MAX)}",
            }
        )

    if not elements:
        elements.append({"tag": "markdown", "content": "*暂未收到可展示的计划内容。*"})

    title = f"Codex 计划 {turn_id[:8]}…" if turn_id else "Codex 计划"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "green",
        },
        "elements": elements,
    }
