"""Feishu presentation for approval and permissions safety baselines.

This module owns only stateless card dictionaries and their binding-scope
description. Runtime setting authority remains in ``CodexSettingsDomain``.
"""

from __future__ import annotations

from bot.permissions_profile import (
    PERMISSION_PROFILE_CHOICES,
    permissions_profile_choice_key,
    permissions_profile_label,
)

BINDING_SAFETY_BASELINE_SCOPE_TEXT = (
    "这是当前 Feishu binding 的安全基线。Focus 发起每个 turn 时都会显式应用到"
    "共享 Codex thread；其他前端可以覆盖上游状态，但下一次 Feishu turn 会重新应用本 binding 的值。"
)


def _card_config() -> dict:
    return {"wide_screen_mode": True, "update_multi": True}


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


def build_approval_policy_card(current_policy: str, *, running: bool = False) -> dict:
    """构造原生审批策略选择卡片。"""
    labels = {
        "untrusted": "untrusted",
        "on-request": "on-request",
        "never": "never",
    }
    descs = {
        "untrusted": "偏保守，更多操作会先停下来等你确认。",
        "on-request": "仅在模型明确请求时，才停下来等你确认。",
        "never": "不请求审批，直接执行。",
    }

    current_label = labels.get(current_policy, current_policy or "（未设置）")
    current_desc = (
        "它只决定什么时候停下来等你确认，不改变文件或网络边界。\n"
        "多数情况下，优先使用 `/permissions`。\n"
        f"{BINDING_SAFETY_BASELINE_SCOPE_TEXT}"
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


def build_permissions_profile_card(
    current_permissions_profile_id: str,
    *,
    running: bool = False,
) -> dict:
    """构造权限基线选择卡片。"""
    current_choice = permissions_profile_choice_key(current_permissions_profile_id)
    current_label = permissions_profile_label(current_permissions_profile_id)
    current_desc = (
        "它只决定执行边界，不决定是否停下来审批。\n"
        "审批策略请单独使用 `/approval`。\n"
        f"{BINDING_SAFETY_BASELINE_SCOPE_TEXT}\n\n"
        f"Profile ID：`{current_permissions_profile_id or '（空）'}`"
    )
    if running:
        current_desc += "\n\n当前若有执行中的 turn，切换仅对下一轮生效。"

    buttons = []
    elements = [
        {
            "tag": "markdown",
            "content": f"当前权限基线：**{current_label}**\n{current_desc}",
        },
        {"tag": "hr"},
    ]
    for key, config in PERMISSION_PROFILE_CHOICES.items():
        elements.append(
            {
                "tag": "markdown",
                "content": f"**{config['label']}**\n{config['description']}",
            }
        )
        buttons.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": f"{'✓ ' if key == current_choice else ''}{config['label']}",
                },
                "type": "primary" if key == current_choice else "default",
                "value": {
                    "action": "set_permissions_profile",
                    "profile": key,
                },
            }
        )
    elements.append({"tag": "action", "layout": "trisection", "actions": buttons})
    elements.append(_back_to_help_action())

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 权限基线"},
            "template": "blue",
        },
        "elements": elements,
    }
