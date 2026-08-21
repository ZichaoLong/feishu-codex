"""
Codex help domain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import CommandResult, make_card_response
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.constants import display_path
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.permissions_profile import permissions_profile_choice_key
from bot.runtime_state import FEISHU_RUNTIME_DETACHED
from bot.shared_command_surface import get_shared_command


_SHARED_RESET_BACKEND_COMMAND = get_shared_command("reset-backend")
_SHARED_PREFLIGHT_COMMAND = get_shared_command("preflight")
_SHARED_ARCHIVE_COMMAND = get_shared_command("archive")
_SHARED_THREADS_COMMAND = get_shared_command("threads")
_SHARED_RESUME_COMMAND = get_shared_command("resume")
_SHARED_DETACH_COMMAND = get_shared_command("detach")
_SHARED_ATTACH_COMMAND = get_shared_command("attach")
_SHARED_COMMANDS_COMMAND = get_shared_command("commands")
_SHARED_GOAL_COMMAND = get_shared_command("goal")
_SHARED_LAST_COMMAND = get_shared_command("last")
_SHARED_STEER_COMMAND = get_shared_command("steer")
_SHARED_MODEL_COMMAND = get_shared_command("model")
_SHARED_EFFORT_COMMAND = get_shared_command("effort")
_SHARED_COMPACT_COMMAND = get_shared_command("compact")

_LOCAL_THREAD_LIST_CWD = "focusctl thread list --scope cwd"
_LOCAL_THREAD_LIST_GLOBAL = "focusctl thread list --scope global"
_LOCAL_RESUME_COMMAND = feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>")
_LOCAL_THREAD_DETACH = feishu_visible_command_syntax(
    "focusctl thread detach --thread-id <thread_id>"
)
_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
_CD_COMMAND = feishu_visible_command_syntax("/cd <path>")
_RENAME_COMMAND = feishu_visible_command_syntax("/rename <title>")
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
class _HelpDirectActionButtonSpec:
    label: str
    value: dict[str, Any]
    button_type: str = "default"


@dataclass(frozen=True)
class _HelpActionRowSpec:
    buttons: tuple[_HelpPageButtonSpec | _HelpCommandButtonSpec | _HelpDirectActionButtonSpec, ...]
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
        resolve_session: Callable[[str, str, str], BindingSessionSnapshot],
        is_group_chat: Callable[[str, str], bool] | None = None,
        get_group_mode: Callable[[str], str] | None = None,
        get_group_activation_snapshot: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._local_thread_safety_rule = local_thread_safety_rule
        self._resolve_session = resolve_session
        self._is_group_chat = is_group_chat
        self._get_group_mode = get_group_mode
        self._get_group_activation_snapshot = get_group_activation_snapshot
        self._page_specs = self._build_page_specs()
        self._page_aliases = self._build_page_aliases()
        self._form_specs_by_field = self._build_form_specs_by_field()

    @staticmethod
    def _home_action_rows() -> tuple[_HelpActionRowSpec, ...]:
        return (
            _HelpActionRowSpec(
                buttons=(
                    _HelpPageButtonSpec(label="开始", page="start-switch"),
                    _HelpPageButtonSpec(label="线程设置", page="thread-settings"),
                ),
                layout="bisected",
            ),
            _HelpActionRowSpec(
                buttons=(
                    _HelpPageButtonSpec(label="本轮设置", page="turn-settings"),
                    _HelpPageButtonSpec(label="连接状态", page="connection-status"),
                ),
                layout="bisected",
            ),
            _HelpActionRowSpec(
                buttons=(
                    _HelpPageButtonSpec(label="群聊设置", page="group-settings"),
                    _HelpPageButtonSpec(label="更多", page="more"),
                ),
                layout="bisected",
            ),
        )

    @staticmethod
    def _return_home_row() -> _HelpActionRowSpec:
        return _HelpActionRowSpec(buttons=(_HelpPageButtonSpec(label="返回首页", page="overview"),))

    @staticmethod
    def _return_previous_row(page: str) -> _HelpActionRowSpec:
        return _HelpActionRowSpec(buttons=(_HelpPageButtonSpec(label="返回上一页", page=page),))

    def _build_page_specs(self) -> dict[str, _HelpPageSpec]:
        return {
            "overview": _HelpPageSpec(
                title="Codex 工作台",
                markdown="",
                action_rows=self._home_action_rows(),
            ),
            "start-switch": _HelpPageSpec(
                title="Codex 工作台：开始",
                markdown=(
                    "处理开新线程、恢复旧线程、浏览线程与切换目录。\n\n"
                    f"{self._local_thread_safety_rule}\n\n"
                    f"本地继续同一 live thread：`{_LOCAL_RESUME_COMMAND}`\n"
                    f"本地查看当前目录线程：`{_LOCAL_THREAD_LIST_CWD}`\n"
                    f"本地全局找线程：`{_LOCAL_THREAD_LIST_GLOBAL}`"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="新建线程",
                                command="/new",
                                title="Codex 线程已新建",
                            ),
                            _HelpPageButtonSpec(label="恢复线程", page="start-switch-resume-form"),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="浏览线程",
                                command="/threads",
                                title="Codex 当前目录线程",
                            ),
                            _HelpPageButtonSpec(label="切换目录", page="start-switch-cd-form"),
                        ),
                        layout="bisected",
                    ),
                    self._return_home_row(),
                ),
            ),
            "start-switch-resume-form": _HelpPageSpec(
                title="Codex 工作台：恢复线程",
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
                action_rows=(self._return_previous_row("start-switch"),),
            ),
            "start-switch-cd-form": _HelpPageSpec(
                title="Codex 工作台：切换目录",
                markdown=(
                    f"填写目标目录并提交，相当于执行 `{_CD_COMMAND}`。\n\n"
                    "- 成功后会清空当前线程绑定\n"
                    "- 支持 `~` 表示用户 home 目录\n"
                    "- 无参数 `/cd` 等价于查看当前目录\n"
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
                action_rows=(self._return_previous_row("start-switch"),),
            ),
            "thread-settings": _HelpPageSpec(
                title="Codex 工作台：线程设置",
                markdown=(
                    "处理当前绑定 thread 的 goal、压缩、重命名与归档。\n\n"
                    "新建、恢复、浏览线程与切换目录，请到“开始”。\n\n"
                    f"当前 goal 可通过 `{_SHARED_GOAL_COMMAND.slash_name}` 查看，"
                    f"也可直接使用 `{_SHARED_GOAL_COMMAND.feishu_usage}`。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="查看 Goal",
                                command="/goal",
                                title="Codex Goal",
                            ),
                            _HelpCommandButtonSpec(
                                label="压缩上下文",
                                command="/compact",
                                title="Codex Compact",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="重命名", page="thread-settings-rename-form"),
                            _HelpCommandButtonSpec(
                                label="归档当前",
                                command="/archive",
                                title="Codex 归档线程",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="按目标归档", page="thread-settings-archive-form"),
                        ),
                    ),
                    self._return_home_row(),
                ),
            ),
            "thread-settings-rename-form": _HelpPageSpec(
                title="Codex 工作台：重命名",
                markdown=(
                    f"填写新标题并提交，相当于执行 `{_RENAME_COMMAND}`。\n\n"
                    "该操作只针对当前绑定线程。"
                ),
                form=_HelpFormSpec(
                    form_name="help_rename_current_form",
                    field_name="help_rename_current_title",
                    placeholder="输入新标题",
                    submit_label="确认重命名",
                    submit_command="/rename",
                    submit_title="Codex 重命名结果",
                    required_text="请输入新标题。",
                ),
                action_rows=(self._return_previous_row("thread-settings"),),
            ),
            "thread-settings-archive-form": _HelpPageSpec(
                title="Codex 工作台：按目标归档",
                markdown=(
                    f"填写目标 thread_id 或 thread_name，相当于执行 `{_SHARED_ARCHIVE_COMMAND.feishu_usage}`。\n\n"
                    f"无参数 `{_SHARED_ARCHIVE_COMMAND.slash_name}` 仍然表示归档当前绑定线程。"
                ),
                form=_HelpFormSpec(
                    form_name="help_archive_target_form",
                    field_name="archive_target",
                    placeholder="输入 thread_id 或 thread_name",
                    submit_label="归档目标线程",
                    submit_command="/archive",
                    submit_title="Codex 归档线程",
                    required_text="请输入 thread_id 或 thread_name。",
                ),
                action_rows=(self._return_previous_row("thread-settings"),),
            ),
            "turn-settings": _HelpPageSpec(
                title="Codex 工作台：本轮设置",
                markdown=(
                    "向当前 active turn 显式补充文本，或调整当前飞书会话后续 turn 的设置。\n\n"
                    f"`{_SHARED_STEER_COMMAND.feishu_usage}` 只作用于当前 exact turn；"
                    "不会排队、创建下一轮或携带附件与设置。\n\n"
                    "推荐先用“权限基线”；模型、推理强度与审批都从下一轮生效。\n"
                    f"`{_SHARED_LAST_COMMAND.feishu_usage}` 可导出当前会话最近一条权威终态文本；"
                    "如果还没有终态结果，会回退到最近执行卡。\n\n"
                    "实例级 backend reset 在“更多 -> 高级操作”。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="权限基线",
                                command="/permissions",
                                title="Codex 权限基线",
                            ),
                            _HelpCommandButtonSpec(
                                label="模型",
                                command="/model",
                                title="Codex 模型 / Effort",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="推理强度",
                                command="/effort",
                                title="Codex 模型 / Effort",
                            ),
                            _HelpCommandButtonSpec(
                                label="审批策略",
                                command="/approval",
                                title="Codex 审批策略",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="最近文本",
                                command="/last text",
                                title="Codex 最近结果文本",
                            ),
                        ),
                    ),
                    self._return_home_row(),
                ),
                form=_HelpFormSpec(
                    form_name="help_steer_form",
                    field_name="steer_text",
                    placeholder="输入要补充给当前 turn 的文本",
                    submit_label="补充到当前 turn",
                    submit_command="/steer",
                    submit_title="Codex Steer",
                    required_text="请输入要补充给当前 turn 的文本。",
                ),
            ),
            "connection-status": _HelpPageSpec(
                title="Codex 工作台：连接状态",
                markdown=(
                    "查看当前状态、发送前检查，以及当前会话是否继续接收飞书推送。\n\n"
                    "最常见的恢复动作是附着整个实例，所以这里直接提供“附着当前实例”。\n\n"
                    "切换线程或目录，请到“开始”。"
                ),
            ),
            "connection-status-attach-more": _HelpPageSpec(
                title="Codex 工作台：更多附着方式",
                markdown=(
                    "低频附着入口。最常用的“附着当前实例”已放在上一页。\n\n"
                    "- `附着当前线程`：恢复当前 thread 的所有相关推送\n"
                    "- `附着当前会话`：只恢复当前 chat binding"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpDirectActionButtonSpec(
                                label="附着当前线程",
                                value={"action": "attach_runtime", "scope": "thread"},
                            ),
                            _HelpDirectActionButtonSpec(
                                label="附着当前会话",
                                value={"action": "attach_runtime"},
                            ),
                        ),
                        layout="bisected",
                    ),
                    self._return_previous_row("connection-status"),
                ),
            ),
            "group-settings": _HelpPageSpec(
                title="Codex 工作台：群聊设置",
                markdown=(
                    "管理当前群的启用状态与群聊工作模式。\n\n"
                    "- 未启用群里，非管理员不能使用机器人\n"
                    "- `all` 风险最高，且当前 thread 会进入更严格的独占规则\n"
                    "- 所有共享状态变更仍以后端权限检查为准"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="群聊启用状态",
                                command="/group",
                                title="Codex 群聊授权",
                            ),
                            _HelpCommandButtonSpec(
                                label="启用本群",
                                command="/group activate",
                                title="Codex 群聊授权",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="停用本群",
                                command="/group deactivate",
                                title="Codex 群聊授权",
                            ),
                            _HelpCommandButtonSpec(
                                label="群工作模式",
                                command="/group-mode",
                                title="Codex 群聊工作态",
                            ),
                        ),
                        layout="bisected",
                    ),
                    self._return_home_row(),
                ),
            ),
            "more": _HelpPageSpec(
                title="Codex 工作台：更多",
                markdown=(
                    "身份、命令索引与低频高级动作。\n\n"
                    f"`/whoami` 与 `{_INIT_COMMAND}` 只支持私聊。"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="身份信息",
                                command="/whoami",
                                title="Codex 身份信息",
                            ),
                            _HelpCommandButtonSpec(
                                label="机器人状态",
                                command="/bot-status",
                                title="Codex 机器人状态",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpPageButtonSpec(label="初始化", page="more-init-form"),
                            _HelpCommandButtonSpec(
                                label="命令索引",
                                command="/commands",
                                title="Codex 命令索引",
                            ),
                        ),
                        layout="bisected",
                    ),
                    _HelpActionRowSpec(
                        buttons=(_HelpPageButtonSpec(label="高级操作", page="more-advanced"),),
                    ),
                    self._return_home_row(),
                ),
            ),
            "more-init-form": _HelpPageSpec(
                title="Codex 工作台：初始化",
                markdown=(
                    f"填写初始化 token 并提交，相当于执行 `{_INIT_COMMAND}`。\n\n"
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
                action_rows=(self._return_previous_row("more"),),
            ),
            "more-advanced": _HelpPageSpec(
                title="Codex 工作台：高级操作",
                markdown=(
                    "低频高级动作与排障入口。\n\n"
                    f"- `{_SHARED_RESET_BACKEND_COMMAND.feishu_usage}`：恢复或排障时重置当前实例 backend\n"
                    f"- `{_DEBUG_CONTACT_COMMAND}`：管理员排查通讯录名字解析问题"
                ),
                action_rows=(
                    _HelpActionRowSpec(
                        buttons=(
                            _HelpCommandButtonSpec(
                                label="重置 backend",
                                command="/reset-backend",
                                title="Codex Backend Reset",
                            ),
                            _HelpPageButtonSpec(label="联系人排障", page="more-debug-contact-form"),
                        ),
                        layout="bisected",
                    ),
                    self._return_previous_row("more"),
                ),
            ),
            "more-debug-contact-form": _HelpPageSpec(
                title="Codex 工作台：联系人排障",
                markdown=(
                    f"填写目标 `open_id` 并提交，相当于执行 `{_DEBUG_CONTACT_COMMAND}`。\n\n"
                    "这是管理员排障入口，不面向日常使用。"
                ),
                form=_HelpFormSpec(
                    form_name="help_debug_contact_form",
                    field_name="debug_contact_open_id",
                    placeholder="输入用户 open_id",
                    submit_label="开始排障",
                    submit_command="/debug-contact",
                    submit_title="Codex 通讯录排障",
                    required_text="请输入用户 open_id。",
                ),
                action_rows=(self._return_previous_row("more-advanced"),),
            ),
        }

    @staticmethod
    def _build_page_aliases() -> dict[str, str]:
        return {
            "": "overview",
            "overview": "overview",
            "home": "overview",
            "workbench": "overview",
            "start": "start-switch",
            "switch": "start-switch",
            "thread": "start-switch",
            "thread-settings": "thread-settings",
            "current-thread": "thread-settings",
            "turn": "turn-settings",
            "runtime": "turn-settings",
            "connection": "connection-status",
            "chat": "connection-status",
            "group": "group-settings",
            "identity": "more",
            "more": "more",
            "chat-cd-form": "start-switch-cd-form",
            "thread-current": "thread-settings",
            "thread-resume-form": "start-switch-resume-form",
            "thread-rename-current-form": "thread-settings-rename-form",
            "identity-init-form": "more-init-form",
        }

    @staticmethod
    def _permissions_summary(permissions_profile_id: str) -> str:
        choice = permissions_profile_choice_key(permissions_profile_id)
        if choice == "read-only":
            return "read-only"
        if choice == "workspace":
            return "workspace"
        if choice == "danger-full-access":
            return "danger-full-access"
        normalized = str(permissions_profile_id or "").strip()
        return normalized or "danger-full-access"

    @staticmethod
    def _overview_permissions_label(permissions_profile_id: str) -> str:
        summary = CodexHelpDomain._permissions_summary(permissions_profile_id)
        if summary == "read-only":
            return "只读"
        if summary == "workspace":
            return "Workspace"
        if summary == "danger-full-access":
            return "Full"
        return "Custom"

    @staticmethod
    def _overview_effort_label(reasoning_effort: str) -> str:
        normalized = str(reasoning_effort or "").strip()
        if not normalized:
            return "Auto"
        if normalized == "xhigh":
            return "XHigh"
        return normalized.capitalize()

    @staticmethod
    def _thread_summary(session: BindingSessionSnapshot) -> str:
        thread_id = session.current_thread_id.strip()
        thread_title = session.current_thread_title.strip()
        if not thread_id:
            return "未绑定"
        short_id = f"{thread_id[:8]}…" if len(thread_id) > 8 else thread_id
        if thread_title:
            return f"{thread_title} · {short_id}"
        return short_id

    def _overview_markdown(
        self,
        *,
        sender_id: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> str:
        session = self._resolve_session(sender_id, chat_id, message_id)
        working_dir = display_path(session.working_dir) or "."
        thread_summary = self._thread_summary(session)
        push_state = session.thread.feishu_runtime_state.strip() or "detached"
        permissions = self._overview_permissions_label(session.permissions_profile_id)
        model = session.model.strip() or "Auto"
        effort = self._overview_effort_label(session.reasoning_effort)
        turn_parts = [
            f"权限 `{permissions}`",
            f"模型 `{model}`",
            f"推理 `{effort}`",
        ]
        lines = [
            f"- 目录：`{working_dir}`",
            f"- 线程：`{thread_summary}`",
            f"- 推送：`{push_state}`",
            f"- 本轮：{' | '.join(turn_parts)}",
        ]
        if (
            self._is_group_chat is not None
            and self._get_group_mode is not None
            and self._get_group_activation_snapshot is not None
            and self._is_group_chat(chat_id, message_id)
        ):
            try:
                snapshot = self._get_group_activation_snapshot(chat_id) or {}
                activated = "已启用" if bool(snapshot.get("activated")) else "未启用"
                group_mode = str(self._get_group_mode(chat_id) or "").strip() or "assistant"
                lines.append(f"- 群聊：`{activated}` / `{group_mode}`")
            except Exception:
                pass
        return "\n".join(lines)

    def _resolve_page_id(self, page_or_alias: str) -> str:
        normalized = str(page_or_alias or "").strip().lower()
        if normalized in self._page_specs:
            return normalized
        return self._page_aliases.get(normalized, "")

    def _resolve_help_page_markdown(
        self,
        page_id: str,
        spec: _HelpPageSpec,
        *,
        sender_id: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> str:
        if page_id == "overview":
            return self._overview_markdown(
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=message_id,
            )
        return spec.markdown

    def _render_button(
        self,
        spec: _HelpPageButtonSpec | _HelpCommandButtonSpec | _HelpDirectActionButtonSpec,
    ) -> dict[str, Any]:
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
        if isinstance(spec, _HelpDirectActionButtonSpec):
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": spec.label},
                "type": spec.button_type,
                "value": dict(spec.value),
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

    @staticmethod
    def _binding_push_toggle_button(feishu_runtime_state: str) -> _HelpCommandButtonSpec:
        if str(feishu_runtime_state or "").strip() == FEISHU_RUNTIME_DETACHED:
            return _HelpCommandButtonSpec(
                label="恢复当前会话",
                command=_SHARED_ATTACH_COMMAND.slash_name,
                title="Codex 已附着飞书推送",
                button_type="primary",
            )
        return _HelpCommandButtonSpec(
            label="暂停推送",
            command=_SHARED_DETACH_COMMAND.slash_name,
            title="Codex 已暂停飞书推送",
            button_type="danger",
        )

    def _resolve_help_page_action_rows(
        self,
        page_id: str,
        *,
        sender_id: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> tuple[_HelpActionRowSpec, ...]:
        if page_id != "connection-status":
            spec = self._page_specs.get(page_id)
            return spec.action_rows if spec is not None else ()
        session = self._resolve_session(sender_id, chat_id, message_id)
        toggle_button = self._binding_push_toggle_button(
            session.thread.feishu_runtime_state
        )
        attach_binding_button: _HelpCommandButtonSpec | _HelpDirectActionButtonSpec = toggle_button
        if isinstance(toggle_button, _HelpCommandButtonSpec) and toggle_button.command == _SHARED_ATTACH_COMMAND.slash_name:
            attach_binding_button = _HelpDirectActionButtonSpec(
                label=toggle_button.label,
                value={"action": "attach_runtime"},
                button_type=toggle_button.button_type,
            )
        return (
            _HelpActionRowSpec(
                buttons=(
                    _HelpCommandButtonSpec(
                        label="当前状态",
                        command="/status",
                        title="Codex 当前状态",
                    ),
                    _HelpCommandButtonSpec(
                        label="发送前检查",
                        command=_SHARED_PREFLIGHT_COMMAND.slash_name,
                        title="Codex Preflight",
                    ),
                ),
                layout="bisected",
            ),
            _HelpActionRowSpec(
                buttons=(
                    attach_binding_button,
                    _HelpDirectActionButtonSpec(
                        label="附着当前实例",
                        value={"action": "attach_runtime", "scope": "service"},
                    ),
                ),
                layout="bisected",
            ),
            _HelpActionRowSpec(
                buttons=(
                    _HelpPageButtonSpec(
                        label="更多附着方式",
                        page="connection-status-attach-more",
                    ),
                ),
            ),
            self._return_home_row(),
        )

    def _render_help_page(
        self,
        page_id: str,
        spec: _HelpPageSpec,
        *,
        sender_id: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> dict:
        markdown = self._resolve_help_page_markdown(
            page_id,
            spec,
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        elements: list[dict[str, Any]] = [{"tag": "markdown", "content": markdown}]
        action_rows = self._resolve_help_page_action_rows(
            page_id,
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        if spec.form is not None or action_rows:
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
        elements.extend(self._render_action_row(row) for row in action_rows)
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": spec.title},
                "template": "blue",
            },
            "elements": elements,
        }

    def _build_help_card(
        self,
        page_or_alias: str,
        *,
        sender_id: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> dict | None:
        page_id = self._resolve_page_id(page_or_alias)
        if not page_id:
            return None
        spec = self._page_specs.get(page_id)
        if spec is None:
            return None
        return self._render_help_page(
            page_id,
            spec,
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
        )

    def _build_form_specs_by_field(self) -> dict[str, _HelpFormSpec]:
        specs: dict[str, _HelpFormSpec] = {}
        for page in self._page_specs.values():
            form = page.form
            if form is None:
                continue
            if form.field_name in specs:
                raise ValueError(f"duplicate help form field: {form.field_name}")
            specs[form.field_name] = form
        return specs

    def resolve_form_submit_payload(self, action_value: dict[str, Any]) -> dict[str, str] | None:
        form_value = action_value.get("_form_value") or {}
        if not isinstance(form_value, dict) or not form_value:
            return None
        matched_fields = [field_name for field_name in self._form_specs_by_field if field_name in form_value]
        if len(matched_fields) != 1:
            return None
        spec = self._form_specs_by_field[matched_fields[0]]
        return {
            "action": "help_submit_command",
            "command": spec.submit_command,
            "field_name": spec.field_name,
            "title": spec.submit_title,
            "required_text": spec.required_text,
        }

    def handle_show_help_page_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        card = self._build_help_card(
            str(action_value.get("page", "")),
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        if card is None:
            return make_card_response(toast="未知帮助页面。", toast_type="warning")
        return make_card_response(card=card)

    def reply_help(self, chat_id: str, topic: str = "", *, sender_id: str = "", message_id: str = "") -> CommandResult:
        card = self._build_help_card(topic, sender_id=sender_id, chat_id=chat_id, message_id=message_id)
        if card is not None:
            return CommandResult(card=card)
        return CommandResult(
            text=(
                "帮助主题支持：`start`、`thread-settings`、`turn`、`connection`、`group`、`more`。\n"
                "发送 `/help` 查看工作台；兼容旧 alias：`chat`、`thread`、`runtime`、`identity`。"
            )
        )

    def reply_commands(self, chat_id: str, *, message_id: str = "") -> CommandResult:
        del chat_id
        del message_id
        return CommandResult(
            text=(
                "常用命令列表（按 `/help` 工作台分组）：\n\n"
                "`帮助`\n"
                "- `/help [overview|start|thread-settings|turn|connection|group|more]`\n"
                "- `/h`\n"
                f"- `{_SHARED_COMMANDS_COMMAND.feishu_usage}`\n\n"
                "`开始`\n"
                "- `/new`\n"
                f"- `{_SHARED_THREADS_COMMAND.feishu_usage}`\n"
                f"- `{_SHARED_RESUME_COMMAND.feishu_usage}`\n"
                "- `/cd [path]`\n\n"
                "`线程设置`\n"
                f"- `{_SHARED_GOAL_COMMAND.feishu_usage}`\n"
                f"- `{_SHARED_COMPACT_COMMAND.feishu_usage}`\n"
                "- `/rename <title>`\n"
                f"- `{_SHARED_ARCHIVE_COMMAND.feishu_usage}`\n\n"
                "`本轮设置`\n"
                f"- `{_SHARED_STEER_COMMAND.feishu_usage}`\n"
                f"- `{_SHARED_LAST_COMMAND.feishu_usage}`\n"
                "- `/permissions [read-only|workspace|danger-full-access]`\n"
                "- `/model [name|auto]`\n"
                "- `/effort [auto|value]`\n"
                "- `/approval [untrusted|on-request|never]`\n\n"
                "`连接状态`\n"
                "- `/status`\n"
                f"- `{_SHARED_PREFLIGHT_COMMAND.feishu_usage}`\n"
                f"- `{_SHARED_DETACH_COMMAND.feishu_usage}`\n"
                f"- `{_SHARED_ATTACH_COMMAND.feishu_usage}`\n\n"
                "`群聊设置`\n"
                "- `/group`\n"
                "- `/group activate`\n"
                "- `/group deactivate`\n"
                "- `/group-mode [assistant|mention-only|all]`\n\n"
                "`更多`\n"
                "- `/whoami`\n"
                "- `/bot-status`\n"
                f"- `{_INIT_COMMAND}`\n"
                f"- `{_SHARED_RESET_BACKEND_COMMAND.feishu_usage}`\n"
                f"- `{_DEBUG_CONTACT_COMMAND}`\n\n"
                "具体私聊 / 群聊限制以命令返回为准；发送 `/help` 或 `/h` 查看工作台。"
            )
        )
