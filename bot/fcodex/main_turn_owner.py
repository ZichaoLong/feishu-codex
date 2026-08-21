"""Exact fcodex main-turn submission and active-writer owner."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from bot.fcodex.interaction_contract import fcodex_deny
from bot.fcodex.interaction_inbox import FcodexInteractionWriter
from bot.fcodex.operation_contract import EXCLUSIVE_MAIN_TURN_START_METHODS
from bot.operation_owner_state import FcodexClientRequest
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
)


logger = logging.getLogger(__name__)


class FcodexMainTurnOwner:
    """Own the fcodex projection of shared main-turn lifecycle identity.

    Ordinary ``turn/start`` is upstream-routed realtime input and creates no
    lease here. Exclusive review/compact and autonomous continuation paths may
    retain an exact blank until method-specific or lifecycle evidence binds it.
    """

    def __init__(
        self,
        *,
        interaction_leases: InteractionLeaseStore,
        track_request: Callable[..., dict[str, Any]],
        endpoint_is_live: Callable[[str, str], bool],
        owner_changed: Callable[[str, str], None],
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        self._leases = interaction_leases
        self._track_request = track_request
        self._endpoint_is_live = endpoint_is_live
        self._owner_changed = owner_changed
        self._runtime_context_guard = runtime_context_guard

    def admit_exclusive_start(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_key: str,
        method: str,
        root_thread_id: str,
        exact_root: bool,
    ) -> dict[str, Any]:
        """Acquire and register one exact exclusive main-turn submission."""

        self._runtime_context_guard()
        if method not in EXCLUSIVE_MAIN_TURN_START_METHODS:
            raise ValueError(
                "FcodexMainTurnOwner 只准入 review/start 与 thread/compact/start。"
            )
        if not exact_root or not root_thread_id:
            return fcodex_deny(
                "Focus 无法确认 fcodex main turn 的 exact root；已拒绝。"
            )
        holder = make_fcodex_interaction_holder(
            participant_id,
            connection_id=connection_id,
            owner_pid=os.getpid(),
        )
        try:
            acquired = self._leases.acquire(root_thread_id, holder)
        except Exception:
            logger.exception(
                "fcodex main-turn submission lease unavailable: thread=%s",
                root_thread_id[:12],
            )
            return fcodex_deny("Focus 无法核对 active-turn owner；请求未转发。")
        if not acquired.granted or not acquired.acquired or acquired.lease is None:
            return fcodex_deny(
                "当前线程已有 main turn writer 或 submission；请等待其结束后再试。"
            )
        request = FcodexClientRequest(
            request_key=request_key,
            participant_id=participant_id,
            connection_id=connection_id,
            method=method,
            thread_id=root_thread_id,
            root_thread_id=root_thread_id,
            turn_submission_lease=acquired.lease,
        )
        try:
            decision = self._track_request(request, root_thread_id=root_thread_id)
        except Exception:
            self._leases.release_if_matches(acquired.lease)
            raise
        self._owner_changed(root_thread_id, "fcodex_turn_submission_acquired")
        return decision

    @staticmethod
    def owns_request(request: FcodexClientRequest) -> bool:
        return request.turn_submission_lease is not None or bool(request.active_turn_id)

    def settle(
        self,
        request: FcodexClientRequest,
        *,
        outcome: str,
        response_result: dict[str, Any] | None,
    ) -> None:
        """Settle one response without treating a submission ID as turn identity."""

        self._runtime_context_guard()
        submission = request.turn_submission_lease
        if submission is not None:
            if outcome == "error":
                if self._leases.release_if_matches(submission):
                    self._owner_changed(
                        request.root_thread_id,
                        "fcodex_turn_submission_rejected",
                    )
            elif outcome == "success" and request.method == "review/start":
                # Inline review is method-specific: its typed response ID is
                # the replacement review turn's lifecycle identity.
                turn = (
                    response_result.get("turn")
                    if isinstance(response_result, dict)
                    and isinstance(response_result.get("turn"), dict)
                    else None
                )
                turn_id = (
                    str(turn.get("id", "") or "").strip()
                    if isinstance(turn, dict)
                    else ""
                )
                if turn_id and self._leases.activate_turn(submission, turn_id) is not None:
                    self._owner_changed(
                        request.root_thread_id,
                        "fcodex_main_turn_started",
                    )
            # Compact success and unknown exclusive outcomes keep only the
            # exact blank PID-bound submission. Lifecycle may activate it;
            # completion alone cannot prove which effect ended.

    def observe_notification(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        thread_id: str,
    ) -> None:
        """Activate or release only lifecycle-confirmed exact turn identity."""

        self._runtime_context_guard()
        if method in {"turn/started", "turn/completed"}:
            turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
            turn_id = str(turn.get("id", "") or "").strip()
            if not turn_id:
                return
            if method == "turn/completed":
                self._leases.release_turn(thread_id, turn_id)
                return
            lease = self._leases.load(thread_id)
            if lease is not None and lease.holder.kind == "fcodex" and not lease.turn_id:
                if self._leases.activate_turn(lease, turn_id) is not None:
                    self._owner_changed(thread_id, "fcodex_main_turn_started")
            return
        if method in {"thread/closed", "thread/archived", "thread/deleted"}:
            self._leases.clear_thread(thread_id)

    def interaction_writer(self, root_thread_id: str) -> FcodexInteractionWriter | None:
        self._runtime_context_guard()
        lease = self._leases.load(root_thread_id)
        if (
            lease is None
            or lease.holder.kind != "fcodex"
            or not lease.turn_id
            or not lease.holder.participant_id
            or not lease.holder.connection_id
        ):
            return None
        return FcodexInteractionWriter(
            participant_id=lease.holder.participant_id,
            connection_id=lease.holder.connection_id,
            holder=lease.holder,
            connected=self._endpoint_is_live(
                lease.holder.participant_id,
                lease.holder.connection_id,
            ),
        )

    def has_active_turn(self, root_thread_id: str) -> bool:
        self._runtime_context_guard()
        lease = self._leases.load(root_thread_id)
        return bool(
            lease is not None
            and lease.holder.kind == "fcodex"
            and lease.turn_id
        )
