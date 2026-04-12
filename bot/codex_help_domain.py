"""
Codex help domain.
"""

from __future__ import annotations

from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import (
    CommandResult,
    build_help_dashboard_card,
    build_help_topic_actions_card,
    build_help_topic_card,
    build_markdown_card,
    make_card_response,
)


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
            return "local"
        return ""

    def _build_help_card(self, topic: str) -> dict | None:
        normalized = self._normalize_help_topic(topic)
        if normalized == "overview":
            return build_help_dashboard_card(self._help_overview_text())
        if normalized == "session":
            return build_help_topic_card("Codex 帮助：线程", self._help_session_text())
        if normalized == "settings":
            return build_help_topic_actions_card(
                "Codex 帮助：设置",
                self._help_settings_text(),
                actions=[
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/permissions"},
                        "type": "default",
                        "value": {
                            "action": "show_permissions_card",
                            "plugin": self._plugin_keyword,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/mode"},
                        "type": "default",
                        "value": {
                            "action": "show_mode_card",
                            "plugin": self._plugin_keyword,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "返回帮助"},
                        "type": "default",
                        "value": {
                            "action": "show_help_overview",
                            "plugin": self._plugin_keyword,
                        },
                    },
                ],
                layout="trisection",
            )
        if normalized == "group":
            return build_help_topic_actions_card(
                "Codex 帮助：群聊",
                self._help_group_text(),
                actions=[
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/groupmode"},
                        "type": "default",
                        "value": {
                            "action": "show_group_mode_card",
                            "plugin": self._plugin_keyword,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "返回帮助"},
                        "type": "default",
                        "value": {
                            "action": "show_help_overview",
                            "plugin": self._plugin_keyword,
                        },
                    },
                ],
            )
        if normalized == "local":
            return build_markdown_card("Codex 帮助：本地继续", self._help_local_text())
        return None

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
        card = self._build_help_card(str(action_value.get("topic", "")))
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
        return make_card_response(card=build_help_dashboard_card(self._help_overview_text()))

    def reply_help(self, chat_id: str, topic: str = "", *, message_id: str = "") -> CommandResult:
        card = self._build_help_card(topic)
        if card is not None:
            return CommandResult(card=card)
        return CommandResult(text="帮助主题仅支持：`session`、`settings`、`group`、`local`。\n发送 `/help` 查看概览。")

    def _help_overview_text(self) -> str:
        return (
            "直接发送普通文本即可向当前线程提问；如果当前没有绑定线程，会在当前目录自动新建。\n\n"
            "**命令**\n"
            "- `/new` 立即新建线程\n"
            "- `/session` 查看当前目录线程\n"
            "- `/resume <thread_id|thread_name>` 恢复指定线程\n"
            "- `/cd <path>` 切换目录并清空当前线程绑定\n"
            "- `/status` 查看当前状态\n\n"
            "- `/init <token>`：私聊初始化管理员和 `bot_open_id`\n"
            "- `/whoami`：私聊查看自己的 `open_id`，以及 best-effort 的 `user_id`（仅用于排障）\n"
            "- `/whoareyou`：查看机器人自己的 `app_id` / `open_id`\n\n"
            "**更多命令与帮助**\n"
            "- 下方按钮可直接切到 `session`、`settings`、`group`\n"
            "- `/help session` 查看线程切换、目录切换与归档\n"
            "- `/help settings` 查看 profile、权限与协作设置\n"
            "- `/help group` 查看群聊工作态、授权策略与上下文规则\n"
            "- `/help local` 查看本地 `fcodex` 的用法\n\n"
            f"{self._local_thread_safety_rule}"
        )

    def _help_session_text(self) -> str:
        return (
            "**线程相关**\n"
            "- `/new` 立即新建并切换到新线程；切走时旧线程会从 app-server 内存中释放。\n"
            "- `/session` 只列当前目录的线程，结果已跨 provider 汇总。\n"
            "- `/resume <thread_id|thread_name>` 会做全局精确匹配；恢复后会切到线程自己的目录。\n"
            "- `/resume` 会尝试应用当前默认 profile 的 model 和 model_provider；切换 profile 后 `/resume` 旧线程可切换 provider。\n"
            "- 如果匹配到多个同名线程，`/resume` 会报错，不会替你猜。\n"
            "- `/cd <path>` 切换目录并清空当前线程绑定；之后发送普通文本，会在新目录自动新建线程。\n"
            "- `/rename` 改标题，`/rm` 归档线程而不是硬删除。\n\n"
            "**本地继续同一线程**\n"
            "- 可先用 `fcodex /session` 找线程；需要精确恢复时再用 `fcodex /resume`。\n"
            f"- {self._local_thread_safety_rule}"
        )

    def _help_settings_text(self) -> str:
        return (
            "**设置相关**\n"
            "- `/profile` 查看或切换默认 profile（打包 model_provider + model 等配置）。\n"
            "- `/profile` 影响 `/new`（新建线程）和 `/resume`（恢复线程时尝试应用新 provider/model）。\n"
            "- `/profile` 不影响已打开线程的后续 turn（`model_provider` 在线程级固定）；`/sandbox`、`/approval`、`/mode` 可在后续 turn 中随时切换。\n"
            "- profile 配置从 `~/.codex/config.toml` 实时读取，修改后无需重启 feishu-codex。\n"
            "- `/init <token>` 仅私聊可用；会把当前发送者加入 `admin_open_ids`，并尽量自动写入 `bot_open_id`。\n"
            "- 运行时只有 `system.yaml.bot_open_id` 会参与群聊 mention 判定；`/whoareyou` 的实时探测结果仅用于诊断和初始化。\n"
            "- 推荐先用 `/permissions`；它会同时设置审批策略和沙箱，只影响当前飞书会话的后续 turn。\n"
            "- `/approval` 只改审批时机；`/sandbox` 只改文件与网络边界。\n"
            "- `/mode` 切换协作方式；`plan` 更容易先规划或提问，`default` 更接近直接执行；也只影响当前飞书会话的后续 turn。\n"
            "- 如果当前正在执行，新设置从下一轮生效。\n\n"
            "**命令**\n"
            "- `/init <token>`\n"
            "- `/profile [name]`\n"
            "- `/permissions [read-only|default|full-access]`\n"
            "- `/approval [untrusted|on-failure|on-request|never]`\n"
            "- `/sandbox [read-only|workspace-write|danger-full-access]`\n"
            "- `/mode [default|plan]`"
        )

    def _help_group_text(self) -> str:
        return (
            "**群聊相关**\n"
            "- `/groupmode` 查看当前群聊工作态；管理员可切到 `assistant`、`all`、`mention-only`。\n"
            "- 私聊底层会话按人隔离；群聊底层会话按 `chat_id` 共享。\n"
            "- `assistant` 会缓存群聊消息，仅在人类有效 mention 时回复；每次有效触发都会回捞最近群历史，把两次触发之间的消息补齐进上下文。\n"
            "- `assistant` 的主聊天流与群话题使用不同上下文边界：主聊天流只看主聊天流，话题只看当前话题；但底层仍是同一个群共享会话。\n"
            "- 主聊天流历史回捞受 `group_history_fetch_lookback_seconds` 和 `group_history_fetch_limit` 共同限制；话题内回捞当前只保证受边界和 `group_history_fetch_limit` 限制。\n"
            "- `group_history_fetch_lookback_seconds` 同时也是历史回捞总开关的一部分；任一项设为 `0`，主聊天流和话题回捞都会一起关闭。\n"
            "- `/acl` 查看当前群授权；管理员可设置 `admin-only`、`allowlist`、`all-members`。\n"
            "- 群里的所有 `/` 命令都只给管理员；在 `assistant` / `mention-only` 下还要先显式 mention 触发对象，在 `all` 下管理员可直接发送。\n"
            "- 有效 mention 默认只认机器人自身 `bot_open_id`；如配置 `trigger_open_ids`，`@这些人` 也会视为触发。\n"
            "- 群命令不写入 `assistant` 上下文日志，也不会推进上下文边界。\n"
            "- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效触发时额外回捞群历史，用来补齐其他机器人和遗漏消息。\n"
            "- 在话题内触发时，执行卡片、ACL 拒绝和长回复会尽量留在原话题。\n"
            "- 未获授权成员在 `all` 模式下直接发普通消息会静默忽略；只有显式 mention 触发对象或发群命令时才会收到拒绝提示。\n"
            "- 管理员授权成员时，推荐在群里直接 `@成员` 使用 `/acl grant` 或 `/acl revoke`。\n\n"
            "**命令**\n"
            "- `/groupmode [assistant|all|mention-only]`\n"
            "- `/acl`\n"
            "- `/acl policy <admin-only|allowlist|all-members>`\n"
            "- `/acl grant @成员`\n"
            "- `/acl revoke @成员`"
        )

    def _help_local_text(self) -> str:
        return (
            "**本地继续线程时再用 `fcodex`**\n"
            "- `fcodex` 是 `codex --remote` 的 wrapper，默认连到 feishu-codex 的 shared backend。\n"
            "- `fcodex /session`、`fcodex /resume <thread_id|thread_name>` 会用共享发现逻辑，跨 provider 找线程。\n"
            "- 进入 TUI 后，里面的 `/resume` 是 Codex 原生命令，不等同于 `fcodex /resume`。\n"
            "- 只想开独立的本地会话，直接用裸 `codex`。\n"
            f"- {self._local_thread_safety_rule}"
        )
