"""Authoritative Codex thread target reads, resolution, and error semantics."""

from __future__ import annotations

from typing import Any

from bot.adapters.base import ThreadResumePage, ThreadSnapshot, ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)
from bot.direct_thread_target_policy import (
    DirectThreadTargetRegistry,
    DirectThreadTargetPolicyError,
    read_direct_thread_target,
)
from bot.exception_chain import iter_exception_chain
from bot.thread_resolution import (
    list_current_dir_threads,
    list_global_threads,
    resolve_resume_target_by_name,
)
from bot.thread_runtime_authority import PendingThreadResume, ThreadRuntimeAuthority
from bot.turn_interrupt_audit import (
    TurnInterruptSource,
    record_turn_interrupt_dispatch_attempt,
)


class CodexThreadTargetService:
    """Own direct-root target proof without owning runtime or binding facts."""

    def __init__(
        self,
        *,
        adapter: CodexAppServerAdapter,
        binding_runtime: BindingRuntimeManager,
        thread_runtime_authority: ThreadRuntimeAuthority,
        direct_thread_targets: DirectThreadTargetRegistry,
        thread_list_query_limit: int,
    ) -> None:
        self._adapter = adapter
        self._binding_runtime = binding_runtime
        self._thread_runtime_authority = thread_runtime_authority
        self._direct_thread_targets = direct_thread_targets
        self._thread_list_query_limit = thread_list_query_limit

    @staticmethod
    def is_turn_thread_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not found:")

    @staticmethod
    def is_request_timeout_error(exc: Exception) -> bool:
        return isinstance(exc, TimeoutError) and str(exc).startswith(
            "Codex request timed out:"
        )

    @staticmethod
    def runtime_recovery_reason(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return str(exc)
        if isinstance(exc, CodexRpcError):
            return str(exc.error.get("message", "") or exc)
        return str(exc)

    def interrupt_running_turn(self, *, thread_id: str, turn_id: str) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        try:
            self.read_direct_thread_summary_authoritatively(
                normalized_thread_id,
                original_arg=normalized_thread_id,
                operation="中断当前 turn",
            )
        except Exception as exc:
            raise CodexRpcPreSendError("turn/interrupt", exc) from exc
        record_turn_interrupt_dispatch_attempt(
            source=TurnInterruptSource.FEISHU_BINDING,
            thread_id=normalized_thread_id,
            turn_id=turn_id,
        )
        self._adapter.interrupt_turn(
            thread_id=normalized_thread_id,
            turn_id=turn_id,
        )

    def resolve_thread_name_target_for_control(
        self,
        thread_name: str,
    ) -> ThreadSummary:
        target = str(thread_name or "").strip()
        if not target:
            raise ValueError("thread_name 不能为空。")
        thread = resolve_resume_target_by_name(
            self._adapter,
            name=target,
            limit=self._thread_list_query_limit,
        )
        return self.read_thread_summary_authoritatively(
            thread.thread_id,
            original_arg=target,
        )

    def resolve_thread_target_for_control_params(
        self,
        params: dict[str, Any],
    ) -> ThreadSummary:
        thread_id = str(params.get("thread_id", "") or "").strip()
        thread_name = str(params.get("thread_name", "") or "").strip()
        if bool(thread_id) == bool(thread_name):
            raise ValueError("必须且只能提供 `thread_id` 或 `thread_name`。")
        if thread_id:
            return self.read_thread_summary_authoritatively(
                thread_id,
                original_arg=thread_id,
            )
        return self.resolve_thread_name_target_for_control(thread_name)

    def read_thread_snapshot_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
        include_turns: bool,
    ) -> ThreadSnapshot:
        try:
            return self._adapter.read_thread(
                thread_id,
                include_turns=include_turns,
            )
        except Exception as exc:
            if self.is_thread_not_found_error(exc):
                raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
            raise

    def read_direct_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
        operation: str,
    ) -> ThreadSummary:
        """Prove that one Feishu/control target is a directly manageable root.

        A thread card or a delayed action contains only an opaque id.  It must
        never be treated as authority to resume or mutate a `ThreadSpawn`
        child: those children are parent-owned, even when their parent record
        is temporarily absent from Focus's observation cache.  The point read
        is deliberately before runtime-lease acquisition and every upstream
        mutation.
        """

        normalized_thread_id = str(thread_id or "").strip()
        try:
            summary = read_direct_thread_target(
                normalized_thread_id,
                read_thread=lambda target_id: self.read_thread_snapshot_authoritatively(
                    target_id,
                    original_arg=original_arg,
                    include_turns=False,
                ),
                operation=operation,
            )
            self._direct_thread_targets.remember(summary)
            return summary
        except DirectThreadTargetPolicyError as exc:
            raise ValueError(str(exc)) from exc

    def read_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
    ) -> ThreadSummary:
        return self.read_direct_thread_summary_authoritatively(
            thread_id,
            original_arg=original_arg,
            operation="直接读取或管理",
        )

    def rename_direct_thread(self, thread_id: str, name: str) -> None:
        verified = self.read_direct_thread_summary_authoritatively(
            thread_id,
            original_arg=thread_id,
            operation="重命名",
        )
        self._adapter.rename_thread(verified.thread_id, name)

    def begin_web_thread_page(
        self,
        thread_id: str,
        *,
        original_arg: str,
        limit: int,
        model: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> PendingThreadResume[ThreadResumePage]:
        thread = self.lookup_thread_summary_in_bounded_list(thread_id)
        try:
            return self._thread_runtime_authority.begin_resume_thread_page(
                thread_id,
                limit=limit,
                config_overrides=config_overrides,
                model=model or None,
                approval_policy=approval_policy or None,
                permissions_profile_id=permissions_profile_id or None,
            )
        except Exception as exc:
            if self.is_thread_not_found_error(exc):
                raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
            if thread and thread.source == "cli" and self.is_transport_disconnect(
                exc
            ):
                raise RuntimeError(
                    "Codex 当前无法通过 app-server 恢复这个 CLI 线程。"
                    "这通常意味着该线程正被本地 TUI 使用，或当前版本暂不支持加载它的完整历史。"
                ) from exc
            raise

    def lookup_thread_summary_in_bounded_list(
        self,
        thread_id: str,
    ) -> ThreadSummary | None:
        threads = self.list_global_threads()
        for thread in threads:
            if thread.thread_id == thread_id:
                return thread
        return None

    @staticmethod
    def is_thread_not_found_error(exc: Exception) -> bool:
        for current in iter_exception_chain(exc):
            if isinstance(current, CodexRpcError):
                message = str(current.error.get("message", "")).lower()
                if message.startswith("no rollout found for thread id "):
                    return True
        return False

    @staticmethod
    def is_thread_not_loaded_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not loaded:")

    @staticmethod
    def is_pre_send_error(exc: Exception) -> bool:
        return isinstance(exc, CodexRpcPreSendError)

    @staticmethod
    def is_turn_interrupt_rejected_error(exc: Exception) -> bool:
        if type(exc) is not CodexRpcError or exc.method != "turn/interrupt":
            return False
        message = str(exc.error.get("message", "") or "")
        if message == "no active turn to interrupt":
            return True
        prefix = "expected active turn id "
        separator = " but found "
        if not message.startswith(prefix) or separator not in message:
            return False
        requested, actual = message[len(prefix) :].split(separator, 1)
        return bool(requested.strip(" `") and actual.strip(" `"))

    @staticmethod
    def is_transport_disconnect(exc: Exception) -> bool:
        for current in iter_exception_chain(exc):
            if isinstance(current, CodexRpcTransportError):
                return True
            if isinstance(current, CodexRpcError) and current.error.get(
                "message"
            ) in {
                "Codex websocket disconnected",
                "Codex app-server closed",
            }:
                return True
        return False

    def list_global_threads(self) -> list[ThreadSummary]:
        return list_global_threads(
            self._adapter,
            limit=self._thread_list_query_limit,
        )

    def list_visible_current_dir_threads(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> list[ThreadSummary]:
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        return list_current_dir_threads(
            self._adapter,
            cwd=runtime.working_dir,
            limit=self._thread_list_query_limit,
        )
