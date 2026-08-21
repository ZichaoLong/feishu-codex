"""Feishu ingress, command, and card-action surface for Focus runtime."""

from __future__ import annotations

import logging
import pathlib
from typing import Any, ContextManager, TypeAlias

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.binding_identity import format_binding_id
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.cards import CommandResult, build_markdown_card, make_card_response
from bot.codex_goal_domain import CodexGoalDomain
from bot.codex_group_domain import CodexGroupDomain
from bot.codex_help_domain import CodexHelpDomain
from bot.codex_settings_domain import CodexSettingsDomain
from bot.codex_threads_ui_domain import CodexThreadsUiDomain
from bot.constants import (
    GROUP_SHARED_BINDING_OWNER_ID,
    KEYWORD,
    display_path,
    resolve_working_dir,
)
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.feishu_execution_queue_service import FeishuExecutionQueueService
from bot.feishu_thread_session_coordinator import FeishuThreadSessionCoordinator
from bot.feishu_turn_steer import FeishuTurnSteerController
from bot.file_message_domain import FileMessageDomain, IncomingAttachmentMessage
from bot.focus_runtime.binding_coordinator import BindingRuntimeCoordinator
from bot.focus_runtime.feishu_platform import FeishuPlatform
from bot.focus_runtime.terminal_results import TerminalResults
from bot.focus_runtime.thread_targets import CodexThreadTargetService
from bot.inbound_surface_controller import (
    ActionRoute,
    CommandRoute,
    InboundSurfaceController,
)
from bot.interaction_request_controller import (
    InteractionRequestController,
    PendingRequestStateDict,
)
from bot.prompt_turn_entry_controller import PromptTurnEntryController
from bot.runtime_admin.controller import RuntimeAdminController
from bot.thread_access_policy import ThreadAccessPolicy


logger = logging.getLogger("bot.focus_runtime")

_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
ChatBindingKey: TypeAlias = tuple[str, str]


class FeishuSurface:
    """Route Feishu ingress through existing capability and authority owners."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        adapter: CodexAppServerAdapter,
        platform: FeishuPlatform,
        binding_runtime: BindingRuntimeManager,
        binding_runtime_coordinator: BindingRuntimeCoordinator,
        thread_access_policy: ThreadAccessPolicy,
        direct_thread_targets: CodexThreadTargetService,
        interaction_requests: InteractionRequestController,
        feishu_execution_queue_service: FeishuExecutionQueueService,
        feishu_thread_sessions: FeishuThreadSessionCoordinator,
        file_message_domain: FileMessageDomain,
        prompt_turn_entry: PromptTurnEntryController,
        runtime_admin: RuntimeAdminController,
        help_domain: CodexHelpDomain,
        settings_domain: CodexSettingsDomain,
        group_domain: CodexGroupDomain,
        threads_ui_domain: CodexThreadsUiDomain,
        goal_domain: CodexGoalDomain,
        terminal_results: TerminalResults,
    ) -> None:
        self._lock = lock
        self._platform = platform
        self._binding_runtime = binding_runtime
        self._binding_runtime_coordinator = binding_runtime_coordinator
        self._thread_access_policy = thread_access_policy
        self._interaction_requests = interaction_requests
        self._feishu_execution_queue_service = feishu_execution_queue_service
        self._feishu_thread_sessions = feishu_thread_sessions
        self._file_message_domain = file_message_domain
        self._prompt_turn_entry = prompt_turn_entry
        self._runtime_admin = runtime_admin
        self._help_domain = help_domain
        self._settings_domain = settings_domain
        self._group_domain = group_domain
        self._threads_ui_domain = threads_ui_domain
        self._goal_domain = goal_domain
        self._terminal_results = terminal_results
        self._turn_steer = FeishuTurnSteerController(
            lock=lock,
            adapter=adapter,
            binding_runtime=binding_runtime,
            access_policy=thread_access_policy,
            direct_thread_targets=direct_thread_targets,
        )

        self._inbound_surface = InboundSurfaceController(
            keyword=KEYWORD,
            activate_binding_if_needed=(
                self._binding_runtime_coordinator.activate_binding_if_needed
            ),
            help_reply=lambda chat_id, message_id: self._help_domain.reply_help(
                chat_id,
                message_id=message_id,
            ),
            handle_prompt=lambda sender_id, chat_id, text, message_id: (
                self.handle_prompt(
                    sender_id,
                    chat_id,
                    text,
                    message_id=message_id,
                )
            ),
            reply_text=self._platform.reply_text,
            reply_card=self._platform.reply_card,
            resolve_chat_type=self._platform.resolve_chat_type,
            group_command_admin_denial_text=(
                self._platform.group_command_admin_denial_text
            ),
            is_group_chat=self._platform.is_group_chat,
            is_group_admin_actor=self._platform.is_group_admin_actor,
            is_group_turn_actor=self.is_group_turn_actor,
            is_group_request_actor_or_admin=self.is_group_request_actor_or_admin,
            handle_rename_form_fallback=(
                self._threads_ui_domain.handle_rename_form_fallback
            ),
            handle_help_form_fallback=self.handle_help_form_fallback,
            handle_settings_form_fallback=self.handle_settings_form_fallback,
            handle_user_input_form_fallback=self.handle_user_input_form_fallback,
        )
        self._inbound_surface.install_routes(
            command_routes=self.build_command_routes(),
            action_routes=self.build_action_routes(),
            prefixed_action_routes=self.build_prefixed_action_routes(),
        )

    def handle_message_impl(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
    ) -> None:
        self._inbound_surface.handle_message(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
        )

    def handle_message_recalled_impl(self, chat_id: str, message_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        outcome = self._feishu_execution_queue_service.remove_recalled_message(
            chat_id=chat_id,
            message_id=normalized_message_id,
        )
        if outcome.removed_count:
            logger.info(
                "已取消撤回消息对应的排队请求: chat=%s message=%s removed=%s",
                chat_id,
                normalized_message_id,
                outcome.removed_count,
            )

    @staticmethod
    def should_bypass_runtime_for_card_action(
        action_value: dict[str, Any],
    ) -> bool:
        action = str(action_value.get("action", "") or "").strip()
        if action == "attach_runtime":
            return True
        if action == "goal_apply_confirm":
            objective = str(action_value.get("objective", "") or "").strip()
            status = str(action_value.get("status", "") or "").strip()
            return not objective and status == "active"
        return False

    def handle_card_action_impl(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        return self._inbound_surface.handle_card_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def seed_help_action_actor_context(
        self,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        operator_open_id = str(
            action_value.get("_operator_open_id", "") or ""
        ).strip()
        operator_user_id = str(
            action_value.get("_operator_user_id", "") or ""
        ).strip()
        if not operator_open_id and not operator_user_id:
            return
        bot = self._platform.bot
        current_context = bot.get_message_context(normalized_message_id)
        merged_context = dict(current_context)
        changed = False
        if operator_open_id and not str(
            merged_context.get("sender_open_id", "") or ""
        ).strip():
            merged_context["sender_open_id"] = operator_open_id
            changed = True
        if operator_user_id and not str(
            merged_context.get("sender_user_id", "") or ""
        ).strip():
            merged_context["sender_user_id"] = operator_user_id
            changed = True
        if "sender_type" not in merged_context or not str(
            merged_context.get("sender_type", "") or ""
        ).strip():
            merged_context["sender_type"] = "user"
            changed = True
        if "chat_type" not in merged_context or not str(
            merged_context.get("chat_type", "") or ""
        ).strip():
            merged_context["chat_type"] = self._platform.resolve_chat_type(
                chat_id,
                normalized_message_id,
            )
            changed = True
        if not changed:
            return
        remember_message_context = getattr(
            bot,
            "_remember_message_context",
            None,
        )
        if callable(remember_message_context):
            remember_message_context(normalized_message_id, merged_context)
            return
        message_contexts = getattr(bot, "message_contexts", None)
        if isinstance(message_contexts, dict):
            message_contexts[normalized_message_id] = merged_context

    def handle_help_execute_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        self.seed_help_action_actor_context(chat_id, message_id, action_value)
        return self._inbound_surface.handle_help_execute_command_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def handle_help_submit_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        self.seed_help_action_actor_context(chat_id, message_id, action_value)
        return self._inbound_surface.handle_help_submit_command_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def handle_help_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        payload = self._help_domain.resolve_form_submit_payload(action_value)
        if payload is None:
            return None
        merged_action_value = dict(action_value)
        merged_action_value.update(payload)
        return self.handle_help_submit_command_action(
            sender_id,
            chat_id,
            message_id,
            merged_action_value,
        )

    def handle_settings_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        payload = (
            self._settings_domain.resolve_runtime_settings_form_submit_payload(
                action_value
            )
        )
        if payload is None:
            return None
        merged_action_value = dict(action_value)
        merged_action_value.update(payload)
        return self.handle_card_action_impl(
            sender_id,
            chat_id,
            message_id,
            merged_action_value,
        )

    def handle_attachment_message_impl(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self._file_message_domain.handle_message(
            IncomingAttachmentMessage(
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=message_id,
                thread_id=str(
                    self._platform.bot.get_message_context(message_id).get(
                        "thread_id",
                        "",
                    )
                    or ""
                ).strip(),
                attachment_type=attachment_type,
                resource_key=resource_key,
                display_name=file_name,
            )
        )

    def handle_user_input_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse | None:
        form_value = action_value.get("_form_value") or {}
        if not message_id or not isinstance(form_value, dict) or not form_value:
            return None

        with self._lock:
            pending_request = (
                self._interaction_requests.find_user_input_request_by_message_locked(
                    message_id
                )
            )
        if not pending_request:
            return None

        request_key, pending = pending_request
        if self._platform.is_group_chat(
            chat_id,
            message_id,
        ) and not self.is_group_request_actor_or_admin(
            chat_id,
            request_key=request_key,
            pending=pending,
            message_id=message_id,
            operator_open_id=str(
                action_value.get("_operator_open_id", "")
            ).strip(),
        ):
            return make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
        matched_question_id = ""
        for question in pending["questions"]:
            qid = str(question.get("id", "")).strip()
            if not qid:
                continue
            options = question.get("options") or []
            allow_custom = bool(question.get("isOther", False)) or not options
            field_name = f"user_input_{qid}"
            if allow_custom and str(form_value.get(field_name, "")).strip():
                matched_question_id = qid
                break
        if not matched_question_id:
            return None

        payload = dict(action_value)
        payload["action"] = "answer_user_input_custom"
        payload["request_id"] = request_key
        payload["question_id"] = matched_question_id
        return self.handle_user_input_action(payload)

    def validate_group_mode_change(
        self,
        chat_id: str,
        mode: str,
        *,
        message_id: str = "",
    ) -> str:
        runtime = self._binding_runtime.resolve_session(
            GROUP_SHARED_BINDING_OWNER_ID,
            chat_id,
            message_id,
        )
        return self._thread_access_policy.validate_group_mode_change(
            chat_id,
            mode,
            thread_id=runtime.current_thread_id.strip(),
            message_id=message_id,
        )

    def preflight_group_prompt_impl(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        return self._prompt_turn_entry.preflight_group_prompt(
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def should_route_group_followup_prompt_impl(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        if not runtime.running:
            return False
        return bool(
            runtime.binding[0] == GROUP_SHARED_BINDING_OWNER_ID
            or runtime.binding[0] == sender_id
        )

    def is_group_turn_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        if not self._platform.is_group_chat(chat_id, message_id):
            return True
        if self._platform.is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
        ):
            return True
        runtime = self._binding_runtime.resolve_session(
            GROUP_SHARED_BINDING_OWNER_ID,
            chat_id,
            message_id,
        )
        actor_open_id = self._platform.group_actor_open_id(
            message_id,
            operator_open_id,
        )
        current_actor_open_id = runtime.execution.current_actor_open_id.strip()
        return bool(
            current_actor_open_id
            and actor_open_id
            and current_actor_open_id == actor_open_id
        )

    def is_group_request_actor_or_admin(
        self,
        chat_id: str,
        *,
        request_key: str,
        pending: PendingRequestStateDict | None = None,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        if not self._platform.is_group_chat(chat_id, message_id):
            return True
        request = pending
        if request is None:
            with self._lock:
                request = (
                    self._interaction_requests.pending_request_snapshot_locked(
                        request_key
                    )
                )
        if not request:
            return False
        request_actor_open_id = request["actor_open_id"].strip()
        # A group deactivation has already revoked this member-origin request
        # and attempted its one fail-close response. If that response outcome
        # is unknown/not-sent, keeping it pending is a release blocker, not a
        # chance for an administrator to accept the member's old approval.
        # The explicit marker remains effective after later reactivation.
        if bool(request.get("group_authority_revoked", False)):
            return False
        bot = self._platform.bot
        activation = bot.get_group_activation_snapshot(chat_id)
        if not bool(activation.get("activated", False)) and (
            not request_actor_open_id
            or not bot.is_group_admin(open_id=request_actor_open_id)
        ):
            # This also closes a small runtime ordering window between group
            # deactivation and the pending-map marker write. Missing origin
            # provenance is not evidence that an administrator may answer.
            return False
        if self._platform.is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
        ):
            return True
        actor_open_id = self._platform.group_actor_open_id(
            message_id,
            operator_open_id,
        )
        if not bot.is_group_user_allowed(chat_id, open_id=actor_open_id):
            return False
        return bool(
            request_actor_open_id
            and actor_open_id
            and request_actor_open_id == actor_open_id
        )

    def deactivate_group_chat(self, chat_id: str):
        normalized_chat_id = str(chat_id or "").strip()
        bot = self._platform.bot
        snapshot = bot.deactivate_group_chat(normalized_chat_id)
        binding = (GROUP_SHARED_BINDING_OWNER_ID, normalized_chat_id)
        self._feishu_execution_queue_service.invalidate_group_continuity(binding)
        fail_closed_count = (
            self._interaction_requests.fail_close_non_admin_chat_requests(
                normalized_chat_id,
                is_admin_actor=lambda open_id: bot.is_group_admin(
                    open_id=open_id
                ),
            )
        )
        logger.info(
            "group deactivated: chat=%s non_admin_pending_fail_closed=%s",
            normalized_chat_id,
            fail_closed_count,
        )
        return snapshot

    def build_command_routes(self) -> dict[str, CommandRoute]:
        return {
            "/help": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, sender_id=sender_id, message_id=message_id
                ),
            ),
            "/h": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, sender_id=sender_id, message_id=message_id
                ),
            ),
            "/commands": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: (
                    CommandResult(
                        text="用法：`/commands`\n说明：该命令不接受额外参数；发送 `/help` 查看导航入口。"
                    )
                    if arg.strip()
                    else self._help_domain.reply_commands(chat_id, message_id=message_id)
                ),
            ),
            "/init": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_init_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text=f"请私聊机器人执行 `{_INIT_COMMAND}`。",
            ),
            "/pwd": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=(
                        "当前目录：`"
                        f"{display_path(self._binding_runtime.resolve_session(sender_id, chat_id, message_id).working_dir)}`"
                    ),
                ),
            ),
            "/cd": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_cd_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/new": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_new_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/status": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_status_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/last": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_last_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/goal": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_goal_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/preflight": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_preflight_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/detach": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_detach_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/attach": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self.handle_attach_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/whoami": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_whoami_command(
                    sender_id, chat_id, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/whoami`。",
            ),
            "/bot-status": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_bot_status_command(
                    chat_id, message_id=message_id
                ),
            ),
            "/debug-contact": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_debug_contact_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text=f"请私聊机器人执行 `{_DEBUG_CONTACT_COMMAND}`。",
            ),
            "/reset-backend": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._runtime_admin.handle_reset_backend_command(
                    arg
                ),
            ),
            "/cancel": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=self.cancel_current_turn(sender_id, chat_id, message_id=message_id)[1],
                ),
            ),
            "/steer": CommandRoute(
                handler=self._turn_steer.handle_command,
            ),
            "/threads": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: (
                    CommandResult(
                        text="用法：`/threads`\n说明：该命令不接受额外参数；发送 `/help thread` 查看线程相关操作。"
                    )
                    if arg.strip()
                    else self._threads_ui_domain.handle_threads_command(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                    )
                ),
            ),
            "/resume": CommandRoute(
                handler=self._threads_ui_domain.handle_resume_command,
            ),
            "/archive": CommandRoute(
                handler=self._threads_ui_domain.handle_archive_command,
            ),
            "/compact": CommandRoute(
                handler=self.handle_compact_command,
            ),
            "/rename": CommandRoute(
                handler=self._threads_ui_domain.handle_rename_command,
            ),
            "/approval": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_approval_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/permissions": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_permissions_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/model": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_model_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/effort": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_effort_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/group-mode": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_group_mode_command(
                    chat_id,
                    arg,
                    sender_id,
                    message_id=message_id,
                ),
                scope="group",
            ),
            "/group": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_group_command(
                    chat_id,
                    arg,
                    sender_id,
                    message_id=message_id,
                ),
                scope="group",
            ),
        }

    def build_action_routes(self) -> dict[str, ActionRoute]:
        return {
            "interaction_approval": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self.handle_approval_card_action(
                    action_value
                ),
                group_guard="request_actor_or_admin",
            ),
            "cancel_turn": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self.handle_cancel_action(
                    sender_id,
                    chat_id,
                    message_id=message_id,
                ),
                group_guard="turn_actor",
            ),
            "resume_thread": ActionRoute(
                handler=self._threads_ui_domain.handle_resume_thread_action,
                group_guard="group_admin",
            ),
            "resume_thread_confirm": ActionRoute(
                handler=self._threads_ui_domain.handle_resume_thread_confirm_action,
                group_guard="group_admin",
            ),
            "goal_refresh": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_pause": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_resume": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_clear": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_apply_confirm": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "show_more_threads": ActionRoute(
                handler=self._threads_ui_domain.handle_show_more_threads_action,
                group_guard="group_admin",
            ),
            "close_threads_card": ActionRoute(
                handler=self._threads_ui_domain.handle_close_threads_card_action,
                group_guard="group_admin",
            ),
            "reopen_threads_card": ActionRoute(
                handler=self._threads_ui_domain.handle_reopen_threads_card_action,
                group_guard="group_admin",
            ),
            "show_help_page": ActionRoute(
                handler=self._help_domain.handle_show_help_page_action,
            ),
            "help_execute_command": ActionRoute(
                handler=self.handle_help_execute_command_action,
            ),
            "help_submit_command": ActionRoute(
                handler=self.handle_help_submit_command_action,
            ),
            "archive_thread": ActionRoute(
                handler=self._threads_ui_domain.handle_archive_thread_action,
                group_guard="group_admin",
            ),
            "show_rename_form": ActionRoute(
                handler=self._threads_ui_domain.handle_show_rename_action,
                group_guard="group_admin",
            ),
            "rename_thread": ActionRoute(
                handler=self._threads_ui_domain.handle_rename_submit_action,
                group_guard="group_admin",
            ),
            "cancel_rename": ActionRoute(
                handler=self._threads_ui_domain.handle_cancel_rename_action,
                group_guard="group_admin",
            ),
            "set_approval_policy": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_approval_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_permissions_profile": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_permissions_profile(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_model": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_model(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "submit_model_override": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_submit_model_override(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "submit_reasoning_effort_override": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_submit_reasoning_effort_override(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_reasoning_effort": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_reasoning_effort(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "reset_backend": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_reset_backend_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "attach_runtime": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_attach_action(
                    sender_id,
                    chat_id,
                    message_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
            "dismiss_attach": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_dismiss_attach_action(),
                group_guard="group_admin",
            ),
            "set_group_mode": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_set_group_mode_action(
                    chat_id,
                    message_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
            "set_group_activation": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_set_group_activation_action(
                    chat_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
        }

    def build_prefixed_action_routes(self) -> list[tuple[str, ActionRoute]]:
        return [
            (
                "answer_user_input_",
                ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: self.handle_user_input_action(
                        action_value
                    ),
                    group_guard="request_actor_or_admin",
                ),
            ),
        ]

    def handle_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
    ) -> None:
        prepared = self._file_message_domain.prepare_prompt_input(
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
        if prepared.blocking_text:
            self._platform.reply_text(
                chat_id,
                prepared.blocking_text,
                message_id=message_id,
                reply_in_thread=self._platform.message_reply_in_thread(
                    message_id
                ),
            )
            return
        prompt_admission = self.start_or_enqueue_prompt(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            input_items=list(prepared.input_items),
        )
        if (
            not prompt_admission.get("accepted")
            and prepared.consumed_attachments
        ):
            self._file_message_domain.restore_consumed_attachments(
                prepared.consumed_attachments
            )

    def start_or_enqueue_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
        surface_failures: bool = True,
    ) -> dict[str, Any]:
        return self._feishu_execution_queue_service.start_or_enqueue_prompt(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            actor_open_id=actor_open_id,
            input_items=input_items,
            synthetic_source=synthetic_source,
            display_mode=display_mode,
            surface_failures=surface_failures,
        )

    def handle_compact_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        if arg.strip():
            return CommandResult(text="用法：`/compact`")
        result = self._feishu_execution_queue_service.start_or_enqueue_compact(
            sender_id,
            chat_id,
            message_id=message_id,
        )
        if result.get("queued"):
            return CommandResult(
                text=(
                    "已排队，compact 将在当前执行结束后开始。"
                    f"队列位置：{result['queue_position']}"
                )
            )
        if not result.get("started"):
            return CommandResult(
                text=str(result.get("reason") or "compact 失败。")
            )
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        title = runtime.current_thread_title or "（无标题）"
        return CommandResult(
            card=build_markdown_card(
                "Codex Compact 已开始",
                (
                    f"已发起当前 thread 的 compact：`{result['thread_id'][:8]}…` "
                    f"{title}\n"
                    "这是上游 Codex 的 thread 级上下文压缩动作；"
                    "完成后会继续在同一 thread 内工作。"
                ),
                template="green",
            )
        )

    def handle_cd_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        if runtime.execution.has_execution_anchor:
            return CommandResult(
                card=build_markdown_card(
                    "Codex 目录未切换",
                    "执行中不能切换目录，请等待结束或先停止当前执行。",
                    template="orange",
                )
            )

        if not arg:
            return CommandResult(
                card=build_markdown_card(
                    "Codex 当前目录",
                    f"当前目录：`{display_path(runtime.working_dir)}`",
                )
            )

        target = resolve_working_dir(arg, fallback=runtime.working_dir)
        if not pathlib.Path(target).exists():
            return CommandResult(
                card=build_markdown_card(
                    "Codex 目录未切换",
                    f"目录不存在：`{display_path(target)}`",
                    template="orange",
                )
            )
        if not pathlib.Path(target).is_dir():
            return CommandResult(
                card=build_markdown_card(
                    "Codex 目录未切换",
                    f"不是目录：`{display_path(target)}`",
                    template="orange",
                )
            )

        cleanup_incomplete = (
            self._binding_runtime_coordinator.clear_thread_binding(
                sender_id,
                chat_id,
                message_id=message_id,
                session=runtime,
                working_dir_after_clear=target,
                require_no_inflight_turn=True,
            )
        )
        invalidated_attachment_count = 0
        try:
            invalidated_attachment_count = (
                self._file_message_domain.invalidate_pending_attachments_for_scope(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    working_dir=target,
                )
            )
        except Exception:
            cleanup_incomplete = True
            logger.exception("目录切换 commit 后清理待消费附件失败")
        message = f"目录：`{display_path(target)}`\n当前线程绑定已清空。\n"
        if invalidated_attachment_count > 0:
            message += (
                f"已使 {invalidated_attachment_count} 个待消费附件失效。\n"
            )
        if cleanup_incomplete:
            message += (
                "目录与 binding 已提交，但后置清理未完成；"
                "重试 `/cd` 可继续清理队列与附件，"
                "旧附件消费仍会 fail closed。\n"
            )
        message += "直接发送普通文本，会在新目录自动新建线程。"
        return CommandResult(
            card=build_markdown_card("Codex 目录已切换", message)
        )

    def handle_new_command(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        if runtime.execution.has_execution_anchor:
            return CommandResult(
                text="执行中不能新建线程，请等待结束或先执行 `/cancel`。"
            )
        try:
            snapshot = self._feishu_thread_sessions.create_and_bind_thread(
                sender_id,
                chat_id,
                message_id=message_id,
                cwd=runtime.working_dir,
                model=runtime.model or None,
                approval_policy=runtime.approval_policy or None,
                permissions_profile_id=(
                    runtime.permissions_profile_id or None
                ),
            )
        except Exception as exc:
            logger.exception("新建线程失败")
            return CommandResult(text=f"新建线程失败：{exc}")
        content = (
            f"线程：`{snapshot.summary.thread_id[:8]}…`\n"
            f"目录：`{display_path(snapshot.summary.cwd)}`\n"
            "直接发送普通文本开始第一轮对话。"
        )
        return CommandResult(
            card=build_markdown_card(
                "Codex 线程已新建",
                content,
                template="green",
            )
        )

    def handle_status_command(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._binding_runtime_coordinator.chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )
        return self._runtime_admin.handle_status_command(binding)

    def handle_last_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        if str(arg or "").strip().lower() != "text":
            return CommandResult(text="用法：`/last text`")
        text = self._terminal_results.find_last_card_text(
            sender_id,
            chat_id,
            message_id=message_id,
        )
        return CommandResult(text=text)

    def handle_goal_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        return self._goal_domain.handle_goal_command(
            sender_id,
            chat_id,
            arg,
            message_id=message_id,
        )

    def handle_preflight_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._binding_runtime_coordinator.chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )
        return self._runtime_admin.handle_preflight_command(binding, arg)

    def handle_detach_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._binding_runtime_coordinator.chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )
        return self._runtime_admin.handle_detach_command(binding, arg)

    def handle_attach_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._binding_runtime_coordinator.chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )
        return self._runtime_admin.handle_attach_command(binding, arg)

    def submit_prompt_for_control(
        self,
        binding: ChatBindingKey,
        *,
        text: str,
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
    ) -> dict[str, Any]:
        binding_id = format_binding_id(binding)
        normalized_text = str(text or "").strip()
        normalized_source = str(synthetic_source or "").strip()
        normalized_display_mode = (
            str(display_mode or "silent").strip().lower() or "silent"
        )
        self._binding_runtime_coordinator.activate_binding_if_needed(*binding)
        result = self.start_or_enqueue_prompt(
            binding[0],
            binding[1],
            normalized_text,
            actor_open_id=str(actor_open_id or "").strip(),
            input_items=(
                list(input_items) if input_items is not None else None
            ),
            synthetic_source=normalized_source,
            display_mode=normalized_display_mode,
            surface_failures=False,
        )
        if normalized_display_mode == "announce" and result.get("started"):
            label = normalized_source or "系统任务"
            self._platform.reply_text(
                binding[1],
                f"{label}触发，开始新一轮执行。",
                reply_in_thread=False,
            )
        return {
            "binding_id": binding_id,
            "thread_id": str(result.get("thread_id", "") or ""),
            "started": bool(result.get("started")),
            "queued": bool(result.get("queued")),
            "queue_position": int(result.get("queue_position") or 0),
            "turn_id": str(result.get("turn_id", "") or ""),
            "reason_code": str(result.get("reason_code", "") or ""),
            "reason": str(result.get("reason", "") or ""),
            "synthetic_source": normalized_source,
            "display_mode": normalized_display_mode,
        }

    def handle_cancel_action(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str,
    ) -> P2CardActionTriggerResponse:
        ok, message = self.cancel_current_turn(
            sender_id,
            chat_id,
            message_id=message_id,
            action_page_message_id=message_id,
        )
        return make_card_response(
            toast=message,
            toast_type="success" if ok else "warning",
        )

    def cancel_current_turn(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        action_page_message_id: str = "",
    ) -> tuple[bool, str]:
        return self._prompt_turn_entry.cancel_current_turn(
            sender_id,
            chat_id,
            message_id=message_id,
            action_page_message_id=action_page_message_id,
        )

    def handle_approval_card_action(
        self,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_approval_card_action(
            action_value
        )

    def handle_user_input_action(
        self,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_user_input_action(action_value)
