"""
Codex help domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import CommandResult, make_card_response
from bot.shared_command_surface import get_shared_command


_SHARED_PROFILE_COMMAND = get_shared_command("profile")
_SHARED_RESET_BACKEND_COMMAND = get_shared_command("reset-backend")
_SHARED_PREFLIGHT_COMMAND = get_shared_command("preflight")
_SHARED_ARCHIVE_COMMAND = get_shared_command("archive")
_SHARED_THREADS_COMMAND = get_shared_command("threads")
_SHARED_RESUME_COMMAND = get_shared_command("resume")

_LOCAL_THREAD_LIST_CWD = "feishu-codexctl thread list --scope cwd"
_LOCAL_THREAD_LIST_GLOBAL = "feishu-codexctl thread list --scope global"
_LOCAL_RESUME_COMMAND = "fcodex resume <thread_id|thread_name>"
_LOCAL_THREAD_UNSUBSCRIBE = "feishu-codexctl thread unsubscribe --thread-id <thread_id>"


@dataclass(frozen=True)
class _HelpPageButtonSpec:
    label: str
    page: str
    button_type: str = "default"


@dataclass(frozen=True)
class _HelpCommandButtonSpec:
    label: str
    command: str
    title: str = ""
    button_type: str = "default"


@dataclass(frozen=True)
class _HelpActionRowSpec:
    buttons: tuple[_HelpPageButtonSpec | _HelpCommandButtonSpec, ...]
    layout: str = ""


@dataclass(frozen=True)
class _HelpFormSpec:
    form_name: str
    field_name: str
    placeholder: str
    submit_label: str
    submit_command: str
    submit_title: str
    required_text: str
    default_value: str = ""


@dataclass(frozen=True)
class _HelpPageSpec:
    title: str
    markdown: str
    action_rows: tuple[_HelpActionRowSpec, ...] = ()
    form: _HelpFormSpec | None = None


class CodexHelpDomain:
    def __init__(
        self,
        *,
        local_thread_safety_rule: str,
    ) -> None:
        self._local_thread_safety_rule = local_thread_safety_rule
        self._page_specs = self._build_page_specs()
        self._page_aliases = self._build_page_aliases()

    def _build_page_specs(self) -> dict[str, _HelpPageSpec]:
        return {
            "overview": _HelpPageSpec(
                title="Codex 帮助",
                markdown=(
                    "从下面五个入口按作用对象进入，不需要先记住命令名。\n\n"
                    "- `当前会话`：当前 chat 的状态、预检、目录切换\n"
                    "- `群聊`：当前群的激活、工作态、管理员边界\n"
                    "- `线程`：新建、浏览、恢复、当前 thread 管理\n"
                    "- `运行时`：当前会话设置，以及当前实例 backend reset\n"
                    "- `身份`：`/whoami`、`/bot-status`、`/init <token>`\n\n"
                    f"{self._local_thread_safety_rule}\n\n"
                    "本地继续同一线程请用 "
                    f"`{_LOCAL_RESUME_COMMAND}`；"
                    "本地查看/管理请用 "
                    f"`{_LOCAL_THREAD_LIST_CWD}`。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="当前会话", page="chat"),
                            _HelpPageButtonSpec(label="群聊", page="group"),
                            _HelpPageButtonSpec(label="线程", page="thread"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="运行时", page="runtime"),
                            _HelpPageButtonSpec(label="身份", page="identity"),
                        ),
                        layout="bisected",
                    ),
                ),
            ),
            "chat": _HelpPageSpec(
                title="Codex 帮助：当前会话",
                markdown=(
                    "作用对象：**当前 chat binding**。\n\n"
                    "- `/status`：查看当前目录、当前线程，以及当前会话设置摘要\n"
                    f"- `{_SHARED_PREFLIGHT_COMMAND.feishu_usage}`：dry-run 下一条普通消息与当前 chat 的 release 可用性，不启动 turn、不改 binding\n"
                    "- `/cd <path>`：切换当前目录并清空当前线程绑定\n"
                    "- 无参数 `/cd` 等价于查看当前目录；`/pwd` 不再作为主导航入口\n"
                    "- 执行中如需停止，直接使用执行卡片里的“取消执行”\n\n"
                    "线程浏览、新建与恢复，请看“线程”页。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/status", command="/status", title="Codex 当前状态"),
                            _HelpCommandButtonSpec(
                                label=_SHARED_PREFLIGHT_COMMAND.slash_name,
                                command=_SHARED_PREFLIGHT_COMMAND.slash_name,
                                title="Codex Preflight",
                            ),
                            _HelpPageButtonSpec(label="切换目录", page="chat-cd-form"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="线程", page="thread"),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                        layout="bisected",
                    ),
                ),
            ),
            "chat-cd-form": _HelpPageSpec(
                title="Codex 帮助：切换目录",
                markdown=(
                    "填写目标目录并提交，相当于执行 `/cd <path>`。\n\n"
                    "- 成功后会清空当前线程绑定\n"
                    "- 之后直接发送普通文本，会在新目录自动新建线程"
                ),
                form=_HelpFormSpec(
                    form_name="help_cd_form",
                    field_name="cd_path",
                    placeholder="输入目标目录路径",
                    submit_label="切换目录",
                    submit_command="/cd",
                    submit_title="Codex 目录切换结果",
                    required_text="请输入目标目录路径。",
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="chat"),),
                    ),
                ),
            ),
            "group": _HelpPageSpec(
                title="Codex 帮助：群聊",
                markdown=(
                    "**群授权面**\n"
                    "- `/group`：查看当前群是否已激活\n"
                    "- `/group activate`：由管理员激活当前群\n"
                    "- `/group deactivate`：由管理员停用当前群\n"
                    "- 未激活群里，非管理员不能使用机器人\n\n"
                    "**群聊工作态**\n"
                    "- `/group-mode`：查看或切换当前群聊工作态\n"
                    "- `assistant`：默认；只在有效 mention 时回复，并附带群上下文\n"
                    "- `mention-only`：只在有效 mention 时触发，不带群历史上下文\n"
                    "- `all`：允许成员直接发普通消息触发；风险最高，且当前 thread 进入 `all` 独占规则\n"
                    "- 这是群聊专属能力；在私聊中触发会按 slash 语义拒绝\n\n"
                    "**权限边界**\n"
                    "- 激活后的群成员可日常对话，并处理自己发起 turn 的审批或补充输入\n"
                    "- 所有会改变共享状态的命令与设置，仍然只允许管理员操作"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="/group",
                                command="/group",
                                title="Codex 群聊授权",
                            ),
                            _HelpCommandButtonSpec(
                                label="/group-mode",
                                command="/group-mode",
                                title="Codex 群聊工作态",
                            ),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                        layout="trisection",
                    ),
                ),
            ),
            "thread": _HelpPageSpec(
                title="Codex 帮助：线程",
                markdown=(
                    "作用对象：**当前或目标 thread**。\n\n"
                    f"- `{_SHARED_THREADS_COMMAND.feishu_usage}`：浏览当前目录线程\n"
                    "- `/new`：立即新建线程\n"
                    f"- `{_SHARED_RESUME_COMMAND.feishu_usage}`：全局精确恢复线程，可填 `thread_id` 或 `thread_name`\n"
                    "- “当前线程”页：查看 `/profile`、重命名、归档当前绑定 thread\n\n"
                    "**本地继续**\n"
                    f"- 需要在本地继续同一 live thread 时，使用 `{_LOCAL_RESUME_COMMAND}`\n"
                    f"- 本地查看当前目录线程请用 `{_LOCAL_THREAD_LIST_CWD}`\n"
                    f"- 本地全局找线程请用 `{_LOCAL_THREAD_LIST_GLOBAL}`\n\n"
                    "当前 chat 的状态、预检与目录切换，请看“当前会话”页。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/threads", command="/threads", title="Codex Threads"),
                            _HelpCommandButtonSpec(
                                label="/new",
                                command="/new",
                                title="Codex 新建线程",
                                button_type="primary",
                            ),
                            _HelpPageButtonSpec(label="恢复线程", page="thread-resume-form"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="当前线程", page="thread-current"),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                        layout="bisected",
                    ),
                ),
            ),
            "thread-current": _HelpPageSpec(
                title="Codex 帮助：当前线程",
                markdown=(
                    "作用对象：**当前绑定 thread**。\n\n"
                    f"- `{_SHARED_PROFILE_COMMAND.feishu_usage}`：查看或切换当前 thread 的 resume profile；必要时会提供 reset backend 路径\n"
                    "- `/rename <title>`：重命名当前线程\n"
                    f"- `{_SHARED_ARCHIVE_COMMAND.slash_name}`：归档当前线程\n\n"
                    "如果当前没有绑定线程，相关命令会按 slash 语义返回明确提示。\n\n"
                    "通常不需要在飞书侧主动理解 `/release-runtime`。\n"
                    "如果只是为了 re-profile，优先直接使用 `/profile <name>` 走现有路径；"
                    "需要排障或本地管理时，再用 "
                    f"`{_LOCAL_THREAD_UNSUBSCRIBE}`。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="/profile",
                                command="/profile",
                                title="Codex Thread Profile",
                            ),
                            _HelpCommandButtonSpec(
                                label="/archive",
                                command="/archive",
                                title="Codex 归档线程",
                            ),
                            _HelpPageButtonSpec(label="重命名", page="thread-rename-current-form"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回线程", page="thread"),),
                    ),
                ),
            ),
            "thread-resume-form": _HelpPageSpec(
                title="Codex 帮助：恢复线程",
                markdown=(
                    f"填写 `{_SHARED_RESUME_COMMAND.feishu_usage}` 里的目标。\n\n"
                    "- 支持精确 `thread_id`\n"
                    "- 也支持全局精确 `thread_name`\n"
                    "- 如果同名命中多个线程，会按 slash 语义报错，不会替你猜"
                ),
                form=_HelpFormSpec(
                    form_name="help_resume_form",
                    field_name="resume_target",
                    placeholder="输入 thread_id 或 thread_name",
                    submit_label="恢复线程",
                    submit_command="/resume",
                    submit_title="Codex 恢复线程",
                    required_text="请输入 thread_id 或 thread_name。",
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="thread"),),
                    ),
                ),
            ),
            "thread-rename-current-form": _HelpPageSpec(
                title="Codex 帮助：重命名当前线程",
                markdown=(
                    "填写新标题并提交，相当于执行 `/rename <title>`。\n\n"
                    "该操作只针对当前绑定线程。"
                ),
                form=_HelpFormSpec(
                    form_name="help_rename_current_form",
                    field_name="rename_title",
                    placeholder="输入新标题",
                    submit_label="确认重命名",
                    submit_command="/rename",
                    submit_title="Codex 重命名结果",
                    required_text="请输入新标题。",
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="thread-current"),),
                    ),
                ),
            ),
            "runtime": _HelpPageSpec(
                title="Codex 帮助：运行时",
                markdown=(
                    "作用对象：**当前飞书会话** 与 **当前实例 backend**。\n\n"
                    "- `/profile` 属于当前 thread 管理，入口在“线程 -> 当前线程”\n"
                    "- 推荐先用 `/permissions`；它会同时设置审批策略与沙箱\n"
                    "- `/approval`、`/sandbox`：单独调整审批或沙箱\n"
                    "- `/collab-mode`：切换当前飞书会话后续 turn 的协作模式\n"
                    f"- `{_SHARED_RESET_BACKEND_COMMAND.feishu_usage}`：管理员预览并重置当前实例 backend；这是实例级管理动作，不是当前 thread 命令\n"
                    "- 如果当前正在执行，新设置从下一轮生效。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="/permissions",
                                command="/permissions",
                                title="Codex 权限预设",
                            ),
                            _HelpCommandButtonSpec(label="/approval", command="/approval", title="Codex 审批策略"),
                            _HelpCommandButtonSpec(label="/sandbox", command="/sandbox", title="Codex 沙箱策略"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="/collab-mode",
                                command="/collab-mode",
                                title="Codex 协作模式",
                            ),
                            _HelpCommandButtonSpec(
                                label="/reset-backend",
                                command="/reset-backend",
                                title="Codex Backend Reset",
                            ),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                        layout="trisection",
                    ),
                ),
            ),
            "identity": _HelpPageSpec(
                title="Codex 帮助：身份",
                markdown=(
                    "- `/whoami`：私聊查看自己的 `open_id` 等身份信息\n"
                    "- `/bot-status`：查看机器人的 `app_id`、配置的 `bot_open_id`、实时探测结果\n"
                    "- `/init <token>`：私聊初始化管理员与 `bot_open_id`\n\n"
                    "注意：`/whoami` 与 `/init` 只支持私聊；如果在群里触发，会按 slash 语义拒绝。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/whoami", command="/whoami", title="Codex 身份信息"),
                            _HelpCommandButtonSpec(
                                label="/bot-status",
                                command="/bot-status",
                                title="Codex 机器人状态",
                            ),
                            _HelpPageButtonSpec(label="初始化", page="identity-init-form"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回帮助", page="overview"),),
                    ),
                ),
            ),
            "identity-init-form": _HelpPageSpec(
                title="Codex 帮助：初始化",
                markdown=(
                    "填写初始化 token 并提交，相当于执行 `/init <token>`。\n\n"
                    "- 仅支持私聊\n"
                    "- 会把当前发送者加入 `admin_open_ids`\n"
                    "- 会尽量自动写入 `bot_open_id`"
                ),
                form=_HelpFormSpec(
                    form_name="help_init_form",
                    field_name="init_token",
                    placeholder="输入 init token",
                    submit_label="执行初始化",
                    submit_command="/init",
                    submit_title="Codex 初始化结果",
                    required_text="请输入 init token。",
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="identity"),),
                    ),
                ),
            ),
        }

    @staticmethod
    def _build_page_aliases() -> dict[str, str]:
        return {
            "": "overview",
            "overview": "overview",
            "chat": "chat",
            "group": "group",
            "thread": "thread",
            "runtime": "runtime",
            "identity": "identity",
        }

    def _resolve_page_id(self, page_or_alias: str) -> str:
        normalized = str(page_or_alias or "").strip().lower()
        if normalized in self._page_specs:
            return normalized
        return self._page_aliases.get(normalized, "")

    def _render_button(self, spec: _HelpPageButtonSpec | _HelpCommandButtonSpec) -> dict[str, Any]:
        if isinstance(spec, _HelpPageButtonSpec):
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": spec.label},
                "type": spec.button_type,
                "value": {
                    "action": "show_help_page",
                    "page": spec.page,
                },
            }
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": spec.label},
            "type": spec.button_type,
            "value": {
                "action": "help_execute_command",
                "command": spec.command,
                "title": spec.title,
            },
        }

    def _render_action_row(self, spec: _HelpActionRowSpec) -> dict[str, Any]:
        row: dict[str, Any] = {
            "tag": "action",
            "actions": [self._render_button(button) for button in spec.buttons],
        }
        if spec.layout:
            row["layout"] = spec.layout
        return row

    def _render_help_page(self, spec: _HelpPageSpec) -> dict:
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": spec.markdown}]
        if spec.form is not None or spec.action_rows:
            elements.append({"tag": "hr"})
        if spec.form is not None:
            elements.append(
                {
                    "tag": "form",
                    "name": spec.form.form_name,
                    "elements": [
                        {
                            "tag": "input",
                            "name": spec.form.field_name,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": spec.form.placeholder,
                            },
                            "default_value": spec.form.default_value,
                        },
                        {
                            "tag": "button",
                            "name": "submit",
                            "text": {"tag": "plain_text", "content": spec.form.submit_label},
                            "type": "primary",
                            "form_action_type": "submit",
                            "value": {
                                "action": "help_submit_command",
                                "command": spec.form.submit_command,
                                "field_name": spec.form.field_name,
                                "title": spec.form.submit_title,
                                "required_text": spec.form.required_text,
                            },
                        },
                    ],
                }
            )
        elements.extend(self._render_action_row(row) for row in spec.action_rows)
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": spec.title},
                "template": "blue",
            },
            "elements": elements,
        }

    def _build_help_card(self, page_or_alias: str) -> dict | None:
        page_id = self._resolve_page_id(page_or_alias)
        if not page_id:
            return None
        spec = self._page_specs.get(page_id)
        if spec is None:
            return None
        return self._render_help_page(spec)

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
        card = self._build_help_card(str(action_value.get("page", "")))
        if card is None:
            return make_card_response(toast="未知帮助页面。", toast_type="warning")
        return make_card_response(card=card)

    def reply_help(self, chat_id: str, topic: str = "", *, message_id: str = "") -> CommandResult:
        del chat_id
        del message_id
        card = self._build_help_card(topic)
        if card is not None:
            return CommandResult(card=card)
        return CommandResult(
            text=(
                "帮助主题仅支持：`chat`、`group`、`thread`、`runtime`、`identity`。\n"
                "发送 `/help` 查看导航入口。"
            )
        )
