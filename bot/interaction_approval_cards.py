"""Feishu presentation for canonical Codex interaction approvals.

This module owns only stateless card dictionaries. Response authority and
external effects remain in the interaction request controller and publisher.
"""

from __future__ import annotations

from collections.abc import Sequence

from bot.constants import display_path


def _card_config() -> dict:
    return {"wide_screen_mode": True, "update_multi": True}


def build_command_approval_card(
    request_id: str,
    *,
    command: str,
    cwd: str = "",
    reason: str = "",
    actions: list[dict],
    context_lines: Sequence[str] = (),
) -> dict:
    """构造命令审批卡片。"""
    cwd_display = display_path(cwd) if cwd else "-"
    content = [f"**工作目录**: `{cwd_display}`", "**命令**:", f"```bash\n{command or '(空命令)'}\n```"]
    if reason:
        content.append(f"**原因**: {reason}")
    content.extend(str(line) for line in context_lines if str(line).strip())

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
                "actions": _interaction_approval_buttons(request_id, actions),
            },
        ],
    }


def build_file_change_approval_card(
    request_id: str,
    *,
    grant_root: str = "",
    reason: str = "",
    actions: list[dict],
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
                "actions": _interaction_approval_buttons(request_id, actions),
            },
        ],
    }


def build_permissions_approval_card(
    request_id: str,
    *,
    permissions: dict,
    reason: str = "",
    actions: list[dict],
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
                "actions": _interaction_approval_buttons(request_id, actions),
            },
        ],
    }


def _interaction_approval_buttons(request_id: str, actions: list[dict]) -> list[dict]:
    buttons: list[dict] = []
    for action in actions:
        action_id = str(action.get("id", "") or "").strip()
        label = str(action.get("label", "") or "").strip()
        if not action_id or not label:
            continue
        buttons.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label[:80]},
                "type": "primary" if action.get("style") == "primary" else (
                    "danger" if action.get("style") == "danger" else "default"
                ),
                "value": {
                    "action": "interaction_approval",
                    "request_id": request_id,
                    "response_action": action_id,
                },
            }
        )
    return buttons


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
