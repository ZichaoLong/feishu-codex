from __future__ import annotations

import json
import logging
from typing import Any, Callable, MutableMapping, TypeAlias

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import (
    build_approval_handled_card,
    build_ask_user_answered_card,
    build_ask_user_card,
    build_command_approval_card,
    build_file_change_approval_card,
    build_markdown_card,
    build_permissions_approval_card,
    make_card_response,
)

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = MutableMapping[str, Any]
PendingRequestState: TypeAlias = MutableMapping[str, Any]

PENDING_REQUEST_STATUS_PENDING = "pending"
PENDING_REQUEST_STATUS_PROCESSING = "processing"


class InteractionRequestController:
    def __init__(
        self,
        *,
        lock,
        pending_requests: dict[str, PendingRequestState],
        get_runtime_state: Callable[[str, str], RuntimeState],
        interactive_binding_for_thread: Callable[[str, bool], tuple[ChatBindingKey | None, bool]],
        send_interactive_card: Callable[[str, dict[str, Any], str, bool], str | None],
        reply_text: Callable[..., None],
        respond: Callable[..., None],
        patch_message: Callable[[str, str], bool],
    ) -> None:
        self._lock = lock
        self._pending_requests = pending_requests
        self._get_runtime_state = get_runtime_state
        self._interactive_binding_for_thread = interactive_binding_for_thread
        self._send_interactive_card = send_interactive_card
        self._reply_text = reply_text
        self._respond = respond
        self._patch_message = patch_message

    @property
    def pending_requests(self) -> dict[str, PendingRequestState]:
        return self._pending_requests

    @staticmethod
    def pending_request_status(pending: PendingRequestState | dict[str, Any]) -> str:
        return str(pending.get("status", PENDING_REQUEST_STATUS_PENDING) or PENDING_REQUEST_STATUS_PENDING)

    def binding_has_pending_request_locked(self, binding: ChatBindingKey) -> bool:
        for pending in self._pending_requests.values():
            pending_binding = (
                str(pending.get("sender_id", "") or "").strip(),
                str(pending.get("chat_id", "") or "").strip(),
            )
            if pending_binding == binding:
                return True
        return False

    def thread_has_pending_request_locked(self, thread_id: str) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        for pending in self._pending_requests.values():
            if str(pending.get("thread_id", "") or "").strip() == normalized_thread_id:
                return True
        return False

    def find_user_input_request_by_message_locked(
        self,
        message_id: str,
    ) -> tuple[str, PendingRequestState] | None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None
        for request_key, pending in self._pending_requests.items():
            if str(pending.get("method", "") or "").strip() != "item/tool/requestUserInput":
                continue
            if str(pending.get("message_id", "") or "").strip() != normalized_message_id:
                continue
            return request_key, pending
        return None

    def fail_close_chat_requests(self, chat_id: str) -> int:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return 0
        pending_to_fail_close: list[tuple[int | str, str, dict[str, Any]]] = []
        with self._lock:
            for request_key, pending in list(self._pending_requests.items()):
                if str(pending.get("chat_id", "") or "").strip() != normalized_chat_id:
                    continue
                pending_to_fail_close.append(
                    (
                        pending["rpc_request_id"],
                        str(pending.get("method", "") or ""),
                        dict(pending.get("params") or {}),
                    )
                )
                self._pending_requests.pop(request_key, None)
        for rpc_request_id, method, params in pending_to_fail_close:
            self.auto_reject_request(rpc_request_id, method, params)
        return len(pending_to_fail_close)

    def handle_adapter_request(
        self,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        binding, handled_elsewhere = self._interactive_binding_for_thread(thread_id, True)
        if not binding:
            if handled_elsewhere:
                logger.info(
                    "interactive request suppressed for non-Feishu owner: method=%s thread=%s",
                    method,
                    thread_id,
                )
                return
            logger.warning("未找到线程绑定，自动 fail-close: method=%s thread=%s", method, thread_id)
            self.auto_reject_request(request_id, method, params)
            return

        sender_id, chat_id = binding
        request_key = str(request_id)
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            prompt_message_id = str(state["current_prompt_message_id"] or "").strip()
            prompt_reply_in_thread = bool(state["current_prompt_reply_in_thread"])
            actor_open_id = str(state["current_actor_open_id"] or "").strip()

        if method == "item/commandExecution/requestApproval":
            card = build_command_approval_card(
                request_key,
                command=params.get("command") or "",
                cwd=params.get("cwd") or "",
                reason=params.get("reason") or "",
            )
            title = "Codex 命令执行审批"
        elif method == "item/fileChange/requestApproval":
            card = build_file_change_approval_card(
                request_key,
                grant_root=params.get("grantRoot") or "",
                reason=params.get("reason") or "",
            )
            title = "Codex 文件修改审批"
        elif method == "item/permissions/requestApproval":
            card = build_permissions_approval_card(
                request_key,
                permissions=params.get("permissions") or {},
                reason=params.get("reason") or "",
            )
            title = "Codex 额外权限审批"
        elif method == "item/tool/requestUserInput":
            card = build_ask_user_card(request_key, params.get("questions") or [])
            title = "Codex 用户输入"
        elif method == "mcpServer/elicitation/request":
            self._reply_text(
                chat_id,
                "收到 MCP elicitation 请求，当前版本暂未支持，已取消该请求。",
                message_id=prompt_message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
            self._respond(request_id, result={"action": "cancel"})
            return
        else:
            logger.warning("未支持的 Codex server request: %s", method)
            self._respond(
                request_id,
                error={"code": -32001, "message": f"Unsupported request: {method}"},
            )
            return

        message_id = self._send_interactive_card(
            chat_id,
            card,
            prompt_message_id,
            prompt_reply_in_thread,
        )
        if not message_id:
            logger.warning("审批/问答卡片发送失败，执行 fail-close: method=%s", method)
            self.auto_reject_request(request_id, method, params)
            return

        with self._lock:
            self._pending_requests[request_key] = {
                "rpc_request_id": request_id,
                "method": method,
                "params": params,
                "thread_id": thread_id,
                "turn_id": params.get("turnId", ""),
                "title": title,
                "message_id": message_id,
                "questions": params.get("questions") or [],
                "answers": {},
                "chat_id": chat_id,
                "sender_id": sender_id,
                "actor_open_id": actor_open_id,
                "status": PENDING_REQUEST_STATUS_PENDING,
            }

    def handle_server_request_resolved(self, params: dict[str, Any]) -> None:
        request_key = str(params.get("requestId", "") or "").strip()
        if not request_key:
            return
        with self._lock:
            pending = self._pending_requests.pop(request_key, None)
        if not pending:
            return
        message_id = str(pending.get("message_id", "") or "").strip()
        if not message_id:
            return
        title = str(pending.get("title", "Codex 请求") or "Codex 请求")
        if str(pending.get("method", "") or "").strip() == "item/tool/requestUserInput":
            card = build_markdown_card(
                title,
                "该请求已在其他终端处理。",
                template="grey",
            )
        else:
            card = build_approval_handled_card(
                title,
                "在其他终端处理",
            )
        try:
            self._patch_message(message_id, json.dumps(card, ensure_ascii=False))
        except Exception:
            logger.exception("收口已解决请求卡片失败: request=%s", request_key)

    def auto_reject_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        if method == "item/commandExecution/requestApproval":
            self._respond(request_id, result={"decision": "abort"})
        elif method == "item/fileChange/requestApproval":
            self._respond(request_id, result={"decision": "cancel"})
        elif method == "item/permissions/requestApproval":
            self._respond(request_id, result={"permissions": {}, "scope": "turn"})
        elif method == "item/tool/requestUserInput":
            self._respond(
                request_id,
                error={"code": -32002, "message": "Unable to deliver user input request to Feishu"},
            )
        elif method == "mcpServer/elicitation/request":
            self._respond(request_id, result={"action": "cancel"})
        else:
            self._respond(request_id, error={"code": -32001, "message": f"Unsupported request: {method}"})

    def handle_approval_card_action(self, action_value: dict[str, Any]) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", "") or "").strip()
        with self._lock:
            pending = self._pending_requests.get(request_key)
            if not pending:
                return make_card_response(toast="该审批请求已失效或已处理。", toast_type="warning")
            if self.pending_request_status(pending) == PENDING_REQUEST_STATUS_PROCESSING:
                return make_card_response(toast="该审批请求正在处理中，请稍候。", toast_type="warning")

            action = str(action_value.get("action", "") or "").strip()
            title = str(pending.get("title", "Codex 审批") or "Codex 审批")
            rpc_request_id = pending["rpc_request_id"]

            if action == "command_allow_once":
                result = {"decision": "accept"}
                decision_text = "允许本次"
            elif action == "command_allow_session":
                result = {"decision": "acceptForSession"}
                decision_text = "允许本会话"
            elif action == "command_deny":
                result = {"decision": "decline"}
                decision_text = "拒绝"
            elif action == "command_abort":
                result = {"decision": "cancel"}
                decision_text = "中止本轮"
            elif action == "file_change_accept":
                result = {"decision": "accept"}
                decision_text = "允许本次"
            elif action == "file_change_accept_session":
                result = {"decision": "acceptForSession"}
                decision_text = "允许本会话"
            elif action == "file_change_decline":
                result = {"decision": "decline"}
                decision_text = "拒绝"
            elif action == "file_change_cancel":
                result = {"decision": "cancel"}
                decision_text = "中止本轮"
            elif action == "permissions_allow_once":
                result = {"permissions": pending.get("params", {}).get("permissions") or {}, "scope": "turn"}
                decision_text = "允许本次"
            elif action == "permissions_allow_session":
                result = {"permissions": pending.get("params", {}).get("permissions") or {}, "scope": "session"}
                decision_text = "允许本会话"
            elif action == "permissions_deny":
                result = {"permissions": {}, "scope": "turn"}
                decision_text = "拒绝"
            else:
                return make_card_response(toast="未知审批动作", toast_type="warning")

            pending["status"] = PENDING_REQUEST_STATUS_PROCESSING

        logger.info(
            "响应审批请求: request_key=%s, rpc_request_id=%s, action=%s, result=%s",
            request_key,
            rpc_request_id,
            action,
            result,
        )
        try:
            self._respond(rpc_request_id, result=result)
        except Exception as exc:
            logger.exception("响应审批请求失败")
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current["status"] = PENDING_REQUEST_STATUS_PENDING
            return make_card_response(toast=f"审批提交失败：{exc}", toast_type="warning")
        with self._lock:
            self._pending_requests.pop(request_key, None)
        return make_card_response(
            card=build_approval_handled_card(title, decision_text),
            toast=f"已{decision_text}",
            toast_type="success",
        )

    def handle_user_input_action(self, action_value: dict[str, Any]) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", "") or "").strip()
        with self._lock:
            pending = self._pending_requests.get(request_key)
        if not pending:
            return make_card_response(toast="该输入请求已失效或已处理。", toast_type="warning")
        if self.pending_request_status(pending) == PENDING_REQUEST_STATUS_PROCESSING:
            return make_card_response(toast="该输入请求正在提交，请稍候。", toast_type="warning")

        question_id = str(action_value.get("question_id", "") or "").strip()
        if not question_id:
            return make_card_response(toast="缺少 question_id", toast_type="warning")

        questions = pending.get("questions") or []
        target_question = next((item for item in questions if str(item.get("id", "") or "").strip() == question_id), None)
        if not target_question:
            return make_card_response(toast="未找到对应问题", toast_type="warning")

        if action_value.get("action") == "answer_user_input_option":
            answer = str(action_value.get("answer", "") or "").strip()
        else:
            options = target_question.get("options") or []
            allow_custom = bool(target_question.get("isOther", False)) or not options
            if not allow_custom:
                return make_card_response(toast="该问题仅支持选择预设选项", toast_type="warning")
            form_value = action_value.get("_form_value") or {}
            answer = str(form_value.get(f"user_input_{question_id}", "") or "").strip()
        if not answer:
            return make_card_response(toast="回答不能为空", toast_type="warning")

        with self._lock:
            pending = self._pending_requests.get(request_key)
            if not pending:
                return make_card_response(toast="该输入请求已失效或已处理。", toast_type="warning")
            if self.pending_request_status(pending) == PENDING_REQUEST_STATUS_PROCESSING:
                return make_card_response(toast="该输入请求正在提交，请稍候。", toast_type="warning")

            questions = pending.get("questions") or []
            answers = pending.setdefault("answers", {})
            if question_id in answers:
                return make_card_response(
                    card=build_ask_user_card(request_key, questions, answers),
                    toast="该问题已记录，请继续剩余问题。",
                    toast_type="warning",
                )

            answers[question_id] = answer
            if len(answers) < len(questions):
                return make_card_response(
                    card=build_ask_user_card(request_key, questions, answers),
                    toast="已记录，继续回答下一题。",
                    toast_type="success",
                )

            pending["status"] = PENDING_REQUEST_STATUS_PROCESSING
            rpc_request_id = pending["rpc_request_id"]
            final_answers = dict(answers)

        result = {
            "answers": {
                str(q.get("id", "") or ""): {"answers": [final_answers[str(q.get("id", "") or "")]]}
                for q in questions
            }
        }
        try:
            self._respond(rpc_request_id, result=result)
        except Exception as exc:
            logger.exception("提交用户输入失败")
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current_answers = current.setdefault("answers", {})
                    current_answers.pop(question_id, None)
                    current["status"] = PENDING_REQUEST_STATUS_PENDING
            return make_card_response(
                toast=f"提交回答失败：{exc}",
                toast_type="warning",
            )
        with self._lock:
            self._pending_requests.pop(request_key, None)
        return make_card_response(
            card=build_ask_user_answered_card(questions, final_answers),
            toast="已提交回答。",
            toast_type="success",
        )
