"""Decode and dispatch the fcodex operation-owner control protocol."""

from __future__ import annotations

import logging
from typing import Any

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.adapters.codex_thread_summary import thread_summary_from_app_server_thread
from bot.codex_protocol.client import CodexRpcError
from bot.direct_thread_target_policy import (
    DirectThreadTargetRegistry,
    DirectThreadTargetPolicyError,
    require_direct_thread_target,
)
from bot.fcodex.operation_contract import is_strict_fcodex_child_metadata_read
from bot.goal_continuation_policy import goal_status_may_continue
from bot.operation_owner_coordinator import OperationOwnerCoordinator

logger = logging.getLogger("bot.focus_runtime")


class FcodexControlDispatcher:
    """Own the service-side ``operation/*`` protocol boundary.

    The caller remains responsible for entering ``RuntimeLoop`` before
    dispatch. This owner holds only references to existing authorities; it
    introduces no mutable protocol or lifecycle fact of its own.
    """

    def __init__(
        self,
        *,
        adapter: CodexAppServerAdapter,
        adapter_ingress_gate: AdapterIngressGate,
        operation_owner: OperationOwnerCoordinator,
        direct_thread_targets: DirectThreadTargetRegistry,
    ) -> None:
        self._adapter = adapter
        self._adapter_ingress_gate = adapter_ingress_gate
        self._operation_owner = operation_owner
        self._direct_thread_targets = direct_thread_targets

    def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one serialized fcodex operation-control request."""

        normalized_method = str(method or "").strip()
        participant_id = str(params.get("participant_id", "") or "").strip()
        connection_id = str(params.get("connection_id", "") or "").strip()
        if normalized_method != "operation/participant-disconnected":
            adapter_ingress = self._adapter_ingress_gate.snapshot()
            if (
                adapter_ingress.backend_reset_blocked
                or adapter_ingress.cleanup_required
                or adapter_ingress.disconnect_cleanup_pending
            ):
                raise RuntimeError(
                    "当前 backend epoch 已关闭且 replacement 尚未发布；"
                    "fcodex operation control 已按 fail-closed 拒绝。"
                )
        if normalized_method == "operation/participant-connected":
            return self._operation_owner.participant_connected(participant_id, connection_id)
        if normalized_method == "operation/participant-heartbeat":
            return self._operation_owner.participant_heartbeat(participant_id, connection_id)
        if normalized_method == "operation/transport-admit":
            return {
                "allowed": self._operation_owner.has_connected_participant_connection(
                    participant_id,
                    connection_id,
                )
            }
        if normalized_method == "operation/participant-disconnected":
            return self._operation_owner.participant_disconnected(participant_id, connection_id)
        if normalized_method == "operation/admit":
            return self._admit(participant_id, connection_id, params)
        if normalized_method == "operation/client-response":
            return self._client_response(participant_id, connection_id, params)
        if normalized_method == "operation/server-request":
            request_params = params.get("request_params")
            if not isinstance(request_params, dict):
                raise ValueError("operation/server-request 缺少 request_params。")
            return self._operation_owner.server_request(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                method=str(params.get("rpc_method", "") or ""),
                params=request_params,
            )
        if normalized_method == "operation/request-response-admit":
            return self._operation_owner.response_admit(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                response_token=str(params.get("response_token", "") or ""),
            )
        if normalized_method == "operation/request-response-submit":
            response_result = params.get("response_result")
            response_error = params.get("response_error")
            return self._operation_owner.response_submit(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                response_token=str(params.get("response_token", "") or ""),
                result=response_result if isinstance(response_result, dict) else None,
                error=response_error if isinstance(response_error, dict) else None,
            )
        if normalized_method == "operation/request-response-invalid":
            return self._operation_owner.response_invalid(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                response_token=str(params.get("response_token", "") or ""),
            )
        if normalized_method == "operation/request-response-sent":
            self._operation_owner.response_sent(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                response_token=str(params.get("response_token", "") or ""),
            )
            return {"ok": True}
        if normalized_method == "operation/request-response-unknown":
            self._operation_owner.response_unknown(
                participant_id=participant_id,
                connection_id=connection_id,
                request_id=params.get("request_id"),
                response_token=str(params.get("response_token", "") or ""),
            )
            return {"ok": True}
        if normalized_method == "operation/notification":
            notification_params = params.get("notification_params")
            if not isinstance(notification_params, dict):
                raise ValueError("operation/notification 缺少 notification_params。")
            self._operation_owner.notification(
                str(params.get("rpc_method", "") or ""),
                notification_params,
            )
            return {"ok": True}
        raise ValueError(f"未知 operation control method：{normalized_method}")

    def _admit(
        self,
        participant_id: str,
        connection_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        rpc_method = str(params.get("rpc_method", "") or "").strip()
        thread_id = str(params.get("thread_id", "") or "").strip()
        request_params = params.get("request_params")
        resume_may_autostart = False
        # `thread/goal/set` persists before applying runtime effects. Derive
        # continuation risk locally so a transport omission cannot downgrade
        # the raw mutation.
        continuation_risk = rpc_method == "thread/goal/set"
        if thread_id:
            try:
                snapshot = self._adapter.read_thread(thread_id, include_turns=False)
                if not isinstance(snapshot, ThreadSnapshot) or not isinstance(
                    snapshot.summary, ThreadSummary
                ):
                    raise DirectThreadTargetPolicyError(
                        "无法确认直接操作目标的权威 thread 摘要；已按 fail-closed 拒绝。"
                    )
                summary = snapshot.summary
                if (
                    is_strict_fcodex_child_metadata_read(
                        rpc_method,
                        thread_id,
                        request_params,
                    )
                    and str(summary.thread_id or "").strip() == thread_id
                    and summary.source_status == "known"
                    and str(summary.source or "").strip() == "subAgent"
                    and str(summary.subagent_kind or "").strip() == "threadSpawn"
                    and bool(str(summary.parent_thread_id or "").strip())
                ):
                    return {
                        "allowed": True,
                        "child_read_only": True,
                        "tracks_response": False,
                        "request_token": None,
                    }
                summary = require_direct_thread_target(
                    summary,
                    expected_thread_id=thread_id,
                    operation=f"通过 fcodex `{rpc_method or 'thread 操作'}` 直接操作",
                )
                self._operation_owner.remember_authoritative_direct_target(
                    summary,
                    expected_thread_id=thread_id,
                    operation=f"通过 fcodex `{rpc_method or 'thread 操作'}` 直接操作",
                )
            except DirectThreadTargetPolicyError as exc:
                return {"allowed": False, "reason": str(exc)}
            except Exception:
                logger.warning(
                    "Unable to authority-read fcodex direct target before admission: "
                    "thread=%s method=%s",
                    thread_id[:12],
                    rpc_method,
                    exc_info=True,
                )
                return {
                    "allowed": False,
                    "reason": (
                        "无法权威确认 fcodex 直接操作目标；已按 fail-closed 拒绝，"
                        "请求未转发到 Codex backend。"
                    ),
                }
            self._direct_thread_targets.remember(summary)
            if rpc_method == "thread/resume":
                resume_may_autostart = self._resume_may_autostart(thread_id)
        return self._operation_owner.admit(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=params.get("request_id"),
            method=rpc_method,
            thread_id=thread_id,
            request_params=request_params,
            resume_may_autostart=resume_may_autostart,
            continuation_risk=continuation_risk,
        )

    def _client_response(
        self,
        participant_id: str,
        connection_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raw_response_result = params.get("response_result")
        response_result = (
            dict(raw_response_result) if isinstance(raw_response_result, dict) else None
        )
        observed_thread_id = ""
        observed_root_thread_id = ""
        raw_thread = (
            response_result.get("thread")
            if isinstance(response_result, dict)
            and isinstance(response_result.get("thread"), dict)
            else None
        )
        if raw_thread is not None:
            try:
                observed = thread_summary_from_app_server_thread(raw_thread)
                observed_thread_id = str(observed.thread_id or "").strip()
                require_direct_thread_target(
                    observed,
                    expected_thread_id=observed_thread_id,
                    operation="结算 fcodex client response",
                )
                observed_root_thread_id = observed_thread_id
            except Exception:
                observed_root_thread_id = ""
        return self._operation_owner.client_response(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=params.get("request_id"),
            request_token=params.get("request_token"),
            outcome=str(params.get("outcome", "") or ""),
            response_result=response_result,
            observed_thread_id=observed_thread_id,
            observed_root_thread_id=observed_root_thread_id,
        )

    def _resume_may_autostart(self, thread_id: str) -> bool:
        """Classify a raw fcodex resume before it crosses app-server."""

        try:
            goal = self._adapter.get_thread_goal(thread_id)
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                return False
            logger.warning(
                "Unable to read persisted goal before fcodex thread/resume; "
                "classifying continuation risk fail-closed: thread=%s",
                str(thread_id or "")[:12],
                exc_info=True,
            )
            return True
        if goal is None:
            return False
        return goal_status_may_continue(getattr(goal, "status", ""))

    @staticmethod
    def _is_goals_feature_disabled_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        return (
            str(exc.error.get("message", "") or "").strip().lower()
            == "goals feature is disabled"
        )
