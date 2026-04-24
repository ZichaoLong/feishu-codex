"""
feishu-codex 飞书卡片构建。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

from bot.card_text_projection import (
    TERMINAL_RESULT_CARD_HINT,
    TERMINAL_RESULT_CARD_TITLE,
    render_final_reply_text_block,
)
from bot.constants import display_path, format_timestamp, shorten
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_bot import _MAX_CARD_TABLES, count_card_tables, limit_card_tables
from bot.shared_command_surface import get_shared_command


def make_card_response(
    card: dict | None = None,
    toast: str | None = None,
    toast_type: str = "info",
) -> P2CardActionTriggerResponse:
    """构造卡片动作的响应（可更新卡片 / 弹 toast）"""
    resp = P2CardActionTriggerResponse()
    if toast:
        resp.toast = CallBackToast()
        resp.toast.type = toast_type
        resp.toast.content = toast
    if card:
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = card
    return resp


@dataclass(frozen=True)
class CommandResult:
    """Command handler return value; handler dispatches the reply."""

    text: str = ""
    card: dict | None = None
    after_dispatch: Callable[[], None] | None = None


_HISTORY_TEXT_MAX = 300
_PLAN_CONTENT_MAX = 4000
_SHARED_HELP_COMMAND = get_shared_command("help")
_SHARED_RESUME_COMMAND = get_shared_command("resume")
_LOCAL_THREAD_LIST_CWD = "feishu-codexctl thread list --scope cwd"
_LOCAL_RESUME_COMMAND = "fcodex resume <thread_id|thread_name>"


def _card_config() -> dict:
    return {"wide_screen_mode": True, "update_multi": True}


def build_markdown_card(title: str, content: str, *, template: str = "blue") -> dict:
    """构造简单说明卡片。"""
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def build_terminal_result_card(final_reply_text: str) -> dict:
    """构造终态结果卡。"""
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": TERMINAL_RESULT_CARD_TITLE},
            "template": "green",
        },
        "elements": [
            {"tag": "markdown", "content": TERMINAL_RESULT_CARD_HINT},
            {"tag": "hr"},
            {"tag": "markdown", "content": render_final_reply_text_block(final_reply_text)},
        ],
    }


def build_profile_card(
    *,
    content: str,
    profile_names: list[str],
    current_profile: str,
    title: str = "Codex Thread Profile",
) -> dict:
    """构造 profile 选择卡片。"""
    elements: list[dict] = [
        {"tag": "markdown", "content": content},
    ]
    if profile_names:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": profile_name},
                            "type": "primary" if profile_name == current_profile else "default",
                            "value": {
                                "action": "set_profile",
                                "profile": profile_name,
                            },
                        }
                        for profile_name in profile_names
                    ],
                },
            ]
        )
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }


def _back_to_help_action() -> dict:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "返回帮助"},
                "type": "default",
                "value": {
                    "action": "show_help_page",
                    "page": "overview",
                },
            }
        ],
    }


def build_execution_card(
    log_text: str,
    reply_segments: list[ExecutionReplySegment],
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

    def _panel_with_elements(title: str, panel_elements: list[dict], expanded: bool) -> dict:
        return {
            "tag": "collapsible_panel",
            "expanded": expanded,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "icon": panel_icon,
                "icon_position": "left",
                "icon_expanded_angle": 90,
            },
            "elements": panel_elements or [{"tag": "markdown", "content": ""}],
        }

    def _reply_panel_elements(segments: list[ExecutionReplySegment]) -> list[dict]:
        panel_elements: list[dict] = []
        remaining = _MAX_CARD_TABLES
        for segment in segments:
            if segment.kind == "divider":
                if panel_elements:
                    panel_elements.append({"tag": "hr"})
                continue
            text = segment.text
            if remaining > 0:
                text = limit_card_tables(text, remaining)
                remaining -= count_card_tables(text)
            panel_elements.append({"tag": "markdown", "content": text})
        return panel_elements

    elements: list[dict] = []
    elements.append(
        {
            "tag": "markdown",
            "content": f"*提示：发送 `{_SHARED_HELP_COMMAND.slash_name}` 查看可用命令列表。*",
        }
    )
    elements.append({"tag": "hr"})
    reply_panel_elements = _reply_panel_elements(reply_segments)
    if log_text and reply_panel_elements:
        log_tables = count_card_tables(log_text)
        if log_tables > _MAX_CARD_TABLES:
            log_text = limit_card_tables(log_text, _MAX_CARD_TABLES)
        elements.append(_panel("执行过程", log_text, expanded=False))
        elements.append(_panel_with_elements("回复", reply_panel_elements, expanded=True))
    elif reply_panel_elements:
        elements.append(_panel_with_elements("回复", reply_panel_elements, expanded=True))
    elif log_text:
        elements.append(_panel("执行过程", limit_card_tables(log_text), expanded=False))
    else:
        elements.append({"tag": "markdown", "content": "*暂无输出*"})

    if running:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "取消执行"},
                "type": "danger",
                "value": {"action": "cancel_turn"},
            }
        )

    return {
        "schema": "2.0",
        "config": _card_config(),
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
        "config": _card_config(),
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
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "command_allow_session",
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "command_deny",
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "中止本轮"},
                        "type": "danger",
                        "value": {
                            "action": "command_abort",
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
        "config": _card_config(),
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
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "file_change_accept_session",
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "file_change_decline",
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "中止本轮"},
                        "type": "danger",
                        "value": {
                            "action": "file_change_cancel",
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
        "config": _card_config(),
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
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许本会话"},
                        "type": "default",
                        "value": {
                            "action": "permissions_allow_session",
                            "request_id": request_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "permissions_deny",
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
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "grey",
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def build_approval_policy_card(current_policy: str, *, running: bool = False) -> dict:
    """构造原生审批策略选择卡片。"""
    labels = {
        "untrusted": "untrusted",
        "on-failure": "on-failure",
        "on-request": "on-request",
        "never": "never",
    }
    descs = {
        "untrusted": "偏保守，更多操作会先停下来等你确认。",
        "on-failure": "受限操作失败后，再请求审批。",
        "on-request": "仅在模型明确请求时，才停下来等你确认。",
        "never": "不请求审批，直接执行。",
    }

    current_label = labels.get(current_policy, current_policy or "（未设置）")
    current_desc = (
        "它只决定什么时候停下来等你确认，不改变文件或网络边界。\n"
        "多数情况下，优先使用 `/permissions`。\n"
        "作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
    )
    if running:
        current_desc += "\n\n当前若有执行中的 turn，切换仅对下一轮生效。"

    buttons = []
    elements = [
        {
            "tag": "markdown",
            "content": f"当前审批策略：**{current_label}**\n{current_desc}",
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
                    "policy": policy,
                },
            }
        )
    elements.append({"tag": "action", "layout": "trisection", "actions": buttons})

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 审批策略"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_sandbox_policy_card(current_sandbox: str, *, running: bool = False) -> dict:
    """构造沙箱策略选择卡片。"""
    labels = {
        "read-only": "read-only",
        "workspace-write": "workspace-write",
        "danger-full-access": "danger-full-access",
    }
    descs = {
        "read-only": "只读当前工作区；修改文件通常需要更高权限组合。",
        "workspace-write": "可读写当前工作区；工作区外写入仍受限。",
        "danger-full-access": "可编辑工作区外文件并直接联网，风险最高。",
    }

    current_label = labels.get(current_sandbox, current_sandbox or "（未设置）")
    current_desc = (
        "它只决定文件和网络边界，不决定是否停下来审批。\n"
        "多数情况下，优先使用 `/permissions`。\n"
        "作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
    )
    if running:
        current_desc += "\n\n当前若有执行中的 turn，切换仅对下一轮生效。"

    buttons = []
    elements = [
        {
            "tag": "markdown",
            "content": f"当前沙箱策略：**{current_label}**\n{current_desc}",
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
                    "content": f"{'✓ ' if policy == current_sandbox else ''}{label}",
                },
                "type": "primary" if policy == current_sandbox else "default",
                "value": {
                    "action": "set_sandbox_policy",
                    "policy": policy,
                },
            }
        )
    elements.append({"tag": "action", "layout": "trisection", "actions": buttons})

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 沙箱策略"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_permissions_preset_card(
    current_approval: str,
    current_sandbox: str,
    *,
    running: bool = False,
) -> dict:
    """构造权限预设选择卡片。"""
    presets = [
        {
            "id": "read-only",
            "label": "read-only",
            "description": "只读当前工作区；更安全，改文件前通常会停下来。",
            "approval": "on-request",
            "sandbox": "read-only",
        },
        {
            "id": "default",
            "label": "default",
            "description": "推荐默认值；可改当前工作区，风险和可用性更平衡。",
            "approval": "on-request",
            "sandbox": "workspace-write",
        },
        {
            "id": "full-access",
            "label": "full-access",
            "description": "不再请求审批；也可联网并写工作区外文件，风险最高。",
            "approval": "never",
            "sandbox": "danger-full-access",
        },
    ]
    current_preset = next(
        (
            preset["id"]
            for preset in presets
            if preset["approval"] == current_approval and preset["sandbox"] == current_sandbox
        ),
        "",
    )
    current_label = next(
        (preset["label"] for preset in presets if preset["id"] == current_preset),
        f"Custom ({current_sandbox}, {current_approval})",
    )
    current_desc = (
        "推荐先用这个；它会同时设置审批策略和沙箱。\n"
        "不确定时，优先选 `default`。\n"
        "作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。\n\n"
        f"审批：`{current_approval}`\n"
        f"沙箱：`{current_sandbox}`"
    )
    if running:
        current_desc += "\n\n当前若有执行中的 turn，切换仅对下一轮生效。"

    buttons = []
    elements = [
        {
            "tag": "markdown",
            "content": f"当前权限预设：**{current_label}**\n{current_desc}",
        },
        {"tag": "hr"},
    ]
    for preset in presets:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{preset['label']}**\n{preset['description']}",
            }
        )
        buttons.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"{'✓ ' if preset['id'] == current_preset else ''}{preset['label']}",
                },
                "type": "primary" if preset["id"] == current_preset else "default",
                "value": {
                    "action": "set_permissions_preset",
                    "preset": preset["id"],
                },
            }
        )
    elements.append({"tag": "action", "layout": "trisection", "actions": buttons})
    elements.append(_back_to_help_action())

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 权限预设"},
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
        "default": "更接近直接执行。",
        "plan": "更容易先规划、提问，并展示计划卡片。",
    }

    current_desc = (
        f"{descs[current_mode]}\n"
        "如果你希望模型先澄清再动手，通常用 `plan`。\n"
        "作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
    )
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
                    "mode": mode,
                },
            }
        )
    elements.append({"tag": "action", "layout": "trisection", "actions": buttons})
    elements.append(_back_to_help_action())

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 协作模式"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_group_mode_card(current_mode: str, *, can_manage: bool) -> dict:
    """构造群聊工作态选择卡片。"""
    labels = {
        "assistant": "assistant",
        "all": "all",
        "mention_only": "mention-only",
    }
    descs = {
        "assistant": "缓存群聊消息，仅在有效 mention 时回复；适合群讨论助手。",
        "all": "群内消息都会直接触发机器人回复；风险最高，容易刷屏。",
        "mention_only": "只有有效 mention 的消息才会触发响应；不缓存上下文。",
    }
    current_desc = descs.get(current_mode, current_mode)
    content = (
        f"当前群聊工作态：**{labels.get(current_mode, current_mode)}**\n"
        f"{current_desc}\n\n"
        "作用范围：只影响当前群。\n\n"
        "在 `assistant` / `mention-only` 中，群命令本身也需要先显式 mention 触发对象。"
    )
    if not can_manage:
        content += "\n\n仅管理员可切换工作态。"

    elements = [{"tag": "markdown", "content": content}, {"tag": "hr"}]
    buttons = []
    for mode, label in labels.items():
        elements.append({"tag": "markdown", "content": f"**{label}**\n{descs[mode]}"})
        if can_manage:
            buttons.append(
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": label,
                    },
                    "type": "primary" if mode == current_mode else "default",
                    "value": {
                        "action": "set_group_mode",
                        "mode": mode,
                    },
                }
            )
    if buttons:
        elements.append({"tag": "action", "layout": "trisection", "actions": buttons})
    elements.append(_back_to_help_action())

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 群聊工作态"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_group_acl_card(
    access_policy: str,
    *,
    allowlist_members: list[str],
    viewer_allowed: bool,
    can_manage: bool,
) -> dict:
    """构造群聊 ACL 状态卡片。"""
    labels = {
        "admin-only": "admin-only",
        "allowlist": "allowlist",
        "all-members": "all-members",
    }
    descs = {
        "admin-only": "只有管理员具备群聊触发资格。",
        "allowlist": "管理员和已授权成员具备群聊触发资格。",
        "all-members": "群内所有成员都具备群聊触发资格。",
    }
    current_label = labels.get(access_policy, access_policy)
    content = (
        f"当前授权策略：**{current_label}**\n"
        f"{descs.get(access_policy, access_policy)}\n\n"
        f"你当前是否可用：**{'是' if viewer_allowed else '否'}**\n\n"
        "说明：ACL 只决定谁有资格；是否需要显式 mention 仍取决于群聊工作态。"
    )
    if allowlist_members:
        shown_members = "\n".join(f"- {member}" for member in allowlist_members[:20])
        if len(allowlist_members) > 20:
            shown_members += f"\n- 其余 {len(allowlist_members) - 20} 人未展开"
        content += f"\n\n**当前 allowlist 成员**\n{shown_members}"
    else:
        content += "\n\n当前 allowlist 为空。"
    if can_manage:
        content += (
            "\n\n管理员可用：`/acl policy admin-only`、`/acl policy allowlist`、"
            "`/acl policy all-members`、`/acl grant @成员`、`/acl revoke @成员`\n"
            "若当前群是 `assistant` / `mention-only`，继续发送这些群命令时仍需先显式 mention 触发对象。"
        )
    else:
        content += "\n\n仅管理员可调整授权策略。"

    elements = [{"tag": "markdown", "content": content}, {"tag": "hr"}]
    buttons = []
    for policy, label in labels.items():
        elements.append({"tag": "markdown", "content": f"**{label}**\n{descs[policy]}"})
        if can_manage:
            buttons.append(
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": f"{'✓ ' if policy == access_policy else ''}{label}",
                    },
                    "type": "primary" if policy == access_policy else "default",
                    "value": {
                        "action": "set_group_acl_policy",
                        "policy": policy,
                    },
                }
            )
    if buttons:
        elements.append({"tag": "action", "layout": "trisection", "actions": buttons})

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 群聊授权"},
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
        "config": _card_config(),
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
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 用户输入 - 已提交"},
            "template": "grey",
        },
        "elements": [{"tag": "markdown", "content": "\n\n".join(lines) or "已提交。"}],
    }


def build_session_row(session: dict, current_thread_id: str) -> list[dict]:
    """构造单个线程行。"""
    thread_id = session["thread_id"]
    current = thread_id == current_thread_id
    title = session.get("title", "（无标题）")

    summary_parts = [f"**{thread_id[:8]}…**", f"`{display_path(session.get('cwd', ''))}`"]
    if session.get("model_provider"):
        summary_parts.append(f"`{session['model_provider']}`")
    summary_parts.append(format_timestamp(session.get("updated_at")))
    line = " | ".join(summary_parts) + f"\n{shorten(title, 120)}"

    return [
        {"tag": "markdown", "content": line},
        {
            "tag": "action",
            "layout": "trisection",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "当前" if current else "恢复",
                    },
                    "type": "primary" if current else "default",
                    "value": {
                        "action": "resume_thread",
                        "thread_id": thread_id,
                        "thread_title": title,
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "归档"},
                    "type": "default",
                    "value": {
                        "action": "archive_thread",
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
    shown_count: int = 0,
    expanded: bool = False,
) -> dict:
    """构造线程列表卡片。"""
    working_dir_display = display_path(working_dir) or working_dir or "."

    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": (
                f"当前目录：`{working_dir_display}`\n"
                "已按当前目录跨 provider 汇总显示线程。\n"
                f"按最近更新时间排序，共 {total_count} 个线程。\n"
                f"想恢复其他目录的线程，或按名字做全局精确查找，请用 `{_SHARED_RESUME_COMMAND.feishu_usage}`。\n"
                f"如需在本地继续同一线程，请用 `{_LOCAL_RESUME_COMMAND}`；"
                f"本地查看线程请用 `{_LOCAL_THREAD_LIST_CWD}`。"
            ),
        },
        {"tag": "hr"},
    ]

    if expanded or total_count <= shown_count:
        display_sessions = sessions
    else:
        display_sessions = sessions[:shown_count]

    for session in display_sessions:
        elements.extend(build_session_row(session, current_thread_id))

    if not sessions:
        elements.append({"tag": "markdown", "content": "*当前目录下暂无可恢复线程。*"})

    bottom_actions: list[dict] = []
    if not expanded and total_count > shown_count and sessions:
        bottom_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "更多"},
                "type": "default",
                "value": {
                    "action": "show_more_sessions",
                },
            }
        )
    bottom_actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "收起"},
            "type": "default",
            "value": {
                "action": "close_sessions_card",
            },
        }
    )
    elements.append({"tag": "action", "actions": bottom_actions})

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 当前目录线程"},
            "template": "blue",
        },
        "elements": elements,
    }


def build_sessions_closed_card() -> dict:
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 当前目录线程（已收起）"},
            "template": "grey",
        },
        "elements": [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "展开会话列表"},
                        "type": "primary",
                        "value": {
                            "action": "reopen_sessions_card",
                        },
                    }
                ],
            },
        ],
    }


def build_sessions_pending_card(thread_id: str, *, title: str) -> dict:
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 当前目录线程"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"正在恢复线程：`{thread_id[:8]}…` {shorten(title or '（无标题）', 120)}\n"
                    "完成后会自动刷新当前会话列表。"
                ),
            }
        ],
    }


def build_rename_card(session: dict) -> dict:
    """构造重命名卡片。"""
    return {
        "config": _card_config(),
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
                        },
                    }
                ],
            },
        ],
    }


def build_history_preview_card(
    thread_id: str,
    rounds: list[tuple[str, str]],
    *,
    summary: str = "",
) -> dict:
    """构造历史预览卡片。"""
    elements: list[dict] = []
    if summary:
        elements.append({"tag": "markdown", "content": summary})
        elements.append({"tag": "hr"})
    for user_text, assistant_text in rounds:
        elements.append({"tag": "markdown", "content": f"👤 **你**\n{shorten(user_text, _HISTORY_TEXT_MAX)}"})
        elements.append({"tag": "markdown", "content": f"🤖 **Codex**\n{shorten(assistant_text, _HISTORY_TEXT_MAX)}"})
        elements.append({"tag": "hr"})

    return {
        "config": _card_config(),
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
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "green",
        },
        "elements": elements,
    }
