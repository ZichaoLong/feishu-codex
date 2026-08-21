"""
FOCUS 飞书卡片构建。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

from bot.adapters.base import ThreadGoalSummary
from bot.card_text_projection import (
    TERMINAL_RESULT_CARD_TITLE,
    render_terminal_result_card_content_for_feishu,
    terminal_result_checksum,
    terminal_result_element_id,
)
from bot.card_limits import MAX_CARD_TABLES, count_card_tables, limit_card_tables
from bot.constants import display_path, format_timestamp, shorten
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_card_markdown import (
    sanitize_runtime_markdown_for_feishu_card,
)
from bot.feishu_command_syntax import feishu_visible_command_syntax
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
_GOAL_OBJECTIVE_MAX = 400
_SHARED_RESUME_COMMAND = get_shared_command("resume")
_SHARED_RESET_BACKEND_COMMAND = get_shared_command("reset-backend")
_LOCAL_THREAD_LIST_CWD = "focusctl thread list --scope cwd"
_LOCAL_RESUME_COMMAND = feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>")
BINDING_OPTIONAL_OVERRIDE_SCOPE_TEXT = (
    "这是当前 Feishu binding 的可选 override。非 `auto` 字段会在 Focus 发起 turn 时"
    "应用到共享 Codex thread；`auto` 不发送对应字段，因此沿用上游当前 thread 状态。"
    "其他前端可以观察或覆盖该上游状态。"
)


def _card_config() -> dict:
    return {"wide_screen_mode": True, "update_multi": True}


def _card_config_v2() -> dict:
    return {"update_multi": True}


def _format_ts_ms(value: int) -> str:
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return "（未知）"
    if timestamp <= 0:
        return "（未知）"
    return format_timestamp(timestamp)


def _format_ts_seconds(value: int) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return "（未知）"
    if timestamp <= 0:
        return "（未知）"
    return format_timestamp(timestamp)


def goal_status_label(status: str) -> str:
    return {
        "active": "进行中",
        "paused": "已暂停",
        "blocked": "已阻塞",
        "usageLimited": "触发 usage 限制",
        "budgetLimited": "触发预算限制",
        "complete": "已完成",
    }.get(str(status or "").strip(), "未知")


def goal_template(status: str, *, has_goal: bool) -> str:
    if not has_goal:
        return "blue"
    normalized = str(status or "").strip()
    if normalized == "active":
        return "turquoise"
    if normalized == "paused":
        return "grey"
    if normalized == "complete":
        return "green"
    if normalized in {"blocked", "usageLimited", "budgetLimited"}:
        return "orange"
    return "blue"


def format_goal_summary_markdown(
    *,
    objective: str,
    status: str,
    token_budget: int | None,
    tokens_used: int,
    time_used_seconds: int,
) -> list[str]:
    normalized_objective = str(objective or "").strip()
    if not normalized_objective:
        return []
    lines = [
        f"当前 goal：`{str(status or '').strip() or 'unknown'}` ({goal_status_label(status)}) {shorten(normalized_objective, 120)}"
    ]
    metrics: list[str] = []
    if token_budget is not None:
        metrics.append(f"预算：`{int(token_budget)}`")
    metrics.append(f"已用 tokens：`{int(tokens_used or 0)}`")
    metrics.append(f"时长：`{int(time_used_seconds or 0)}s`")
    lines.append("goal 摘要：" + "；".join(metrics))
    return lines


def build_markdown_card(title: str, content: str, *, template: str = "blue") -> dict:
    """构造简单说明卡片。"""
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [{"tag": "markdown", "content": sanitize_runtime_markdown_for_feishu_card(content)}],
    }


def build_markdown_action_card(
    title: str,
    content: str,
    *,
    action_rows: list[dict] | None = None,
    template: str = "blue",
) -> dict:
    elements: list[dict] = [
        {"tag": "markdown", "content": sanitize_runtime_markdown_for_feishu_card(content)},
    ]
    if action_rows:
        elements.append({"tag": "hr"})
        elements.extend(action_rows)
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def _iter_card_dict_nodes(node: dict | list | None) -> list[dict]:
    nodes: list[dict] = []
    if isinstance(node, dict):
        nodes.append(node)
        for value in node.values():
            nodes.extend(_iter_card_dict_nodes(value))
    elif isinstance(node, list):
        for item in node:
            nodes.extend(_iter_card_dict_nodes(item))
    return nodes


def _card_has_help_home_button(card: dict, *, page: str) -> bool:
    for node in _iter_card_dict_nodes(card):
        if node.get("tag") != "button":
            continue
        value = node.get("value")
        if not isinstance(value, dict):
            continue
        if str(value.get("action", "")).strip() != "show_help_page":
            continue
        if str(value.get("page", "")).strip() == page:
            return True
    return False


def _card_elements_list(card: dict) -> list[dict] | None:
    elements = card.get("elements")
    if isinstance(elements, list):
        return elements
    body = card.get("body")
    if isinstance(body, dict):
        body_elements = body.get("elements")
        if isinstance(body_elements, list):
            return body_elements
    return None


def _stamp_card_help_origin(card: dict, *, page: str) -> None:
    for node in _iter_card_dict_nodes(card):
        if node.get("tag") != "button":
            continue
        value = node.get("value")
        if isinstance(value, dict) and "help_origin" not in value:
            value["help_origin"] = page


def decorate_help_navigation_card(card: dict, *, page: str = "overview") -> dict:
    normalized_page = str(page or "").strip() or "overview"
    has_help_home_button = _card_has_help_home_button(card, page=normalized_page)
    if has_help_home_button:
        _stamp_card_help_origin(card, page=normalized_page)
        return card
    elements = _card_elements_list(card)
    if elements is None:
        _stamp_card_help_origin(card, page=normalized_page)
        return card
    if elements and (not isinstance(elements[-1], dict) or elements[-1].get("tag") != "hr"):
        elements.append({"tag": "hr"})
    elements.append(_back_to_help_action(page=normalized_page))
    _stamp_card_help_origin(card, page=normalized_page)
    return card


def build_terminal_result_card(
    final_reply_text: str,
    *,
    terminal_result_id: str = "",
    checksum: str = "",
) -> dict:
    """构造终态结果卡。"""
    raw_text = str(final_reply_text or "")
    normalized_result_id = str(terminal_result_id or "").strip().lower()
    normalized_checksum = str(checksum or "").strip().lower() or terminal_result_checksum(raw_text)
    element: dict[str, str] = {
        "tag": "markdown",
        "content": render_terminal_result_card_content_for_feishu(raw_text),
    }
    element_id = terminal_result_element_id(normalized_result_id, normalized_checksum)
    if element_id:
        element["element_id"] = element_id
    return {
        "schema": "2.0",
        "config": _card_config_v2(),
        "header": {
            "title": {"tag": "plain_text", "content": TERMINAL_RESULT_CARD_TITLE},
            "template": "green",
        },
        "body": {
            "elements": [element]
        },
    }


def build_terminal_result_card_message_content(
    final_reply_text: str,
    *,
    terminal_result_id: str = "",
    checksum: str = "",
) -> str:
    """Return the exact Feishu `interactive` message content for a terminal result card."""
    return json.dumps(
        build_terminal_result_card(
            final_reply_text,
            terminal_result_id=terminal_result_id,
            checksum=checksum,
        ),
        ensure_ascii=False,
    )


def build_backend_reset_card(
    *,
    content: str,
    force: bool | None = None,
    extra_action_rows: list[dict] | None = None,
    title: str = "Codex Backend Reset",
    template: str = "blue",
) -> dict:
    """构造 backend reset 预览/结果卡片。"""
    elements: list[dict] = [
        {"tag": "markdown", "content": content},
    ]
    if force is not None:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "强制重置 backend" if force else "重置 backend",
                            },
                            "type": "danger" if force else "primary",
                            "value": {
                                "action": "reset_backend",
                                "force": bool(force),
                            },
                        }
                    ],
                },
            ]
        )
    elif extra_action_rows:
        elements.extend(extra_action_rows)
    else:
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"重新检查请发送 `{_SHARED_RESET_BACKEND_COMMAND.slash_name}`。",
                },
            ]
        )
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def build_goal_card(
    *,
    thread_id: str,
    thread_title: str,
    goal: ThreadGoalSummary | None,
    notice: str = "",
) -> dict:
    has_goal = goal is not None and bool(str(goal.objective or "").strip())
    normalized_thread_id = str(thread_id or "").strip()
    normalized_thread_title = str(thread_title or "").strip()
    elements: list[dict] = []

    lines = [
        (
            f"线程：`{normalized_thread_id[:8]}…` {normalized_thread_title or '（无标题）'}"
            if normalized_thread_id
            else "线程：-"
        )
    ]
    if notice:
        lines.extend(["", notice])
    if has_goal and goal is not None:
        lines.extend(
            [
                "",
                f"目标：{shorten(goal.objective, _GOAL_OBJECTIVE_MAX)}",
                f"状态：`{goal.status}` ({goal_status_label(goal.status)})",
                (
                    f"Token Budget：`{goal.token_budget}`"
                    if goal.token_budget is not None
                    else "Token Budget：`未设置`"
                ),
                f"已用 Tokens：`{goal.tokens_used}`",
                f"已用时长：`{goal.time_used_seconds}s`",
                f"创建时间：`{_format_ts_seconds(goal.created_at)}`",
                f"更新时间：`{_format_ts_seconds(goal.updated_at)}`",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "当前 thread 暂无 goal。",
                f"设置方式：`{feishu_visible_command_syntax('/goal set <objective>')}`",
            ]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": sanitize_runtime_markdown_for_feishu_card("\n".join(lines)),
        }
    )

    actions: list[dict] = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "刷新"},
            "type": "default",
            "value": {"action": "goal_refresh"},
        }
    ]
    if has_goal and goal is not None:
        if goal.status == "active":
            actions.insert(
                0,
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "暂停"},
                    "type": "default",
                    "value": {"action": "goal_pause"},
                },
            )
        elif goal.status == "paused":
            actions.insert(
                0,
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "恢复"},
                    "type": "primary",
                    "value": {"action": "goal_resume"},
                },
            )
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "清除"},
                "type": "danger",
                "value": {"action": "goal_clear"},
            }
        )
    elements.extend(
        [
            {"tag": "hr"},
            {"tag": "action", "actions": actions},
        ]
    )

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex Goal"},
            "template": goal_template(goal.status if goal is not None else "", has_goal=has_goal),
        },
        "elements": elements,
    }


def build_goal_detached_confirm_card(
    *,
    thread_id: str,
    thread_title: str,
    objective: str = "",
    status: str = "",
) -> dict:
    normalized_thread_id = str(thread_id or "").strip()
    if not normalized_thread_id:
        raise ValueError("detached goal 确认卡缺少 thread_id。")
    normalized_thread_title = str(thread_title or "").strip()
    normalized_objective = str(objective or "").strip()
    normalized_status = str(status or "").strip()
    action_label = "恢复当前 thread goal" if normalized_status == "active" and not normalized_objective else "更新当前 thread goal"
    lines = [
        (
            f"线程：`{normalized_thread_id[:8]}…` {normalized_thread_title or '（无标题）'}"
            if normalized_thread_id
            else "线程：-"
        ),
        "",
        "当前会话处于 `detached`。",
        f"{action_label} 可能让已 loaded 的 backend 在后台继续执行，但当前会话不会自动恢复飞书推送。",
    ]
    if normalized_objective:
        lines.append(f"目标：{shorten(normalized_objective, _GOAL_OBJECTIVE_MAX)}")
    if normalized_status:
        lines.append(f"状态：`{normalized_status}` ({goal_status_label(normalized_status)})")
    lines.extend(
        [
            "",
            "请选择：",
            "- 恢复推送并继续：先把当前会话 attach 回来，再应用这次 goal 变更",
            "- 保持 detached：只应用 goal 变更，不恢复当前会话推送",
        ]
    )
    return build_markdown_action_card(
        "Codex Goal",
        "\n".join(lines),
        template="orange",
        action_rows=[
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "恢复推送并继续"},
                        "type": "primary",
                        "value": {
                            "action": "goal_apply_confirm",
                            "thread_id": normalized_thread_id,
                            "attach_binding": "true",
                            "objective": normalized_objective,
                            "status": normalized_status,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "保持 detached"},
                        "type": "default",
                        "value": {
                            "action": "goal_apply_confirm",
                            "thread_id": normalized_thread_id,
                            "attach_binding": "",
                            "objective": normalized_objective,
                            "status": normalized_status,
                        },
                    },
                ],
            }
        ],
    )


def build_resume_active_goal_confirm_card(
    *,
    thread_id: str,
    thread_title: str,
    origin: str,
) -> dict:
    normalized_thread_id = str(thread_id or "").strip()
    normalized_thread_title = str(thread_title or "").strip()
    normalized_origin = str(origin or "").strip() or "command"
    lines = [
        (
            f"线程：`{normalized_thread_id[:8]}…` {normalized_thread_title or '（无标题）'}"
            if normalized_thread_id
            else "线程：-"
        ),
        "",
        "当前目标 thread 尚未加载到本实例 backend。",
        "该 thread 的 persisted goal 当前是 `active`。",
        "如果直接恢复，app-server 可能会立刻继续这条 goal；第一轮自动续跑只保证沿用 backend 当时已生效的 loaded-thread 设置。",
        "",
        "请选择：",
        "- 按当前设置恢复并保持 paused：先暂停 persisted goal，再 cold resume、同步当前会话设置，但不自动恢复 goal",
        "- 直接恢复：按当前 backend 状态直接恢复；active goal 可能立刻续跑，当前会话设置只会影响后续已同步完成的 turn",
    ]
    return build_markdown_action_card(
        "Codex 恢复线程确认",
        "\n".join(lines),
        template="orange",
        action_rows=[
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "按当前设置恢复并保持 paused"},
                        "type": "primary",
                        "value": {
                            "action": "resume_thread_confirm",
                            "thread_id": normalized_thread_id,
                            "thread_title": normalized_thread_title,
                            "pause_active_goal_on_resume": "true",
                            "origin": normalized_origin,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "直接恢复"},
                        "type": "default",
                        "value": {
                            "action": "resume_thread_confirm",
                            "thread_id": normalized_thread_id,
                            "thread_title": normalized_thread_title,
                            "pause_active_goal_on_resume": "",
                            "origin": normalized_origin,
                        },
                    },
                ],
            }
        ],
    )


def _back_to_help_action(*, page: str = "overview") -> dict:
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "返回帮助"},
                "type": "default",
                "value": {
                    "action": "show_help_page",
                    "page": page,
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
    cancelable: bool = True,
) -> dict:
    """构造主执行卡片。"""
    if running:
        template = "turquoise"
        header_content = (
            f"Codex 执行过程（执行中 {elapsed}s）"
            if elapsed > 0
            else "Codex 执行过程（执行中）"
        )
    elif cancelled:
        template = "grey"
        header_content = "Codex 执行过程（已停止）"
    else:
        template = "blue"
        header_content = "Codex 执行过程"

    panel_icon = {
        "tag": "standard_icon",
        "token": "right-small-ccm_outlined",
        "size": "16px 16px",
    }

    def _panel(title: str, content: str, expanded: bool) -> dict:
        safe_content = sanitize_runtime_markdown_for_feishu_card(content)
        return {
            "tag": "collapsible_panel",
            "expanded": expanded,
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "icon": panel_icon,
                "icon_position": "left",
                "icon_expanded_angle": 90,
            },
            "elements": [{"tag": "markdown", "content": safe_content or ""}],
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
        remaining = MAX_CARD_TABLES
        for segment in segments:
            if segment.kind == "divider":
                if panel_elements:
                    panel_elements.append({"tag": "hr"})
                continue
            text = segment.text
            if remaining > 0:
                text = limit_card_tables(text, remaining)
                remaining -= count_card_tables(text)
            panel_elements.append(
                {
                    "tag": "markdown",
                    "content": sanitize_runtime_markdown_for_feishu_card(text),
                }
            )
        return panel_elements

    elements: list[dict] = []
    reply_panel_elements = _reply_panel_elements(reply_segments)
    if log_text and reply_panel_elements:
        log_tables = count_card_tables(log_text)
        if log_tables > MAX_CARD_TABLES:
            log_text = limit_card_tables(log_text, MAX_CARD_TABLES)
        elements.append(_panel("执行过程", log_text, expanded=False))
        elements.append(_panel_with_elements("回复", reply_panel_elements, expanded=running))
    elif reply_panel_elements:
        elements.append(_panel_with_elements("回复", reply_panel_elements, expanded=running))
    elif log_text:
        elements.append(_panel("执行过程", limit_card_tables(log_text), expanded=False))
    else:
        elements.append(
            {
                "tag": "markdown",
                "content": "*暂无输出*" if running else "无",
            }
        )

    if running and cancelable:
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


def build_model_effort_card(
    *,
    current_model: str,
    current_reasoning_effort: str,
    content: str,
    supported_reasoning_efforts: Sequence[tuple[str, str]] | None = None,
    reasoning_effort_conflict: str = "",
    running: bool = False,
) -> dict:
    """构造 model / effort 联合运行时设置卡片。"""
    current_model_value = str(current_model or "").strip()
    current_effort_value = str(current_reasoning_effort or "").strip()
    elements = [
        {
            "tag": "markdown",
            "content": content,
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": (
                "**model override**\n"
                "输入任意非空 model 名称即可；如需让 Focus 不发送 model 字段，请点 `auto`。"
            ),
        },
        {
            "tag": "form",
            "name": "model_override_form",
            "elements": [
                {
                    "tag": "input",
                    "name": "model_override",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "输入 model 名称，例如 gpt-5.5 或 glm-4.5…",
                    },
                    "default_value": current_model_value,
                },
                {
                    "tag": "button",
                    "name": "submit_model_override",
                    "text": {"tag": "plain_text", "content": "保存 model"},
                    "type": "primary",
                    "form_action_type": "submit",
                    "value": {
                        "action": "submit_model_override",
                    },
                },
            ],
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "auto",
                    },
                    "type": "primary" if not current_model_value else "default",
                    "value": {
                        "action": "set_model",
                        "model": "",
                    },
                }
            ],
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": (
                "**effort override**\n"
                "`auto` 表示 Focus 不发送 effort 字段；它不会主动恢复 backend 或 model 默认值。"
            ),
        },
    ]
    effort_actions = [("auto", "")]
    seen_efforts: set[str] = {"auto"}
    if supported_reasoning_efforts is not None:
        for effort, _description in supported_reasoning_efforts:
            normalized_effort = str(effort or "").strip()
            if not normalized_effort or normalized_effort in seen_efforts:
                continue
            seen_efforts.add(normalized_effort)
            effort_actions.append((normalized_effort, normalized_effort))
    if reasoning_effort_conflict:
        elements.append(
            {
                "tag": "markdown",
                "content": f"**当前组合冲突**\n{reasoning_effort_conflict}",
            }
        )
    if "ultra" in seen_efforts:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    "提示：当前 model metadata 提供 `ultra`。Focus 会原样发送，"
                    "Codex 将走原生 Ultra 路径，并可启用其 proactive multi-agent 行为。"
                ),
            }
        )
    buttons = []
    for label, value in effort_actions:
        selected = (not current_effort_value and not value) or current_effort_value == value
        buttons.append(
            {
                "tag": "button",
                "text": {
                    "tag": "plain_text",
                    "content": label,
                },
                "type": "primary" if selected else "default",
                "value": {
                    "action": "set_reasoning_effort",
                    "reasoning_effort": value,
                },
            }
        )
    for index in range(0, len(buttons), 3):
        row_actions = buttons[index:index + 3]
        row = {"tag": "action", "actions": row_actions}
        if len(row_actions) == 3:
            row["layout"] = "trisection"
        elements.append(row)
    if supported_reasoning_efforts is None:
        elements.extend(
            [
                {
                    "tag": "markdown",
                    "content": "当前 model 为 auto 或没有可用的 effort metadata；可输入任意非空值，由 app-server 决定。",
                },
                {
                    "tag": "form",
                    "name": "reasoning_effort_override_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "reasoning_effort_override",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "输入 effort，例如 ultra 或上游自定义值…",
                            },
                            "default_value": current_effort_value,
                        },
                        {
                            "tag": "button",
                            "name": "submit_reasoning_effort_override",
                            "text": {"tag": "plain_text", "content": "保存 effort"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "value": {
                                "action": "submit_reasoning_effort_override",
                            },
                        },
                    ],
                },
            ]
        )
    if running:
        elements.append(
            {
                "tag": "markdown",
                "content": "当前若有执行中的 turn，切换仅对下一轮生效。",
            }
        )
    elements.append(_back_to_help_action())
    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 模型 / Effort"},
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


def build_group_activation_card(
    *,
    activated: bool,
    activated_by: str = "",
    activated_at: int = 0,
    can_manage: bool,
) -> dict:
    """构造 owner-activated 群聊状态卡片。"""
    state_label = "已激活" if activated else "未激活"
    template = "green" if activated else "yellow"
    lines = [f"当前群聊状态：**{state_label}**"]
    if activated:
        if activated_by:
            lines.append(f"激活管理员：`{activated_by}`")
        if activated_at > 0:
            lines.append(f"激活时间：`{_format_ts_ms(activated_at)}`")
        lines.extend(
            [
                "",
                "当前群成员可直接在这里日常对话，并处理自己发起 turn 的审批或补充输入。",
                "所有会改变共享状态的命令与设置，仍然只允许管理员操作。",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "当前群聊还没有被管理员初始化。",
                "非管理员暂时不能使用机器人；请让管理员执行 `/group activate`。",
            ]
        )
    if can_manage:
        lines.extend(
            [
                "",
                "管理员可用：`/group activate`、`/group deactivate`。",
            ]
        )
    else:
        lines.extend(["", "仅管理员可激活或停用当前群聊。"])

    elements = [{"tag": "markdown", "content": "\n".join(lines)}, {"tag": "hr"}]
    if can_manage:
        elements.append(
            {
                "tag": "action",
                "layout": "bisected",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "激活当前群"},
                        "type": "primary" if activated else "default",
                        "value": {
                            "action": "set_group_activation",
                            "activated": True,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "停用当前群"},
                        "type": "danger" if activated else "default",
                        "value": {
                            "action": "set_group_activation",
                            "activated": False,
                        },
                    },
                ],
            }
        )

    return {
        "config": _card_config(),
        "header": {
            "title": {"tag": "plain_text", "content": "Codex 群聊授权"},
            "template": template,
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


def build_thread_row(thread: dict, current_thread_id: str) -> list[dict]:
    """构造单个线程行。"""
    thread_id = thread["thread_id"]
    current = thread_id == current_thread_id
    title = thread.get("title", "（无标题）")

    summary_parts = [f"**{thread_id[:8]}…**", f"`{display_path(thread.get('cwd', ''))}`"]
    if thread.get("model_provider"):
        summary_parts.append(f"`{thread['model_provider']}`")
    summary_parts.append(format_timestamp(thread.get("updated_at")))
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


def build_threads_card(
    threads: list[dict],
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
        display_threads = threads
    else:
        display_threads = threads[:shown_count]

    for thread in display_threads:
        elements.extend(build_thread_row(thread, current_thread_id))

    if not threads:
        elements.append({"tag": "markdown", "content": "*当前目录下暂无可恢复线程。*"})

    bottom_actions: list[dict] = []
    if not expanded and total_count > shown_count and threads:
        bottom_actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "更多"},
                "type": "default",
                "value": {
                    "action": "show_more_threads",
                },
            }
        )
    bottom_actions.append(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "收起"},
            "type": "default",
            "value": {
                "action": "close_threads_card",
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


def build_threads_closed_card() -> dict:
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
                        "text": {"tag": "plain_text", "content": "展开线程列表"},
                        "type": "primary",
                        "value": {
                            "action": "reopen_threads_card",
                        },
                    }
                ],
            },
        ],
    }


def build_threads_pending_card(thread_id: str, *, title: str) -> dict:
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
                    "完成后会自动刷新当前线程列表。"
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
                "content": sanitize_runtime_markdown_for_feishu_card(
                    f"**说明**\n{shorten(explanation, _PLAN_CONTENT_MAX)}"
                ),
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
                    "content": sanitize_runtime_markdown_for_feishu_card("**计划步骤**\n" + "\n".join(lines)),
                }
            )
            elements.append({"tag": "hr"})

    if plan_text:
        elements.append(
            {
                "tag": "markdown",
                "content": sanitize_runtime_markdown_for_feishu_card(
                    f"**计划正文**\n{shorten(plan_text, _PLAN_CONTENT_MAX)}"
                ),
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
