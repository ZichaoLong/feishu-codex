"""
Codex help domain.
"""

from __future__ import annotations

from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import CommandResult, make_card_response
from bot.shared_command_surface import get_shared_command


_SHARED_PROFILE_COMMAND = get_shared_command("profile")
_SHARED_SESSION_COMMAND = get_shared_command("session")
_SHARED_RESUME_COMMAND = get_shared_command("resume")


class CodexHelpDomain:
    def __init__(
        self,
        *,
        plugin_keyword: str,
        local_thread_safety_rule: str,
    ) -> None:
        self._plugin_keyword = plugin_keyword
        self._local_thread_safety_rule = local_thread_safety_rule

    @staticmethod
    def _normalize_help_topic(topic: str) -> str:
        normalized = (topic or "").strip().lower()
        if normalized in {"", "basic", "basics", "overview"}:
            return "overview"
        if normalized in {"session", "sessions", "resume", "thread", "threads"}:
            return "session"
        if normalized in {
            "settings",
            "permission",
            "permissions",
            "approval",
            "sandbox",
            "mode",
            "advanced",
        }:
            return "settings"
        if normalized in {"group", "groups", "acl"}:
            return "group"
        if normalized in {"local", "fcodex", "wrapper"}:
            return "local-redirect"
        return ""

    def _page_card(self, title: str, *elements: dict[str, Any], template: str = "blue") -> dict:
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": list(elements),
        }

    def _page_button(self, label: str, page: str, *, button_type: str = "default") -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "value": {
                "action": "show_help_page",
                "plugin": self._plugin_keyword,
                "page": page,
            },
        }

    def _command_button(
        self,
        label: str,
        command: str,
        *,
        title: str = "",
        button_type: str = "default",
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "action": "help_execute_command",
            "plugin": self._plugin_keyword,
            "command": command,
        }
        if title:
            value["title"] = title
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": button_type,
            "value": value,
        }

    @staticmethod
    def _action_row(actions: list[dict[str, Any]], *, layout: str = "") -> dict[str, Any]:
        row: dict[str, Any] = {"tag": "action", "actions": actions}
        if layout:
            row["layout"] = layout
        return row

    def _command_form(
        self,
        *,
        title: str,
        intro: str,
        form_name: str,
        field_name: str,
        placeholder: str,
        submit_label: str,
        submit_command: str,
        submit_title: str,
        back_page: str,
        default_value: str = "",
        required_text: str,
    ) -> dict:
        return self._page_card(
            title,
            {"tag": "markdown", "content": intro},
            {"tag": "hr"},
            {
                "tag": "form",
                "name": form_name,
                "elements": [
                    {
                        "tag": "input",
                        "name": field_name,
                        "placeholder": {
                            "tag": "plain_text",
                            "content": placeholder,
                        },
                        "default_value": default_value,
                    },
                    {
                        "tag": "button",
                        "name": "submit",
                        "text": {"tag": "plain_text", "content": submit_label},
                        "type": "primary",
                        "form_action_type": "submit",
                        "value": {
                            "action": "help_submit_command",
                            "plugin": self._plugin_keyword,
                            "command": submit_command,
                            "field_name": field_name,
                            "title": submit_title,
                            "required_text": required_text,
                        },
                    },
                ],
            },
            self._action_row(
                [self._page_button("返回上一页", back_page)],
            ),
        )

    def _build_help_page(self, page: str) -> dict | None:
        normalized = str(page or "").strip().lower()
        if normalized in {"", "overview"}:
            return self._page_card(
                "Codex 帮助",
                {
                    "tag": "markdown",
                    "content": (
                        "从下面三个入口按场景进入，不需要先记住命令名。\n\n"
                        "- `session`：线程、恢复、当前线程状态、目录切换\n"
                        "- `settings`：profile、权限预设、审批、沙箱、协作模式、身份初始化\n"
                        "- `group`：群聊工作态、ACL 使用规则、群内触发约束\n\n"
                        f"{self._local_thread_safety_rule}\n\n"
                        "本地 `fcodex` wrapper 用法不放在飞书 `/help`；请在终端执行 `fcodex /help`。"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._page_button("session", "session"),
                        self._page_button("settings", "settings"),
                        self._page_button("group", "group"),
                    ],
                    layout="trisection",
                ),
            )
        if normalized == "session":
            return self._page_card(
                "Codex 帮助：线程",
                {
                    "tag": "markdown",
                    "content": (
                        "**线程与目录**\n"
                        f"- `{_SHARED_SESSION_COMMAND.feishu_usage}`：浏览当前目录线程\n"
                        "- `/new`：立即新建线程\n"
                        f"- `{_SHARED_RESUME_COMMAND.feishu_usage}`：全局精确恢复线程，可填 `thread_id` 或 `thread_name`\n"
                        "- `/cd <path>`：切换目录并清空当前线程绑定\n"
                        "- “当前线程”页：查看 `/status`、释放 runtime、重命名、归档当前绑定线程\n\n"
                        "**本地继续**\n"
                        "- 需要在本地继续同一 live thread 时，使用 `fcodex`\n"
                        "- 本地 wrapper 命令请在终端执行 `fcodex /help`\n\n"
                        f"{self._local_thread_safety_rule}"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._page_button("当前线程", "session-current"),
                        self._command_button("/session", "/session", title="Codex Session"),
                        self._command_button("/new", "/new", title="Codex 新建线程", button_type="primary"),
                    ],
                    layout="trisection",
                ),
                self._action_row(
                    [
                        self._page_button("恢复线程", "session-resume-form"),
                        self._page_button("切换目录", "session-cd-form"),
                        self._page_button("返回帮助", "overview"),
                    ],
                    layout="trisection",
                ),
            )
        if normalized == "session-current":
            return self._page_card(
                "Codex 帮助：当前线程",
                {
                    "tag": "markdown",
                    "content": (
                        "这些操作都以**当前绑定线程**为目标。\n\n"
                        "- `/status`：查看当前 binding、feishu runtime、backend thread status、profile 相关信息\n"
                        "- `/release-feishu-runtime`：释放 Feishu 对当前线程的 runtime 持有，但不解绑 thread\n"
                        "- `/rename <title>`：重命名当前线程\n"
                        "- `/rm`：归档当前线程\n\n"
                        "如果当前没有绑定线程，相关命令会按 slash 语义返回明确提示。"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._command_button("/status", "/status", title="Codex 当前状态"),
                        self._command_button(
                            "释放 runtime",
                            "/release-feishu-runtime",
                            title="Codex 释放 Feishu Runtime",
                        ),
                        self._page_button("重命名", "session-rename-current-form"),
                    ],
                    layout="trisection",
                ),
                self._action_row(
                    [
                        self._command_button("/rm", "/rm", title="Codex 归档线程"),
                        self._page_button("返回 Session", "session"),
                    ],
                ),
            )
        if normalized == "session-resume-form":
            return self._command_form(
                title="Codex 帮助：恢复线程",
                intro=(
                    f"填写 `{_SHARED_RESUME_COMMAND.feishu_usage}` 里的目标。\n\n"
                    "- 支持精确 `thread_id`\n"
                    "- 也支持全局精确 `thread_name`\n"
                    "- 如果同名命中多个线程，会按 slash 语义报错，不会替你猜"
                ),
                form_name="help_resume_form",
                field_name="resume_target",
                placeholder="输入 thread_id 或 thread_name",
                submit_label="恢复线程",
                submit_command="/resume",
                submit_title="Codex 恢复线程",
                back_page="session",
                required_text="请输入 thread_id 或 thread_name。",
            )
        if normalized == "session-cd-form":
            return self._command_form(
                title="Codex 帮助：切换目录",
                intro=(
                    "填写目标目录并提交，相当于执行 `/cd <path>`。\n\n"
                    "- 成功后会清空当前线程绑定\n"
                    "- 之后直接发送普通文本，会在新目录自动新建线程"
                ),
                form_name="help_cd_form",
                field_name="cd_path",
                placeholder="输入目标目录路径",
                submit_label="切换目录",
                submit_command="/cd",
                submit_title="Codex 目录切换结果",
                back_page="session",
                required_text="请输入目标目录路径。",
            )
        if normalized == "session-rename-current-form":
            return self._command_form(
                title="Codex 帮助：重命名当前线程",
                intro=(
                    "填写新标题并提交，相当于执行 `/rename <title>`。\n\n"
                    "该操作只针对当前绑定线程。"
                ),
                form_name="help_rename_current_form",
                field_name="rename_title",
                placeholder="输入新标题",
                submit_label="确认重命名",
                submit_command="/rename",
                submit_title="Codex 重命名结果",
                back_page="session-current",
                required_text="请输入新标题。",
            )
        if normalized == "settings":
            return self._page_card(
                "Codex 帮助：设置",
                {
                    "tag": "markdown",
                    "content": (
                        "**默认 profile 与当前会话设置**\n"
                        f"- `{_SHARED_PROFILE_COMMAND.feishu_usage}`：查看或切换默认 profile\n"
                        "- 推荐先用 `/permissions`；它会同时设置审批策略与沙箱\n"
                        "- `/approval`、`/sandbox`：单独调整审批或沙箱\n"
                        "- `/mode`：切换当前飞书会话后续 turn 的协作模式\n"
                        "- 如果当前正在执行，新设置从下一轮生效。\n\n"
                        "**身份与初始化**\n"
                        "- `/whoami`、`/whoareyou`、`/init <token>` 在单独子页里"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._command_button("/profile", "/profile", title="Codex 默认 Profile"),
                        self._command_button("/permissions", "/permissions", title="Codex 权限预设"),
                        self._command_button("/approval", "/approval", title="Codex 审批策略"),
                    ],
                    layout="trisection",
                ),
                self._action_row(
                    [
                        self._command_button("/sandbox", "/sandbox", title="Codex 沙箱策略"),
                        self._command_button("/mode", "/mode", title="Codex 协作模式"),
                        self._page_button("身份与初始化", "settings-identity"),
                    ],
                    layout="trisection",
                ),
                self._action_row([self._page_button("返回帮助", "overview")]),
            )
        if normalized == "settings-identity":
            return self._page_card(
                "Codex 帮助：身份与初始化",
                {
                    "tag": "markdown",
                    "content": (
                        "- `/whoami`：私聊查看自己的 `open_id` 等身份信息\n"
                        "- `/whoareyou`：查看机器人的 `app_id`、配置的 `bot_open_id`、实时探测结果\n"
                        "- `/init <token>`：私聊初始化管理员与 `bot_open_id`\n\n"
                        "注意：`/whoami` 与 `/init` 只支持私聊；如果在群里触发，会按 slash 语义拒绝。"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._command_button("/whoami", "/whoami", title="Codex 身份信息"),
                        self._command_button("/whoareyou", "/whoareyou", title="Codex 机器人身份"),
                        self._page_button("初始化", "settings-init-form"),
                    ],
                    layout="trisection",
                ),
                self._action_row([self._page_button("返回 Settings", "settings")]),
            )
        if normalized == "settings-init-form":
            return self._command_form(
                title="Codex 帮助：初始化",
                intro=(
                    "填写初始化 token 并提交，相当于执行 `/init <token>`。\n\n"
                    "- 仅支持私聊\n"
                    "- 会把当前发送者加入 `admin_open_ids`\n"
                    "- 会尽量自动写入 `bot_open_id`"
                ),
                form_name="help_init_form",
                field_name="init_token",
                placeholder="输入 init token",
                submit_label="执行初始化",
                submit_command="/init",
                submit_title="Codex 初始化结果",
                back_page="settings-identity",
                required_text="请输入 init token。",
            )
        if normalized == "group":
            return self._page_card(
                "Codex 帮助：群聊",
                {
                    "tag": "markdown",
                    "content": (
                        "**群聊工作态**\n"
                        "- `/groupmode`：查看或切换当前群聊工作态\n"
                        "- 这是群聊专属能力；在私聊中触发会按 slash 语义拒绝\n\n"
                        "**ACL 说明**\n"
                        "- `/acl`：查看当前群授权\n"
                        "- `/acl policy <admin-only|allowlist|all-members>`\n"
                        "- `/acl grant @成员`\n"
                        "- `/acl revoke @成员`\n\n"
                        "`/acl` 当前只提供文字导航，不做表单按钮化；因为 `grant` / `revoke` 常需要 mention，直接用 slash 更清楚。"
                    ),
                },
                {"tag": "hr"},
                self._action_row(
                    [
                        self._command_button("/groupmode", "/groupmode", title="Codex 群聊工作态"),
                        self._page_button("返回帮助", "overview"),
                    ],
                ),
            )
        return None

    def handle_show_help_page_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del message_id
        card = self._build_help_page(str(action_value.get("page", "")))
        if card is None:
            return make_card_response(toast="未知帮助页面。", toast_type="warning")
        return make_card_response(card=card)

    def handle_show_help_topic_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del message_id
        topic = self._normalize_help_topic(str(action_value.get("topic", "")))
        if topic == "local-redirect":
            return make_card_response(
                card=self._page_card(
                    "Codex 帮助：本地命令",
                    {
                        "tag": "markdown",
                        "content": "本地 `fcodex` wrapper 命令不再放在飞书 `/help`。请在终端执行 `fcodex /help`。",
                    },
                    {"tag": "hr"},
                    self._action_row([self._page_button("返回帮助", "overview")]),
                )
            )
        card = self._build_help_page(topic)
        if card is None:
            return make_card_response(toast="未知帮助主题。", toast_type="warning")
        return make_card_response(card=card)

    def handle_show_help_overview_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del message_id
        del action_value
        return make_card_response(card=self._build_help_page("overview"))

    def reply_help(self, chat_id: str, topic: str = "", *, message_id: str = "") -> CommandResult:
        del chat_id
        del message_id
        normalized = self._normalize_help_topic(topic)
        if normalized == "local-redirect":
            return CommandResult(
                text="本地 `fcodex` wrapper 用法请在终端执行 `fcodex /help`；飞书 `/help` 仅覆盖 `session`、`settings`、`group`。"
            )
        card = self._build_help_page(normalized)
        if card is not None:
            return CommandResult(card=card)
        return CommandResult(text="帮助主题仅支持：`session`、`settings`、`group`。\n发送 `/help` 查看导航入口。")
