"""RuntimeLoop-owned fcodex request and active-main-turn coordination."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Literal

from bot.adapters.base import ThreadSummary
from bot.direct_thread_target_policy import require_direct_thread_target
from bot.fcodex.interaction_contract import (
    fcodex_allow as _allow,
    fcodex_client_request_key,
    fcodex_connection_id,
    fcodex_deny as _deny,
)
from bot.fcodex.interaction_inbox import FcodexInteractionWriter
from bot.fcodex.main_turn_owner import FcodexMainTurnOwner
from bot.fcodex.operation_contract import (
    EXPLICITLY_DENIED_THREAD_MUTATION_METHODS,
    EXCLUSIVE_MAIN_TURN_START_METHODS,
    FcodexRequestEpochCloseReceipt,
    THREAD_MUTATION_METHODS,
    THREAD_READ_METHODS,
    THREAD_RUNTIME_METHODS,
    UNSUPPORTED_ASYNC_THREAD_MUTATION_METHODS,
    fcodex_client_response_receipt,
    fcodex_known_noncontinuing_goal_mutation_result,
    fcodex_notification_proves_runtime_unloaded,
    fcodex_successful_response_thread_identity,
    strict_fcodex_interrupt_target,
    strict_fcodex_steer_target,
)
from bot.fcodex.participant_runtime_registry import (
    FcodexParticipantRuntimeRegistry,
    FcodexRequestTransitionReceipt,
)
from bot.fcodex.thread_create_owner import FcodexThreadCreateOwner
from bot.operation_owner_state import FcodexClientRequest as _ClientRequest
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.interaction_lease_store import (
    InteractionLeaseHolder,
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
)
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry


logger = logging.getLogger(__name__)


_EXTERNAL_SETTINGS_UNKNOWN_METHODS = frozenset(
    {
        "review/start",
        "thread/archive",
        "thread/compact/start",
        "thread/delete",
        "thread/resume",
        "thread/settings/update",
        "thread/unarchive",
        "turn/start",
    }
)


class FcodexOperationService:
    """Own exact proxy requests and the fcodex active-main-turn projection.

    Socket liveness and runtime sources remain in the participant Registry.
    Neither creates a root-operation writer. Ordinary ``turn/start`` is tracked
    without a lease. A blank interaction lease exists only around an exclusive
    or autonomous call that can start a main turn; lifecycle events activate or
    release that exact process-bound generation.
    """

    def __init__(
        self,
        *,
        interaction_lease_store: InteractionLeaseStore,
        participant_runtime_registry: FcodexParticipantRuntimeRegistry,
        thread_create_owner: FcodexThreadCreateOwner,
        effective_settings: ThreadEffectiveSettingsRegistry,
        runtime_context_guard: RuntimeContextGuard,
        owner_changed: Callable[[str, str], None],
    ) -> None:
        if not isinstance(participant_runtime_registry, FcodexParticipantRuntimeRegistry):
            raise TypeError("FcodexOperationService 需要 participant runtime owner。")
        if not isinstance(thread_create_owner, FcodexThreadCreateOwner):
            raise TypeError("FcodexOperationService 需要 targetless thread/create owner。")
        if not isinstance(effective_settings, ThreadEffectiveSettingsRegistry):
            raise TypeError("FcodexOperationService 需要 effective settings owner。")
        required = (
            runtime_context_guard,
            owner_changed,
        )
        if any(not callable(capability) for capability in required):
            raise TypeError("FcodexOperationService 的 effect capability 必须全部可调用。")
        self._runtime_context_guard = runtime_context_guard
        self._interaction_lease_store = interaction_lease_store
        self._participant_runtime_registry = participant_runtime_registry
        self._thread_create_owner = thread_create_owner
        self._effective_settings = effective_settings
        self._owner_changed = owner_changed
        self._client_requests: dict[str, _ClientRequest] = {}
        self._direct_root_ids: set[str] = set()
        self._request_token_sequence = 0
        self._main_turns = FcodexMainTurnOwner(
            interaction_leases=interaction_lease_store,
            track_request=self._tracked_admission,
            endpoint_is_live=participant_runtime_registry.has_live_endpoint,
            owner_changed=owner_changed,
            runtime_context_guard=runtime_context_guard,
        )

    def admit(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        method: str,
        thread_id: str,
        request_params: object,
        resume_may_autostart: bool = False,
        continuation_risk: bool = False,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        self._participant_runtime_registry.require_live_endpoint(
            participant_id,
            connection_id,
        )
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = fcodex_connection_id(connection_id)
        normalized_method = str(method or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        request_key = fcodex_client_request_key(
            normalized_participant_id,
            normalized_connection_id,
            request_id,
        )
        if request_key in self._client_requests:
            return _deny(
                "fcodex connection 复用了尚未完成的 JSON-RPC request id；已拒绝该请求。"
            )

        if normalized_method == "thread/start":
            if normalized_thread_id:
                return _deny("thread/start 不应携带 threadId。")
            try:
                attempt = self._thread_create_owner.begin(
                    participant_id=normalized_participant_id,
                )
            except Exception as exc:
                return _deny(str(exc))
            return self._tracked_admission(
                _ClientRequest(
                    request_key=request_key,
                    participant_id=normalized_participant_id,
                    connection_id=normalized_connection_id,
                    method=normalized_method,
                    thread_id="",
                    root_thread_id="",
                    external_create_attempt=attempt,
                ),
                root_thread_id="",
            )

        if not normalized_thread_id:
            if normalized_method == "turn/interrupt":
                return _deny(
                    "turn/interrupt 缺少 exact root threadId；已按 fail-closed 拒绝。"
                )
            if normalized_method == "turn/steer":
                return _deny(
                    "turn/steer 缺少 exact root threadId；已按 fail-closed 拒绝。"
                )
            return _allow(root_thread_id="")

        if normalized_method in THREAD_READ_METHODS:
            return _allow(root_thread_id=self._known_root(normalized_thread_id))

        if normalized_method == "turn/start":
            exact_root = self._known_root(normalized_thread_id)
            if not exact_root or exact_root != normalized_thread_id:
                return _deny(
                    "Focus 无法确认 fcodex main turn 的 exact root；已拒绝。"
                )
            decision = self._tracked_admission(
                _ClientRequest(
                    request_key=request_key,
                    participant_id=normalized_participant_id,
                    connection_id=normalized_connection_id,
                    method=normalized_method,
                    thread_id=normalized_thread_id,
                    root_thread_id=exact_root,
                ),
                root_thread_id=exact_root,
            )
            if decision.get("allowed") is True:
                self._retire_external_settings_evidence(
                    normalized_method,
                    normalized_thread_id,
                )
            return decision

        if normalized_method in EXCLUSIVE_MAIN_TURN_START_METHODS:
            exact_root = self._known_root(normalized_thread_id)
            decision = self._main_turns.admit_exclusive_start(
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                request_key=request_key,
                method=normalized_method,
                root_thread_id=exact_root,
                exact_root=exact_root == normalized_thread_id,
            )
            if decision.get("allowed") is True:
                self._retire_external_settings_evidence(
                    normalized_method,
                    normalized_thread_id,
                )
            return decision

        if normalized_method == "thread/resume":
            decision = self._admit_thread_resume(
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                request_key=request_key,
                thread_id=normalized_thread_id,
                resume_may_autostart=bool(resume_may_autostart),
            )
            if decision.get("allowed") is True:
                self._retire_external_settings_evidence(
                    normalized_method,
                    normalized_thread_id,
                )
            return decision

        if normalized_method in THREAD_RUNTIME_METHODS:
            request = _ClientRequest(
                request_key=request_key,
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                method=normalized_method,
                thread_id=normalized_thread_id,
                root_thread_id=self._known_root(normalized_thread_id),
            )
            return self._tracked_admission(
                request,
                root_thread_id=request.root_thread_id,
            )

        if normalized_method in EXPLICITLY_DENIED_THREAD_MUTATION_METHODS:
            return _deny(
                f"Focus v1 不支持 fcodex method `{normalized_method}`；"
                "它会创建一个尚未定义独立 thread 合同的目标，已在本地拒绝。"
            )
        if normalized_method in UNSUPPORTED_ASYNC_THREAD_MUTATION_METHODS:
            return _deny(
                f"fcodex method `{normalized_method}` 尚未纳入共享 backend 的多前端合同；"
                "已在本地拒绝。"
            )
        if normalized_method not in THREAD_MUTATION_METHODS:
            return _deny(
                f"Focus 尚未分类 fcodex 的 thread-scoped method `{normalized_method}`；已按 fail-closed 拒绝。"
            )

        root_thread_id = self._known_root(normalized_thread_id)
        if not root_thread_id:
            return _deny("Focus 正在确认该 thread 是否为直接目标；请稍后重试。")

        if normalized_method == "turn/interrupt":
            interrupt_target = strict_fcodex_interrupt_target(request_params)
            if interrupt_target is None:
                return _deny(
                    "turn/interrupt 只接受 exact nonempty string threadId 与"
                    " whitespace-exact string turnId；"
                    "请求未转发。"
                )
            requested_thread_id, requested_turn_id = interrupt_target
            if requested_thread_id != root_thread_id:
                return _deny(
                    "turn/interrupt 的 raw threadId 与权威 direct root 不一致；"
                    "请求未转发。"
                )
            if requested_turn_id:
                attached = self._exact_turn_endpoint_is_attached(
                    normalized_participant_id,
                    normalized_connection_id,
                    root_thread_id,
                    requested_turn_id,
                )
            else:
                attached = self._live_endpoint_has_connection_source(
                    normalized_participant_id,
                    normalized_connection_id,
                    root_thread_id,
                )
            if not attached:
                return _deny(
                    "当前 fcodex connection 未 attach 到该 exact direct root；"
                    "请求未转发。"
                )
            return self._tracked_admission(
                _ClientRequest(
                    request_key=request_key,
                    participant_id=normalized_participant_id,
                    connection_id=normalized_connection_id,
                    method=normalized_method,
                    thread_id=root_thread_id,
                    root_thread_id=root_thread_id,
                ),
                root_thread_id=root_thread_id,
            )

        if normalized_method == "turn/steer":
            steer_target = strict_fcodex_steer_target(request_params)
            if steer_target is None:
                return _deny(
                    "turn/steer 只接受 exact threadId/input/expectedTurnId、"
                    "stable clientUserMessageId 与空 experimental context；"
                    "请求未转发。"
                )
            requested_thread_id, expected_turn_id = steer_target
            if requested_thread_id != root_thread_id:
                return _deny(
                    "turn/steer 的 raw threadId 与权威 direct root 不一致；请求未转发。"
                )
            if not self._exact_turn_endpoint_is_attached(
                normalized_participant_id,
                normalized_connection_id,
                root_thread_id,
                expected_turn_id,
            ):
                return _deny(
                    "当前 fcodex connection 未 attach 到该 exact active turn；"
                    "请求未转发。"
                )
            return self._tracked_admission(
                _ClientRequest(
                    request_key=request_key,
                    participant_id=normalized_participant_id,
                    connection_id=normalized_connection_id,
                    method=normalized_method,
                    thread_id=root_thread_id,
                    root_thread_id=root_thread_id,
                ),
                root_thread_id=root_thread_id,
            )

        goal_may_start = bool(
            normalized_method == "thread/goal/set" and continuation_risk
        )
        if goal_may_start:
            decision = self._admit_autonomous_mutation(
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                request_key=request_key,
                method=normalized_method,
                root_thread_id=root_thread_id,
            )
            if decision.get("allowed") is True:
                self._effective_settings.mark_external_unknown(root_thread_id)
            return decision

        denial = self._control_writer_denial(
            normalized_participant_id,
            normalized_connection_id,
            root_thread_id,
        )
        if denial:
            return _deny(denial)
        decision = self._tracked_admission(
            _ClientRequest(
                request_key=request_key,
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                method=normalized_method,
                thread_id=normalized_thread_id,
                root_thread_id=root_thread_id,
            ),
            root_thread_id=root_thread_id,
        )
        self._retire_external_settings_evidence(
            normalized_method,
            normalized_thread_id,
        )
        return decision

    def _retire_external_settings_evidence(
        self,
        method: str,
        thread_id: str,
    ) -> None:
        """Make settings unknown before a non-canonical effect can be sent.

        Fcodex responses and connection-local notifications are not registry
        writers. Cross-connection delivery exposes no revision or causal
        ordering token, so reviewed turn/settings/lifecycle effects retire the
        exact thread for the remainder of this backend epoch. This one coarse
        negative fact is safer than duplicating an evolving upstream schema.
        """

        if method in _EXTERNAL_SETTINGS_UNKNOWN_METHODS:
            self._effective_settings.mark_external_unknown(thread_id)

    def _admit_thread_resume(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_key: str,
        thread_id: str,
        resume_may_autostart: bool,
    ) -> dict[str, Any]:
        root_thread_id = self._known_root(thread_id)
        if not root_thread_id:
            return _deny("Focus 正在确认该 thread 是否为直接目标；请稍后重试。")

        request = _ClientRequest(
            request_key=request_key,
            participant_id=participant_id,
            connection_id=connection_id,
            method="thread/resume",
            thread_id=thread_id,
            root_thread_id=root_thread_id,
            resume_may_autostart=resume_may_autostart,
            continuation_risk=resume_may_autostart,
        )
        active_turn_attach = self._root_has_active_turn(root_thread_id)
        if active_turn_attach:
            # An active-turn attach needs no blank submission. This does not
            # promise that upstream running-resume cannot invoke idle goal
            # continuation if the turn races to completion.
            denial = ""
        elif resume_may_autostart:
            denial = self._acquire_blank_submission(request)
        else:
            denial = self._observer_resume_denial(
                participant_id,
                connection_id,
                root_thread_id,
            )
        if denial:
            return _deny(denial)
        try:
            request.runtime_request_source = (
                self._participant_runtime_registry.retain_request_source(
                    participant_id,
                    connection_id,
                    request_key,
                    thread_id,
                )
            )
        except Exception as exc:
            self._release_known_unforwarded_submission(request)
            return _deny(str(exc))
        return self._tracked_admission(
            request,
            root_thread_id=root_thread_id,
        )

    def _admit_autonomous_mutation(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_key: str,
        method: str,
        root_thread_id: str,
    ) -> dict[str, Any]:
        request = _ClientRequest(
            request_key=request_key,
            participant_id=participant_id,
            connection_id=connection_id,
            method=method,
            thread_id=root_thread_id,
            root_thread_id=root_thread_id,
            continuation_risk=True,
        )
        holder = self._holder(participant_id, connection_id)
        try:
            current = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return _deny("Focus 无法核对 active-turn owner；请求未转发。")
        if current is not None:
            if not current.holder.same_holder(holder) or not current.turn_id:
                return _deny("当前线程已有 main turn writer 或 submission；请等待其结束后再试。")
            request.active_turn_id = current.turn_id
        else:
            denial = self._acquire_blank_submission(request)
            if denial:
                return _deny(denial)
        return self._tracked_admission(request, root_thread_id=root_thread_id)

    def client_response(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        request_token: int,
        outcome: str,
        response_result: dict[str, Any] | None = None,
        observed_thread_id: str = "",
        observed_root_thread_id: str = "",
    ) -> dict[str, Any]:
        """Settle one exact proxy request without creating root retention."""

        self._runtime_context_guard()
        request_key = fcodex_client_request_key(participant_id, connection_id, request_id)
        request = self._client_requests.get(request_key)
        if request is None or not self._request_token_matches(
            request,
            participant_id=participant_id,
            connection_id=connection_id,
            request_token=request_token,
        ):
            return {"known": False, "settled": False}
        pending = fcodex_client_response_receipt(request.request_token, settled=False)
        normalized_outcome = str(outcome or "").strip().lower()
        if normalized_outcome not in {"success", "error", "unknown"}:
            return pending

        if request.method == "thread/start":
            return self._thread_create_owner.settle_client_response(
                request,
                outcome=normalized_outcome,
                observed_thread_id=observed_thread_id,
                observed_root_thread_id=observed_root_thread_id,
                remember_direct_root=self._remember_created_direct_root,
                settle_local_request=lambda receipt: (
                    self._commit_client_request_settlement(
                        request,
                        runtime_receipt=receipt,
                    )
                ),
            )

        trusted_resume = True
        if normalized_outcome == "success" and request.method == "thread/resume":
            trusted_resume = fcodex_successful_response_thread_identity(
                request.method,
                admitted_thread_id=request.thread_id,
                admitted_root_thread_id=request.root_thread_id,
                observed_thread_id=observed_thread_id,
                observed_root_thread_id=observed_root_thread_id,
            ) is not None
            if not trusted_resume:
                normalized_outcome = "unknown"

        runtime_receipt: FcodexRequestTransitionReceipt | None = None
        if request.method == "thread/resume":
            target: Literal["connection", "unknown", "discard"]
            if normalized_outcome == "success" and trusted_resume:
                target = "connection"
            elif normalized_outcome == "error":
                target = "discard"
            else:
                target = "unknown"
            runtime_receipt = self._transition_runtime_request(request, target=target)
            if request.runtime_request_source is not None and (
                runtime_receipt is None or not runtime_receipt.exact_settled
            ):
                return {**pending, "outcome_unknown": normalized_outcome == "unknown"}
        elif request.method == "thread/unsubscribe" and normalized_outcome == "success":
            self._participant_runtime_registry.forget_connection_source(
                request.participant_id,
                request.connection_id,
                request.thread_id,
            )

        if self._main_turns.owns_request(request):
            if (
                request.turn_submission_lease is not None
                and normalized_outcome == "success"
                and fcodex_known_noncontinuing_goal_mutation_result(
                    request.method,
                    response_result,
                )
            ):
                self._release_known_unforwarded_submission(
                    request,
                    reason="fcodex_goal_known_no_start",
                )
            else:
                self._main_turns.settle(
                    request,
                    outcome=normalized_outcome,
                    response_result=response_result,
                )
        if (
            normalized_outcome == "success"
            and fcodex_known_noncontinuing_goal_mutation_result(
                request.method,
                response_result,
            )
        ):
            self._release_matching_blank_for_holder(request)

        if not self._commit_client_request_settlement(
            request,
            runtime_receipt=runtime_receipt,
        ):
            return {**pending, "outcome_unknown": normalized_outcome == "unknown"}
        return fcodex_client_response_receipt(
            request.request_token,
            settled=True,
            outcome_unknown=normalized_outcome == "unknown",
        )

    def notification(self, method: str, params: dict[str, Any]) -> None:
        self._runtime_context_guard()
        normalized_method = str(method or "").strip()
        if normalized_method == "serverRequest/resolved":
            return
        payload = dict(params or {})
        thread_id = str(payload.get("threadId", "") or "").strip()
        if not thread_id:
            return
        self._main_turns.observe_notification(
            normalized_method,
            payload,
            thread_id=thread_id,
        )
        if fcodex_notification_proves_runtime_unloaded(normalized_method, payload):
            self._participant_runtime_registry.clear_thread_sources(thread_id)

    def _remember_created_direct_root(self, root_thread_id: str) -> str:
        """Remember identity proven by an exact successful thread/start."""

        self._runtime_context_guard()
        normalized_root_id = str(root_thread_id or "").strip()
        if normalized_root_id:
            self._direct_root_ids.add(normalized_root_id)
        return normalized_root_id

    def remember_authoritative_direct_target(
        self,
        summary: ThreadSummary,
        *,
        expected_thread_id: str,
        operation: str,
    ) -> str:
        self._runtime_context_guard()
        verified = require_direct_thread_target(
            summary,
            expected_thread_id=expected_thread_id,
            operation=operation,
        )
        thread_id = str(verified.thread_id or "").strip()
        self._direct_root_ids.add(thread_id)
        return thread_id

    def connection_lost(self, participant_id: str, connection_id: str) -> int:
        """Classify in-flight requests as unknown; do not retain socket ownership."""

        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        settled_unknown = 0
        for request in tuple(self._client_requests.values()):
            if (
                request.participant_id != normalized_participant_id
                or request.connection_id != normalized_connection_id
            ):
                continue
            if request.method == "thread/start":
                unknown, settled = self._settle_targetless_connection_loss(request)
                if unknown and settled:
                    settled_unknown += 1
                continue
            runtime_receipt = self._transition_runtime_request(
                request,
                target="unknown",
            )
            if request.runtime_request_source is not None and (
                runtime_receipt is None or not runtime_receipt.exact_settled
            ):
                continue
            if self._commit_client_request_settlement(
                request,
                runtime_receipt=runtime_receipt,
            ):
                settled_unknown += 1
        return settled_unknown

    def backend_disconnected(self) -> None:
        self._runtime_context_guard()
        for request in self._client_requests.values():
            if request.method == "thread/start":
                self._thread_create_owner.invalidate_backend_epoch(request)
        self._direct_root_ids.clear()

    def settle_backend_epoch_after_stop(self) -> FcodexRequestEpochCloseReceipt:
        """Retire process-local request/routing facts after the backend stopped."""

        self._runtime_context_guard()
        receipt = FcodexRequestEpochCloseReceipt(
            client_request_keys=tuple(sorted(self._client_requests)),
            routed_thread_ids=tuple(sorted(self._direct_root_ids)),
        )
        self._client_requests.clear()
        self._direct_root_ids.clear()
        return receipt

    def interaction_root_for_thread(self, thread_id: str) -> str:
        self._runtime_context_guard()
        return self._known_root(thread_id)

    def interaction_writer_for_root(
        self,
        root_thread_id: str,
    ) -> FcodexInteractionWriter | None:
        self._runtime_context_guard()
        return self._main_turns.interaction_writer(root_thread_id)

    def interaction_lease_holder_for_root(
        self,
        root_thread_id: str,
    ) -> InteractionLeaseHolder | None:
        self._runtime_context_guard()
        try:
            lease = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return None
        return lease.holder if lease is not None else None

    def shared_interaction_request_is_eligible(
        self,
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool:
        self._runtime_context_guard()
        normalized_root_id = str(root_thread_id or "").strip()
        normalized_request_thread_id = str(request_thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        return bool(
            normalized_root_id
            and normalized_request_thread_id == normalized_root_id
            and normalized_turn_id
        )

    def shared_interaction_endpoint_is_attached(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> bool:
        self._runtime_context_guard()
        return self._live_endpoint_has_connection_source(
            participant_id,
            connection_id,
            root_thread_id,
        )

    def shared_interaction_has_live_recipient(self, root_thread_id: str) -> bool:
        self._runtime_context_guard()
        return self._participant_runtime_registry.has_live_connection_source(
            root_thread_id,
        )

    def _exact_turn_endpoint_is_attached(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
        turn_id: str,
    ) -> bool:
        """Use a runtime source or exact writer lease only as attach proof."""

        if (
            not isinstance(turn_id, str)
            or not turn_id
            or turn_id != turn_id.strip()
        ):
            return False
        if self._live_endpoint_has_connection_source(
            participant_id,
            connection_id,
            root_thread_id,
        ):
            return True
        if not self._participant_runtime_registry.has_live_endpoint(
            participant_id,
            connection_id,
        ):
            return False
        try:
            lease = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return False
        return bool(
            lease is not None
            and lease.thread_id == root_thread_id
            and lease.turn_id == turn_id
            and lease.holder.kind == "fcodex"
            and lease.holder.same_holder(self._holder(participant_id, connection_id))
        )

    def _live_endpoint_has_connection_source(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> bool:
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        normalized_root_id = str(root_thread_id or "").strip()
        if not (
            normalized_participant_id
            and normalized_connection_id
            and normalized_root_id
            and self._participant_runtime_registry.has_live_endpoint(
                normalized_participant_id,
                normalized_connection_id,
            )
        ):
            return False
        source = self._participant_runtime_registry.source_snapshot(
            normalized_participant_id,
            normalized_root_id,
        )
        return normalized_connection_id in source.connection_ids

    def _known_root(self, thread_id: str) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ""
        return normalized_thread_id if normalized_thread_id in self._direct_root_ids else ""

    def _control_writer_denial(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> str:
        try:
            lease = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return "Focus 无法核对 active-turn owner；请求未转发。"
        if lease is None:
            return ""
        if lease.holder.same_holder(self._holder(participant_id, connection_id)):
            return ""
        return "当前线程由另一个 frontend 的 main turn 或 submission 控制。"

    def _observer_resume_denial(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> str:
        """Prevent a non-continuing resume from bypassing a blank submission."""

        try:
            lease = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return "Focus 无法核对 active-turn owner；请求未转发。"
        if lease is None or lease.turn_id:
            return ""
        if lease.holder.same_holder(self._holder(participant_id, connection_id)):
            return ""
        return "当前线程正处于另一个 frontend 的 turn submission；请稍后重试。"

    def _root_has_active_turn(self, root_thread_id: str) -> bool:
        try:
            lease = self._interaction_lease_store.load(root_thread_id)
        except Exception:
            return False
        return bool(
            lease is not None
            and lease.thread_id == root_thread_id
            and lease.turn_id
        )

    def _acquire_blank_submission(self, request: _ClientRequest) -> str:
        holder = self._holder(request.participant_id, request.connection_id)
        try:
            acquired = self._interaction_lease_store.acquire(
                request.root_thread_id,
                holder,
            )
        except Exception:
            return "Focus 无法核对 active-turn owner；请求未转发。"
        if not acquired.granted or not acquired.acquired or acquired.lease is None:
            return "当前线程已有 main turn writer 或 submission；请等待其结束后再试。"
        request.turn_submission_lease = acquired.lease
        self._owner_changed(
            request.root_thread_id,
            "fcodex_autonomous_turn_submission_acquired",
        )
        return ""

    def _release_known_unforwarded_submission(
        self,
        request: _ClientRequest,
        *,
        reason: str = "fcodex_submission_not_forwarded",
    ) -> None:
        lease = request.turn_submission_lease
        if lease is None or lease.turn_id:
            return
        try:
            released = self._interaction_lease_store.release_if_matches(lease)
        except Exception:
            logger.exception(
                "Unable to release exact fcodex blank submission: thread=%s",
                request.root_thread_id[:12],
            )
            return
        if released:
            self._owner_changed(request.root_thread_id, reason)

    def _release_matching_blank_for_holder(self, request: _ClientRequest) -> None:
        try:
            lease = self._interaction_lease_store.load(request.root_thread_id)
        except Exception:
            return
        if (
            lease is None
            or lease.turn_id
            or not lease.holder.same_holder(
                self._holder(request.participant_id, request.connection_id)
            )
        ):
            return
        try:
            released = self._interaction_lease_store.release_if_matches(lease)
        except Exception:
            return
        if released:
            self._owner_changed(
                request.root_thread_id,
                "fcodex_goal_known_no_start",
            )

    @staticmethod
    def _holder(participant_id: str, connection_id: str) -> InteractionLeaseHolder:
        return make_fcodex_interaction_holder(
            participant_id,
            connection_id=connection_id,
            owner_pid=os.getpid(),
        )

    def _tracked_admission(
        self,
        request: _ClientRequest,
        *,
        root_thread_id: str,
    ) -> dict[str, Any]:
        token = self._remember_client_request(request)
        decision = _allow(root_thread_id=root_thread_id)
        decision.update(
            tracks_response=True,
            request_token=token,
        )
        return decision

    def _remember_client_request(self, request: _ClientRequest) -> int:
        if not request.request_key:
            raise ValueError("fcodex client request key 不能为空。")
        self._request_token_sequence += 1
        request.request_token = self._request_token_sequence
        self._client_requests[request.request_key] = request
        return request.request_token

    @staticmethod
    def _request_token_matches(
        request: _ClientRequest,
        *,
        participant_id: str,
        connection_id: str,
        request_token: int,
    ) -> bool:
        return bool(
            request.participant_id == str(participant_id or "").strip()
            and request.connection_id == str(connection_id or "").strip()
            and not isinstance(request_token, bool)
            and isinstance(request_token, int)
            and request_token > 0
            and request.request_token == request_token
        )

    def _settle_client_request(self, request: _ClientRequest) -> None:
        if self._client_requests.get(request.request_key) is request:
            self._client_requests.pop(request.request_key, None)

    def _commit_client_request_settlement(
        self,
        request: _ClientRequest,
        *,
        runtime_receipt: FcodexRequestTransitionReceipt | None,
    ) -> bool:
        self._settle_client_request(request)
        if runtime_receipt is None:
            return True
        if self._acknowledge_runtime_request_transition(
            runtime_receipt,
            request.request_key,
        ):
            return True
        self._client_requests.setdefault(request.request_key, request)
        return False

    def _transition_runtime_request(
        self,
        request: _ClientRequest,
        *,
        target: Literal["connection", "unknown", "discard"],
    ) -> FcodexRequestTransitionReceipt | None:
        source = request.runtime_request_source
        if source is None:
            return None
        if target == "connection":
            return self._participant_runtime_registry.promote_request_to_connection(source)
        if target == "unknown":
            return self._participant_runtime_registry.promote_request_to_unknown(source)
        return self._participant_runtime_registry.discard_request_source(source)

    def _acknowledge_runtime_request_transition(
        self,
        receipt: FcodexRequestTransitionReceipt,
        request_key: str,
    ) -> bool:
        try:
            return (
                self._participant_runtime_registry.acknowledge_request_transition(receipt)
                is True
            )
        except Exception:
            logger.exception(
                "fcodex Registry request-transition ACK failed: request=%s",
                request_key,
            )
            return False

    def _settle_targetless_connection_loss(
        self,
        request: _ClientRequest,
    ) -> tuple[bool, bool]:
        return self._thread_create_owner.settle_connection_lost(
            request,
            remember_direct_root=self._remember_created_direct_root,
            settle_local_request=lambda receipt: self._commit_client_request_settlement(
                request,
                runtime_receipt=receipt,
            ),
        )
