from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, TypeAlias, TypedDict

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.cards import (
    build_ask_user_answered_card,
    build_ask_user_card,
    build_markdown_card,
    make_card_response,
)
from bot.codex_protocol.client import (
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.feishu_outbound import (
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.interaction_contract import (
    MCP_ELICITATION,
    SHARED_APPROVAL_METHODS,
    USER_INPUT,
    fail_closed_interaction_response,
    interaction_response_payload,
    normalize_interaction_request,
)
from bot.interaction_approval_cards import (
    build_approval_handled_card,
    build_command_approval_card,
    build_file_change_approval_card,
    build_permissions_approval_card,
)
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestResponseSupersededError,
    ServerRequestRoutingMode,
)
from bot.server_request_dispatch import ServerRequestSurfaceIdentityConflict

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
_SHARED_CARD_RECONCILIATION_WINDOW_SECONDS = 50 * 60


@dataclass(frozen=True, slots=True)
class _UnknownSharedCardPublishIntent:
    """Immutable process-local evidence for one unresolved Feishu effect."""

    attempt_id: str
    operation: FeishuOutboundOperation
    chat_id: str
    card_json: str
    parent_message_id: str
    reply_in_thread: bool
    issued_wall_time: float
    issued_monotonic_time: float


class PendingRequestStateDict(TypedDict, total=False):
    # Exact process-local capability issued by ServerRequestRegistry. Request
    # key and value equality are deliberately insufficient across backend
    # epochs: an older pending response must never become authority for an ABA
    # replacement which reused the same JSON-RPC id and envelope.
    identity: ServerRequestIdentity
    response_capability: str
    rpc_request_id: int | str
    method: str
    params: dict[str, Any]
    thread_id: str
    owner_thread_id: str
    turn_id: str
    title: str
    message_id: str
    questions: list[dict[str, Any]]
    answers: dict[str, str]
    chat_id: str
    sender_id: str
    actor_open_id: str
    status: str
    shared_approval: bool
    shared_card_unknown_intent: _UnknownSharedCardPublishIntent
    fail_close_card_note: str
    auto_resolution_backend_epoch: int
    auto_resolution_generation: int
    auto_resolution_visible_at_ms: int
    auto_resolution_due_at_ms: int
    # Set when group deactivation revokes a non-admin member's pending
    # interaction.  A transport-unknown automatic cancellation remains a
    # root-operation blocker, never an approval card an administrator may
    # later take over (including after the group is reactivated).
    group_authority_revoked: bool


PendingRequestState: TypeAlias = PendingRequestStateDict

PENDING_REQUEST_STATUS_NOT_SENT = "not_sent"
PENDING_REQUEST_STATUS_PROCESSING = "processing"
PENDING_REQUEST_STATUS_SUBMITTED = "submitted"
PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN = "submitted_unknown"
PENDING_REQUEST_STATUS_SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class _RetiredBackendCardProjectionCandidate:
    """Detached old-card projection with no live response capability."""

    request_key: str
    chat_id: str
    message_id: str
    title: str
    method: str
    note: str


@dataclass(frozen=True, slots=True)
class FeishuInteractionBackendEpochRetirement:
    """Local Feishu capabilities retired after the old machine stopped."""

    request_keys: frozenset[str]

    @property
    def count(self) -> int:
        return len(self.request_keys)


class InteractionRequestController:
    def __init__(
        self,
        *,
        lock,
        resident_session_snapshot_locked: Callable[
            [ChatBindingKey], BindingSessionSnapshot | None
        ],
        interactive_binding_for_thread: Callable[[str], tuple[ChatBindingKey | None, bool]],
        interaction_actor_allowed: Callable[[str, str, str], bool],
        send_interactive_card: Callable[[str, dict[str, Any], str, bool], str | None],
        reply_text: Callable[..., None],
        respond: Callable[..., None],
        revoke_response_authority: Callable[[ServerRequestIdentity], bool],
        patch_message: Callable[[str, str, str], bool],
        publish_interactive_card: Callable[..., FeishuOutboundResult] | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = lock
        self._pending_requests: dict[str, PendingRequestState] = {}
        self._retired_backend_card_projections: list[
            _RetiredBackendCardProjectionCandidate
        ] = []
        self._resident_session_snapshot_locked = resident_session_snapshot_locked
        self._interactive_binding_for_thread = interactive_binding_for_thread
        self._interaction_actor_allowed = interaction_actor_allowed
        self._send_interactive_card = send_interactive_card
        self._publish_interactive_card = publish_interactive_card
        self._reply_text = reply_text
        self._respond = respond
        self._revoke_response_authority = revoke_response_authority
        self._patch_message = patch_message
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock

    def has_pending_request(self, request_key: str) -> bool:
        normalized_request_key = str(request_key or "").strip()
        if not normalized_request_key:
            return False
        with self._lock:
            return normalized_request_key in self._pending_requests

    def pending_request_snapshot(self, request_key: str) -> PendingRequestState | None:
        with self._lock:
            return self.pending_request_snapshot_locked(request_key)

    def pending_requests_snapshot(self) -> list[PendingRequestState]:
        with self._lock:
            return self.pending_requests_snapshot_locked()

    def pending_count(self) -> int:
        """Return the number of Feishu-local interaction capabilities."""

        with self._lock:
            return len(self._pending_requests)

    def pending_request_snapshot_locked(self, request_key: str) -> PendingRequestState | None:
        normalized_request_key = str(request_key or "").strip()
        if not normalized_request_key:
            return None
        pending = self._pending_requests.get(normalized_request_key)
        if pending is None:
            return None
        return dict(pending)

    def pending_requests_snapshot_locked(self) -> list[PendingRequestState]:
        return [dict(pending) for pending in self._pending_requests.values()]

    def store_pending_request(self, request_key: str, pending: PendingRequestState | dict[str, Any]) -> None:
        normalized_request_key = str(request_key or "").strip()
        if not normalized_request_key:
            raise ValueError("request_key 不能为空")
        with self._lock:
            self._pending_requests[normalized_request_key] = dict(pending)

    @staticmethod
    def pending_request_status(pending: PendingRequestState | dict[str, Any]) -> str:
        return str(
            pending.get("status", PENDING_REQUEST_STATUS_NOT_SENT)
            or PENDING_REQUEST_STATUS_NOT_SENT
        )

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
            owner_thread_id = str(
                pending.get("owner_thread_id", pending.get("thread_id", "")) or ""
            ).strip()
            if owner_thread_id == normalized_thread_id:
                return True
        return False

    def has_pending_request_for_root(self, root_thread_id: str) -> bool:
        """Return whether an unresolved exact-thread interaction blocks a root."""

        with self._lock:
            return self.thread_has_pending_request_locked(root_thread_id)

    def pending_request_keys_for_root(self, root_thread_id: str) -> set[str]:
        """Return local request ids held for one already-proven root."""

        normalized_root_id = str(root_thread_id or "").strip()
        if not normalized_root_id:
            return set()
        with self._lock:
            return {
                request_key
                for request_key, pending in self._pending_requests.items()
                if str(
                    pending.get("owner_thread_id", pending.get("thread_id", "")) or ""
                ).strip()
                == normalized_root_id
            }

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
        return self._fail_close_matching_requests(
            lambda pending: str(pending.get("chat_id", "") or "").strip() == normalized_chat_id,
            note="当前 chat 运行态已关闭，已自动结束该请求。",
            retire_shared_projection=True,
        )

    def fail_close_non_admin_chat_requests(
        self,
        chat_id: str,
        *,
        is_admin_actor: Callable[[str], bool],
    ) -> int:
        """Fail-close and permanently revoke member-origin group requests.

        A cancellation transport error does not turn a member's old approval
        into an administrator takeover opportunity.  Such a request stays in
        the pending map as an invisible release blocker until app-server
        confirms resolution or the root is explicitly stopped; the marker also
        survives a later group reactivation in this service process.
        """
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return 0
        with self._lock:
            for pending in self._pending_requests.values():
                if str(pending.get("chat_id", "") or "").strip() != normalized_chat_id:
                    continue
                actor_open_id = str(pending.get("actor_open_id", "") or "").strip()
                if not is_admin_actor(actor_open_id):
                    pending["group_authority_revoked"] = True
        return self._fail_close_matching_requests(
            lambda pending: (
                str(pending.get("chat_id", "") or "").strip() == normalized_chat_id
                and bool(pending.get("group_authority_revoked", False))
            ),
            note="当前群聊已停用，已自动拒绝该请求。",
            revoke_shared_authority=True,
        )

    def project_backend_reset_cards_best_effort(self) -> None:
        """Drain old-card projections after machine-stop retirement."""

        with self._lock:
            retired_candidates = tuple(self._retired_backend_card_projections)
            self._retired_backend_card_projections.clear()

        for candidate in retired_candidates:
            try:
                self._patch_confirmed_fail_close_card(
                    {
                        "chat_id": candidate.chat_id,
                        "message_id": candidate.message_id,
                        "title": candidate.title,
                        "method": candidate.method,
                    },
                    note=candidate.note,
                )
            except Exception:
                logger.exception(
                    "retired Feishu 请求卡片投影失败: request=%s",
                    candidate.request_key,
                )

    def retire_backend_epoch_after_stop(
        self,
    ) -> FeishuInteractionBackendEpochRetirement:
        """Retire old response/card capabilities after machine stop proof.

        No upstream resolution or cross-owner settlement is synthesized.
        The pending map and queued response-bound projections are cleared
        under the owner lock. Immutable old-card projections are only queued;
        the detached projection worker performs any Feishu I/O later, so a
        slow card patch cannot delay backend replacement.
        """

        with self._lock:
            retired = tuple(self._pending_requests.items())
            self._pending_requests.clear()
            self._retired_backend_card_projections.extend(
                _RetiredBackendCardProjectionCandidate(
                    request_key=request_key,
                    chat_id=str(pending.get("chat_id", "") or "").strip(),
                    message_id=str(pending.get("message_id", "") or "").strip(),
                    title=str(pending.get("title", "Codex 请求") or "Codex 请求"),
                    method=str(pending.get("method", "") or "").strip(),
                    note="当前实例 backend 已停止，该请求已失效。",
                )
                for request_key, pending in retired
                if str(pending.get("message_id", "") or "").strip()
            )
        return FeishuInteractionBackendEpochRetirement(
            frozenset(request_key for request_key, _pending in retired)
        )

    def fail_close_all_requests_without_response(self, *, note: str) -> int:
        return self._fail_close_matching_requests(
            lambda _pending: True,
            note=note,
            respond_upstream=False,
        )

    def _fail_close_matching_requests(
        self,
        predicate: Callable[[PendingRequestState], bool],
        *,
        note: str,
        respond_upstream: bool = True,
        retire_shared_projection: bool = False,
        revoke_shared_authority: bool = False,
    ) -> int:
        request_keys: list[str] = []
        with self._lock:
            for request_key, pending in self._pending_requests.items():
                if not predicate(pending):
                    continue
                request_keys.append(request_key)

        for request_key in request_keys:
            shared_retired: PendingRequestState | None = None
            revoke_identity: ServerRequestIdentity | None = None
            skip_response = False
            with self._lock:
                pending = self._pending_requests.get(request_key)
                if pending is None or not predicate(pending):
                    continue
                if retire_shared_projection and bool(
                    pending.get("shared_approval", False)
                ):
                    self._pending_requests.pop(request_key, None)
                    shared_retired = pending
                if shared_retired is not None:
                    status = PENDING_REQUEST_STATUS_SUPERSEDED
                else:
                    status = self.pending_request_status(pending)
                if (
                    revoke_shared_authority
                    and bool(pending.get("shared_approval", False))
                    and isinstance(pending.get("identity"), ServerRequestIdentity)
                ):
                    revoke_identity = pending["identity"]
                if status in {
                    PENDING_REQUEST_STATUS_PROCESSING,
                    PENDING_REQUEST_STATUS_SUBMITTED,
                    PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN,
                    # A local socket write does not prove app-server
                    # settlement.  It must nevertheless never receive a
                    # second speculative response while it remains pending.
                }:
                    skip_response = True
                if shared_retired is not None:
                    method = ""
                    params = {}
                elif skip_response:
                    method = ""
                    params = {}
                elif not respond_upstream:
                    pending["status"] = PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN
                    continue
                else:
                    method = str(pending.get("method", "") or "")
                    params = dict(pending.get("params") or {})
            if shared_retired is not None:
                self._patch_confirmed_fail_close_card(
                    shared_retired,
                    note="当前飞书入口已失效；该审批仍可在其他可信终端处理。",
                )
                continue
            if skip_response:
                if revoke_identity is not None:
                    self._revoke_exact_response_authority(revoke_identity)
                continue
            with self._lock:
                current = self._pending_requests.get(request_key)
                if (
                    current is not pending
                    or not predicate(current)
                    or self.pending_request_status(current) != status
                ):
                    continue
                current["status"] = PENDING_REQUEST_STATUS_PROCESSING
                current["fail_close_card_note"] = note
            result, error = fail_closed_interaction_response(method, params, message=note)
            outcome, exc = self._submit_response(
                pending["identity"],
                result=result,
                error=error,
            )
            if revoke_identity is not None:
                self._revoke_exact_response_authority(revoke_identity)
            if outcome == PENDING_REQUEST_STATUS_SUPERSEDED:
                self._retire_superseded_pending(
                    request_key,
                    pending,
                    note="该请求已由其他端处理或被 Codex 清理。",
                )
                continue
            if outcome != PENDING_REQUEST_STATUS_SUBMITTED:
                with self._lock:
                    current = self._pending_requests.get(request_key)
                    if current is pending:
                        current["status"] = outcome
                if exc is not None:
                    logger.warning(
                        "fail-close response was not confirmed: request=%s status=%s error=%s",
                        request_key,
                        outcome,
                        exc,
                    )
                continue

            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is not pending:
                    continue
                current["status"] = PENDING_REQUEST_STATUS_SUBMITTED
            # ``serverRequest/response`` is a websocket response, not an RPC
            # with an acknowledgement.  Keep the root blocker until the
            # matching, typed ``serverRequest/resolved`` notification.
            self._patch_confirmed_fail_close_card(pending, note=note)
        return len(request_keys)

    def _revoke_exact_response_authority(
        self,
        identity: ServerRequestIdentity,
    ) -> None:
        """Revoke one current-epoch capability without claiming settlement."""

        if not self._revoke_response_authority(identity):
            logger.info(
                "Exact server-request response authority was already retired: "
                "request=%s generation=%s",
                identity.request_key,
                identity.connection_generation,
            )

    def _patch_confirmed_fail_close_card(
        self,
        pending: PendingRequestState | dict[str, Any],
        *,
        note: str,
    ) -> None:
        message_id = str(pending.get("message_id", "") or "").strip()
        if not message_id:
            return
        title = str(pending.get("title", "Codex 请求") or "Codex 请求")
        method = str(pending.get("method", "") or "").strip()
        card = (
            build_markdown_card(title, note, template="grey")
            if method == "item/tool/requestUserInput"
            else build_approval_handled_card(title, note)
        )
        try:
            self._patch_message(
                str(pending.get("chat_id", "") or "").strip(),
                message_id,
                json.dumps(card, ensure_ascii=False),
            )
        except Exception:
            logger.exception("fail-close 请求卡片收口失败: message=%s", message_id)

    def _install_canonical_pending(
        self,
        identity: ServerRequestIdentity,
        pending: PendingRequestState,
    ) -> Literal["stored", "replay", "conflict"]:
        """Claim Feishu's local slot for one exact registry capability.

        The request key is only an index.  A process-distinct identity can be
        value-equal after a backend epoch change, so it must not inherit the
        old card, response status, or responder authority.  Returning
        ``replay`` consumes only the exact same object; ``conflict`` preserves
        the old pending record byte-for-byte.
        """

        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("Feishu server requests require a canonical identity")
        if pending.get("identity") is not identity:
            raise ValueError("Feishu pending state requires its exact identity")
        request_key = identity.request_key
        with self._lock:
            existing = self._pending_requests.get(request_key)
            if existing is None:
                self._pending_requests[request_key] = pending
                return "stored"
            if existing.get("identity") is identity:
                return "replay"
            logger.error(
                "Declining reused Feishu server-request id; preserving old capability: "
                "request=%s old_method=%s new_method=%s",
                request_key,
                str(existing.get("method", "") or "") or "<unknown>",
                identity.method,
            )
            return "conflict"

    def auto_reject_server_request(
        self,
        identity: ServerRequestIdentity,
        *,
        note: str = "Unable to deliver interaction request to Feishu",
    ) -> bool:
        """Retain and fail-close one canonical request without ABA takeover.

        An exact replay is already retained and therefore sends no second
        response.  A distinct capability with the same typed key is an
        ambiguous cross-epoch conflict: preserve the older pending state and
        force the complete dispatcher to stop without fallback.
        """

        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("Feishu server requests require a canonical identity")
        with self._lock:
            existing = self._pending_requests.get(identity.request_key)
            if existing is not None:
                if existing.get("identity") is identity:
                    return True
                logger.error(
                    "Conflicting Feishu automatic server-request id: "
                    "request=%s old_method=%s new_method=%s",
                    identity.request_key,
                    str(existing.get("method", "") or "") or "<unknown>",
                    identity.method,
                )
                raise ServerRequestSurfaceIdentityConflict(
                    "Feishu retained a different canonical server request"
                )
        params = identity.params
        thread_id = identity.thread_id
        pending: PendingRequestState = {
            "identity": identity,
            "rpc_request_id": identity.request_id,
            "method": identity.method,
            "params": params,
            "thread_id": thread_id,
            "owner_thread_id": thread_id,
            "turn_id": identity.turn_id,
            "title": "Codex 请求",
            "message_id": "",
            "questions": [],
            "answers": {},
            "chat_id": "",
            "sender_id": "",
            "actor_open_id": "",
            "status": PENDING_REQUEST_STATUS_NOT_SENT,
            "shared_approval": False,
        }
        disposition = self._install_canonical_pending(identity, pending)
        if disposition == "conflict":
            raise ServerRequestSurfaceIdentityConflict(
                "Feishu retained a different canonical server request"
            )
        if disposition == "replay":
            return True
        self._auto_reject_pending(pending, note=note)
        return True

    def handle_adapter_request(
        self,
        identity: ServerRequestIdentity,
        *,
        auto_resolution_timing: AutoResolutionTiming | None = None,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> bool:
        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("Feishu server requests require a canonical identity")
        request_id = identity.request_id
        request_key = identity.request_key
        method = identity.method
        params = identity.params
        thread_id = identity.thread_id
        shared_approval = routing_mode == "shared_approval"
        if shared_approval and method not in SHARED_APPROVAL_METHODS:
            return False
        # Classify before consulting mutable binding/runtime facts.  An exact
        # replay is already retained by Feishu and must not be re-presented or
        # re-submitted.  Every object-distinct capability is an ABA conflict,
        # including a value-equal envelope from a later registry epoch.
        with self._lock:
            existing = self._pending_requests.get(request_key)
            if existing is not None:
                if existing.get("identity") is identity:
                    return True
                logger.error(
                    "Conflicting Feishu server-request id before routing: "
                    "request=%s old_method=%s new_method=%s",
                    request_key,
                    str(existing.get("method", "") or "") or "<unknown>",
                    method,
                )
                raise ServerRequestSurfaceIdentityConflict(
                    "Feishu retained a different canonical server request"
                )
        owner_thread_id = thread_id
        if shared_approval and (
            owner_thread_id != thread_id or not identity.turn_id
        ):
            return False
        binding, handled_elsewhere = self._interactive_binding_for_thread(owner_thread_id)
        if not binding:
            if handled_elsewhere:
                logger.info(
                    "interactive request suppressed for non-Feishu owner: method=%s thread=%s owner=%s",
                    method,
                    thread_id,
                    owner_thread_id,
                )
                return False
            if shared_approval:
                return False
            logger.warning(
                "未找到 root 线程绑定，自动 fail-close: method=%s thread=%s owner=%s",
                method,
                thread_id,
                owner_thread_id,
            )
            return self.auto_reject_server_request(identity)

        sender_id, chat_id = binding
        with self._lock:
            session = self._resident_session_snapshot_locked(binding)
        if session is None:
            if shared_approval:
                return False
            logger.warning(
                "exact binding 缺少 resident session，自动 fail-close: "
                "method=%s thread=%s owner=%s binding=%s",
                method,
                thread_id,
                owner_thread_id,
                binding,
            )
            return self.auto_reject_server_request(
                identity,
                note="Focus 无法确认该交互请求的 resident Feishu session。",
            )
        if session.current_thread_id.strip() != owner_thread_id:
            if shared_approval:
                return False
            logger.warning(
                "exact binding 的 resident thread 与请求 owner 不一致，自动 fail-close: "
                "method=%s thread=%s owner=%s resident=%s binding=%s",
                method,
                thread_id,
                owner_thread_id,
                session.current_thread_id,
                binding,
            )
            return self.auto_reject_server_request(
                identity,
                note="Focus 无法确认该交互请求仍属于当前 Feishu binding。",
            )
        prompt_message_id = session.execution.current_prompt_message_id.strip()
        prompt_reply_in_thread = session.execution.current_prompt_reply_in_thread
        actor_open_id = session.execution.current_actor_open_id.strip()

        # Cards can carry only a string token.  Use the shared typed key as
        # that opaque token so numeric ``1`` and string ``"1"`` never point
        # at the same pending request.
        if not self._interaction_actor_allowed(sender_id, chat_id, actor_open_id):
            logger.info(
                "interactive request rejected because its group actor is no longer allowed: "
                "method=%s chat=%s actor=%s",
                method,
                chat_id,
                actor_open_id,
            )
            if shared_approval:
                pending: PendingRequestState = {
                    "identity": identity,
                    "rpc_request_id": request_id,
                    "method": method,
                    "params": params,
                    "thread_id": thread_id,
                    "owner_thread_id": owner_thread_id,
                    "turn_id": identity.turn_id,
                    "title": "Codex 审批",
                    "message_id": "",
                    "questions": [],
                    "answers": {},
                    "chat_id": chat_id,
                    "sender_id": sender_id,
                    "actor_open_id": actor_open_id,
                    "status": PENDING_REQUEST_STATUS_NOT_SENT,
                    "shared_approval": True,
                    "group_authority_revoked": True,
                }
                disposition = self._install_canonical_pending(identity, pending)
                if disposition == "conflict":
                    raise ServerRequestSurfaceIdentityConflict(
                        "Feishu retained a different canonical server request"
                    )
                if disposition == "stored":
                    self._fail_close_matching_requests(
                        lambda candidate: candidate is pending,
                        note="该群聊已停用，非管理员不能处理此请求。",
                        revoke_shared_authority=True,
                    )
                return True
            return self.auto_reject_server_request(
                identity,
                note="该群聊已停用，非管理员不能处理此请求。",
            )

        normalized = normalize_interaction_request(method, params)
        if not normalized.get("presentable") or method == MCP_ELICITATION:
            if shared_approval:
                return False
            reason = str(normalized.get("unsupported_reason", "") or "").strip()
            if method == MCP_ELICITATION:
                reason = reason or "飞书端暂不支持 MCP elicitation 表单。"
            message = "收到 Codex 交互请求，但当前飞书界面无法可靠呈现，已取消该请求。"
            if reason:
                message = f"{message}\n\n{reason}"
            self._reply_text(
                chat_id,
                message,
                message_id=prompt_message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
            return self.auto_reject_server_request(
                identity,
                note=reason or "请求无法可靠呈现",
            )

        actions = list(normalized.get("actions") or [])
        if method == "item/commandExecution/requestApproval":
            card = build_command_approval_card(
                request_key,
                command=params.get("command") or "",
                cwd=params.get("cwd") or "",
                reason=params.get("reason") or "",
                actions=actions,
                context_lines=self._command_approval_context_lines(params),
            )
            title = "Codex 命令执行审批"
        elif method == "item/fileChange/requestApproval":
            card = build_file_change_approval_card(
                request_key,
                grant_root=params.get("grantRoot") or "",
                reason=params.get("reason") or "",
                actions=actions,
            )
            title = "Codex 文件修改审批"
        elif method == "item/permissions/requestApproval":
            card = build_permissions_approval_card(
                request_key,
                permissions=params.get("permissions") or {},
                reason=params.get("reason") or "",
                actions=actions,
            )
            title = "Codex 额外权限审批"
        elif method == USER_INPUT:
            questions = normalized.get("params", {}).get("questions") or []
            card = build_ask_user_card(request_key, questions)
            title = "Codex 用户输入"
        else:
            if shared_approval:
                return False
            logger.warning("未支持的 Codex server request: %s", method)
            return self.auto_reject_server_request(identity)
        response_capability = secrets.token_urlsafe(32)
        card = self._bind_card_action_capability(
            card,
            identity.connection_generation,
            response_capability,
        )

        pending: PendingRequestState = {
            "identity": identity,
            "response_capability": response_capability,
            "rpc_request_id": request_id,
            "method": method,
            "params": params,
            "thread_id": thread_id,
            "owner_thread_id": owner_thread_id,
            "turn_id": identity.turn_id,
            "title": title,
            "message_id": "",
            "questions": normalized.get("params", {}).get("questions") or [],
            "answers": {},
            "chat_id": chat_id,
            "sender_id": sender_id,
            "actor_open_id": actor_open_id,
            "status": PENDING_REQUEST_STATUS_NOT_SENT,
            "shared_approval": shared_approval,
            "auto_resolution_backend_epoch": (
                auto_resolution_timing.backend_epoch if auto_resolution_timing else 0
            ),
            "auto_resolution_generation": (
                auto_resolution_timing.generation if auto_resolution_timing else 0
            ),
            "auto_resolution_visible_at_ms": (
                auto_resolution_timing.visible_at_ms if auto_resolution_timing else 0
            ),
            "auto_resolution_due_at_ms": (
                auto_resolution_timing.due_at_ms if auto_resolution_timing else 0
            ),
        }
        disposition = self._install_canonical_pending(identity, pending)
        if disposition == "conflict":
            raise ServerRequestSurfaceIdentityConflict(
                "Feishu retained a different canonical server request"
            )
        if disposition == "replay":
            return True

        message_id = ""
        if shared_approval and self._publish_interactive_card is not None:
            issued_wall_time = float(self._wall_clock())
            issued_monotonic_time = float(self._monotonic_clock())
            try:
                published = self._publish_interactive_card(
                    chat_id,
                    card,
                    prompt_message_id,
                    prompt_reply_in_thread,
                )
            except Exception:
                logger.exception(
                    "共享审批卡片发送抛出异常；保留 exact 本地请求但不重放: "
                    "method=%s request=%s",
                    method,
                    request_key,
                )
                return True
            if not isinstance(published, FeishuOutboundResult):
                logger.error(
                    "共享审批卡片发送缺少 typed outcome；保留 exact 本地请求: "
                    "method=%s request=%s",
                    method,
                    request_key,
                )
                return True
            expected_operation = (
                FeishuOutboundOperation.REPLY_MESSAGE
                if prompt_message_id
                else FeishuOutboundOperation.CREATE_MESSAGE
            )
            if (
                published.operation is not expected_operation
                or published.chat_id != chat_id
            ):
                logger.error(
                    "共享审批卡片发送返回了不同 effect identity；保留 exact 本地请求: "
                    "method=%s request=%s operation=%s chat=%s attempt=%s",
                    method,
                    request_key,
                    published.operation.value,
                    published.chat_id,
                    published.attempt_id,
                )
                return True
            if published.effect is FeishuOutboundEffect.CONFIRMED:
                message_id = published.message_id
            elif published.effect is FeishuOutboundEffect.UNKNOWN:
                with self._lock:
                    current = self._pending_requests.get(request_key)
                    if current is pending:
                        current["shared_card_unknown_intent"] = (
                            _UnknownSharedCardPublishIntent(
                                attempt_id=published.attempt_id,
                                operation=published.operation,
                                chat_id=chat_id,
                                card_json=json.dumps(card, ensure_ascii=False),
                                parent_message_id=prompt_message_id,
                                reply_in_thread=prompt_reply_in_thread,
                                issued_wall_time=issued_wall_time,
                                issued_monotonic_time=issued_monotonic_time,
                            )
                        )
                logger.warning(
                    "共享审批卡片发送结果未知；保留原 UUID，等待 canonical resolution 后一次对账: "
                    "method=%s request=%s attempt=%s",
                    method,
                    request_key,
                    published.attempt_id,
                )
                return True
            else:
                logger.warning(
                    "共享审批卡片发送未确认成功；保留本地请求但不替其他端回答: "
                    "method=%s request=%s effect=%s attempt=%s",
                    method,
                    request_key,
                    published.effect.value,
                    published.attempt_id,
                )
                return True
        else:
            message_id = self._send_interactive_card(
                chat_id,
                card,
                prompt_message_id,
                prompt_reply_in_thread,
            ) or ""
        if not message_id:
            if shared_approval:
                logger.warning(
                    "共享审批卡片发送结果未确认；保留本地请求但不替其他端回答: "
                    "method=%s request=%s",
                    method,
                    request_key,
                )
                return True
            logger.warning("审批/问答卡片发送失败，执行 fail-close: method=%s", method)
            self._auto_reject_pending(
                pending,
                note="Unable to deliver interaction request to Feishu",
            )
            return True

        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is pending:
                current["message_id"] = message_id
        return True

    def remove_resolved_server_request(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        """Remove one Feishu projection after canonical settlement is proven.

        The shared server-request coordinator owns canonical settlement. This
        controller may only reconcile its process-local inbox/card after the
        registry has retired that identity; an unconfirmed or malformed
        callback must leave the local projection untouched.
        """

        if not isinstance(identity, ServerRequestIdentity):
            return ServerRequestLocalRemoval("invalid")
        normalized_request_key = identity.request_key
        normalized_thread_id = identity.thread_id
        if not normalized_thread_id:
            return ServerRequestLocalRemoval(
                "invalid", request_key=normalized_request_key
            )
        with self._lock:
            pending = self._pending_requests.get(normalized_request_key)
            if pending is None:
                return ServerRequestLocalRemoval(
                    "missing",
                    request_key=normalized_request_key,
                    thread_id=normalized_thread_id,
                )
            if pending.get("identity") is not identity:
                return ServerRequestLocalRemoval(
                    "mismatch",
                    request_key=normalized_request_key,
                    thread_id=normalized_thread_id,
                )
            self._pending_requests.pop(normalized_request_key)
        resolution = ServerRequestLocalRemoval(
            "removed",
            request_key=normalized_request_key,
            thread_id=normalized_thread_id,
            root_thread_id=str(
                pending.get("owner_thread_id", pending.get("thread_id", "")) or ""
            ).strip(),
        )
        unknown_shared_card = isinstance(
            pending.get("shared_card_unknown_intent"),
            _UnknownSharedCardPublishIntent,
        )
        if (
            self.pending_request_status(pending) == PENDING_REQUEST_STATUS_SUBMITTED
            and not unknown_shared_card
        ):
            return resolution
        message_id = str(pending.get("message_id", "") or "").strip()
        if not message_id:
            message_id = self._reconcile_unknown_shared_card(
                pending,
                request_key=normalized_request_key,
            )
        if not message_id:
            return resolution
        title = str(pending.get("title", "Codex 请求") or "Codex 请求")
        fail_close_note = str(pending.get("fail_close_card_note", "") or "").strip()
        if fail_close_note:
            card = build_approval_handled_card(title, fail_close_note)
        elif str(pending.get("method", "") or "").strip() == "item/tool/requestUserInput":
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
            self._patch_message(
                str(pending.get("chat_id", "") or "").strip(),
                message_id,
                json.dumps(card, ensure_ascii=False),
            )
        except Exception:
            logger.exception(
                "收口已解决请求卡片失败: request=%s",
                normalized_request_key,
            )
        return resolution

    def _reconcile_unknown_shared_card(
        self,
        pending: PendingRequestState,
        *,
        request_key: str,
    ) -> str:
        """Reconcile one unknown shared-card effect after canonical settlement."""

        if not pending.get("shared_approval") or self._publish_interactive_card is None:
            return ""
        intent = pending.get("shared_card_unknown_intent")
        if not isinstance(intent, _UnknownSharedCardPublishIntent):
            return ""
        wall_elapsed = float(self._wall_clock()) - intent.issued_wall_time
        monotonic_elapsed = (
            float(self._monotonic_clock()) - intent.issued_monotonic_time
        )
        if not (
            0.0 <= wall_elapsed < _SHARED_CARD_RECONCILIATION_WINDOW_SECONDS
            and 0.0
            <= monotonic_elapsed
            < _SHARED_CARD_RECONCILIATION_WINDOW_SECONDS
        ):
            logger.warning(
                "共享审批卡片已超过安全 UUID 对账窗口；停止该卡片路径: "
                "request=%s attempt=%s wall_elapsed=%.1f monotonic_elapsed=%.1f",
                request_key,
                intent.attempt_id,
                wall_elapsed,
                monotonic_elapsed,
            )
            return ""
        try:
            card = json.loads(intent.card_json)
        except (TypeError, ValueError):
            logger.exception(
                "共享审批卡片保存的 publish intent 无法解码；停止该卡片路径: "
                "request=%s attempt=%s",
                request_key,
                intent.attempt_id,
            )
            return ""
        if (
            type(card) is not dict
            or json.dumps(card, ensure_ascii=False) != intent.card_json
        ):
            logger.error(
                "共享审批卡片保存的 publish intent 无法 exact round-trip；"
                "停止该卡片路径: request=%s attempt=%s",
                request_key,
                intent.attempt_id,
            )
            return ""
        try:
            reconciled = self._publish_interactive_card(
                intent.chat_id,
                card,
                intent.parent_message_id,
                intent.reply_in_thread,
                attempt_id=intent.attempt_id,
            )
        except Exception:
            logger.exception(
                "共享审批卡片 canonical-resolution 对账调用异常；停止该卡片路径: "
                "request=%s attempt=%s",
                request_key,
                intent.attempt_id,
            )
            return ""
        if not isinstance(reconciled, FeishuOutboundResult):
            logger.error(
                "共享审批卡片 canonical-resolution 对账缺少 typed outcome；停止该卡片路径: "
                "request=%s attempt=%s",
                request_key,
                intent.attempt_id,
            )
            return ""
        if (
            reconciled.operation is not intent.operation
            or reconciled.chat_id != intent.chat_id
            or reconciled.attempt_id != intent.attempt_id
        ):
            logger.error(
                "共享审批卡片 canonical-resolution 对账返回了不同 effect identity；"
                "停止该卡片路径: request=%s expected_attempt=%s actual_attempt=%s",
                request_key,
                intent.attempt_id,
                reconciled.attempt_id,
            )
            return ""
        if reconciled.effect is FeishuOutboundEffect.CONFIRMED:
            return reconciled.message_id
        logger.warning(
            "共享审批卡片 canonical-resolution 对账未确认；停止该卡片路径: "
            "request=%s effect=%s attempt=%s",
            request_key,
            reconciled.effect.value,
            intent.attempt_id,
        )
        return ""

    def _auto_reject_pending(
        self,
        pending: PendingRequestState,
        *,
        note: str,
    ) -> str:
        """Submit fail-close only for the exact current pending object."""

        identity = pending.get("identity")
        request_id = pending["rpc_request_id"]
        request_key = (
            identity.request_key
            if isinstance(identity, ServerRequestIdentity)
            else jsonrpc_id_key(request_id)
        )
        method = str(pending.get("method", "") or "")
        params = dict(pending.get("params") or {})
        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is not pending:
                return (
                    self.pending_request_status(current)
                    if current is not None
                    else PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN
                )
            status = self.pending_request_status(current)
            if status in {
                PENDING_REQUEST_STATUS_PROCESSING,
                PENDING_REQUEST_STATUS_SUBMITTED,
                PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN,
            }:
                return status
            current["status"] = PENDING_REQUEST_STATUS_PROCESSING
        result, error = fail_closed_interaction_response(method, params, message=note)
        outcome, exc = self._submit_response(
            identity,
            result=result,
            error=error,
        )
        if outcome == PENDING_REQUEST_STATUS_SUPERSEDED:
            self._retire_superseded_pending(
                request_key,
                pending,
                note="该请求已由其他端处理或被 Codex 清理。",
            )
            return outcome
        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is pending:
                current["status"] = outcome
        logger.warning(
            "automatic fail-close response remains pending: request=%s status=%s error=%s",
            request_key,
            outcome,
            exc or "-",
        )
        return outcome

    def auto_resolve_request(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> bool:
        normalized_request_key = str(request_key or "").strip()
        with self._lock:
            pending = self._pending_requests.get(normalized_request_key)
            if pending is None:
                return False
            if (
                int(pending.get("auto_resolution_backend_epoch", 0) or 0)
                != int(backend_epoch)
                or int(pending.get("auto_resolution_generation", 0) or 0)
                != int(generation)
            ):
                return True
            if str(pending.get("method", "") or "").strip() != USER_INPUT:
                return True
            if bool(pending.get("group_authority_revoked", False)):
                # Deactivation already sent (or attempted) the only permitted
                # fail-close response.  Do not turn a later timer into a
                # second, valid user-input answer; retain the pending request
                # as a fail-closed blocker until upstream resolves it.
                return True
            if self.pending_request_status(pending) != PENDING_REQUEST_STATUS_NOT_SENT:
                return True
            pending["status"] = PENDING_REQUEST_STATUS_PROCESSING
            result, error = interaction_response_payload(
                USER_INPUT,
                self._response_params(pending),
                action="auto_resolve",
            )
        outcome, exc = self._submit_response(
            pending["identity"],
            result=result,
            error=error,
        )
        if outcome == PENDING_REQUEST_STATUS_SUPERSEDED:
            self._retire_superseded_pending(
                normalized_request_key,
                pending,
                note="该请求已由其他端处理或被 Codex 清理。",
            )
            return True
        if outcome != PENDING_REQUEST_STATUS_SUBMITTED:
            with self._lock:
                current = self._pending_requests.get(normalized_request_key)
                if current is pending:
                    current["status"] = outcome
            logger.warning(
                "user-input auto-resolution was not confirmed: request=%s status=%s error=%s",
                normalized_request_key,
                outcome,
                exc or "-",
            )
            return True
        with self._lock:
            current = self._pending_requests.get(normalized_request_key)
            if current is pending:
                current["status"] = PENDING_REQUEST_STATUS_SUBMITTED
        message_id = str(pending.get("message_id", "") or "").strip()
        if message_id:
            card = build_markdown_card(
                str(pending.get("title", "Codex 用户输入") or "Codex 用户输入"),
                "未在自动解决时限内回答，已向 Codex 提交空答案。",
                template="grey",
            )
            try:
                self._patch_message(
                    str(pending.get("chat_id", "") or "").strip(),
                    message_id,
                    json.dumps(card, ensure_ascii=False),
                )
            except Exception:
                logger.exception(
                    "自动解决用户输入后收口卡片失败: request=%s",
                    normalized_request_key,
                )
        return True

    def _submit_response(
        self,
        identity: ServerRequestIdentity,
        *,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        responder: Callable[..., None] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, Exception | None]:
        try:
            selected_responder = responder or self._respond
            if responder is not None and timeout is not None:
                selected_responder(
                    identity,
                    result=result,
                    error=error,
                    timeout=timeout,
                )
            else:
                selected_responder(
                    identity,
                    result=result,
                    error=error,
                )
        except ServerRequestResponseSupersededError as exc:
            return PENDING_REQUEST_STATUS_SUPERSEDED, exc
        except CodexRpcPreSendError as exc:
            return PENDING_REQUEST_STATUS_NOT_SENT, exc
        except (TimeoutError, CodexRpcTransportError, CodexRpcProtocolError) as exc:
            return PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN, exc
        except Exception as exc:
            # Only CodexRpcPreSendError proves that no bytes left Focus. An
            # untyped responder failure may have happened after dispatch and
            # must therefore block a speculative second answer.
            return PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN, exc
        return PENDING_REQUEST_STATUS_SUBMITTED, None

    def handle_approval_card_action(self, action_value: dict[str, Any]) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", "") or "").strip()
        with self._lock:
            pending = self._pending_requests.get(request_key)
            if not pending:
                return make_card_response(toast="该审批请求已失效或已处理。", toast_type="warning")
            if not self._action_matches_pending_generation(action_value, pending):
                return make_card_response(
                    toast="该审批动作来自已失效的 Codex 连接。",
                    toast_type="warning",
                )
            if self.pending_request_status(pending) in {
                PENDING_REQUEST_STATUS_PROCESSING,
                PENDING_REQUEST_STATUS_SUBMITTED,
                PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN,
            }:
                return make_card_response(toast="该审批请求正在处理中，请稍候。", toast_type="warning")

            action = str(action_value.get("action", "") or "").strip()
            title = str(pending.get("title", "Codex 审批") or "Codex 审批")
            rpc_request_id = pending["rpc_request_id"]
            response_action = self._approval_response_action(action_value)
            if not response_action:
                return make_card_response(toast="未知审批动作", toast_type="warning")
            try:
                result, error = interaction_response_payload(
                    str(pending.get("method", "") or ""),
                    self._response_params(pending),
                    action=response_action,
                )
            except ValueError:
                return make_card_response(toast="该审批动作不在上游允许的选项中", toast_type="warning")
            decision_text = self._approval_action_label(pending, response_action)

            pending["status"] = PENDING_REQUEST_STATUS_PROCESSING

        logger.info(
            "响应审批请求: request_key=%s, rpc_request_id=%s, action=%s, result=%s",
            request_key,
            rpc_request_id,
            action,
            result,
        )
        outcome, exc = self._submit_response(
            pending["identity"],
            result=result,
            error=error,
        )
        if outcome == PENDING_REQUEST_STATUS_SUPERSEDED:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    self._pending_requests.pop(request_key, None)
            return make_card_response(
                card=build_markdown_card(
                    title,
                    "该审批已由其他端处理或被 Codex 清理。",
                    template="grey",
                ),
                toast="该审批已由其他端处理或失效。",
                toast_type="warning",
            )
        if outcome == PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current["status"] = outcome
            return make_card_response(
                toast=f"审批响应结果未知，请勿重复提交：{exc}",
                toast_type="warning",
            )
        if outcome == PENDING_REQUEST_STATUS_NOT_SENT:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current["status"] = outcome
            return make_card_response(toast=f"审批未发送：{exc}", toast_type="warning")
        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is pending:
                current["status"] = PENDING_REQUEST_STATUS_SUBMITTED
        return make_card_response(
            card=build_approval_handled_card(title, decision_text),
            toast=f"已{decision_text}",
            toast_type="success",
        )

    def handle_user_input_action(self, action_value: dict[str, Any]) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", "") or "").strip()
        with self._lock:
            pending = self._pending_requests.get(request_key)
            if pending and not self._action_matches_pending_generation(
                action_value,
                pending,
            ):
                pending = None
        if not pending:
            return make_card_response(toast="该输入请求已失效或已处理。", toast_type="warning")
        if self.pending_request_status(pending) in {
            PENDING_REQUEST_STATUS_PROCESSING,
            PENDING_REQUEST_STATUS_SUBMITTED,
            PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN,
        }:
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
            if not self._action_matches_pending_generation(action_value, pending):
                return make_card_response(
                    toast="该输入动作来自已失效的 Codex 连接。",
                    toast_type="warning",
                )
            if self.pending_request_status(pending) in {
                PENDING_REQUEST_STATUS_PROCESSING,
                PENDING_REQUEST_STATUS_SUBMITTED,
                PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN,
            }:
                return make_card_response(toast="该输入请求正在提交，请稍候。", toast_type="warning")

            questions = pending.get("questions") or []
            answers = pending.setdefault("answers", {})
            if question_id in answers:
                return make_card_response(
                    card=self._bind_card_action_capability(
                        build_ask_user_card(request_key, questions, answers),
                        pending["identity"].connection_generation,
                        str(pending["response_capability"]),
                    ),
                    toast="该问题已记录，请继续剩余问题。",
                    toast_type="warning",
                )

            answers[question_id] = answer
            if len(answers) < len(questions):
                return make_card_response(
                    card=self._bind_card_action_capability(
                        build_ask_user_card(request_key, questions, answers),
                        pending["identity"].connection_generation,
                        str(pending["response_capability"]),
                    ),
                    toast="已记录，继续回答下一题。",
                    toast_type="success",
                )

            pending["status"] = PENDING_REQUEST_STATUS_PROCESSING
            final_answers = dict(answers)

        try:
            result, error = interaction_response_payload(
                str(pending.get("method", "") or ""),
                self._response_params(pending),
                action="answer",
                answers=final_answers,
            )
        except ValueError as exc:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current_answers = current.setdefault("answers", {})
                    current_answers.pop(question_id, None)
                    current["status"] = PENDING_REQUEST_STATUS_NOT_SENT
            return make_card_response(toast=f"回答无效：{exc}", toast_type="warning")
        outcome, exc = self._submit_response(
            pending["identity"],
            result=result,
            error=error,
        )
        if outcome == PENDING_REQUEST_STATUS_SUPERSEDED:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    self._pending_requests.pop(request_key, None)
            return make_card_response(
                card=build_markdown_card(
                    str(pending.get("title", "Codex 用户输入") or "Codex 用户输入"),
                    "该输入请求已由其他端处理或被 Codex 清理。",
                    template="grey",
                ),
                toast="该输入请求已处理或失效。",
                toast_type="warning",
            )
        if outcome == PENDING_REQUEST_STATUS_SUBMITTED_UNKNOWN:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current["status"] = outcome
            return make_card_response(
                toast=f"回答提交结果未知，请勿重复提交：{exc}",
                toast_type="warning",
            )
        if outcome == PENDING_REQUEST_STATUS_NOT_SENT:
            with self._lock:
                current = self._pending_requests.get(request_key)
                if current is pending:
                    current_answers = current.setdefault("answers", {})
                    current_answers.pop(question_id, None)
                    current["status"] = outcome
            return make_card_response(toast=f"回答未发送：{exc}", toast_type="warning")
        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is pending:
                current["status"] = PENDING_REQUEST_STATUS_SUBMITTED
        return make_card_response(
            card=build_ask_user_answered_card(questions, final_answers),
            toast="已提交回答。",
            toast_type="success",
        )

    def _retire_superseded_pending(
        self,
        request_key: str,
        pending: PendingRequestState,
        *,
        note: str,
    ) -> None:
        retain_unknown_projection = False
        with self._lock:
            current = self._pending_requests.get(request_key)
            if current is pending:
                retain_unknown_projection = bool(
                    pending.get("shared_approval")
                    and not str(pending.get("message_id", "") or "").strip()
                    and isinstance(
                        pending.get("shared_card_unknown_intent"),
                        _UnknownSharedCardPublishIntent,
                    )
                )
                if retain_unknown_projection:
                    # Another surface has already consumed response authority,
                    # but an UNKNOWN Feishu publish may still have created a
                    # visible card. Keep only the immutable same-UUID intent
                    # until canonical resolution can reconcile and patch it.
                    pending["status"] = PENDING_REQUEST_STATUS_SUPERSEDED
                    pending["response_capability"] = ""
                    pending["fail_close_card_note"] = note
                else:
                    self._pending_requests.pop(request_key, None)
        if retain_unknown_projection:
            return
        self._patch_confirmed_fail_close_card(pending, note=note)

    @staticmethod
    def _action_matches_pending_generation(
        action_value: dict[str, Any],
        pending: PendingRequestState,
    ) -> bool:
        generation = action_value.get("connection_generation")
        response_capability = action_value.get("response_capability")
        identity = pending.get("identity")
        expected_capability = pending.get("response_capability")
        return (
            isinstance(identity, ServerRequestIdentity)
            and isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation > 0
            and generation == identity.connection_generation
            and isinstance(response_capability, str)
            and bool(response_capability)
            and response_capability == expected_capability
        )

    @staticmethod
    def _bind_card_action_capability(
        card: dict[str, Any],
        connection_generation: int,
        response_capability: str,
    ) -> dict[str, Any]:
        """Bind each card action to one backend and Focus service lifetime."""

        def bind(value: Any) -> None:
            if isinstance(value, dict):
                action_value = value.get("value")
                if (
                    isinstance(action_value, dict)
                    and "request_id" in action_value
                ):
                    action_value["connection_generation"] = connection_generation
                    action_value["response_capability"] = response_capability
                for nested in value.values():
                    bind(nested)
            elif isinstance(value, list):
                for nested in value:
                    bind(nested)

        bind(card)
        return card

    @staticmethod
    def _approval_response_action(action_value: dict[str, Any]) -> str:
        action = str(action_value.get("action", "") or "").strip()
        if action != "interaction_approval":
            return ""
        return str(action_value.get("response_action", "") or "").strip()

    @classmethod
    def _approval_action_label(cls, pending: PendingRequestState, action_id: str) -> str:
        localized = {
            "approve_once": "允许本次",
            "approve_session": "本会话允许",
            "reject": "拒绝",
            "cancel": "中止本轮",
            "approve_execpolicy_amendment": "允许并保存命令策略",
        }
        if action_id in localized:
            return localized[action_id]
        normalized = normalize_interaction_request(
            str(pending.get("method", "") or ""),
            cls._response_params(pending),
        )
        for action in normalized.get("actions") or []:
            if str(action.get("id", "") or "") == action_id:
                return str(action.get("label", "") or action_id)
        return action_id

    @staticmethod
    def _response_params(pending: PendingRequestState | dict[str, Any]) -> dict[str, Any]:
        params = dict(pending.get("params") or {})
        if (
            str(pending.get("method", "") or "").strip() == USER_INPUT
            and not isinstance(params.get("questions"), list)
        ):
            params["questions"] = list(pending.get("questions") or [])
        return params

    @staticmethod
    def _command_approval_context_lines(params: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        network = params.get("networkApprovalContext")
        if isinstance(network, dict) and network:
            host = str(network.get("host", "") or "").strip()
            protocol = str(network.get("protocol", "") or "").strip()
            target = ":".join(value for value in (protocol, host) if value)
            lines.append(f"**网络上下文**: `{target}`" if target else "**网络上下文**: 已提供")
        command_actions = params.get("commandActions")
        if isinstance(command_actions, list) and command_actions:
            names = [
                str(action.get("type", "") or "").strip()
                for action in command_actions
                if isinstance(action, dict) and str(action.get("type", "") or "").strip()
            ]
            if names:
                lines.append(f"**命令动作**: {', '.join(names[:8])}")
        if isinstance(params.get("additionalPermissions"), dict):
            lines.append("**附加权限**: 本次命令同时请求额外权限")
        if params.get("proposedExecpolicyAmendment"):
            lines.append("**策略变更**: 上游提供了可选的命令策略 amendment")
        amendments = params.get("proposedNetworkPolicyAmendments")
        if isinstance(amendments, list) and amendments:
            lines.append("**网络策略**: 上游提供了可选的网络策略 amendment")
        return lines
