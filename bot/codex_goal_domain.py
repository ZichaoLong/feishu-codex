from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import ThreadGoalSummary
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.codex_protocol.client import CodexRpcError
from bot.cards import (
    CommandResult,
    build_goal_card,
    build_goal_detached_confirm_card,
    build_markdown_card,
    goal_status_label,
    make_card_response,
)
from bot.constants import format_timestamp
from bot.runtime_state import FEISHU_RUNTIME_DETACHED

_GOAL_USAGE = (
    "用法：`/goal`\n"
    "别名：`/goal show`\n"
    "导出文本：`/goal text`\n"
    "设置：`/goal set <objective>`\n"
    "暂停：`/goal pause`\n"
    "恢复：`/goal resume`\n"
    "清除：`/goal clear`"
)
_GOALS_DISABLED_TEXT = "当前 backend 未启用 goal 功能。"


def _is_goals_feature_disabled_error(exc: Exception) -> bool:
    if not isinstance(exc, CodexRpcError):
        return False
    return str(exc.error.get("message", "") or "").strip().lower() == "goals feature is disabled"


def _render_goal_text(*, thread_id: str, thread_title: str, goal: ThreadGoalSummary | None) -> str:
    normalized_thread_id = str(thread_id or "").strip()
    normalized_thread_title = str(thread_title or "").strip()
    lines = [
        f"thread: {normalized_thread_id or '-'}",
        f"title: {normalized_thread_title or '（无标题）'}",
    ]
    if goal is None or not str(goal.objective or "").strip():
        lines.extend(["", "当前 thread 暂无 goal。"])
        return "\n".join(lines)

    status = str(goal.status or "").strip()
    token_budget = str(goal.token_budget) if goal.token_budget is not None else "未设置"
    lines.extend(
        [
            f"status: {status or '-'} ({goal_status_label(status)})",
            f"token_budget: {token_budget}",
            f"tokens_used: {int(goal.tokens_used or 0)}",
            f"time_used_seconds: {int(goal.time_used_seconds or 0)}",
            f"created_at: {format_timestamp(goal.created_at)}",
            f"updated_at: {format_timestamp(goal.updated_at)}",
            "",
            "objective:",
            str(goal.objective or "").strip(),
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class GoalDomainPorts:
    resolve_session: Callable[[str, str, str], BindingSessionSnapshot]
    get_thread_goal: Callable[[str], ThreadGoalSummary | None]
    # Goal mutations are routed through the owning frontend controller rather
    # than directly to the adapter.  Setting an active goal can immediately
    # start an autonomous turn, so the controller must acquire its exact
    # blank submission lease *before* the RPC leaves Focus.
    mutate_goal: Callable[..., ThreadGoalSummary]
    clear_goal: Callable[..., bool]
    thread_mutation_denial_text: Callable[..., str]
    attach_current_binding: Callable[[str, str, str], None]
    update_runtime_goal_projection: Callable[[str, str, str, ThreadGoalSummary | None], None]
    submit_to_runtime: Callable[..., None]
    resume_goal: Callable[[str, str, str, bool, str], dict]
    reply_card: Callable[..., None]


class CodexGoalDomain:
    def __init__(self, *, ports: GoalDomainPorts) -> None:
        self._ports = ports

    def handle_goal_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        normalized = str(arg or "").strip()
        if not normalized:
            return self._show_goal(sender_id, chat_id, message_id=message_id)
        subcommand, _, tail = normalized.partition(" ")
        subcommand = subcommand.strip().lower()
        payload = tail.strip()
        try:
            if subcommand == "show":
                if payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._show_goal(sender_id, chat_id, message_id=message_id)
            if subcommand == "text":
                if payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._show_goal_text(sender_id, chat_id, message_id=message_id)
            if subcommand == "set":
                if not payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._set_goal(sender_id, chat_id, payload, message_id=message_id)
            if subcommand == "pause":
                if payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._update_goal_status(sender_id, chat_id, "paused", message_id=message_id)
            if subcommand == "resume":
                if payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._update_goal_status(sender_id, chat_id, "active", message_id=message_id)
            if subcommand == "clear":
                if payload:
                    return CommandResult(text=_GOAL_USAGE)
                return self._clear_goal(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            return CommandResult(
                card=build_markdown_card("Codex Goal 操作失败", str(exc) or "goal 操作失败", template="red")
            )
        return CommandResult(text=_GOAL_USAGE)

    def handle_goal_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, str],
    ) -> P2CardActionTriggerResponse:
        action = str(action_value.get("action", "") or "").strip()
        try:
            if action == "goal_refresh":
                result = self._show_goal(sender_id, chat_id, message_id=message_id)
                return make_card_response(card=result.card)
            if action == "goal_pause":
                result = self._update_goal_status(sender_id, chat_id, "paused", message_id=message_id)
                return make_card_response(card=result.card, toast="已暂停 goal。", toast_type="success")
            if action == "goal_resume":
                thread_id, _thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
                self._require_resumable_goal(thread_id)
                confirm_card = self._build_detached_goal_confirm_card(
                    sender_id,
                    chat_id,
                    objective="",
                    status="active",
                    message_id=message_id,
                )
                if confirm_card is not None:
                    return make_card_response(card=confirm_card)
                self._ports.submit_to_runtime(
                    self.resume_goal_on_runtime,
                    sender_id,
                    chat_id,
                    thread_id,
                    False,
                    message_id,
                )
                return make_card_response(card=self._build_goal_resume_pending_card())
            if action == "goal_clear":
                result = self._clear_goal(sender_id, chat_id, message_id=message_id)
                return make_card_response(card=result.card, toast="已清除 goal。", toast_type="success")
            if action == "goal_apply_confirm":
                result, toast = self._apply_goal_confirmed(
                    sender_id,
                    chat_id,
                    expected_thread_id=str(
                        action_value.get("thread_id", "") or ""
                    ).strip(),
                    objective=str(action_value.get("objective", "") or "").strip(),
                    status=str(action_value.get("status", "") or "").strip(),
                    attach_binding=str(action_value.get("attach_binding", "") or "").strip().lower() == "true",
                    message_id=message_id,
                )
                if toast:
                    return make_card_response(card=result.card, toast=toast, toast_type="success")
                return make_card_response(card=result.card)
        except Exception as exc:
            return make_card_response(toast=str(exc) or "goal 操作失败", toast_type="warning")
        return P2CardActionTriggerResponse()

    def _current_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> tuple[str, str]:
        runtime = self._ports.resolve_session(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            raise ValueError("当前没有绑定 thread；请先直接发送消息、执行 `/new`，或 `/resume` 目标线程。")
        return thread_id, runtime.current_thread_title.strip()

    def _project_goal(
        self,
        sender_id: str,
        chat_id: str,
        goal: ThreadGoalSummary | None,
        *,
        message_id: str = "",
    ) -> None:
        self._ports.update_runtime_goal_projection(sender_id, chat_id, message_id, goal)

    def _show_goal(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        thread_id, thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        goal = self._get_thread_goal_or_raise(thread_id)
        self._project_goal(sender_id, chat_id, goal, message_id=message_id)
        return CommandResult(card=build_goal_card(thread_id=thread_id, thread_title=thread_title, goal=goal))

    def _show_goal_text(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        thread_id, thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        goal = self._get_thread_goal_or_raise(thread_id)
        self._project_goal(sender_id, chat_id, goal, message_id=message_id)
        return CommandResult(text=_render_goal_text(thread_id=thread_id, thread_title=thread_title, goal=goal))

    def _set_goal(
        self,
        sender_id: str,
        chat_id: str,
        objective: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        confirm_card = self._build_detached_goal_confirm_card(
            sender_id,
            chat_id,
            objective=objective,
            status="",
            message_id=message_id,
        )
        if confirm_card is not None:
            return CommandResult(card=confirm_card)
        return self._set_goal_direct(sender_id, chat_id, objective, message_id=message_id)

    def _update_goal_status(
        self,
        sender_id: str,
        chat_id: str,
        status: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        if status == "active":
            thread_id, _thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
            self._require_resumable_goal(thread_id)
        confirm_card = self._build_detached_goal_confirm_card(
            sender_id,
            chat_id,
            objective="",
            status=status,
            message_id=message_id,
        )
        if confirm_card is not None:
            return CommandResult(card=confirm_card)
        if status == "active":
            return self._resume_goal_async(sender_id, chat_id, attach_binding=False, message_id=message_id)
        return self._update_goal_status_direct(sender_id, chat_id, status, message_id=message_id)

    def _clear_goal(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        thread_id, thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        self._require_thread_mutation_access(
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        cleared = self._ports.clear_goal(
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        self._project_goal(sender_id, chat_id, None, message_id=message_id)
        notice = "已清除当前 thread goal。" if cleared else "当前 thread 原本就没有 goal。"
        return CommandResult(
            card=build_goal_card(
                thread_id=thread_id,
                thread_title=thread_title,
                goal=None,
                notice=notice,
            )
        )

    def _build_detached_goal_confirm_card(
        self,
        sender_id: str,
        chat_id: str,
        *,
        objective: str,
        status: str,
        message_id: str = "",
    ) -> dict | None:
        runtime = self._ports.resolve_session(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id or runtime.thread.feishu_runtime_state != FEISHU_RUNTIME_DETACHED:
            return None
        return build_goal_detached_confirm_card(
            thread_id=thread_id,
            thread_title=runtime.current_thread_title.strip(),
            objective=objective,
            status=status,
        )

    def _apply_goal_confirmed(
        self,
        sender_id: str,
        chat_id: str,
        *,
        expected_thread_id: str,
        objective: str,
        status: str,
        attach_binding: bool,
        message_id: str = "",
    ) -> tuple[CommandResult, str]:
        normalized_expected_thread_id = str(expected_thread_id or "").strip()
        if not normalized_expected_thread_id:
            raise ValueError("确认卡缺少 thread_id；请刷新 goal 卡后重试。")
        normalized_objective = str(objective or "").strip()
        normalized_status = str(status or "").strip()
        if normalized_status == "active":
            self._ports.submit_to_runtime(
                self.resume_goal_on_runtime,
                sender_id,
                chat_id,
                normalized_expected_thread_id,
                attach_binding,
                message_id,
            )
            return (CommandResult(card=self._build_goal_resume_pending_card()), "")
        thread_id, _thread_title = self._current_thread(
            sender_id,
            chat_id,
            message_id=message_id,
        )
        if thread_id != normalized_expected_thread_id:
            raise ValueError(
                "确认卡已过期：当前会话已切换到另一 thread；"
                "请刷新 goal 卡后重试。"
            )
        if attach_binding:
            self._ports.attach_current_binding(sender_id, chat_id, message_id)
        if normalized_objective:
            result = self._set_goal_direct(
                sender_id,
                chat_id,
                normalized_objective,
                message_id=message_id,
                attached_notice=attach_binding,
            )
            return result, "已更新 goal 并恢复当前会话推送。" if attach_binding else "已更新 goal，保持 detached。"
        if normalized_status:
            result = self._update_goal_status_direct(
                sender_id,
                chat_id,
                normalized_status,
                message_id=message_id,
                attached_notice=attach_binding,
            )
            if normalized_status == "active":
                return result, "已恢复 goal 并恢复当前会话推送。" if attach_binding else "已恢复 goal，保持 detached。"
            return result, "已更新 goal 并恢复当前会话推送。" if attach_binding else "已更新 goal，保持 detached。"
        raise ValueError("goal 变更缺少 objective 或 status。")

    def _resume_goal_async(
        self,
        sender_id: str,
        chat_id: str,
        *,
        attach_binding: bool,
        message_id: str = "",
    ) -> CommandResult:
        thread_id, _thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        self._require_thread_mutation_access(
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        return CommandResult(
            card=self._build_goal_resume_pending_card(),
            after_dispatch=lambda: self._ports.submit_to_runtime(
                self.resume_goal_on_runtime,
                sender_id,
                chat_id,
                thread_id,
                attach_binding,
                message_id,
            ),
        )

    def resume_goal_on_runtime(
        self,
        sender_id: str,
        chat_id: str,
        expected_thread_id: str,
        attach_binding: bool,
        message_id: str = "",
    ) -> None:
        """Run one serialized goal resume, then present its settled result."""

        card = self._ports.resume_goal(
            sender_id,
            chat_id,
            expected_thread_id,
            attach_binding,
            message_id,
        )
        self._ports.reply_card(chat_id, card, message_id=message_id)

    @staticmethod
    def _build_goal_resume_pending_card() -> dict:
        return build_markdown_card(
            "Codex 正在恢复 Goal",
            "正在同步 thread、goal 与当前会话设置；完成后会自动回复结果。",
        )

    def _get_thread_goal_or_raise(self, thread_id: str) -> ThreadGoalSummary | None:
        try:
            return self._ports.get_thread_goal(thread_id)
        except Exception as exc:
            if _is_goals_feature_disabled_error(exc):
                raise ValueError(_GOALS_DISABLED_TEXT) from exc
            raise

    def _require_resumable_goal(self, thread_id: str) -> ThreadGoalSummary:
        goal = self._get_thread_goal_or_raise(thread_id)
        if goal is None:
            raise ValueError("当前 thread 没有可恢复的 goal。")
        return goal


    def _set_goal_direct(
        self,
        sender_id: str,
        chat_id: str,
        objective: str,
        *,
        message_id: str = "",
        attached_notice: bool = False,
    ) -> CommandResult:
        thread_id, thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        self._require_thread_mutation_access(
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        goal = self._ports.mutate_goal(
            sender_id,
            chat_id,
            thread_id,
            objective=objective,
            message_id=message_id,
        )
        self._project_goal(sender_id, chat_id, goal, message_id=message_id)
        notice = "已设置当前 thread goal。"
        if attached_notice:
            notice += "\n当前会话已恢复接收该 thread 的飞书推送。"
        return CommandResult(
            card=build_goal_card(
                thread_id=thread_id,
                thread_title=thread_title,
                goal=goal,
                notice=notice,
            )
        )

    def _update_goal_status_direct(
        self,
        sender_id: str,
        chat_id: str,
        status: str,
        *,
        message_id: str = "",
        attached_notice: bool = False,
    ) -> CommandResult:
        thread_id, thread_title = self._current_thread(sender_id, chat_id, message_id=message_id)
        self._require_thread_mutation_access(
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        goal = self._ports.mutate_goal(
            sender_id,
            chat_id,
            thread_id,
            status=status,
            message_id=message_id,
        )
        self._project_goal(sender_id, chat_id, goal, message_id=message_id)
        notice = "已暂停当前 thread goal。" if status == "paused" else "已恢复当前 thread goal。"
        if attached_notice:
            notice += "\n当前会话已恢复接收该 thread 的飞书推送。"
        return CommandResult(
            card=build_goal_card(
                thread_id=thread_id,
                thread_title=thread_title,
                goal=goal,
                notice=notice,
            )
        )

    def _require_thread_mutation_access(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> None:
        """Keep Feishu goal controls behind the live interaction writer.

        A goal update changes the same shared Codex thread as a prompt or
        steer.  It is therefore not a harmless local preference and cannot
        bypass a Web/fcodex owner.
        """

        denial = str(
            self._ports.thread_mutation_denial_text(
                sender_id,
                chat_id,
                thread_id,
                message_id=message_id,
            )
            or ""
        ).strip()
        if denial:
            raise ValueError(denial)
