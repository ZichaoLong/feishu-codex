"""Closed Feishu binding thread transitions.

``BindingRuntimeManager`` remains the single mutable binding owner.  This
coordinator owns the exact session check, durable bind/clear commit, process-
local FIFO generation invalidation, and detached timer effects.  Callers never
take the manager lock or compose those steps themselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ContextManager, Protocol

from bot.binding_runtime_contract import (
    OWNER_LOSS_DISPOSITION_ABANDON,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
    BindingThreadBindResult,
    BindingThreadClearResult,
    OwnerLossDisposition,
)
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects


logger = logging.getLogger(__name__)


class FeishuBindingTransitionChanged(RuntimeError):
    """The captured binding session no longer authorizes this transition."""


class FeishuBindingTransitionRuntime(Protocol):
    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...

    def resident_session_snapshot_locked(
        self,
        binding: tuple[str, str],
    ) -> BindingSessionSnapshot | None: ...

    def bind_thread_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        thread_id: str,
        thread_title: str,
        working_dir: str,
        owner_loss_disposition: OwnerLossDisposition,
    ) -> BindingThreadBindResult: ...

    def clear_thread_binding_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        working_dir_after_clear: str | None,
        require_no_inflight_turn: bool,
        owner_loss_disposition: OwnerLossDisposition,
    ) -> BindingThreadClearResult: ...


class FeishuBindingExecutionQueue(Protocol):
    def invalidate_binding(self, binding: tuple[str, str]) -> object: ...


@dataclass(frozen=True, slots=True)
class BindFeishuThreadCommand:
    session: BindingSessionSnapshot
    thread_id: str
    thread_title: str
    working_dir: str | None = None
    owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON


@dataclass(frozen=True, slots=True)
class ClearFeishuThreadCommand:
    session: BindingSessionSnapshot
    working_dir_after_clear: str | None = None
    require_no_inflight_turn: bool = False
    owner_loss_disposition: OwnerLossDisposition = OWNER_LOSS_DISPOSITION_ABANDON


@dataclass(frozen=True, slots=True)
class FeishuBindingTransitionCommit:
    session: BindingSessionSnapshot
    previous_thread_id: str
    unsubscribe_thread_id: str
    queue_cleanup_failed: bool = False


class FeishuBindingTransitionOwner:
    """Commit one exact Feishu binding transition and return cleanup facts."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: FeishuBindingTransitionRuntime,
        execution_queue: FeishuBindingExecutionQueue,
    ) -> None:
        if not all(
            hasattr(binding_runtime, member)
            for member in (
                "session_snapshot_locked",
                "resident_session_snapshot_locked",
                "bind_thread_locked",
                "clear_thread_binding_locked",
            )
        ):
            raise TypeError("Feishu binding transition 缺少 binding runtime owner。")
        if not hasattr(execution_queue, "invalidate_binding"):
            raise TypeError("Feishu binding transition 缺少 execution queue owner。")
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._execution_queue = execution_queue

    def bind_thread(
        self,
        command: BindFeishuThreadCommand,
    ) -> FeishuBindingTransitionCommit:
        self._require_session(command.session)
        thread_id = self._nonempty_text(command.thread_id, field="thread_id")
        thread_title = self._text(command.thread_title, field="thread_title")
        working_dir_override = self._optional_nonempty_text(
            command.working_dir,
            field="working_dir",
        )
        disposition = self._disposition(command.owner_loss_disposition)

        with self._lock:
            current = self._require_transition_current_locked(command.session)
            previous_thread_id = current.current_thread_id.strip()
            working_dir = working_dir_override or current.working_dir.strip()
            if not working_dir:
                raise ValueError("binding thread transition working_dir 不能为空。")
            result = self._binding_runtime.bind_thread_locked(
                command.session.handle,
                thread_id=thread_id,
                thread_title=thread_title,
                working_dir=working_dir,
                owner_loss_disposition=disposition,
            )
            committed = self._require_committed_session_locked(
                command.session.binding,
                expected_thread_id=thread_id,
            )

        queue_cleanup_failed = self._invalidate_replaced_queue(
            command.session.binding,
            previous_thread_id=previous_thread_id,
            current_thread_id=thread_id,
        )
        cancel_runtime_timer_effects(result.timer_cancellations)
        return FeishuBindingTransitionCommit(
            session=committed,
            previous_thread_id=previous_thread_id,
            unsubscribe_thread_id=result.unsubscribe_thread_id,
            queue_cleanup_failed=queue_cleanup_failed,
        )

    def clear_thread(
        self,
        command: ClearFeishuThreadCommand,
    ) -> FeishuBindingTransitionCommit:
        self._require_session(command.session)
        working_dir_after_clear = self._optional_nonempty_text(
            command.working_dir_after_clear,
            field="working_dir_after_clear",
        )
        if type(command.require_no_inflight_turn) is not bool:
            raise TypeError("require_no_inflight_turn 必须是 exact bool。")
        disposition = self._disposition(command.owner_loss_disposition)

        with self._lock:
            current = self._require_transition_current_locked(command.session)
            previous_thread_id = current.current_thread_id.strip()
            result = self._binding_runtime.clear_thread_binding_locked(
                command.session.handle,
                working_dir_after_clear=working_dir_after_clear,
                require_no_inflight_turn=command.require_no_inflight_turn,
                owner_loss_disposition=disposition,
            )
            committed = self._require_committed_session_locked(
                command.session.binding,
                expected_thread_id="",
            )

        queue_cleanup_failed = self._invalidate_replaced_queue(
            command.session.binding,
            previous_thread_id=previous_thread_id,
            current_thread_id="",
        )
        cancel_runtime_timer_effects(result.timer_cancellations)
        return FeishuBindingTransitionCommit(
            session=committed,
            previous_thread_id=previous_thread_id,
            unsubscribe_thread_id=result.unsubscribe_thread_id,
            queue_cleanup_failed=queue_cleanup_failed,
        )

    def _require_transition_current_locked(
        self,
        captured: BindingSessionSnapshot,
    ) -> BindingSessionSnapshot:
        try:
            current = self._binding_runtime.session_snapshot_locked(captured.handle)
        except RuntimeError as exc:
            raise FeishuBindingTransitionChanged(
                "binding session retired or replaced before thread transition"
            ) from exc
        if current.current_thread_id != captured.current_thread_id:
            raise FeishuBindingTransitionChanged(
                "binding thread changed before exact thread transition"
            )
        return current

    def _require_committed_session_locked(
        self,
        binding: tuple[str, str],
        *,
        expected_thread_id: str,
    ) -> BindingSessionSnapshot:
        committed = self._binding_runtime.resident_session_snapshot_locked(binding)
        if committed is None:
            raise RuntimeError("binding runtime disappeared after thread transition commit")
        if committed.current_thread_id != expected_thread_id:
            raise RuntimeError("binding thread transition committed an unexpected thread")
        return committed

    def _invalidate_replaced_queue(
        self,
        binding: tuple[str, str],
        *,
        previous_thread_id: str,
        current_thread_id: str,
    ) -> bool:
        if not previous_thread_id or previous_thread_id == current_thread_id:
            return False
        try:
            self._execution_queue.invalidate_binding(binding)
        except Exception:
            logger.exception(
                "binding commit 后清理旧 owner FIFO 失败: binding=%s/%s",
                binding[0],
                binding[1],
            )
            return True
        return False

    @staticmethod
    def _require_session(value: object) -> None:
        if type(value) is not BindingSessionSnapshot:
            raise TypeError("Feishu binding transition requires an exact session")

    @staticmethod
    def _text(value: object, *, field: str) -> str:
        if type(value) is not str:
            raise TypeError(f"{field} 必须是 exact string。")
        return value.strip()

    @classmethod
    def _nonempty_text(cls, value: object, *, field: str) -> str:
        normalized = cls._text(value, field=field)
        if not normalized:
            raise ValueError(f"{field} 不能为空。")
        return normalized

    @classmethod
    def _optional_nonempty_text(
        cls,
        value: object,
        *,
        field: str,
    ) -> str | None:
        if value is None:
            return None
        return cls._nonempty_text(value, field=field)

    @staticmethod
    def _disposition(value: object) -> OwnerLossDisposition:
        if value not in {"abandon", "terminal"}:
            raise ValueError("owner_loss_disposition 必须是 abandon 或 terminal。")
        return value  # type: ignore[return-value]
