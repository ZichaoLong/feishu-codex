"""
Codex settings domain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from secrets import compare_digest
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import RuntimeConfigSummary
from bot.cards import (
    CommandResult,
    build_approval_policy_card,
    build_collaboration_mode_card,
    build_profile_card,
    build_permissions_preset_card,
    build_sandbox_policy_card,
    make_card_response,
)
from bot.config import ensure_init_token, load_system_config_raw, save_system_config
from bot.profile_resolution import DefaultProfileResolution
from bot.runtime_view import RuntimeView

logger = logging.getLogger(__name__)

_UNSET = object()


@dataclass(frozen=True, slots=True)
class SettingsDomainPorts:
    get_message_context: Callable[[str], dict[str, Any]]
    get_sender_display_name: Callable[..., str]
    get_bot_identity_snapshot: Callable[[], dict[str, Any]]
    add_admin_open_id: Callable[[str], None]
    set_configured_bot_open_id: Callable[[str], None]
    save_default_profile: Callable[[str], None]
    adapter_model_provider: str
    get_runtime_view: Callable[[str, str, str], RuntimeView]
    update_runtime_settings: Callable[..., None]
    safe_read_runtime_config: Callable[[], RuntimeConfigSummary | None]
    current_default_profile_resolution: Callable[[RuntimeConfigSummary | None], DefaultProfileResolution]


class CodexSettingsDomain:
    def __init__(
        self,
        *,
        ports: SettingsDomainPorts,
        approval_policies: set[str],
        sandbox_policies: set[str],
        permissions_presets: dict[str, dict[str, str]],
    ) -> None:
        self._ports = ports
        self._approval_policies = approval_policies
        self._sandbox_policies = sandbox_policies
        self._permissions_presets = permissions_presets

    def _runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView:
        return self._ports.get_runtime_view(sender_id, chat_id, message_id)

    def _update_runtime_settings(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        approval_policy: Any = _UNSET,
        sandbox: Any = _UNSET,
        collaboration_mode: Any = _UNSET,
    ) -> None:
        changes: dict[str, Any] = {"message_id": message_id}
        if approval_policy is not _UNSET:
            changes["approval_policy"] = approval_policy
        if sandbox is not _UNSET:
            changes["sandbox"] = sandbox
        if collaboration_mode is not _UNSET:
            changes["collaboration_mode"] = collaboration_mode
        self._ports.update_runtime_settings(sender_id, chat_id, **changes)

    def handle_init_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        del sender_id, chat_id
        ports = self._ports
        context = ports.get_message_context(message_id) if message_id else {}
        provided_token = str(arg or "").strip()
        if not provided_token:
            return CommandResult(text="用法：`/init <token>`\n`token` 默认保存在本机配置目录的 `init.token` 文件。")
        expected_token = ensure_init_token()
        if not compare_digest(provided_token, expected_token):
            return CommandResult(text="初始化口令错误。请检查本机配置目录中的 `init.token`。")
        sender_open_id = str(context.get("sender_open_id", "") or "").strip()
        sender_user_id = str(context.get("sender_user_id", "") or "").strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        if not sender_open_id:
            return CommandResult(text="初始化失败：当前消息上下文里没有发送者 `open_id`，暂时无法写入管理员配置。")
        sender_name = ports.get_sender_display_name(
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
        admin_added = sender_open_id not in admin_open_ids
        admin_open_ids.add(sender_open_id)
        configured_bot_open_id = str(config.get("bot_open_id", "") or "").strip()
        identity = ports.get_bot_identity_snapshot()
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
            return CommandResult(text=f"初始化失败：保存配置时出错：{exc}")

        ports.add_admin_open_id(sender_open_id)
        if configured_bot_open_id:
            ports.set_configured_bot_open_id(configured_bot_open_id)

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
        return CommandResult(text="\n".join(lines))

    def handle_whoami_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        del sender_id, chat_id
        ports = self._ports
        context = ports.get_message_context(message_id) if message_id else {}
        sender_user_id = str(context.get("sender_user_id", "")).strip()
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        name = ports.get_sender_display_name(
            user_id=sender_user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        return CommandResult(text="\n".join(
            [
                "你的身份信息：",
                f"- name: `{name}`",
                f"- user_id: `{sender_user_id or '（空）'}`",
                f"- open_id: `{sender_open_id or '（空）'}`",
                "",
                "配置管理员时，把 `open_id` 写进 `system.yaml` 的 `admin_open_ids`。",
                "其中 `user_id` 仅用于排障；若未开 `contact:user.employee_id:readonly`，这里允许为空。",
            ]
        ))

    def handle_botinfo_command(self, chat_id: str, *, message_id: str = "") -> CommandResult:
        del chat_id, message_id
        identity = self._ports.get_bot_identity_snapshot()
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
        return CommandResult(text="\n".join(lines))

    def handle_profile_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        ports = self._ports
        runtime_config = ports.safe_read_runtime_config()
        if runtime_config is None:
            return CommandResult(text="读取 Codex 运行时配置失败，无法查看或切换 profile。")
        profile_resolution = ports.current_default_profile_resolution(runtime_config)
        local_profile = profile_resolution.effective_profile
        profiles = {profile.name: profile for profile in runtime_config.profiles}
        profile_names = [profile.name for profile in runtime_config.profiles if profile.name]

        def _profile_provider_text(profile_name: str) -> str:
            if not profile_name:
                return "跟随 Codex 原生默认"
            profile = profiles.get(profile_name)
            if profile and profile.model_provider:
                return f"`{profile.model_provider}`"
            return "未显式设置，实际以新线程解析结果为准"

        def _build_profile_summary_card(
            *,
            leading_lines: list[str] | None = None,
            current_profile: str,
        ) -> dict:
            lines = list(leading_lines or [])
            lines.extend(
                [
                    f"当前默认 profile：`{current_profile or '（未设置）'}`",
                    f"默认 profile 对应 provider：{_profile_provider_text(current_profile)}",
                ]
            )
            if profile_names:
                lines.extend(
                    [
                        "切换方式：发送 `/profile <name>`，或直接点下面按钮。",
                        "",
                        "**可用 profile**",
                    ]
                )
                for profile_name in profile_names:
                    provider = _profile_provider_text(profile_name)
                    marker = " <- 默认" if profile_name == current_profile else ""
                    lines.append(f"- `{profile_name}` -> {provider}{marker}")
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
            if ports.adapter_model_provider:
                lines.append(
                    "注意：当前 feishu-codex 配置写死了 "
                    f"`model_provider: {ports.adapter_model_provider}`，新建线程时可能仍以它为准。"
                )
            return build_profile_card(
                content="\n".join(lines),
                profile_names=profile_names,
                current_profile=current_profile,
            )

        if not arg:
            return CommandResult(card=_build_profile_summary_card(current_profile=local_profile))

        target_profile = arg.strip()
        if target_profile not in profiles:
            return CommandResult(
                text=f"未找到 profile：`{target_profile}`\n用法：`/profile <name>`\n先发 `/profile` 查看可用 profile。"
            )

        try:
            ports.save_default_profile(target_profile)
        except Exception as exc:
            logger.exception("保存 feishu-codex 默认 profile 失败")
            return CommandResult(text=f"切换 profile 失败：{exc}")

        runtime = self._runtime_view(sender_id, chat_id, message_id)
        lines = [f"已切换默认 profile：`{target_profile}`"]
        if runtime.running:
            lines.append("如果当前正在执行，新 profile 从下一轮生效。")
        return CommandResult(card=_build_profile_summary_card(
            leading_lines=lines + [""],
            current_profile=target_profile,
        ))

    def handle_approval_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in self._approval_policies:
                return CommandResult(text="审批策略仅支持：`untrusted`、`on-failure`、`on-request`、`never`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                approval_policy=policy,
            )
            running = runtime.running
            message = f"已切换审批策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(card=build_approval_policy_card(runtime.approval_policy, running=runtime.running))

    def handle_sandbox_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in self._sandbox_policies:
                return CommandResult(text="沙箱策略仅支持：`read-only`、`workspace-write`、`danger-full-access`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                sandbox=policy,
            )
            running = runtime.running
            message = f"已切换沙箱策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(card=build_sandbox_policy_card(runtime.sandbox, running=runtime.running))

    def handle_permissions_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        if arg:
            preset = arg.strip().lower()
            config = self._permissions_presets.get(preset)
            if config is None:
                return CommandResult(text="权限预设仅支持：`read-only`、`default`、`full-access`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                approval_policy=config["approval_policy"],
                sandbox=config["sandbox"],
            )
            running = runtime.running
            message = (
                f"已切换权限预设：`{config['label']}`\n"
                f"审批：`{config['approval_policy']}`\n"
                f"沙箱：`{config['sandbox']}`\n"
                "作用范围：只影响当前飞书会话的后续 turn。"
            )
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(card=build_permissions_preset_card(
            runtime.approval_policy,
            runtime.sandbox,
            running=runtime.running,
        ))

    def handle_mode_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        if arg:
            mode = arg.strip().lower()
            if mode not in {"default", "plan"}:
                return CommandResult(text="协作模式仅支持：`default`、`plan`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                collaboration_mode=mode,
            )
            running = runtime.running
            message = f"已切换协作模式：`{mode}`\n作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(card=build_collaboration_mode_card(
            runtime.collaboration_mode,
            running=runtime.running,
        ))

    def handle_set_approval_policy(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in self._approval_policies:
            return make_card_response(toast="非法审批策略", toast_type="warning")
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            approval_policy=policy,
        )
        running = runtime.running
        toast = f"已切换审批策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return make_card_response(
            card=build_approval_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_set_sandbox_policy(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in self._sandbox_policies:
            return make_card_response(toast="非法沙箱策略", toast_type="warning")
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            sandbox=policy,
        )
        running = runtime.running
        toast = f"已切换沙箱策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return make_card_response(
            card=build_sandbox_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_set_permissions_preset(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        preset = str(action_value.get("preset", "")).strip().lower()
        config = self._permissions_presets.get(preset)
        if config is None:
            return make_card_response(toast="非法权限预设", toast_type="warning")
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            approval_policy=config["approval_policy"],
            sandbox=config["sandbox"],
        )
        running = runtime.running
        toast = f"已切换权限预设：{config['label']}"
        if running:
            toast += "；下一轮生效"
        return make_card_response(
            card=build_permissions_preset_card(
                config["approval_policy"],
                config["sandbox"],
                running=running,
            ),
            toast=toast,
            toast_type="success",
        )

    def handle_set_collaboration_mode(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        mode = str(action_value.get("mode", "")).strip().lower()
        if mode not in {"default", "plan"}:
            return make_card_response(toast="非法协作模式", toast_type="warning")
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            collaboration_mode=mode,
        )
        running = runtime.running
        toast = f"已切换协作模式：{mode}"
        if running:
            toast += "；下一轮生效"
        return make_card_response(
            card=build_collaboration_mode_card(mode, running=running),
            toast=toast,
            toast_type="success",
        )

    def handle_show_permissions_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
    ) -> P2CardActionTriggerResponse:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        return make_card_response(
            card=build_permissions_preset_card(
                runtime.approval_policy,
                runtime.sandbox,
                running=runtime.running,
            )
        )

    def handle_show_mode_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
    ) -> P2CardActionTriggerResponse:
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        return make_card_response(
            card=build_collaboration_mode_card(
                runtime.collaboration_mode,
                running=runtime.running,
            )
        )

    def handle_set_profile(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        target_profile = str(action_value.get("profile", "")).strip()
        if not target_profile:
            return make_card_response(toast="缺少 profile 名称", toast_type="warning")
        result = self.handle_profile_command(sender_id, chat_id, target_profile, message_id=message_id)
        if result.card is None:
            return make_card_response(toast=result.text or "切换 profile 失败", toast_type="warning")
        toast = f"已切换默认 profile：{target_profile}"
        runtime = self._runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            toast += "；下一轮生效"
        return make_card_response(
            card=result.card,
            toast=toast,
            toast_type="success",
        )
