"""
Codex settings domain.
"""

from __future__ import annotations

import logging
import threading
from secrets import compare_digest
from typing import Any, Protocol

from bot.adapters.base import RuntimeConfigSummary
from bot.cards import (
    build_approval_policy_card,
    build_collaboration_mode_card,
    build_markdown_card,
    build_permissions_preset_card,
    build_sandbox_policy_card,
)
from bot.config import ensure_init_token, load_system_config_raw, save_system_config
from bot.profile_resolution import DefaultProfileResolution

logger = logging.getLogger(__name__)


class _SettingsDomainOwner(Protocol):
    bot: Any
    _lock: threading.RLock
    _profile_state: Any
    _adapter_config: Any

    def _get_state(self, sender_id: str, chat_id: str, message_id: str = "") -> Any: ...

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...

    def _reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None: ...

    def _safe_read_runtime_config(self) -> RuntimeConfigSummary | None: ...

    def _current_default_profile_resolution(
        self,
        runtime_config: RuntimeConfigSummary | None,
    ) -> DefaultProfileResolution: ...


class CodexSettingsDomain:
    def __init__(
        self,
        owner: _SettingsDomainOwner,
        *,
        approval_policies: set[str],
        sandbox_policies: set[str],
        permissions_presets: dict[str, dict[str, str]],
    ) -> None:
        self._owner = owner
        self._approval_policies = approval_policies
        self._sandbox_policies = sandbox_policies
        self._permissions_presets = permissions_presets

    def handle_init_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> None:
        owner = self._owner
        context = owner.bot.get_message_context(message_id) if message_id else {}
        provided_token = str(arg or "").strip()
        if not provided_token:
            owner._reply_text(
                chat_id,
                "用法：`/init <token>`\n`token` 默认保存在本机配置目录的 `init.token` 文件。",
                message_id=message_id,
            )
            return
        expected_token = ensure_init_token()
        if not compare_digest(provided_token, expected_token):
            owner._reply_text(
                chat_id,
                "初始化口令错误。请检查本机配置目录中的 `init.token`。",
                message_id=message_id,
            )
            return
        sender_open_id = str(context.get("sender_open_id", "") or "").strip()
        sender_user_id = str(context.get("sender_user_id", "") or "").strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        if not sender_open_id:
            owner._reply_text(
                chat_id,
                "初始化失败：当前消息上下文里没有发送者 `open_id`，暂时无法写入管理员配置。",
                message_id=message_id,
            )
            return
        sender_name = owner.bot.get_sender_display_name(
            user_id=sender_user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        config = load_system_config_raw()
        admin_open_ids = {
            str(item).strip()
            for item in config.get("admin_open_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        admin_open_ids.update(owner.bot.list_admin_open_ids())
        admin_added = sender_open_id not in admin_open_ids
        admin_open_ids.add(sender_open_id)
        configured_bot_open_id = str(config.get("bot_open_id", "") or "").strip()
        identity = owner.bot.get_bot_identity_snapshot()
        discovered_bot_open_id = str(identity.get("discovered_open_id", "") or "").strip()
        bot_open_id_written = False
        if discovered_bot_open_id and discovered_bot_open_id != configured_bot_open_id:
            configured_bot_open_id = discovered_bot_open_id
            bot_open_id_written = True

        updated_config = dict(config)
        updated_config["admin_open_ids"] = sorted(admin_open_ids)
        if configured_bot_open_id:
            updated_config["bot_open_id"] = configured_bot_open_id

        try:
            save_system_config(updated_config)
        except Exception as exc:
            logger.exception("保存初始化配置失败")
            owner._reply_text(chat_id, f"初始化失败：保存配置时出错：{exc}", message_id=message_id)
            return

        owner.bot.add_admin_open_id(sender_open_id)
        if configured_bot_open_id:
            owner.bot.set_configured_bot_open_id(configured_bot_open_id)

        lines = [
            "初始化结果：",
            (
                f"- admin_open_ids：已加入 `{sender_name}`"
                if admin_added
                else f"- admin_open_ids：`{sender_name}` 已在管理员列表中"
            ),
        ]
        if configured_bot_open_id:
            lines.append(
                f"- bot_open_id：`{configured_bot_open_id}`"
                + ("（本次已写入）" if bot_open_id_written else "（保持不变）")
            )
        else:
            lines.extend(
                [
                    "- bot_open_id：未写入",
                    "- 请检查 `application:application:self_manage` 权限后重试 `/init <token>`，或手动填写 `system.yaml.bot_open_id`。",
                ]
            )
        lines.append("- 当前命令只会更新管理员和 bot open id，不会改动 `trigger_open_ids`。")
        owner._reply_text(chat_id, "\n".join(lines), message_id=message_id)

    def handle_whoami_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        owner = self._owner
        context = owner.bot.get_message_context(message_id) if message_id else {}
        sender_user_id = str(context.get("sender_user_id", "")).strip()
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        name = owner.bot.get_sender_display_name(
            user_id=sender_user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        owner._reply_text(
            chat_id,
            "\n".join(
                [
                    "你的身份信息：",
                    f"- name: `{name}`",
                    f"- user_id: `{sender_user_id or '（空）'}`",
                    f"- open_id: `{sender_open_id or '（空）'}`",
                    "",
                    "配置管理员时，把 `open_id` 写进 `system.yaml` 的 `admin_open_ids`。",
                    "其中 `user_id` 仅用于排障；若未开 `contact:user.employee_id:readonly`，这里允许为空。",
                ]
            ),
            message_id=message_id,
        )

    def handle_botinfo_command(self, chat_id: str, *, message_id: str = "") -> None:
        owner = self._owner
        identity = owner.bot.get_bot_identity_snapshot()
        configured_open_id = str(identity.get("configured_open_id", "") or "").strip()
        discovered_open_id = str(identity.get("discovered_open_id", "") or "").strip()
        trigger_open_ids = [
            str(item).strip()
            for item in (identity.get("trigger_open_ids") or [])
            if str(item).strip()
        ]
        lines = [
            "机器人身份信息：",
            f"- app_id: `{identity.get('app_id', '') or '（空）'}`",
            f"- configured bot_open_id: `{configured_open_id or '（空）'}`",
            f"- discovered open_id: `{discovered_open_id or '（空）'}`",
            f"- runtime mention matching: `{'enabled' if configured_open_id else 'disabled'}`",
            f"- trigger_open_ids: `{', '.join(trigger_open_ids) or '（空）'}`",
            "- 运行时权威值：`system.yaml.bot_open_id`",
        ]
        if configured_open_id and discovered_open_id and configured_open_id != discovered_open_id:
            lines.extend(
                [
                    "",
                    "警告：",
                    "- 当前运行时仍只按 `system.yaml.bot_open_id` 判定 mention；实时探测值仅用于诊断和初始化。",
                    "- 当前配置值与实时探测值不一致，请优先核对 `system.yaml.bot_open_id` 是否写错。",
                ]
            )
        if not configured_open_id:
            lines.extend(
                [
                    "",
                    "建议：",
                    (
                        f"- 直接执行 `/init <token>` 自动写入，或手动把 `{discovered_open_id}` 写进 `system.yaml.bot_open_id`"
                        if discovered_open_id
                        else "- 先让 `/whoareyou` 能看到 `discovered open_id`，再手动写入 `system.yaml.bot_open_id`；如需自动写入，再执行 `/init <token>`"
                    ),
                    "- 运行时只有 `system.yaml.bot_open_id` 会参与群聊 mention 判定；`/whoareyou` 的实时探测结果不会自动生效。",
                    "- 如需让“别人 @你本人时由机器人代答”，再把对应人的 open_id 写进 `system.yaml.trigger_open_ids`",
                    "- 如果 `discovered open_id` 为空，检查 `application:application:self_manage` 权限",
                ]
            )
        owner._reply_text(chat_id, "\n".join(lines), message_id=message_id)

    def handle_profile_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        owner = self._owner
        runtime_config = owner._safe_read_runtime_config()
        if runtime_config is None:
            owner._reply_text(chat_id, "读取 Codex 运行时配置失败，无法查看或切换 profile。", message_id=message_id)
            return
        profile_resolution = owner._current_default_profile_resolution(runtime_config)
        local_profile = profile_resolution.effective_profile
        profiles = {profile.name: profile for profile in runtime_config.profiles}

        def _profile_provider_text(profile_name: str) -> str:
            if not profile_name:
                return "跟随 Codex 原生默认"
            profile = profiles.get(profile_name)
            if profile and profile.model_provider:
                return f"`{profile.model_provider}`"
            return "未显式设置，实际以新线程解析结果为准"

        if not arg:
            example_profile = runtime_config.profiles[0].name if runtime_config.profiles else "name"
            lines = [
                f"当前默认 profile：`{local_profile or '（未设置）'}`",
                f"默认 profile 对应 provider：{_profile_provider_text(local_profile)}",
                f"切换方式：`/profile <name>`，例如：`/profile {example_profile}`",
            ]
            if runtime_config.profiles:
                lines.extend(["", "**可用 profile**"])
                for profile in runtime_config.profiles:
                    provider = _profile_provider_text(profile.name)
                    marker = " <- 默认" if profile.name == local_profile else ""
                    lines.append(f"- `{profile.name}` -> {provider}{marker}")
            else:
                lines.append("未在当前 Codex 配置中发现可用 profile。")
            lines.extend(
                [
                    "",
                    "**说明**",
                    "作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。",
                    "已打开的 `fcodex` TUI 不会热切换。",
                    "如用 `fcodex -p <profile>`，以显式 profile 为准。",
                ]
            )
            if profile_resolution.stale_profile:
                lines.append(
                    f"注意：之前保存的默认 profile `{profile_resolution.stale_profile}` 已不存在，现已自动清空并回退到 Codex 原生默认。"
                )
            if owner._adapter_config.model_provider:
                lines.append(
                    "注意：当前 feishu-codex 配置写死了 "
                    f"`model_provider: {owner._adapter_config.model_provider}`，新建线程时可能仍以它为准。"
                )
            owner._reply_card(
                chat_id,
                build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
                message_id=message_id,
            )
            return

        target_profile = arg.strip()
        if target_profile not in profiles:
            owner._reply_text(
                chat_id,
                f"未找到 profile：`{target_profile}`\n用法：`/profile <name>`\n先发 `/profile` 查看可用 profile。",
                message_id=message_id,
            )
            return

        try:
            owner._profile_state.save_default_profile(target_profile)
        except Exception as exc:
            logger.exception("保存 feishu-codex 默认 profile 失败")
            owner._reply_text(chat_id, f"切换 profile 失败：{exc}", message_id=message_id)
            return

        state = owner._get_state(sender_id, chat_id, message_id)
        lines = [
            f"已切换默认 profile：`{target_profile}`",
            f"默认 profile 对应 provider：{_profile_provider_text(target_profile)}",
            "再次切换：`/profile <name>`",
        ]
        lines.extend(
            [
                "",
                "**说明**",
                "作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。",
            ]
        )
        if state["running"]:
            lines.append("如果当前正在执行，新 profile 从下一轮生效。")
        lines.append("已打开的 `fcodex` TUI 不会热切换。")
        lines.append("如用 `fcodex -p` 固定 profile，该会话不受影响。")
        if owner._adapter_config.model_provider:
            lines.append(
                "注意：当前 feishu-codex 配置写死了 "
                f"`model_provider: {owner._adapter_config.model_provider}`，新建线程时可能仍以它为准。"
            )
        owner._reply_card(
            chat_id,
            build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
            message_id=message_id,
        )

    def handle_approval_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in self._approval_policies:
                owner._reply_text(
                    chat_id,
                    "审批策略仅支持：`untrusted`、`on-failure`、`on-request`、`never`",
                    message_id=message_id,
                )
                return
            with owner._lock:
                state["approval_policy"] = policy
                running = state["running"]
            message = f"已切换审批策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            owner._reply_text(chat_id, message, message_id=message_id)
            return
        owner._reply_card(
            chat_id,
            build_approval_policy_card(state["approval_policy"], running=state["running"]),
            message_id=message_id,
        )

    def handle_sandbox_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in self._sandbox_policies:
                owner._reply_text(
                    chat_id,
                    "沙箱策略仅支持：`read-only`、`workspace-write`、`danger-full-access`",
                    message_id=message_id,
                )
                return
            with owner._lock:
                state["sandbox"] = policy
                running = state["running"]
            message = f"已切换沙箱策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            owner._reply_text(chat_id, message, message_id=message_id)
            return
        owner._reply_card(
            chat_id,
            build_sandbox_policy_card(state["sandbox"], running=state["running"]),
            message_id=message_id,
        )

    def handle_permissions_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id, message_id)
        if arg:
            preset = arg.strip().lower()
            config = self._permissions_presets.get(preset)
            if config is None:
                owner._reply_text(
                    chat_id,
                    "权限预设仅支持：`read-only`、`default`、`full-access`",
                    message_id=message_id,
                )
                return
            with owner._lock:
                state["approval_policy"] = config["approval_policy"]
                state["sandbox"] = config["sandbox"]
                running = state["running"]
            message = (
                f"已切换权限预设：`{config['label']}`\n"
                f"审批：`{config['approval_policy']}`\n"
                f"沙箱：`{config['sandbox']}`\n"
                "作用范围：只影响当前飞书会话的后续 turn。"
            )
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            owner._reply_text(chat_id, message, message_id=message_id)
            return
        owner._reply_card(
            chat_id,
            build_permissions_preset_card(
                state["approval_policy"],
                state["sandbox"],
                running=state["running"],
            ),
            message_id=message_id,
        )

    def handle_mode_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id, message_id)
        if arg:
            mode = arg.strip().lower()
            if mode not in {"default", "plan"}:
                owner._reply_text(chat_id, "协作模式仅支持：`default`、`plan`", message_id=message_id)
                return
            with owner._lock:
                state["collaboration_mode"] = mode
                running = state["running"]
            message = f"已切换协作模式：`{mode}`\n作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            owner._reply_text(chat_id, message, message_id=message_id)
            return
        owner._reply_card(
            chat_id,
            build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            ),
            message_id=message_id,
        )

    def handle_set_approval_policy(self, sender_id: str, chat_id: str, action_value: dict) -> dict:
        owner = self._owner
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in self._approval_policies:
            return owner.bot.make_card_response(toast="非法审批策略", toast_type="warning")
        state = owner._get_state(sender_id, chat_id)
        with owner._lock:
            state["approval_policy"] = policy
            running = state["running"]
        toast = f"已切换审批策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return owner.bot.make_card_response(
            card=build_approval_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_set_sandbox_policy(self, sender_id: str, chat_id: str, action_value: dict) -> dict:
        owner = self._owner
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in self._sandbox_policies:
            return owner.bot.make_card_response(toast="非法沙箱策略", toast_type="warning")
        state = owner._get_state(sender_id, chat_id)
        with owner._lock:
            state["sandbox"] = policy
            running = state["running"]
        toast = f"已切换沙箱策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return owner.bot.make_card_response(
            card=build_sandbox_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_set_permissions_preset(self, sender_id: str, chat_id: str, action_value: dict) -> dict:
        owner = self._owner
        preset = str(action_value.get("preset", "")).strip().lower()
        config = self._permissions_presets.get(preset)
        if config is None:
            return owner.bot.make_card_response(toast="非法权限预设", toast_type="warning")
        state = owner._get_state(sender_id, chat_id)
        with owner._lock:
            state["approval_policy"] = config["approval_policy"]
            state["sandbox"] = config["sandbox"]
            running = state["running"]
        toast = f"已切换权限预设：{config['label']}"
        if running:
            toast += "；下一轮生效"
        return owner.bot.make_card_response(
            card=build_permissions_preset_card(
                config["approval_policy"],
                config["sandbox"],
                running=running,
            ),
            toast=toast,
            toast_type="success",
        )

    def handle_set_collaboration_mode(self, sender_id: str, chat_id: str, action_value: dict) -> dict:
        owner = self._owner
        mode = str(action_value.get("mode", "")).strip().lower()
        if mode not in {"default", "plan"}:
            return owner.bot.make_card_response(toast="非法协作模式", toast_type="warning")
        state = owner._get_state(sender_id, chat_id)
        with owner._lock:
            state["collaboration_mode"] = mode
            running = state["running"]
        toast = f"已切换协作模式：{mode}"
        if running:
            toast += "；下一轮生效"
        return owner.bot.make_card_response(
            card=build_collaboration_mode_card(mode, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_show_permissions_card_action(self, sender_id: str, chat_id: str) -> dict:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id)
        return owner.bot.make_card_response(
            card=build_permissions_preset_card(
                state["approval_policy"],
                state["sandbox"],
                running=state["running"],
            )
        )

    def handle_show_mode_card_action(self, sender_id: str, chat_id: str) -> dict:
        owner = self._owner
        state = owner._get_state(sender_id, chat_id)
        return owner.bot.make_card_response(
            card=build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            )
        )
