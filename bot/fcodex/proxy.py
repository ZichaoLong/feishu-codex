"""
fcodex 本地 websocket proxy。

Upstream Codex TUI 在 `--remote` 模式下不会给 `thread/start` 带 `cwd`，
shared app-server 会回退到服务进程自己的工作目录。这里补一个很薄的
本地代理，在需要时给 `thread/start` 补回调用方 cwd。

另外，upstream `codex --remote ... resume <id>` 启动时会先连一次 remote
app-server 做 session lookup，再断开后重连进入正式 TUI；因此这里不能在
首条 websocket 连接结束后立即自关，而要保留一段 idle 窗口给下一次连接。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.client import connect
from websockets.sync.server import serve

from bot.interaction_contract import (
    INTERACTIVE_SERVER_REQUEST_METHODS,
    automatic_server_request_response,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.local_websocket_auth import (
    AppServerWebsocketAuthTokenStore,
    FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR,
    FOCUS_SERVICE_TOKEN_ENV_VAR,
    build_bearer_authorization_headers,
    parse_bearer_authorization_header,
)
from bot.process_utils import process_exists
from bot.service_control_plane import (
    ServiceControlKnownNotCommittedError,
    control_request,
)
from bot.turn_interrupt_audit import (
    TurnInterruptSource,
    record_turn_interrupt_dispatch_attempt,
)

logger = logging.getLogger(__name__)
_ControlReceipt = Literal["acknowledged", "known_not_committed", "outcome_unknown"]

_CWD_PROXY_METHODS = {"thread/start"}
_DEFAULT_IDLE_TIMEOUT_SECONDS = 30.0
_OPERATION_HEARTBEAT_SECONDS = 3.0
_RESOLVED_SERVER_REQUEST_RECEIPT_LIMIT = 256
# A detached review is a separate app-server thread.  Upstream deliberately
# excludes review/Guardian threads from the persisted ``ThreadSpawn``
# ancestor inventory. fcodex's reviewed TUI path already asks
# for inline review; retain only that delivery at this shared proxy boundary.
_REVIEW_START_ALLOWED_PARAMS = frozenset({"threadId", "target", "delivery"})
_COORDINATED_NOTIFICATION_METHODS = {
    "serverRequest/resolved",
    "thread/started",
    "thread/status/changed",
    "thread/closed",
    "thread/archived",
    "thread/deleted",
    "turn/started",
    "turn/completed",
}
# A request without a concrete ``params.threadId`` has no thread target for
# the service-owned coordinator to classify. Do not treat the shared
# app-server as a generic fcodex control plane in that case: this small list
# is deliberately limited to reviewed connection bootstrap and trusted
# discovery/read calls needed by the upstream remote TUI.  In Focus's explicit
# shared-trust deployment, ``hooks/list`` and ``skills/list`` may enumerate
# their requested CWDs; that exact discovery allowance is not a wildcard for
# arbitrary host/process/file/configuration operations.  Every other such
# request is rejected locally before it can reach state shared by the other
# Focus surfaces.
#
# Keep this in sync with the reviewed policy in
# docs/contracts/codex-app-server-schema-baseline.json.  The schema drift
# guard compares the two sets during an upgrade.
_FCODEX_UNSCOPED_ALLOWED_CLIENT_REQUEST_METHODS = frozenset(
    {
        "initialize",
        "account/read",
        "config/read",
        "configRequirements/read",
        "model/list",
        "hooks/list",
        "skills/list",
        "account/rateLimits/read",
        "thread/list",
        "thread/loaded/list",
        "app/list",
        "app/installed",
        "experimentalFeature/list",
        "mcpServerStatus/list",
    }
)
# ``initialized`` is the one current client notification.  It completes the
# per-connection handshake after the allowed ``initialize`` request; unknown
# notifications are suppressed rather than becoming an unreviewed side
# channel to the shared backend.
_FCODEX_CONNECTION_LOCAL_CLIENT_NOTIFICATION_METHODS = {"initialized"}
_UNAUTHORIZED_RESPONSE_BODY = b"missing or invalid websocket bearer token\n"


def _rewrite_thread_start_cwd(message: str | bytes, cwd: str) -> str | bytes:
    raw: str
    if isinstance(message, bytes):
        try:
            raw = message.decode("utf-8")
        except UnicodeDecodeError:
            return message
    else:
        raw = message

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return message
    if not isinstance(payload, dict):
        return message
    if payload.get("method") not in _CWD_PROXY_METHODS:
        return message
    params = payload.get("params")
    if not isinstance(params, dict):
        return message
    if params.get("cwd") not in (None, ""):
        return message

    updated_payload = dict(payload)
    updated_params = dict(params)
    updated_params["cwd"] = cwd
    updated_payload["params"] = updated_params
    encoded = json.dumps(updated_payload, ensure_ascii=False, separators=(",", ":"))
    if isinstance(message, bytes):
        return encoded.encode("utf-8")
    return encoded


def _parse_jsonrpc_message(message: str | bytes) -> tuple[dict[str, Any], bool] | None:
    raw: str
    is_bytes = isinstance(message, bytes)
    if is_bytes:
        try:
            raw = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        raw = message

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload, is_bytes


def _encode_jsonrpc_payload(payload: dict[str, Any], *, as_bytes: bool) -> str | bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if as_bytes:
        return encoded.encode("utf-8")
    return encoded


def _payload_thread_id(payload: dict[str, Any]) -> str:
    params = payload.get("params")
    if not isinstance(params, dict):
        return ""
    return str(params.get("threadId", "") or "").strip()


def _validate_thread_resume_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Validate the admitted target without rewriting native TUI semantics."""

    params = payload.get("params")
    if not isinstance(params, dict):
        return None, "fcodex `thread/resume` 必须携带一个确切的 string threadId；已在本地拒绝。"

    thread_id = params.get("threadId")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None, "fcodex `thread/resume` 必须携带一个非空 string threadId；已在本地拒绝。"
    if thread_id != thread_id.strip():
        return None, "fcodex `thread/resume.threadId` 必须是无首尾空白的确切值；已在本地拒绝。"

    # Upstream's cold-resume precedence is history > non-empty path >
    # threadId.  These are target aliases, not harmless optional metadata.
    if "history" in params and params.get("history") is not None:
        return None, "Focus 不支持 fcodex `thread/resume.history`；请使用确切 threadId。"
    path = params.get("path")
    if path is not None and path != "":
        return None, "Focus 不支持 fcodex `thread/resume.path`；请使用确切 threadId。"
    return payload, ""


def _canonicalize_review_start_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Pin fcodex review requests to the already-admitted root thread.

    ``review/start`` defaults to inline upstream, but an explicitly detached
    request creates a new review thread which is intentionally not a
    ``ThreadSpawn`` descendant.  Focus v1 has no independent detached-review
    writer/lifecycle contract, so accept only the normal inline form and make
    the default explicit before admission.
    """

    params = payload.get("params")
    if not isinstance(params, dict):
        return None, "fcodex `review/start` 必须携带 object params；已在本地拒绝。"

    thread_id = params.get("threadId")
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None, "fcodex `review/start` 必须携带一个非空 string threadId；已在本地拒绝。"
    target = params.get("target")
    if not isinstance(target, dict):
        return None, "fcodex `review/start.target` 必须是 object；已在本地拒绝。"

    unknown_params = sorted(str(key) for key in set(params) - _REVIEW_START_ALLOWED_PARAMS)
    if unknown_params:
        return (
            None,
            "Focus 尚未审阅 fcodex `review/start` 参数 "
            f"`{', '.join(unknown_params)}`；已在本地拒绝。",
        )

    delivery = params.get("delivery")
    if delivery not in (None, "inline"):
        if delivery == "detached":
            return (
                None,
                "Focus v1 不支持 fcodex detached review：它会创建不属于 "
                "root-child inventory 的独立 review thread；请使用 inline review。",
            )
        return None, "Focus 只支持 fcodex `review/start.delivery=inline`；已在本地拒绝。"

    canonical_payload = dict(payload)
    canonical_payload["params"] = {
        "threadId": thread_id.strip(),
        "target": dict(target),
        # Make upstream's default explicit.  This prevents a future default
        # change from silently creating an unowned detached operation.
        "delivery": "inline",
    }
    return canonical_payload, ""


def _require_backend_auth_data_dir(data_dir: str | pathlib.Path | None) -> pathlib.Path:
    normalized = str(data_dir or os.environ.get("FOCUS_DATA_DIR", "") or "").strip()
    if not normalized:
        raise RuntimeError(
            "fcodex proxy backend websocket auth requires instance data dir；"
            "请通过 `--data-dir` 或 `FOCUS_DATA_DIR` 指定目标实例数据目录。"
        )
    return pathlib.Path(normalized)


def _load_backend_auth_headers(data_dir: pathlib.Path) -> dict[str, str]:
    token = AppServerWebsocketAuthTokenStore(data_dir).require()
    return build_bearer_authorization_headers(token)


def _proxy_upgrade_auth_response(expected_token: str, request: Request) -> Response | None:
    normalized_expected = str(expected_token or "").strip()
    if not normalized_expected:
        raise RuntimeError("proxy auth token must not be empty")
    actual_token = parse_bearer_authorization_header(request.headers.get("Authorization"))
    if actual_token and secrets.compare_digest(actual_token, normalized_expected):
        return None
    return Response(
        401,
        "Unauthorized",
        Headers([("Content-Type", "text/plain; charset=utf-8")]),
        _UNAUTHORIZED_RESPONSE_BODY,
    )


@dataclass(frozen=True, slots=True)
class _PendingClientRequest:
    """One forwarded JSON-RPC request reserved until its exact response."""

    request_id: Any
    # ``None`` denotes an owner-free passthrough/read. A positive token is the
    # Service capability whose mutation outcome must be settled exactly.
    request_token: int | None
    method: str
    thread_id: str


def _is_usable_client_response(
    payload: dict[str, Any],
    request: _PendingClientRequest,
) -> bool:
    """Validate the app-server response shared by tracked and passthrough RPCs."""

    if "method" in payload:
        return False
    try:
        if _jsonrpc_id_key(payload.get("id")) != _jsonrpc_id_key(request.request_id):
            return False
    except ValueError:
        return False
    has_result = "result" in payload
    has_error = "error" in payload
    if has_result == has_error:
        return False
    return not has_error or isinstance(payload.get("error"), dict)


def _classify_coordinated_client_response(
    payload: dict[str, Any],
    request: _PendingClientRequest,
) -> tuple[str, dict[str, Any] | None] | None:
    """Accept only one exact, usable JSON-RPC response for an admitted RPC.

    The proxy's `operation/client-response` call can release a newly claimed
    operation on a known backend error.  Treating an ambiguous frame as that
    error is unsafe: the actual mutation could have reached the app-server.
    In particular, a resume response must prove it resumed the same concrete
    thread Focus admitted, and inline review must prove it stayed on that
    root rather than quietly producing an untracked detached review thread.
    """

    if not _is_usable_client_response(payload, request):
        return None
    if "error" in payload:
        return "error", None

    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    if request.method == "thread/start":
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or not isinstance(thread.get("id"), str)
            or not thread["id"].strip()
        ):
            return None
    elif request.method == "thread/resume":
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != request.thread_id
        ):
            return None
    elif request.method == "thread/goal/set":
        # The coordinator needs the target and the resulting status to decide
        # whether this request's process-local submission receipt can be
        # released without waiting for lifecycle reconciliation.
        # A generic object is not enough: accepting a schema-drifted result
        # as success would leave a possibly active mutation looking like a
        # normal known ACK.
        goal = result.get("goal")
        if (
            not isinstance(goal, dict)
            or goal.get("threadId") != request.thread_id
            or not isinstance(goal.get("status"), str)
            or not goal["status"].strip()
        ):
            return None
    elif request.method == "thread/goal/clear":
        # `cleared=false` is a valid typed no-op, so it is a known RPC
        # outcome but cannot causally settle an earlier active/resume
        # submission. Preserve the distinction so a malformed response keeps
        # only that exact process-local uncertainty instead of releasing it.
        if not isinstance(result.get("cleared"), bool):
            return None
    elif request.method == "review/start":
        # The proxy always sent inline. A different review thread would be an
        # independent operation outside the admitted direct thread.
        if result.get("reviewThreadId") != request.thread_id:
            return None
    return "success", result


def _jsonrpc_id_key(value: Any) -> str:
    """Backward-compatible local name for the shared typed-id codec."""

    return jsonrpc_id_key(value)


def _send_local_error_response(client_ws: Any, request_id: Any, message: str) -> None:
    if request_id in (None, ""):
        raise ValueError("local JSON-RPC error response requires a request id")
    client_ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32002,
                    "message": message,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _send_server_response(
    backend_ws: Any,
    *,
    request_id: int | str,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    backend_ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


class _ProxyInteractionGate:
    """Thin fcodex transport adapter; ownership stays in the FOCUS service."""

    def __init__(
        self,
        *,
        cwd: str,
        data_dir: pathlib.Path,
        participant_id: str = "",
        connection_id: str = "",
        control_request_fn: Callable[[pathlib.Path, str, dict[str, Any]], Any] = control_request,
        # Ignored compatibility values never grant proxy ownership.
        global_data_dir: pathlib.Path | None = None,
        instance_name: str = "",
        service_token: str = "",
        holder_pid: int = 0,
        runtime_lease_keeper: Any | None = None,
        enable_heartbeat: bool = False,
        heartbeat_interval_seconds: float = _OPERATION_HEARTBEAT_SECONDS,
    ) -> None:
        del global_data_dir, instance_name, service_token, runtime_lease_keeper
        self._cwd = cwd
        self._data_dir = pathlib.Path(data_dir)
        pid = int(holder_pid or os.getpid())
        self._participant_id = str(
            participant_id or f"fcodex:{pid}:{secrets.token_urlsafe(12)}"
        ).strip()
        self._connection_id = str(connection_id or secrets.token_urlsafe(12)).strip()
        self._control_request_fn = control_request_fn
        self._lock = threading.Lock()
        # Serialize every interactive control attempt with quarantine/close.
        # Once close wins this lock, no queued frame can start another attempt.
        self._interaction_attempt_lock = threading.RLock()
        # Keep the original envelope only to correlate a TUI answer with the
        # service-owned pending request. All upstream responses are submitted
        # by the RuntimeLoop service; this proxy has no fallback response path.
        self._pending_server_request_ids: dict[
            str, tuple[int | str, str, dict[str, Any], str]
        ] = {}
        # Current-wire receipts suppress a TUI action which was already queued
        # when the exact upstream resolved notification removed its overlay.
        # They are bounded correlation facts, not callback lifecycle state.
        self._resolved_server_request_ids: dict[str, None] = {}
        # Every forwarded request, including an unscoped bootstrap/read, owns
        # its exact typed wire id until one usable response arrives. Keeping a
        # single registry prevents an old passthrough response from consuming
        # a later Service-tracked mutation which reused that id.
        self._pending_client_request_by_id: dict[str, _PendingClientRequest] = {}
        self._closed = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_interval_seconds = max(float(heartbeat_interval_seconds), 0.1)

        connected = self._control("operation/participant-connected", {})
        if not isinstance(connected, dict):
            raise RuntimeError("FOCUS operation control returned an invalid participant response")
        # This is informational only: the RuntimeLoop coordinator remains the
        # authorization source for every request. Reconnecting a participant
        # does not recreate an old main-turn writer; the proxy has no
        # process-wide writer mode to infer locally.
        if enable_heartbeat:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="fcodex-operation-heartbeat",
                daemon=True,
            )
            self._heartbeat_thread.start()

    @property
    def participant_id(self) -> str:
        return self._participant_id

    def _control(self, method: str, params: dict[str, Any]) -> Any:
        payload = {
            "participant_id": self._participant_id,
            "connection_id": self._connection_id,
            **dict(params),
        }
        return self._control_request_fn(self._data_dir, method, payload)

    def _control_receipt(self, method: str, params: dict[str, Any]) -> tuple[_ControlReceipt, Any]:
        with self._interaction_attempt_lock:
            if self._is_closed():
                return "outcome_unknown", None
            try:
                return "acknowledged", self._control(method, params)
            except ServiceControlKnownNotCommittedError:
                return "known_not_committed", None
            except Exception:
                return "outcome_unknown", None

    def _best_effort_control(self, method: str, params: dict[str, Any]) -> Any | None:
        try:
            return self._control(method, params)
        except Exception:
            return None

    def _quarantine_interaction_transport(
        self,
        client_ws: Any,
        backend_ws: Any,
        *,
        reason: str,
    ) -> None:
        """Revoke this proxy connection after an unreceipted owner decision."""

        with self._interaction_attempt_lock:
            logger.error("Quarantining fcodex interaction transport: %s", reason)
            self.close()
            _close_quietly(client_ws)
            _close_quietly(backend_ws)

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _take_pending_client_request(self, response_id: Any) -> _PendingClientRequest | None:
        """Remove the one request matched by an exact, type-preserved id."""

        try:
            request_key = _jsonrpc_id_key(response_id)
        except ValueError:
            return None
        with self._lock:
            return self._pending_client_request_by_id.pop(request_key, None)

    def _retire_server_request_if_matches(
        self,
        request_key: str,
        pending: tuple[int | str, str, dict[str, Any], str],
    ) -> None:
        with self._lock:
            current = self._pending_server_request_ids.get(request_key)
            if current is not pending:
                return
            self._pending_server_request_ids.pop(request_key, None)
            self._remember_resolved_server_request_locked(request_key)

    def _remember_resolved_server_request_locked(self, request_key: str) -> None:
        self._resolved_server_request_ids[request_key] = None
        while (
            len(self._resolved_server_request_ids)
            > _RESOLVED_SERVER_REQUEST_RECEIPT_LIMIT
        ):
            oldest = next(iter(self._resolved_server_request_ids))
            self._resolved_server_request_ids.pop(oldest, None)

    def _take_type_mismatched_client_requests(
        self,
        response_id: Any,
    ) -> list[_PendingClientRequest]:
        """Fail-close the identifiable ``1`` vs ``\"1\"`` response mistake.

        Exact lookup always runs first, so numeric ``1`` and string ``"1"``
        may safely coexist. If neither exact key exists but the text matches,
        the backend changed the id type and the whole websocket generation is
        no longer a trustworthy correlation boundary.
        """

        if isinstance(response_id, bool) or not isinstance(response_id, (str, int, float)):
            return []
        response_text = str(response_id)
        with self._lock:
            matching_keys = [
                request_key
                for request_key, request in self._pending_client_request_by_id.items()
                if str(request.request_id) == response_text
            ]
            return [
                self._pending_client_request_by_id.pop(request_key)
                for request_key in matching_keys
            ]

    def _report_client_request_outcome(
        self,
        request: _PendingClientRequest,
        *,
        outcome: str,
        response_result: dict[str, Any] | None = None,
    ) -> bool:
        """Commit one exact outcome or require this connection to quarantine.

        A JSON-RPC id is reusable after its request completes.  The
        service-issued token therefore participates in every settlement so a
        delayed control call for the old request cannot consume a newer
        request with the same wire id.  Only an explicit, exact service
        receipt lets this websocket remain writable.
        """

        if request.request_token is None:
            raise ValueError("owner-free client request has no settlement capability")
        params: dict[str, Any] = {
            "request_id": request.request_id,
            "request_token": request.request_token,
            "outcome": outcome,
        }
        if response_result is not None:
            params["response_result"] = response_result
        receipt, result = self._control_receipt(
            "operation/client-response",
            params,
        )
        return bool(
            receipt == "acknowledged"
            and isinstance(result, dict)
            and result.get("known") is True
            and result.get("settled") is True
            and result.get("request_token") == request.request_token
        )

    def _mark_pending_client_requests_unknown(
        self,
        *already_removed: _PendingClientRequest,
    ) -> bool:
        """Retire one invalid correlation epoch and settle tracked mutations."""

        with self._lock:
            requests = [*already_removed, *self._pending_client_request_by_id.values()]
            self._pending_client_request_by_id.clear()
        tracked: list[_PendingClientRequest] = []
        seen: set[int] = set()
        for request in requests:
            if request.request_token is None or id(request) in seen:
                continue
            seen.add(id(request))
            tracked.append(request)
        receipts = [
            self._report_client_request_outcome(request, outcome="unknown")
            for request in tracked
        ]
        return all(receipts)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(self._heartbeat_interval_seconds):
            self._best_effort_control("operation/participant-heartbeat", {})

    def close(self) -> None:
        with self._interaction_attempt_lock:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                self._pending_server_request_ids.clear()
                self._resolved_server_request_ids.clear()
                self._pending_client_request_by_id.clear()
            self._heartbeat_stop.set()
            heartbeat_thread = self._heartbeat_thread
            if heartbeat_thread is not None and heartbeat_thread is not threading.current_thread():
                heartbeat_thread.join(timeout=0.2)
            # Service first fails closed delivered requests, then enters grace.
            self._best_effort_control("operation/participant-disconnected", {})

    def handle_client_message(self, message: str | bytes, *, client_ws: Any, backend_ws: Any) -> None:
        with self._interaction_attempt_lock:
            if self._is_closed():
                return
            self._handle_client_message_open(
                message,
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

    def _handle_client_message_open(
        self,
        message: str | bytes,
        *,
        client_ws: Any,
        backend_ws: Any,
    ) -> None:
        rewritten = _rewrite_thread_start_cwd(message, self._cwd)
        parsed = _parse_jsonrpc_message(rewritten)
        if parsed is None:
            # A batch, scalar, malformed JSON, or undecodable frame has no
            # safely classifiable method/id envelope.  Forwarding it would
            # let a batch wrapper bypass the unscoped default-deny policy.
            # The remote TUI speaks one JSON-RPC object per frame, so fail
            # closed rather than pass an unknown envelope to the shared
            # backend.
            return
        payload, is_bytes = parsed

        method = payload.get("method")
        if isinstance(method, str):
            # Client notifications cannot receive a JSON-RPC error.  Accept
            # only the reviewed connection-local handshake notification and
            # suppress every other notification before it reaches the shared
            # app-server.  Presence matters: an explicit null/empty id is an
            # invalid request id, never a notification.
            if "id" not in payload:
                if method not in _FCODEX_CONNECTION_LOCAL_CLIENT_NOTIFICATION_METHODS:
                    return
                try:
                    transport_admission = self._control(
                        "operation/transport-admit",
                        {},
                    )
                except Exception as exc:
                    self._quarantine_interaction_transport(
                        client_ws,
                        backend_ws,
                        reason=(
                            "connection notification crossed a closed service "
                            f"epoch: {exc}"
                        ),
                    )
                    return
                if (
                    not isinstance(transport_admission, dict)
                    or transport_admission.get("allowed") is not True
                ):
                    self._quarantine_interaction_transport(
                        client_ws,
                        backend_ws,
                        reason="service rejected connection notification transport",
                    )
                    return
                backend_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
                return

            request_id = payload.get("id")
            try:
                request_key = _jsonrpc_id_key(request_id)
            except ValueError:
                # null/empty cannot safely correlate an error response.  They
                # are still requests by envelope shape, so reject locally
                # instead of treating them as handshake notifications.
                if request_id not in (None, ""):
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        "fcodex 请求携带了无效的 JSON-RPC id；已在本地拒绝。",
                    )
                return
            with self._lock:
                if request_key in self._pending_client_request_by_id:
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        "fcodex connection 复用了尚未完成的 JSON-RPC request id；已拒绝该请求。",
                    )
                    return

            if method == "thread/resume":
                payload, resume_policy_error = _validate_thread_resume_payload(payload)
                if payload is None:
                    _send_local_error_response(client_ws, request_id, resume_policy_error)
                    return
            elif method == "review/start":
                payload, review_policy_error = _canonicalize_review_start_payload(payload)
                if payload is None:
                    _send_local_error_response(client_ws, request_id, review_policy_error)
                    return

            thread_id = _payload_thread_id(payload)

            # A missing/nullable optional threadId is materially different
            # from a root-scoped request: it has no operation owner to route
            # through RuntimeLoop.  The allowlist is intentionally a narrow
            # bootstrap/read surface.  Default-deny also covers malformed
            # thread-scoped requests that omit their required threadId.
            if (
                not thread_id
                and method != "thread/start"
                and method not in _FCODEX_UNSCOPED_ALLOWED_CLIENT_REQUEST_METHODS
            ):
                _send_local_error_response(
                    client_ws,
                    request_id,
                    "FOCUS 不支持 fcodex 无 threadId 的 app-server RPC "
                    f"`{method}`；已在本地拒绝，未转发到共享 backend。",
                )
                return

            # Every reviewed request, including unscoped reads, crosses the
            # service-owned backend epoch gate before its websocket write.
            # Root-operation tracking remains conditional in the Service;
            # transport admission is not.
            coordinated = True
            child_read_only = False
            tracks_response = False
            request_token: int | None = None
            if coordinated:
                try:
                    admission = self._control(
                        "operation/admit",
                        {
                            "request_id": request_id,
                            "rpc_method": method,
                            "thread_id": thread_id,
                            # The service validates this raw shape before it
                            # grants the one child metadata-read exception.
                            # It is not a client assertion that a target is a
                            # child or a root.
                            "request_params": (
                                dict(payload.get("params"))
                                if isinstance(payload.get("params"), dict)
                                else {}
                            ),
                            # App-server persists a new goal and sends this
                            # JSON-RPC response *before* applying its runtime
                            # effects.  A raw objective-only goal defaults to
                            # active upstream, so it can start a turn after
                            # this proxy has seen its ACK.  Do not let the
                            # service classify the request from a later
                            # notification: every raw goal/set is conservatively
                            # continuation-risk before it crosses the backend
                            # boundary. A typed, known non-continuing result
                            # may release that exact submission later.
                            "continuation_risk": method == "thread/goal/set",
                        },
                    )
                except Exception as exc:
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        f"FOCUS operation control unavailable; request was not forwarded: {exc}",
                    )
                    return
                if not isinstance(admission, dict) or not admission.get("allowed"):
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        str((admission or {}).get("reason", "当前操作被 Focus 拒绝。")),
                    )
                    return
                child_read_only = admission.get("child_read_only") is True
                if child_read_only and method != "thread/read":
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        "FOCUS service 返回了无效的 child read-only 准入；请求未转发。",
                    )
                    return
                if (
                    not isinstance(admission.get("tracks_response"), bool)
                    or "request_token" not in admission
                ):
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        "FOCUS service 未返回明确的 client-response tracking contract。",
                    )
                    return
                tracks_response = admission["tracks_response"]
                request_token = admission["request_token"]
                if child_read_only:
                    if tracks_response or request_token is not None:
                        _send_local_error_response(
                            client_ws,
                            request_id,
                            "FOCUS service 返回了冲突的 child read-only response capability。",
                        )
                        return
                elif tracks_response:
                    if (
                        isinstance(request_token, bool)
                        or not isinstance(request_token, int)
                        or request_token <= 0
                    ):
                        _send_local_error_response(
                            client_ws,
                            request_id,
                            "FOCUS service 未返回合法的 exact client-response capability。",
                        )
                        return
                elif request_token is not None:
                    _send_local_error_response(
                        client_ws,
                        request_id,
                        "FOCUS service 返回了未启用却非空的 client-response capability。",
                    )
                    return
            with self._lock:
                self._pending_client_request_by_id[request_key] = _PendingClientRequest(
                    request_id=request_id,
                    request_token=request_token if tracks_response else None,
                    method=method,
                    thread_id=thread_id,
                )
            try:
                if method == "turn/interrupt":
                    interrupt_params = payload.get("params")
                    raw_turn_id = (
                        interrupt_params.get("turnId")
                        if isinstance(interrupt_params, dict)
                        else ""
                    )
                    record_turn_interrupt_dispatch_attempt(
                        source=TurnInterruptSource.FCODEX_ENDPOINT,
                        thread_id=thread_id,
                        turn_id=raw_turn_id if isinstance(raw_turn_id, str) else "",
                    )
                backend_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            except Exception:
                request_context = self._take_pending_client_request(request_id)
                if (
                    request_context is not None
                    and request_context.request_token is not None
                    and not self._report_client_request_outcome(
                        request_context,
                        outcome="unknown",
                    )
                ):
                    self._quarantine_interaction_transport(
                        client_ws,
                        backend_ws,
                        reason=(
                            "backend send failed without an exact service-owned "
                            "client outcome receipt"
                        ),
                    )
                raise
            return

        # Any non-method object is a client-response candidate. Correlation is
        # the only authority to answer a globally scoped app-server request;
        # a missing, invalid, or unmatched id destroys that proof and must
        # quarantine the whole wire epoch instead of leaving service-owned
        # interaction state pending forever.
        if "id" not in payload:
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="TUI response candidate omitted its JSON-RPC id",
            )
            return
        response_id = payload.get("id")
        try:
            request_key = _jsonrpc_id_key(response_id)
        except ValueError:
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="TUI response candidate carried an invalid JSON-RPC id",
            )
            return
        with self._lock:
            pending_interaction = self._pending_server_request_ids.get(request_key)
            resolved_here = request_key in self._resolved_server_request_ids
        if pending_interaction is None:
            if resolved_here:
                return
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="TUI response candidate did not match a delivered request",
            )
            return

        response_result = payload.get("result")
        response_error = payload.get("error")
        has_valid_result = "result" in payload and isinstance(response_result, dict)
        has_valid_error = "error" in payload and isinstance(response_error, dict)
        valid_envelope = bool(
            "method" not in payload and has_valid_result != has_valid_error
        )
        if not valid_envelope:
            receipt, outcome = self._control_receipt(
                "operation/request-response-invalid",
                {
                    "request_id": response_id,
                    "response_token": pending_interaction[3],
                },
            )
            action = (
                str(outcome.get("action", "") or "")
                if receipt == "acknowledged" and isinstance(outcome, dict)
                else ""
            )
            if action == "suppress":
                self._retire_server_request_if_matches(
                    request_key,
                    pending_interaction,
                )
                return
            if action not in {"fail_closed", "deferred"}:
                self._quarantine_interaction_transport(
                    client_ws,
                    backend_ws,
                    reason=(
                        "invalid TUI response did not receive a complete "
                        f"service receipt ({receipt})"
                    ),
                )
            return
        receipt, submission = self._control_receipt(
            "operation/request-response-submit",
            {
                "request_id": response_id,
                "response_token": pending_interaction[3],
                "response_result": response_result if has_valid_result else None,
                "response_error": response_error if has_valid_error else None,
            },
        )
        if receipt != "acknowledged" or not isinstance(submission, dict):
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason=(
                    "TUI response did not receive an explicit service "
                    f"commit receipt ({receipt})"
                ),
            )
            return
        disposition = str(submission.get("response_disposition", "") or "")
        if submission.get("allowed") is True:
            if disposition in {"submitted", "superseded"}:
                self._retire_server_request_if_matches(
                    request_key,
                    pending_interaction,
                )
                return
            if disposition == "deferred":
                return
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="TUI response was accepted without an exact disposition",
            )
            return
        if disposition == "not_sent":
            request_id, request_method, request_params, _response_token = pending_interaction
            client_ws.send(
                _encode_jsonrpc_payload(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": request_method,
                        "params": request_params,
                    },
                    as_bytes=is_bytes,
                )
            )
            return
        if disposition == "unknown":
            self._retire_server_request_if_matches(
                request_key,
                pending_interaction,
            )
            return
        self._quarantine_interaction_transport(
            client_ws,
            backend_ws,
            reason="TUI response was rejected without an exact disposition",
        )
        return

    def handle_backend_message(self, message: str | bytes, *, client_ws: Any, backend_ws: Any) -> None:
        with self._interaction_attempt_lock:
            if self._is_closed():
                return
            self._handle_backend_message_open(
                message,
                client_ws=client_ws,
                backend_ws=backend_ws,
            )

    def _handle_backend_message_open(
        self,
        message: str | bytes,
        *,
        client_ws: Any,
        backend_ws: Any,
    ) -> None:
        parsed = _parse_jsonrpc_message(message)
        if parsed is None:
            # The app-server speaks one JSON-RPC object per frame.  If that
            # frame cannot even be classified, every in-flight coordinated
            # mutation on this proxy has an unknown outcome; do not let a
            # later disconnect/replay turn a persistent writer into a known
            # failure by accident.
            self._mark_pending_client_requests_unknown()
            client_ws.send(message)
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="malformed backend frame destroyed client-response correlation",
            )
            return
        payload, is_bytes = parsed

        method = payload.get("method")
        # A message that tries to be both a server request and a client
        # response is not a valid JSON-RPC envelope.  If its id belongs to an
        # admitted mutation, settle that mutation as unknown rather than
        # routing the malformed frame as a server request and accidentally
        # treating an upstream side effect as a known error.
        if (
            isinstance(method, str)
            and "id" in payload
            and ("result" in payload or "error" in payload)
        ):
            self._mark_pending_client_requests_unknown()
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="hybrid response/request frame destroyed client correlation",
            )
            return
        if isinstance(method, str) and "id" in payload:
            request_params = payload.get("params")
            automatic_response = automatic_server_request_response(
                method,
                {} if request_params is None else request_params,
            )
            if automatic_response is not None:
                request_id = payload.get("id")
                if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
                    logger.error(
                        "Suppressing automatic fcodex server request with invalid id: method=%s",
                        method,
                    )
                    return
                result, error = automatic_response
                _send_server_response(
                    backend_ws,
                    request_id=request_id,
                    result=result,
                    error=error,
                )
                return
            if method in INTERACTIVE_SERVER_REQUEST_METHODS:
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                receipt, route = self._control_receipt(
                    "operation/server-request",
                    {
                        "request_id": payload["id"],
                        "rpc_method": method,
                        "request_params": params,
                    },
                )
                if receipt != "acknowledged" or not isinstance(route, dict):
                    self._quarantine_interaction_transport(
                        client_ws,
                        backend_ws,
                        reason=(
                            "server-request routing did not receive a complete "
                            f"service receipt ({receipt})"
                        ),
                    )
                    return
                action = str((route or {}).get("action", "") or "")
                if action == "deliver":
                    response_token = str(route.get("response_token", "") or "")
                    if not response_token:
                        self._quarantine_interaction_transport(
                            client_ws,
                            backend_ws,
                            reason="server-request route omitted its exact response token",
                        )
                        return
                    with self._lock:
                        self._resolved_server_request_ids.pop(
                            _jsonrpc_id_key(payload["id"]),
                            None,
                        )
                        self._pending_server_request_ids[_jsonrpc_id_key(payload["id"])] = (
                            payload["id"],
                            method,
                            dict(params),
                            response_token,
                        )
                    client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
                    return
                if action in {"suppress", "fail_closed", "deferred"}:
                    # suppress belongs to another surface; fail_closed was
                    # sent by the service-owned coordinator; deferred is an
                    # exact service-owned response intent waiting for the
                    # canonical app-server connection generation.
                    return
                if action == "quarantine":
                    self._quarantine_interaction_transport(
                        client_ws,
                        backend_ws,
                        reason="service retained the request without a response commit",
                    )
                    return
                self._quarantine_interaction_transport(
                    client_ws,
                    backend_ws,
                    reason=f"service returned unknown server-request action {action!r}",
                )
                return
            # The proxy cannot safely pass through a server request it has not
            # classified and delivered under the service-owned ownership
            # protocol: its response id is global upstream, not local to this
            # websocket. The service adapter copy remains the only response
            # owner; quarantine this proxy connection instead of answering a
            # request for which it has no claim.
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason=f"unclassified server request {method!r}",
            )
            return

        if isinstance(method, str):
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if method in _COORDINATED_NOTIFICATION_METHODS:
                self._best_effort_control(
                    "operation/notification",
                    {
                        "rpc_method": method,
                        "notification_params": params,
                    },
                )
            if method == "serverRequest/resolved":
                request_id = params.get("requestId")
                if request_id not in (None, ""):
                    with self._lock:
                        request_key = _jsonrpc_id_key(request_id)
                        removed = self._pending_server_request_ids.pop(
                            request_key,
                            None,
                        )
                        if removed is not None:
                            self._remember_resolved_server_request_locked(request_key)
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            return

        # This is the only remaining legal shape: a response to one of this
        # connection's client requests.  A result/error frame without a
        # usable id cannot be correlated, so conservatively retain every
        # outstanding coordinated mutation as unknown.
        if "id" not in payload:
            self._mark_pending_client_requests_unknown()
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="backend response omitted its client correlation id",
            )
            return

        response_id = payload.get("id")
        request_context = self._take_pending_client_request(response_id)
        if request_context is None:
            mismatched = self._take_type_mismatched_client_requests(response_id)
            self._mark_pending_client_requests_unknown(*mismatched)
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason=(
                    "backend response changed a pending id type"
                    if mismatched
                    else "backend response did not match any forwarded client request"
                ),
            )
            return

        if request_context.request_token is None:
            if _is_usable_client_response(payload, request_context):
                client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
                return
            self._mark_pending_client_requests_unknown(request_context)
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="passthrough request received an unusable backend response",
            )
            return

        classified = _classify_coordinated_client_response(payload, request_context)
        if classified is None:
            self._mark_pending_client_requests_unknown(request_context)
            client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="tracked request received an unusable backend response",
            )
            return

        outcome, response_result = classified
        outcome_settled = self._report_client_request_outcome(
            request_context,
            outcome=outcome,
            response_result=response_result,
        )
        client_ws.send(_encode_jsonrpc_payload(payload, as_bytes=is_bytes))
        if not outcome_settled:
            self._quarantine_interaction_transport(
                client_ws,
                backend_ws,
                reason="backend response lacked an exact service settlement receipt",
            )


def _close_quietly(ws: Any) -> None:
    try:
        ws.close()
    except Exception:
        pass


def _relay_messages(
    source_ws: Any,
    target_ws: Any,
    *,
    transform: Callable[[str | bytes], str | bytes] | None = None,
) -> None:
    try:
        for message in source_ws:
            payload = transform(message) if transform is not None else message
            try:
                target_ws.send(payload)
            except ConnectionClosed:
                break
    except ConnectionClosed:
        pass


def _relay_client_messages(
    gate: _ProxyInteractionGate,
    client_ws: Any,
    backend_ws: Any,
) -> None:
    """Forward one client epoch without surfacing its expected close."""

    try:
        for client_message in client_ws:
            gate.handle_client_message(
                client_message,
                client_ws=client_ws,
                backend_ws=backend_ws,
            )
    except ConnectionClosed:
        pass


def run_proxy(
    *,
    backend_url: str,
    cwd: str,
    proxy_auth_token: str,
    data_dir: str | pathlib.Path | None = None,
    global_data_dir: str | pathlib.Path | None = None,
    instance_name: str = "",
    service_token: str = "",
    listen_host: str = "127.0.0.1",
    listen_port: int = 0,
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    parent_pid: int | None = None,
    on_listen: Callable[[str], None] | None = None,
    control_request_fn: Callable[[pathlib.Path, str, dict[str, Any]], Any] = control_request,
) -> None:
    normalized_proxy_auth_token = str(proxy_auth_token or "").strip()
    if not normalized_proxy_auth_token:
        raise RuntimeError("proxy auth token must not be empty")
    effective_data_dir = _require_backend_auth_data_dir(data_dir)
    backend_auth_headers = _load_backend_auth_headers(effective_data_dir)
    server_ref: dict[str, Any] = {}
    shutdown_once = threading.Event()
    state_lock = threading.Lock()
    active_connections = 0
    idle_deadline = 0.0
    # One wrapper process may establish a lookup websocket and then a formal
    # TUI websocket.  Keep the participant incarnation stable across both;
    # the service owns their reconnect grace rather than the proxy process.
    holder_pid = parent_pid or os.getpid()
    participant_id = f"fcodex:{holder_pid}:{secrets.token_urlsafe(12)}"
    del global_data_dir, instance_name, service_token

    def _shutdown_server() -> None:
        if shutdown_once.is_set():
            return
        shutdown_once.set()
        server = server_ref.get("server")
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _arm_idle_shutdown() -> None:
        nonlocal idle_deadline
        with state_lock:
            idle_deadline = time.monotonic() + max(0.0, idle_timeout_seconds)

    def _cancel_idle_shutdown() -> None:
        nonlocal idle_deadline
        with state_lock:
            idle_deadline = 0.0

    def _wait_until_idle_deadline() -> None:
        while not shutdown_once.is_set():
            with state_lock:
                current_connections = active_connections
                deadline = idle_deadline
            if current_connections > 0 or deadline <= 0.0:
                time.sleep(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.05))
                continue
            with state_lock:
                if active_connections == 0 and idle_deadline == deadline:
                    _shutdown_server()
                    return

    def _wait_until_parent_exit() -> None:
        if parent_pid is None:
            return
        while not shutdown_once.is_set():
            if not process_exists(parent_pid):
                _shutdown_server()
                return
            time.sleep(0.25)

    def _process_request(_connection: Any, request: Request) -> Response | None:
        return _proxy_upgrade_auth_response(normalized_proxy_auth_token, request)

    def _handler(client_ws: Any) -> None:
        nonlocal active_connections
        with state_lock:
            active_connections += 1
        _cancel_idle_shutdown()
        try:
            backend_connect_kwargs: dict[str, Any] = {
                "max_size": None,
                "proxy": None,
            }
            if backend_auth_headers:
                backend_connect_kwargs["additional_headers"] = backend_auth_headers
            with connect(backend_url, **backend_connect_kwargs) as backend_ws:
                gate: _ProxyInteractionGate | None = None
                gate = _ProxyInteractionGate(
                    cwd=cwd,
                    data_dir=effective_data_dir,
                    participant_id=participant_id,
                    holder_pid=holder_pid,
                    control_request_fn=control_request_fn,
                    enable_heartbeat=True,
                )

                def _backend_to_client() -> None:
                    try:
                        try:
                            for backend_message in backend_ws:
                                gate.handle_backend_message(
                                    backend_message,
                                    client_ws=client_ws,
                                    backend_ws=backend_ws,
                                )
                        except ConnectionClosed:
                            pass
                    finally:
                        _close_quietly(client_ws)
                        _close_quietly(backend_ws)

                thread = threading.Thread(target=_backend_to_client, daemon=True)
                thread.start()
                try:
                    _relay_client_messages(gate, client_ws, backend_ws)
                finally:
                    # Coordinator disconnect first: it sends fail-closed
                    # responses through the service adapter before this local
                    # proxy gives up its websocket transport.
                    if gate is not None:
                        gate.close()
                    _close_quietly(backend_ws)
                    _close_quietly(client_ws)
                    thread.join(timeout=1)
        finally:
            with state_lock:
                active_connections = max(0, active_connections - 1)
                should_arm_idle = active_connections == 0
            if should_arm_idle:
                _arm_idle_shutdown()

    with serve(
        _handler,
        listen_host,
        listen_port,
        max_size=None,
        process_request=_process_request,
    ) as server:
        server_ref["server"] = server
        actual_port = server.socket.getsockname()[1]
        listen_url = f"ws://{listen_host}:{actual_port}"
        if on_listen is not None:
            on_listen(listen_url)
        else:
            print(listen_url, flush=True)
        _arm_idle_shutdown()
        threading.Thread(target=_wait_until_idle_deadline, daemon=True).start()
        if parent_pid is not None:
            threading.Thread(target=_wait_until_parent_exit, daemon=True).start()
        server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="fcodex local cwd proxy")
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--global-data-dir", default="")
    parser.add_argument("--instance", default="")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args(argv)
    proxy_auth_token = str(os.environ.get(FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR, "")).strip()
    if not proxy_auth_token:
        print(
            f"缺少 proxy websocket 鉴权环境变量 `{FOCUS_REMOTE_AUTH_TOKEN_ENV_VAR}`。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    service_token = str(os.environ.get(FOCUS_SERVICE_TOKEN_ENV_VAR, "")).strip()
    run_proxy(
        backend_url=args.backend_url,
        cwd=args.cwd,
        proxy_auth_token=proxy_auth_token,
        data_dir=args.data_dir or None,
        global_data_dir=args.global_data_dir or None,
        instance_name=args.instance,
        service_token=service_token,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        parent_pid=args.parent_pid or None,
    )


if __name__ == "__main__":
    main()
