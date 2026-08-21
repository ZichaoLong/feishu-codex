"""
Codex settings domain.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from secrets import compare_digest
from typing import Any, Literal

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import RuntimeModelSummary
from bot.approval_settings_cards import (
    BINDING_SAFETY_BASELINE_SCOPE_TEXT,
    build_approval_policy_card,
    build_permissions_profile_card,
)
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.cards import (
    BINDING_OPTIONAL_OVERRIDE_SCOPE_TEXT,
    CommandResult,
    build_model_effort_card,
    make_card_response,
)
from bot.config import ensure_init_token, load_system_config_raw, save_system_config
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.system_config import SystemConfig
from bot.permissions_profile import PERMISSION_PROFILE_CHOICES

logger = logging.getLogger(__name__)

_UNSET = object()
_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
_MODEL_WITH_NAME_COMMAND = feishu_visible_command_syntax("/model <name|auto>")
_EFFORT_WITH_NAME_COMMAND = feishu_visible_command_syntax("/effort <auto|value>")
_MODEL_AUTO = "auto"
_EFFORT_AUTO = "auto"
_CANONICAL_REASONING_EFFORT_VALUES = frozenset(
    ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
)
ModelEffortValidationState = Literal["validated", "deferred", "rejected"]


@dataclass(frozen=True, slots=True)
class ModelEffortValidation:
    state: ModelEffortValidationState
    model: str
    reasoning_effort: str
    model_summary: RuntimeModelSummary | None


@dataclass(frozen=True, slots=True)
class SettingsDomainPorts:
    get_message_context: Callable[[str], dict[str, Any]]
    get_sender_display_name: Callable[..., str]
    debug_sender_name_resolution: Callable[[str], dict[str, Any]]
    get_bot_identity_snapshot: Callable[[], dict[str, Any]]
    add_admin_open_id: Callable[[str], None]
    set_configured_bot_open_id: Callable[[str], None]
    resolve_session: Callable[[str, str, str], BindingSessionSnapshot]
    list_models: Callable[[], list[RuntimeModelSummary]]
    update_runtime_settings: Callable[..., None]


class CodexSettingsDomain:
    def __init__(
        self,
        *,
        ports: SettingsDomainPorts,
        approval_policies: set[str],
    ) -> None:
        self._ports = ports
        self._approval_policies = approval_policies

    def _session(self, sender_id: str, chat_id: str, message_id: str = "") -> BindingSessionSnapshot:
        return self._ports.resolve_session(sender_id, chat_id, message_id)

    def _update_runtime_settings(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        approval_policy: Any = _UNSET,
        permissions_profile_id: Any = _UNSET,
        model: Any = _UNSET,
        reasoning_effort: Any = _UNSET,
    ) -> None:
        changes: dict[str, Any] = {"message_id": message_id}
        if approval_policy is not _UNSET:
            changes["approval_policy"] = approval_policy
        if permissions_profile_id is not _UNSET:
            changes["permissions_profile_id"] = permissions_profile_id
        if model is not _UNSET:
            changes["model"] = model
        if reasoning_effort is not _UNSET:
            changes["reasoning_effort"] = reasoning_effort
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
            return CommandResult(text=f"用法：`{_INIT_COMMAND}`\n`token` 默认保存在本机配置目录的 `init.token` 文件。")
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
        try:
            config = load_system_config_raw()
            validated_config = SystemConfig.from_dict(
                config,
                require_credentials=True,
            )
        except Exception as exc:
            logger.exception("读取初始化配置失败")
            return CommandResult(text=f"初始化失败：system.yaml 配置无效：{exc}")

        admin_open_ids = set(validated_config.admin_open_ids)
        admin_added = sender_open_id not in admin_open_ids
        admin_open_ids.add(sender_open_id)
        configured_bot_open_id = validated_config.bot_open_id
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
                    f"- 请检查 `application:application:self_manage` 权限后重试 `{_INIT_COMMAND}`，或手动填写 `system.yaml.bot_open_id`。",
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

    def handle_debug_contact_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        del sender_id, chat_id, message_id
        normalized_open_id = str(arg or "").strip()
        if not normalized_open_id:
            return CommandResult(
                text=f"用法：`{_DEBUG_CONTACT_COMMAND}`\n用于排查联系人接口名字解析、缓存命中与 fallback 原因。"
            )
        snapshot = self._ports.debug_sender_name_resolution(normalized_open_id)
        cache_state = "hit" if snapshot.get("cache_hit") else "miss"
        lines = [
            "联系人解析诊断：",
            f"- open_id: `{snapshot.get('open_id', '') or '（空）'}`",
            f"- cache: `{cache_state}`",
            f"- cached_name: `{snapshot.get('cached_name', '') or '（空）'}`",
            f"- resolved_name: `{snapshot.get('resolved_name', '') or '（空）'}`",
            f"- source: `{snapshot.get('source', '') or '（空）'}`",
            f"- used_fallback: `{'yes' if snapshot.get('used_fallback') else 'no'}`",
        ]
        fallback_reason = str(snapshot.get("fallback_reason", "") or "").strip()
        if fallback_reason:
            lines.append(f"- fallback_reason: `{fallback_reason}`")
        api_code = snapshot.get("api_code")
        if api_code not in ("", None):
            lines.append(f"- api_code: `{api_code}`")
        api_msg = str(snapshot.get("api_msg", "") or "").strip()
        if api_msg:
            lines.append(f"- api_msg: `{api_msg}`")
        exception_text = str(snapshot.get("exception", "") or "").strip()
        if exception_text:
            lines.append(f"- exception: `{exception_text}`")
        lines.extend(
            [
                "",
                "排查提示：",
                "- 如需 `/whoami`、群授权卡片、群上下文显示可读名字，确认已开 `contact:contact.base:readonly`、`contact:user.base:readonly`。",
                "- 若这里只能 fallback 到 open_id 前缀，请先检查通讯录权限、应用可用范围，以及目标成员是否仍在可见范围内。",
            ]
        )
        return CommandResult(text="\n".join(lines))

    def handle_bot_status_command(self, chat_id: str, *, message_id: str = "") -> CommandResult:
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
                        f"- 直接执行 `{_INIT_COMMAND}` 自动写入，或手动把 `{discovered_open_id}` 写进 `system.yaml.bot_open_id`"
                        if discovered_open_id
                        else f"- 先让 `/bot-status` 能看到 `discovered open_id`，再手动写入 `system.yaml.bot_open_id`；如需自动写入，再执行 `{_INIT_COMMAND}`"
                    ),
                    "- 运行时只有 `system.yaml.bot_open_id` 会参与群聊 mention 判定；`/bot-status` 的实时探测结果不会自动生效。",
                    "- 如需让“别人 @你本人时由机器人代答”，再把对应人的 open_id 写进 `system.yaml.trigger_open_ids`",
                    "- 如果 `discovered open_id` 为空，检查 `application:application:self_manage` 权限",
                ]
            )
        return CommandResult(text="\n".join(lines))

    @staticmethod
    def _runtime_model_display_text(model: str) -> str:
        normalized = str(model or "").strip()
        return normalized or _MODEL_AUTO

    @staticmethod
    def _runtime_effort_display_text(reasoning_effort: str) -> str:
        normalized = str(reasoning_effort or "").strip()
        return normalized or _EFFORT_AUTO

    @staticmethod
    def _normalize_model_override(target: str) -> str:
        normalized_target = str(target or "").strip()
        if normalized_target.lower() == _MODEL_AUTO:
            return ""
        return normalized_target

    @staticmethod
    def _normalize_reasoning_effort_override(target: str) -> str:
        normalized_target = str(target or "").strip()
        if not normalized_target:
            raise ValueError("reasoning effort 不能为空")
        canonical_target = normalized_target.lower()
        if canonical_target == _EFFORT_AUTO:
            return ""
        if canonical_target in _CANONICAL_REASONING_EFFORT_VALUES:
            return canonical_target
        return normalized_target

    def _list_models(self) -> list[RuntimeModelSummary]:
        try:
            return list(self._ports.list_models())
        except Exception as exc:
            logger.warning("读取 Codex model metadata 失败；按 metadata unavailable 处理：%s", exc)
            return []

    @staticmethod
    def _classify_model_effort(
        *,
        model: str,
        reasoning_effort: str,
        models: list[RuntimeModelSummary],
    ) -> ModelEffortValidation:
        normalized_model = str(model or "").strip()
        normalized_effort = str(reasoning_effort or "").strip()
        model_summary = next(
            (item for item in models if str(item.model or "").strip() == normalized_model),
            None,
        )
        if not normalized_effort:
            state: ModelEffortValidationState = "validated"
        elif not normalized_model:
            state = "deferred"
        elif model_summary is None or model_summary.supported_reasoning_efforts is None:
            state = "deferred"
        elif any(
            option.reasoning_effort == normalized_effort
            for option in model_summary.supported_reasoning_efforts
        ):
            state = "validated"
        else:
            state = "rejected"
        return ModelEffortValidation(
            state=state,
            model=normalized_model,
            reasoning_effort=normalized_effort,
            model_summary=model_summary,
        )

    @staticmethod
    def _model_effort_rejection_text(validation: ModelEffortValidation) -> str:
        model_summary = validation.model_summary
        supported_options = (
            model_summary.supported_reasoning_efforts
            if model_summary is not None
            else None
        )
        supported_values = [
            option.reasoning_effort
            for option in (supported_options or [])
            if option.reasoning_effort
        ]
        choices = "、".join(f"`{value}`" for value in (_EFFORT_AUTO, *supported_values))
        return (
            f"model `{validation.model}` 的 metadata 未声明支持 effort "
            f"`{validation.reasoning_effort}`；可选：{choices}。"
        )

    @classmethod
    def _model_effort_validation_text(cls, validation: ModelEffortValidation) -> str:
        if validation.state == "rejected":
            return cls._model_effort_rejection_text(validation)
        if not validation.reasoning_effort:
            return "effort 为 `auto`，Focus 不发送 effort 字段。"
        if validation.state == "validated":
            return "当前 model metadata 明确支持这个 effort。"
        if not validation.model:
            return "model 为 `auto`，Focus 不解析实际 model；显式 effort 将原样交给 app-server。"
        return "当前 model 没有可用的 effort metadata；显式 effort 将原样交给 app-server。"

    def _build_model_effort_summary_card(
        self,
        *,
        runtime: BindingSessionSnapshot,
        models: list[RuntimeModelSummary] | None = None,
    ) -> dict:
        available_models = self._list_models() if models is None else models
        validation = self._classify_model_effort(
            model=runtime.model,
            reasoning_effort=runtime.reasoning_effort,
            models=available_models,
        )
        current_thread_id = str(runtime.current_thread_id or "").strip()
        lines = [
            f"model override: `{self._runtime_model_display_text(runtime.model)}`",
            f"effort override: `{self._runtime_effort_display_text(runtime.reasoning_effort)}`",
            f"validation: `{validation.state}`",
            "",
            self._model_effort_validation_text(validation),
            BINDING_OPTIONAL_OVERRIDE_SCOPE_TEXT,
            "`auto` 的精确定义是 Focus 不发送对应字段；它不会主动恢复 backend 或 model 默认值。",
        ]
        if (
            validation.model_summary is not None
            and validation.model_summary.default_reasoning_effort
        ):
            lines.append(
                "model metadata default effort: "
                f"`{validation.model_summary.default_reasoning_effort}`"
                "（仅供参考；auto 仍然不发送字段）。"
            )
        if current_thread_id:
            lines.extend(["", f"当前 thread：`{current_thread_id[:8]}…`"])
        supported_reasoning_efforts = (
            None
            if validation.model_summary is None
            or validation.model_summary.supported_reasoning_efforts is None
            else [
                (option.reasoning_effort, option.description)
                for option in validation.model_summary.supported_reasoning_efforts
            ]
        )
        return build_model_effort_card(
            current_model=runtime.model,
            current_reasoning_effort=runtime.reasoning_effort,
            content="\n".join(lines),
            supported_reasoning_efforts=supported_reasoning_efforts,
            reasoning_effort_conflict=(
                self._model_effort_rejection_text(validation)
                if validation.state == "rejected"
                else ""
            ),
            running=runtime.running,
        )

    def handle_model_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._session(sender_id, chat_id, message_id)
        models = self._list_models()
        if not arg:
            return CommandResult(card=self._build_model_effort_summary_card(runtime=runtime, models=models))
        target = str(arg or "").strip()
        if not target:
            return CommandResult(text=f"用法：`{_MODEL_WITH_NAME_COMMAND}`")
        desired_model = self._normalize_model_override(target)
        validation = self._classify_model_effort(
            model=desired_model,
            reasoning_effort=runtime.reasoning_effort,
            models=models,
        )
        if validation.state == "rejected":
            return CommandResult(text=f"未保存 model override：{self._model_effort_rejection_text(validation)}")
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            model=desired_model,
        )
        if str(runtime.model or "").strip() == desired_model:
            label = self._runtime_model_display_text(desired_model)
            message = f"当前会话的 model override 已是：`{label}`"
        else:
            label = self._runtime_model_display_text(desired_model)
            message = f"已切换当前会话的 model override：`{label}`"
        message += f"\nvalidation: `{validation.state}`\n{BINDING_OPTIONAL_OVERRIDE_SCOPE_TEXT}"
        if runtime.running:
            message += "\n如果当前正在执行，新设置从下一轮生效。"
        return CommandResult(text=message)

    def handle_effort_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._session(sender_id, chat_id, message_id)
        models = self._list_models()
        if not arg:
            return CommandResult(card=self._build_model_effort_summary_card(runtime=runtime, models=models))
        target = str(arg or "").strip()
        if not target:
            return CommandResult(text=f"用法：`{_EFFORT_WITH_NAME_COMMAND}`")
        try:
            desired_effort = self._normalize_reasoning_effort_override(target)
        except ValueError as exc:
            return CommandResult(text=f"非法 reasoning effort：`{target}`\n用法：`{_EFFORT_WITH_NAME_COMMAND}`\n{exc}")
        validation = self._classify_model_effort(
            model=runtime.model,
            reasoning_effort=desired_effort,
            models=models,
        )
        if validation.state == "rejected":
            return CommandResult(text=f"未保存 effort override：{self._model_effort_rejection_text(validation)}")
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            reasoning_effort=desired_effort,
        )
        if str(runtime.reasoning_effort or "").strip() == desired_effort:
            label = self._runtime_effort_display_text(desired_effort)
            message = f"当前会话的 effort override 已是：`{label}`"
        else:
            label = self._runtime_effort_display_text(desired_effort)
            message = f"已切换当前会话的 effort override：`{label}`"
        message += f"\nvalidation: `{validation.state}`\n{BINDING_OPTIONAL_OVERRIDE_SCOPE_TEXT}"
        if desired_effort == "ultra":
            message += "\n提示：Focus 会把 `ultra` 原样发送给 Codex，由上游走原生 Ultra 路径。"
        if runtime.running:
            message += "\n如果当前正在执行，新设置从下一轮生效。"
        return CommandResult(text=message)

    def handle_approval_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._session(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in self._approval_policies:
                return CommandResult(text="审批策略仅支持：`untrusted`、`on-request`、`never`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                approval_policy=policy,
            )
            running = runtime.running
            message = f"已切换审批策略：`{policy}`\n{BINDING_SAFETY_BASELINE_SCOPE_TEXT}"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(card=build_approval_policy_card(runtime.approval_policy, running=runtime.running))

    def handle_permissions_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._session(sender_id, chat_id, message_id)
        if arg:
            choice = arg.strip().lower()
            config = PERMISSION_PROFILE_CHOICES.get(choice)
            if config is None:
                return CommandResult(text="权限基线仅支持：`read-only`、`workspace`、`danger-full-access`")
            self._update_runtime_settings(
                sender_id,
                chat_id,
                message_id=message_id,
                permissions_profile_id=config["profile_id"],
            )
            running = runtime.running
            message = (
                f"已切换权限基线：`{config['label']}`\n"
                f"Profile ID：`{config['profile_id']}`\n"
                "它只决定执行边界；是否需要停下来审批，仍由 `/approval` 单独控制。\n"
                f"{BINDING_SAFETY_BASELINE_SCOPE_TEXT}"
            )
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            return CommandResult(text=message)
        return CommandResult(
            card=build_permissions_profile_card(
                runtime.permissions_profile_id,
                running=runtime.running,
            )
        )

    def _model_effort_rejection_response(
        self,
        *,
        runtime: BindingSessionSnapshot,
        models: list[RuntimeModelSummary],
        validation: ModelEffortValidation,
    ) -> P2CardActionTriggerResponse:
        return make_card_response(
            card=self._build_model_effort_summary_card(runtime=runtime, models=models),
            toast=f"未保存：{self._model_effort_rejection_text(validation)}",
            toast_type="warning",
        )

    def _apply_model_card_update(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        desired_model: str,
    ) -> P2CardActionTriggerResponse:
        runtime = self._session(sender_id, chat_id, message_id)
        models = self._list_models()
        validation = self._classify_model_effort(
            model=desired_model,
            reasoning_effort=runtime.reasoning_effort,
            models=models,
        )
        if validation.state == "rejected":
            return self._model_effort_rejection_response(
                runtime=runtime,
                models=models,
                validation=validation,
            )
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            model=desired_model,
        )
        toast = f"已切换 model override：{self._runtime_model_display_text(desired_model)}"
        if validation.state == "deferred":
            toast += "；validation: deferred"
        if runtime.running:
            toast += "；下一轮生效"
        return make_card_response(
            card=self._build_model_effort_summary_card(
                runtime=self._session(sender_id, chat_id, message_id),
                models=models,
            ),
            toast=toast,
            toast_type="success",
        )

    def _apply_reasoning_effort_card_update(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        desired_effort: str,
    ) -> P2CardActionTriggerResponse:
        runtime = self._session(sender_id, chat_id, message_id)
        models = self._list_models()
        validation = self._classify_model_effort(
            model=runtime.model,
            reasoning_effort=desired_effort,
            models=models,
        )
        if validation.state == "rejected":
            return self._model_effort_rejection_response(
                runtime=runtime,
                models=models,
                validation=validation,
            )
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            reasoning_effort=desired_effort,
        )
        toast = f"已切换 effort override：{self._runtime_effort_display_text(desired_effort)}"
        if validation.state == "deferred":
            toast += "；validation: deferred"
        if runtime.running:
            toast += "；下一轮生效"
        return make_card_response(
            card=self._build_model_effort_summary_card(
                runtime=self._session(sender_id, chat_id, message_id),
                models=models,
            ),
            toast=toast,
            toast_type="success",
        )

    def handle_set_model(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        desired_model = self._normalize_model_override(
            str(action_value.get("model", "") or "")
        )
        return self._apply_model_card_update(
            sender_id,
            chat_id,
            message_id,
            desired_model,
        )

    def handle_submit_model_override(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        form_value = action_value.get("_form_value") or {}
        if not isinstance(form_value, dict):
            return make_card_response(toast="表单缺少 model 输入。", toast_type="warning")
        submitted = str(form_value.get("model_override", "") or "").strip()
        if not submitted:
            return make_card_response(toast="请输入 model 名称；如需清除 override，请点 auto。", toast_type="warning")
        return self._apply_model_card_update(
            sender_id,
            chat_id,
            message_id,
            self._normalize_model_override(submitted),
        )

    def resolve_runtime_settings_form_submit_payload(self, action_value: dict[str, Any]) -> dict[str, str] | None:
        form_value = action_value.get("_form_value") or {}
        if not isinstance(form_value, dict) or not form_value:
            return None
        if "model_override" in form_value:
            return {"action": "submit_model_override"}
        if "reasoning_effort_override" in form_value:
            return {"action": "submit_reasoning_effort_override"}
        return None

    def handle_set_reasoning_effort(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        target_effort = str(action_value.get("reasoning_effort", "") or "").strip()
        try:
            desired_effort = self._normalize_reasoning_effort_override(target_effort or _EFFORT_AUTO)
        except ValueError as exc:
            return make_card_response(toast=str(exc), toast_type="warning")
        return self._apply_reasoning_effort_card_update(
            sender_id,
            chat_id,
            message_id,
            desired_effort,
        )

    def handle_submit_reasoning_effort_override(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        form_value = action_value.get("_form_value") or {}
        if not isinstance(form_value, dict):
            return make_card_response(toast="表单缺少 effort 输入。", toast_type="warning")
        submitted = str(form_value.get("reasoning_effort_override", "") or "").strip()
        if not submitted:
            return make_card_response(toast="请输入 effort；如需清除 override，请点 auto。", toast_type="warning")
        try:
            desired_effort = self._normalize_reasoning_effort_override(submitted)
        except ValueError as exc:
            return make_card_response(toast=str(exc), toast_type="warning")
        return self._apply_reasoning_effort_card_update(
            sender_id,
            chat_id,
            message_id,
            desired_effort,
        )

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
        runtime = self._session(sender_id, chat_id, message_id)
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

    def handle_set_permissions_profile(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        choice = str(action_value.get("profile", "")).strip().lower()
        config = PERMISSION_PROFILE_CHOICES.get(choice)
        if config is None:
            return make_card_response(toast="非法权限基线", toast_type="warning")
        runtime = self._session(sender_id, chat_id, message_id)
        self._update_runtime_settings(
            sender_id,
            chat_id,
            message_id=message_id,
            permissions_profile_id=config["profile_id"],
        )
        running = runtime.running
        toast = f"已切换权限基线：{config['label']}"
        if running:
            toast += "；下一轮生效"
        return make_card_response(
            card=build_permissions_profile_card(
                config["profile_id"],
                running=running,
            ),
            toast=toast,
            toast_type="success",
        )
