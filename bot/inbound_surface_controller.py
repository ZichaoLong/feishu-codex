from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import CommandResult, build_markdown_card, make_card_response


@dataclass(frozen=True)
class CommandRoute:
    handler: Callable[[str, str, str, str], CommandResult | None]
    scope: str = "any"
    admin_only_in_group: bool = True
    scope_denied_text: str = ""


@dataclass(frozen=True)
class ActionRoute:
    handler: Callable[[str, str, str, dict[str, Any]], P2CardActionTriggerResponse]
    group_guard: str = "none"


@dataclass(frozen=True)
class CommandExecution:
    result: CommandResult | None = None
    error_text: str = ""


class InboundSurfaceController:
    def __init__(
        self,
        *,
        keyword: str,
        activate_binding_if_needed: Callable[[str, str, str], None],
        help_reply: Callable[[str, str], CommandResult],
        handle_prompt: Callable[[str, str, str, str], None],
        reply_text: Callable[..., None],
        reply_card: Callable[..., None],
        resolve_chat_type: Callable[[str, str], str],
        group_command_admin_denial_text: Callable[[str, str, str], str],
        is_group_chat: Callable[[str, str], bool],
        is_group_admin_actor: Callable[..., bool],
        is_group_turn_actor: Callable[..., bool],
        is_group_request_actor_or_admin: Callable[..., bool],
        handle_rename_form_fallback: Callable[..., P2CardActionTriggerResponse | None],
        handle_user_input_form_fallback: Callable[..., P2CardActionTriggerResponse | None],
    ) -> None:
        self._keyword = keyword
        self._activate_binding_if_needed = activate_binding_if_needed
        self._help_reply = help_reply
        self._handle_prompt = handle_prompt
        self._reply_text = reply_text
        self._reply_card = reply_card
        self._resolve_chat_type = resolve_chat_type
        self._group_command_admin_denial_text = group_command_admin_denial_text
        self._is_group_chat = is_group_chat
        self._is_group_admin_actor = is_group_admin_actor
        self._is_group_turn_actor = is_group_turn_actor
        self._is_group_request_actor_or_admin = is_group_request_actor_or_admin
        self._handle_rename_form_fallback = handle_rename_form_fallback
        self._handle_user_input_form_fallback = handle_user_input_form_fallback
        self._command_routes: dict[str, CommandRoute] = {}
        self._action_routes: dict[str, ActionRoute] = {}
        self._prefixed_action_routes: list[tuple[str, ActionRoute]] = []

    def install_routes(
        self,
        *,
        command_routes: dict[str, CommandRoute],
        action_routes: dict[str, ActionRoute],
        prefixed_action_routes: list[tuple[str, ActionRoute]],
    ) -> None:
        self._command_routes = dict(command_routes)
        self._action_routes = dict(action_routes)
        self._prefixed_action_routes = list(prefixed_action_routes)

    def has_command_route(self, command_name: str) -> bool:
        return str(command_name or "").strip().lower() in self._command_routes

    def handle_message(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        cleaned = (text or "").strip()
        self._activate_binding_if_needed(sender_id, chat_id, message_id)

        if not cleaned or cleaned.upper() == self._keyword:
            self.dispatch_command_result(
                chat_id,
                self._help_reply(chat_id, message_id),
                message_id=message_id,
            )
            return

        if cleaned.startswith("/"):
            self.handle_command(sender_id, chat_id, cleaned, message_id=message_id)
            return

        self._handle_prompt(sender_id, chat_id, cleaned, message_id)

    def handle_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        is_group_chat = self._is_group_chat(chat_id, message_id)
        action = action_value.get("action", "")
        if not action:
            rename_fallback = self._handle_rename_form_fallback(
                sender_id,
                chat_id,
                message_id,
                action_value,
            )
            if rename_fallback is not None:
                return rename_fallback
            fallback = self._handle_user_input_form_fallback(
                sender_id,
                chat_id,
                message_id,
                action_value,
            )
            if fallback is not None:
                return fallback
            form_value = action_value.get("_form_value") or {}
            if isinstance(form_value, dict) and form_value:
                return make_card_response(
                    toast="表单已失效或未找到对应问题，请重新触发该请求。",
                    toast_type="warning",
                )
        route = self._action_routes.get(action)
        if route is None:
            for prefix, prefixed_route in self._prefixed_action_routes:
                if action.startswith(prefix):
                    route = prefixed_route
                    break
        if route is None:
            return P2CardActionTriggerResponse()
        denied = self._check_action_group_guard(
            route,
            is_group_chat=is_group_chat,
            chat_id=chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
            action_value=action_value,
        )
        if denied is not None:
            return denied
        return route.handler(sender_id, chat_id, message_id, action_value)

    def handle_command(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        execution = self.execute_command_text(sender_id, chat_id, text, message_id=message_id)
        if execution.error_text:
            self._reply_text(chat_id, execution.error_text, message_id=message_id)
            return
        if execution.result is not None:
            self.dispatch_command_result(chat_id, execution.result, message_id=message_id)

    def execute_command_text(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
    ) -> CommandExecution:
        command, _, arg = text.partition(" ")
        arg = arg.strip()
        cmd = command.lower()
        route = self._command_routes.get(cmd)
        if route is None:
            return CommandExecution(
                error_text=f"未知命令：`{command}`\n发送 `/help` 查看可用命令。"
            )
        denied_text = self._command_denial_text(route, sender_id, chat_id, message_id=message_id)
        if denied_text:
            return CommandExecution(error_text=denied_text)
        return CommandExecution(result=route.handler(sender_id, chat_id, arg, message_id))

    def dispatch_command_result(self, chat_id: str, result: CommandResult, *, message_id: str = "") -> None:
        if result.card is not None:
            self._reply_card(chat_id, result.card, message_id=message_id)
        elif result.text:
            self._reply_text(chat_id, result.text, message_id=message_id)
        if result.after_dispatch is not None:
            result.after_dispatch()

    def handle_help_execute_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        command = str(action_value.get("command", "") or "").strip()
        if not command.startswith("/"):
            return make_card_response(
                toast="帮助按钮配置异常：缺少合法命令。",
                toast_type="warning",
            )
        title = str(action_value.get("title", "") or "").strip() or f"Codex {command.split()[0]}"
        execution = self.execute_command_text(
            sender_id,
            chat_id,
            command,
            message_id=message_id,
        )
        return self._command_action_response(execution, title=title)

    def handle_help_submit_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        command = str(action_value.get("command", "") or "").strip()
        field_name = str(action_value.get("field_name", "") or "").strip()
        required_text = str(action_value.get("required_text", "") or "").strip() or "请输入必填参数。"
        form_value = action_value.get("_form_value") or {}
        if not command.startswith("/"):
            return make_card_response(
                toast="帮助表单配置异常：缺少合法命令。",
                toast_type="warning",
            )
        if not field_name or not isinstance(form_value, dict):
            return make_card_response(
                toast="帮助表单配置异常：缺少参数字段。",
                toast_type="warning",
            )
        arg = str(form_value.get(field_name, "") or "").strip()
        if not arg:
            return make_card_response(toast=required_text, toast_type="warning")
        title = str(action_value.get("title", "") or "").strip() or f"Codex {command}"
        execution = self.execute_command_text(
            sender_id,
            chat_id,
            f"{command} {arg}",
            message_id=message_id,
        )
        return self._command_action_response(execution, title=title)

    def _command_scope_denial_text(self, route: CommandRoute, chat_id: str, message_id: str = "") -> str:
        if route.scope == "any":
            return ""
        chat_type = self._resolve_chat_type(chat_id, message_id)
        if route.scope == "group" and chat_type == "group":
            return ""
        if route.scope == "p2p" and chat_type != "group":
            return ""
        denied_text = route.scope_denied_text
        if not denied_text:
            if route.scope == "group":
                denied_text = "该命令仅支持群聊使用。"
            else:
                denied_text = "该命令仅支持私聊使用。"
        return denied_text

    def _command_denial_text(
        self,
        route: CommandRoute,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> str:
        scope_denial = self._command_scope_denial_text(route, chat_id, message_id=message_id)
        if scope_denial:
            return scope_denial
        if route.admin_only_in_group:
            return self._group_command_admin_denial_text(chat_id, message_id, sender_id)
        return ""

    @staticmethod
    def _command_action_response(
        execution: CommandExecution,
        *,
        title: str,
    ) -> P2CardActionTriggerResponse:
        if execution.error_text:
            return make_card_response(
                toast=execution.error_text,
                toast_type="warning",
            )
        result = execution.result
        if result is None:
            return make_card_response(
                toast="命令已执行。",
                toast_type="success",
            )
        if result.after_dispatch is not None:
            result.after_dispatch()
        if result.card is not None:
            return make_card_response(card=result.card)
        if result.text:
            return make_card_response(card=build_markdown_card(title or "Codex 命令结果", result.text))
        return P2CardActionTriggerResponse()

    def _check_action_group_guard(
        self,
        route: ActionRoute,
        *,
        is_group_chat: bool,
        chat_id: str,
        message_id: str,
        operator_open_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        if not is_group_chat or route.group_guard == "none":
            return None
        if route.group_guard == "group_admin":
            if self._is_group_admin_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return make_card_response(
                toast="仅管理员可操作群共享会话或群设置。",
                toast_type="warning",
            )
        if route.group_guard == "turn_actor":
            if self._is_group_turn_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return make_card_response(
                toast="仅管理员或当前提问者可停止当前群聊执行。",
                toast_type="warning",
            )
        if route.group_guard == "approval_admin":
            if self._is_group_admin_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return make_card_response(
                toast="仅管理员可审批群共享会话请求。",
                toast_type="warning",
            )
        if route.group_guard == "request_actor_or_admin":
            if self._is_group_request_actor_or_admin(
                chat_id,
                request_key=str(action_value.get("request_id", "")).strip(),
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
        return make_card_response(
            toast="当前卡片动作配置异常。",
            toast_type="warning",
        )
