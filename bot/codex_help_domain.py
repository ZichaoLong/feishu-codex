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
_SHARED_RM_COMMAND = get_shared_command("rm")
_SHARED_SESSION_COMMAND = get_shared_command("session")
_SHARED_RESUME_COMMAND = get_shared_command("resume")

_LOCAL_THREAD_LIST_CWD = "feishu-codexctl thread list --scope cwd"
_LOCAL_THREAD_LIST_GLOBAL = "feishu-codexctl thread list --scope global"
_LOCAL_RESUME_COMMAND = "fcodex resume <thread_id|thread_name>"


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
                    "从下面三个入口按场景进入，不需要先记住命令名。\n\n"
                    "- `session`：线程、恢复、当前线程状态、目录切换\n"
                    "- `settings`：profile、权限预设、审批、沙箱、协作模式、身份初始化\n"
                    "- `group`：群聊工作态、ACL 使用规则、群内触发约束\n\n"
                    f"{self._local_thread_safety_rule}\n\n"
                    "本地继续同一线程请用 "
                    f"`{_LOCAL_RESUME_COMMAND}`；"
                    "本地查看/管理请用 "
                    f"`{_LOCAL_THREAD_LIST_CWD}`。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="session", page="session"),
                            _HelpPageButtonSpec(label="settings", page="settings"),
                            _HelpPageButtonSpec(label="group", page="group"),
                        ),
                        layout="trisection",
                    ),
                ),
            ),
            "session": _HelpPageSpec(
                title="Codex 帮助：线程",
                markdown=(
                    "**线程与目录**\n"
                    f"- `{_SHARED_SESSION_COMMAND.feishu_usage}`：浏览当前目录线程\n"
                    "- `/new`：立即新建线程\n"
                    f"- `{_SHARED_RESUME_COMMAND.feishu_usage}`：全局精确恢复线程，可填 `thread_id` 或 `thread_name`\n"
                    "- `/cd <path>`：切换目录并清空当前线程绑定\n"
                    "- “当前线程”页：查看 `/status`、`/preflight`、释放 runtime、重命名、归档当前绑定线程\n\n"
                    "**本地继续**\n"
                    f"- 需要在本地继续同一 live thread 时，使用 `{_LOCAL_RESUME_COMMAND}`\n"
                    f"- 本地查看当前目录线程请用 `{_LOCAL_THREAD_LIST_CWD}`\n"
                    f"- 本地全局找线程请用 `{_LOCAL_THREAD_LIST_GLOBAL}`\n\n"
                    f"{self._local_thread_safety_rule}"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="当前线程", page="session-current"),
                            _HelpCommandButtonSpec(label="/session", command="/session", title="Codex Session"),
                            _HelpCommandButtonSpec(
                                label="/new",
                                command="/new",
                                title="Codex 新建线程",
                                button_type="primary",
                            ),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="恢复线程", page="session-resume-form"),
                            _HelpPageButtonSpec(label="切换目录", page="session-cd-form"),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                        layout="trisection",
                    ),
                ),
            ),
            "session-current": _HelpPageSpec(
                title="Codex 帮助：当前线程",
                markdown=(
                    "这些操作都以**当前绑定线程**为目标。\n\n"
                    "- `/status`：查看当前 binding、feishu runtime、backend thread status、profile 相关信息\n"
                    "- `/preflight`：dry-run 当前 chat 下一条普通消息与 release 操作，不启动 turn、不改 binding\n"
                    "- `/unsubscribe`：让 Feishu 释放自己对当前线程的 runtime 持有，但不解绑 thread\n"
                    "- `/rename <title>`：重命名当前线程\n"
                    f"- `{_SHARED_RM_COMMAND.slash_name}`：归档当前线程\n\n"
                    "如果当前没有绑定线程，相关命令会按 slash 语义返回明确提示。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/status", command="/status", title="Codex 当前状态"),
                            _HelpCommandButtonSpec(
                                label="/preflight",
                                command="/preflight",
                                title="Codex Preflight",
                            ),
                            _HelpCommandButtonSpec(
                                label="unsubscribe",
                                command="/unsubscribe",
                                title="Codex 取消 Feishu 订阅",
                            ),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/rm", command="/rm", title="Codex 归档线程"),
                            _HelpPageButtonSpec(label="重命名", page="session-rename-current-form"),
                            _HelpPageButtonSpec(label="返回 Session", page="session"),
                        ),
                        layout="trisection",
                    ),
                ),
            ),
            "session-resume-form": _HelpPageSpec(
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
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="session"),),
                    ),
                ),
            ),
            "session-cd-form": _HelpPageSpec(
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
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="session"),),
                    ),
                ),
            ),
            "session-rename-current-form": _HelpPageSpec(
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
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="session-current"),),
                    ),
                ),
            ),
            "settings": _HelpPageSpec(
                title="Codex 帮助：设置",
                markdown=(
                    "**当前 thread profile 与当前会话设置**\n"
                    f"- `{_SHARED_PROFILE_COMMAND.feishu_usage}`：查看或切换当前绑定 thread 的 resume profile\n"
                    "- 推荐先用 `/permissions`；它会同时设置审批策略与沙箱\n"
                    "- `/approval`、`/sandbox`：单独调整审批或沙箱\n"
                    "- `/mode`：切换当前飞书会话后续 turn 的协作模式\n"
                    "- 如果当前正在执行，新设置从下一轮生效。\n\n"
                    "**身份与初始化**\n"
                    "- `/whoami`、`/whoareyou`、`/init <token>` 在单独子页里"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/profile", command="/profile", title="Codex Thread Profile"),
                            _HelpCommandButtonSpec(
                                label="/permissions",
                                command="/permissions",
                                title="Codex 权限预设",
                            ),
                            _HelpCommandButtonSpec(label="/approval", command="/approval", title="Codex 审批策略"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/sandbox", command="/sandbox", title="Codex 沙箱策略"),
                            _HelpCommandButtonSpec(label="/mode", command="/mode", title="Codex 协作模式"),
                            _HelpPageButtonSpec(label="身份与初始化", page="settings-identity"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回帮助", page="overview"),),
                    ),
                ),
            ),
            "settings-identity": _HelpPageSpec(
                title="Codex 帮助：身份与初始化",
                markdown=(
                    "- `/whoami`：私聊查看自己的 `open_id` 等身份信息\n"
                    "- `/whoareyou`：查看机器人的 `app_id`、配置的 `bot_open_id`、实时探测结果\n"
                    "- `/debug-contact <open_id>`：私聊排查通讯录名字解析、缓存命中与 fallback 原因\n"
                    "- `/init <token>`：私聊初始化管理员与 `bot_open_id`\n\n"
                    "注意：`/whoami` 与 `/init` 只支持私聊；如果在群里触发，会按 slash 语义拒绝。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(label="/whoami", command="/whoami", title="Codex 身份信息"),
                            _HelpCommandButtonSpec(
                                label="/whoareyou",
                                command="/whoareyou",
                                title="Codex 机器人身份",
                            ),
                            _HelpPageButtonSpec(label="初始化", page="settings-init-form"),
                        ),
                        layout="trisection",
                    ),
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回 Settings", page="settings"),),
                    ),
                ),
            ),
            "settings-init-form": _HelpPageSpec(
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
                        buttons=(_HelpPageButtonSpec(label="返回上一页", page="settings-identity"),),
                    ),
                ),
            ),
            "group": _HelpPageSpec(
                title="Codex 帮助：群聊",
                markdown=(
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
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="/groupmode",
                                command="/groupmode",
                                title="Codex 群聊工作态",
                            ),
                            _HelpPageButtonSpec(label="返回帮助", page="overview"),
                        ),
                    ),
                ),
            ),
            "local-wrapper-redirect": _HelpPageSpec(
                title="Codex 帮助：本地命令",
                markdown=(
                    f"本地继续同一线程请用 `{_LOCAL_RESUME_COMMAND}`。\n\n"
                    "本地查看/管理请用：\n"
                    f"- `{_LOCAL_THREAD_LIST_CWD}`\n"
                    f"- `{_LOCAL_THREAD_LIST_GLOBAL}`\n"
                    "- `feishu-codexctl thread status --thread-id <thread_id>`\n"
                    "- `feishu-codexctl thread bindings --thread-id <thread_id>`"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="返回帮助", page="overview"),),
                    ),
                ),
            ),
        }

    @staticmethod
    def _build_page_aliases() -> dict[str, str]:
        return {
            "": "overview",
            "basic": "overview",
            "basics": "overview",
            "overview": "overview",
            "session": "session",
            "sessions": "session",
            "resume": "session",
            "thread": "session",
            "threads": "session",
            "settings": "settings",
            "permission": "settings",
            "permissions": "settings",
            "approval": "settings",
            "sandbox": "settings",
            "mode": "settings",
            "advanced": "settings",
            "group": "group",
            "groups": "group",
            "acl": "group",
            "local": "local-wrapper-redirect",
            "fcodex": "local-wrapper-redirect",
            "wrapper": "local-wrapper-redirect",
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
        return CommandResult(text="帮助主题仅支持：`session`、`settings`、`group`。\n发送 `/help` 查看导航入口。")
