"""
基于 Codex app-server 的适配层。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, ContextManager, Mapping

from bot.adapters.base import (
    AgentAdapter,
    RuntimeConfigSummary,
    RuntimeModelSummary,
    RuntimeModelServiceTier,
    RuntimeModelUpgradeInfo,
    RuntimeReasoningEffortOption,
    ThreadGoalSummary,
    ThreadItemsPage,
    ThreadResumePage,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
    TurnInputItem,
)
from bot.adapters.codex_thread_summary import (
    read_optional_string as _read_string,
    thread_summary_from_app_server_thread,
)
from bot.adapters.codex_thread_inspection import (
    require_optional_request_cursor,
    require_optional_u32,
    require_request_identity,
    thread_items_page_from_result,
    thread_search_occurrences_page_from_result,
)
from bot.adapters.codex_goal_response import (
    decode_thread_goal_clear_response,
    decode_thread_goal_response,
)
from bot.codex_config import DEFAULT_CODEX_CONFIG, CodexConfig
from bot.codex_protocol.client import (
    AppServerEndpointMode,
    CodexRpcClient,
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.permissions_profile import (
    PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX,
    normalize_permissions_profile_id,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.thread_memory_mode import (
    deep_merge_config_overrides,
    thread_memory_mode_from_memories_config,
)
logger = logging.getLogger(__name__)
_THREAD_UNSUBSCRIBE_SUCCESS_STATUSES = frozenset(
    {"unsubscribed", "notSubscribed", "notLoaded"}
)
_FOCUS_PERSISTED_THREAD_HISTORY_MODE = "paginated"


def _deadline_from_timeout(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    return time.monotonic() + max(float(timeout), 0.0)


def _remaining_before_deadline(
    deadline_monotonic: float | None,
    *,
    operation: str,
) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Codex {operation} exceeded its caller deadline")
    return remaining


@dataclass(slots=True)
class CodexAppServerConfig:
    codex_command: str = DEFAULT_CODEX_CONFIG.codex_command
    endpoint_mode: AppServerEndpointMode = AppServerEndpointMode.OWNED_PROCESS
    app_server_url: str = DEFAULT_CODEX_CONFIG.app_server_url
    connect_timeout_seconds: float = DEFAULT_CODEX_CONFIG.connect_timeout_seconds
    request_timeout_seconds: float = DEFAULT_CODEX_CONFIG.request_timeout_seconds
    service_name: str = DEFAULT_CODEX_CONFIG.service_name
    permissions_profile_id: str = DEFAULT_CODEX_CONFIG.permissions_profile_id
    approval_policy: str = DEFAULT_CODEX_CONFIG.approval_policy
    approvals_reviewer: str = DEFAULT_CODEX_CONFIG.approvals_reviewer
    personality: str = DEFAULT_CODEX_CONFIG.personality
    model: str = DEFAULT_CODEX_CONFIG.model
    service_tier: str = DEFAULT_CODEX_CONFIG.service_tier
    reasoning_effort: str = DEFAULT_CODEX_CONFIG.reasoning_effort
    app_server_data_dir: str = ""
    source_kinds: list[str] = field(
        default_factory=lambda: list(DEFAULT_CODEX_CONFIG.source_kinds)
    )

    def __post_init__(self) -> None:
        self.endpoint_mode = AppServerEndpointMode(self.endpoint_mode)

    @property
    def sandbox(self) -> str:
        return PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX.get(self.permissions_profile_id, "danger-full-access")

    @classmethod
    def from_config(cls, config: CodexConfig) -> "CodexAppServerConfig":
        return cls(
            codex_command=config.codex_command,
            app_server_url=config.app_server_url,
            connect_timeout_seconds=config.connect_timeout_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            service_name=config.service_name,
            permissions_profile_id=config.permissions_profile_id,
            approval_policy=config.approval_policy,
            approvals_reviewer=config.approvals_reviewer,
            personality=config.personality,
            model=config.model,
            service_tier=config.service_tier,
            reasoning_effort=config.reasoning_effort,
            source_kinds=list(config.source_kinds),
        )

    @classmethod
    def from_dict(cls, config: Mapping[str, Any]) -> "CodexAppServerConfig":
        return cls.from_config(CodexConfig.from_dict(config))

    def with_attached_endpoint(
        self,
        *,
        app_server_url: str,
        app_server_data_dir: str = "",
    ) -> "CodexAppServerConfig":
        """Return a client config for an endpoint owned by another component."""

        return replace(
            self,
            endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
            app_server_url=app_server_url,
            app_server_data_dir=app_server_data_dir,
        )


class CodexAppServerAdapter(AgentAdapter):
    """通过 app-server 与 Codex 交互。"""

    def __init__(
        self,
        config: CodexAppServerConfig,
        *,
        on_notification: Callable[[int, str, dict[str, Any]], None] | None = None,
        on_request: Callable[[int, int | str, str, dict[str, Any]], None] | None = None,
        on_disconnect_ingress: Callable[[int], bool] | None = None,
        on_disconnect: Callable[[int], None] | None = None,
        app_server_runtime_store: AppServerRuntimeStore | None = None,
        issue_outbound_request: Callable[[str], object] | None = None,
        guard_outbound_send: Callable[[object], ContextManager[None]] | None = None,
        confirm_outbound_request: Callable[[object], None] | None = None,
    ) -> None:
        self._config = config
        # Standalone adapter consumers do not own Focus' backend-reset epoch.
        # The service composition injects its required gate; absence here means
        # that this adapter instance has no external epoch owner, not that a
        # service-owned gate may be bypassed at individual call sites.
        outbound_ports = (
            issue_outbound_request,
            guard_outbound_send,
            confirm_outbound_request,
        )
        if any(port is not None for port in outbound_ports) and not all(
            port is not None for port in outbound_ports
        ):
            raise TypeError(
                "outbound request issue/send-guard/confirm ports must be supplied together"
            )
        self._issue_outbound_request_port = issue_outbound_request
        self._guard_outbound_send_port = guard_outbound_send
        self._confirm_outbound_request_port = confirm_outbound_request
        self._rpc = CodexRpcClient(
            codex_command=config.codex_command,
            endpoint_mode=config.endpoint_mode,
            app_server_url=config.app_server_url,
            connect_timeout_seconds=config.connect_timeout_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            on_notification=on_notification,
            on_request=on_request,
            on_disconnect_ingress=on_disconnect_ingress,
            on_disconnect=on_disconnect,
            on_initialized=self._handle_rpc_initialized,
            app_server_runtime_store=app_server_runtime_store,
            app_server_data_dir=config.app_server_data_dir or None,
        )
        self._initialize_identity: dict[str, Any] = {}

    def _handle_rpc_initialized(
        self,
        connection_generation: int,
        initialize_result: dict[str, Any],
    ) -> None:
        """Validate Focus requirements for this exact connection before work."""

        self._initialize_identity = {
            "connection_generation": int(connection_generation),
            "user_agent": _read_string(initialize_result, "userAgent"),
            "codex_home": _read_string(initialize_result, "codexHome"),
            "platform_family": _read_string(initialize_result, "platformFamily"),
            "platform_os": _read_string(initialize_result, "platformOs"),
        }
        result = self._require_object_result(
            "configRequirements/read",
            self._rpc_request(
                "configRequirements/read",
                None,
                connection_initialization=True,
            ),
        )
        self._validate_focus_requirements(result)

    def require_owned_backend_lifecycle(self) -> None:
        """Fail unless this adapter owns the backend process-tree barrier."""

        self._rpc.require_owned_backend_lifecycle()

    @staticmethod
    def _validate_focus_requirements(result: dict[str, Any]) -> None:
        if "requirements" not in result:
            raise CodexRpcProtocolError(
                "configRequirements/read",
                "Codex configRequirements/read response is missing requirements",
            )
        requirements = result.get("requirements")
        if requirements is None:
            return
        if not isinstance(requirements, dict):
            raise CodexRpcProtocolError(
                "configRequirements/read",
                "Codex configRequirements/read requirements must be an object or null",
            )

        # Other managed allow-lists constrain concrete upstream effects. They
        # are not a requirement that every item in Focus' static catalogs be
        # available before this shared connection can be used.
        raw_reviewers = requirements.get("allowedApprovalsReviewers")
        if raw_reviewers is not None:
            if not isinstance(raw_reviewers, list) or any(
                not isinstance(value, str) for value in raw_reviewers
            ):
                raise CodexRpcProtocolError(
                    "configRequirements/read",
                    "allowedApprovalsReviewers must be an array of strings",
                )
            reviewers = {str(value).strip() for value in raw_reviewers}
            if "user" not in reviewers:
                raise RuntimeError(
                    "Focus requires approvalsReviewer=user, but the connected Codex requirements disallow it"
                )

    def start(self) -> None:
        self._rpc.start()

    def stop(self) -> None:
        self._rpc.stop()

    def current_app_server_url(self) -> str:
        return self._rpc.current_app_server_url()

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        """Return the current websocket identity without reviving a backend."""

        return self._rpc.connection_generation(
            timeout=timeout,
            require_existing_connection=require_existing_connection,
        )

    def fence_backend_reset_generation(
        self,
        *,
        expected_connection_generation: int,
        fence_ingress: Callable[[], None],
        timeout: float | None = None,
    ) -> None:
        self._rpc.fence_backend_reset_generation(
            expected_connection_generation=expected_connection_generation,
            fence_ingress=fence_ingress,
            timeout=timeout,
        )

    def _rpc_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
        connection_initialization: bool = False,
        admit_existing_connection: bool = False,
    ) -> Any:
        """Forward a request, optionally forbidding reconnect-on-demand.

        Ordinary foreground commands retain the app-server client's convenient
        reconnect behavior. Generation-pinned calls instead use the already-live
        websocket so stale work cannot cross a backend replacement. Data-plane
        calls may additionally request ordinary outbound admission; the exact
        connection pin and the service epoch permit prove different boundaries.
        """

        if connection_initialization and require_existing_connection:
            raise ValueError(
                "connection initialization cannot use caller-owned existing-connection authority"
            )
        if admit_existing_connection and not require_existing_connection:
            raise ValueError(
                "admitted existing-connection requests must require an existing connection"
            )
        if connection_initialization and admit_existing_connection:
            raise ValueError(
                "connection initialization cannot use ordinary outbound admission"
            )
        permit = None
        outbound_transport_guard = None
        if not connection_initialization and (
            not require_existing_connection or admit_existing_connection
        ):
            permit = self._issue_outbound_request(method)
            outbound_transport_guard = self._outbound_transport_guard(permit)

        effective_timeout = (
            self._config.request_timeout_seconds
            if timeout is None
            else float(timeout)
        )
        if effective_timeout <= 0:
            raise ValueError("Codex RPC timeout must be positive")
        request_kwargs: dict[str, Any] = {"timeout": effective_timeout}
        if require_existing_connection:
            request_kwargs["require_existing_connection"] = True
            if expected_connection_generation is not None:
                request_kwargs["expected_connection_generation"] = (
                    expected_connection_generation
                )
        elif expected_connection_generation is not None:
            raise ValueError(
                "expected_connection_generation requires require_existing_connection=True"
            )
        if outbound_transport_guard is not None:
            request_kwargs["outbound_transport_guard"] = outbound_transport_guard
        try:
            response = self._rpc.request(method, params, **request_kwargs)
        except CodexRpcTransportError:
            raise
        except CodexRpcError:
            # A decoded JSON-RPC error is still a response from one exact
            # backend epoch. Never let an old rejection drive fallback or
            # capability decisions after replacement has reopened.
            if permit is not None:
                self._confirm_outbound_request(method, permit)
            raise
        if permit is not None:
            self._confirm_outbound_request(method, permit)
        return response

    def _issue_outbound_request(self, method: str) -> object | None:
        """Map service epoch denial to an exact pre-send RPC failure."""

        issue = self._issue_outbound_request_port
        if issue is None:
            return None
        try:
            return issue(method)
        except CodexRpcPreSendError:
            raise
        except Exception as exc:
            raise CodexRpcPreSendError(method, exc) from exc

    def _confirm_outbound_request(self, method: str, permit: object | None) -> None:
        confirm = self._confirm_outbound_request_port
        if confirm is None:
            return
        try:
            confirm(permit)
        except Exception as exc:
            raise CodexRpcTransportError(
                method,
                {
                    "code": -32000,
                    "message": "Focus backend epoch changed while the request was in flight",
                },
            ) from exc

    def _outbound_transport_guard(
        self,
        permit: object | None,
    ) -> Callable[[], ContextManager[None]] | None:
        guard = self._guard_outbound_send_port
        if guard is None:
            return None
        return lambda: guard(permit)

    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        """Remove this connection's subscription to one thread.

        Current upstream keeps a last-subscriber thread loaded until its idle
        unload timer expires; this response proves only the connection-local
        subscription transition.  Failure or an unrecognized future status is
        observable so callers do not clear local subscription facts or release
        their runtime interest on ambiguous evidence.
        """

        result = self._rpc_request(
            "thread/unsubscribe",
            {"threadId": thread_id},
            require_existing_connection=(expected_connection_generation is not None),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        response = self._require_object_result("thread/unsubscribe", result)
        status = response.get("status")
        if status not in _THREAD_UNSUBSCRIBE_SUCCESS_STATUSES:
            raise CodexRpcProtocolError(
                "thread/unsubscribe",
                f"invalid unsubscribe status: {status!r}",
            )

    def create_thread(
        self,
        *,
        cwd: str,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
    ) -> ThreadSnapshot:
        params = self._thread_params(
            cwd=cwd,
            include_service_name=True,
            config_overrides=config_overrides,
            model=model,
            model_provider=model_provider,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id or sandbox,
        )
        # Focus-owned Web/Feishu create is a persistent-thread path. Its
        # bounded inspection contract requires the upstream paginated store;
        # fcodex bypasses this adapter method and retains its native TUI payload.
        params["historyMode"] = _FOCUS_PERSISTED_THREAD_HISTORY_MODE
        result = self._request_with_permissions_fallback(
            "thread/start",
            params,
            legacy_field="sandbox",
            legacy_value=self._legacy_sandbox(permissions_profile_id or sandbox),
        )
        _result, snapshot = self._require_thread_snapshot_result(
            "thread/start",
            result,
            expected_history_mode=_FOCUS_PERSISTED_THREAD_HISTORY_MODE,
        )
        return snapshot

    def resume_thread(
        self,
        thread_id: str,
        *,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> ThreadSnapshot:
        result = self._resume_thread_result(
            thread_id,
            config_overrides=config_overrides,
            model=model,
            model_provider=model_provider,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            expected_connection_generation=expected_connection_generation,
        )
        _result, snapshot = self._require_thread_snapshot_result(
            "thread/resume",
            result,
            expected_thread_id=thread_id,
            expected_approval_policy=approval_policy or "",
            expected_permissions_profile_id=permissions_profile_id or "",
        )
        return snapshot

    def resume_thread_page(
        self,
        thread_id: str,
        *,
        limit: int,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> ThreadResumePage:
        result = self._resume_thread_result(
            thread_id,
            config_overrides=config_overrides,
            model=model,
            model_provider=model_provider,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            exclude_turns=True,
            initial_turns_page={
                "limit": max(int(limit), 1),
                "sortDirection": "desc",
                "itemsView": "full",
            },
            expected_connection_generation=expected_connection_generation,
        )
        raw_page = result.get("initialTurnsPage")
        if not isinstance(raw_page, dict):
            raise CodexRpcProtocolError(
                "thread/resume",
                "Codex thread/resume did not return initialTurnsPage; upgrade Codex app-server",
            )
        _result, snapshot = self._require_thread_snapshot_result(
            "thread/resume",
            result,
            expected_thread_id=thread_id,
            expected_approval_policy=approval_policy or "",
            expected_permissions_profile_id=permissions_profile_id or "",
        )
        return ThreadResumePage(
            snapshot=snapshot,
            initial_turns_page=self._turns_page_from_result(raw_page, sort_direction="desc"),
            turns_backwards_cursor=_read_string(result, "turnsBackwardsCursor"),
            items_backwards_cursor=_read_string(result, "itemsBackwardsCursor"),
        )

    def _resume_thread_result(
        self,
        thread_id: str,
        *,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        exclude_turns: bool = False,
        initial_turns_page: dict[str, Any] | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        # A resumed thread may carry a persisted approvals reviewer from the
        # client that originally created it.  In particular, upstream keeps
        # ``auto_review`` across a cold resume unless this request explicitly
        # replaces it. Focus has reviewed only the explicit ``user`` reviewer
        # route, so establish that safety boundary before an active persisted
        # goal can continue as a side effect of ``thread/resume``.
        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalsReviewer": "user",
        }
        if model:
            params["model"] = model
        if model_provider:
            params["modelProvider"] = model_provider
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if permissions_profile_id:
            params["permissions"] = normalize_permissions_profile_id(
                permissions_profile_id,
                fallback=self._config.permissions_profile_id,
            )
        merged_config = self._merge_request_config(config_overrides=config_overrides)
        if merged_config:
            params["config"] = merged_config
        if exclude_turns:
            params["excludeTurns"] = True
        if initial_turns_page:
            params["initialTurnsPage"] = dict(initial_turns_page)
        result = self._request_with_permissions_fallback(
            "thread/resume",
            params,
            legacy_field="sandbox",
            legacy_value=self._legacy_sandbox(permissions_profile_id),
            require_existing_connection=(expected_connection_generation is not None),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return self._require_object_result("thread/resume", result)

    def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        search_term: str | None = None,
        sort_key: str | None = None,
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
        archived: bool | None = None,
        parent_thread_id: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> tuple[list[ThreadSummary], str | None]:
        relation_query = bool(parent_thread_id)
        effective_source_kinds = source_kinds
        if effective_source_kinds is None and not relation_query:
            effective_source_kinds = self._config.source_kinds
        params = _compact(
            {
                "cwd": cwd,
                "limit": limit,
                "cursor": cursor,
                "searchTerm": search_term,
                "sortKey": sort_key,
                "sourceKinds": effective_source_kinds,
                "archived": archived,
                "parentThreadId": parent_thread_id,
            }
        )
        if source_kinds is not None:
            # Preserve the caller's explicit filter, including an empty list.
            # Relationship discovery deliberately passes None to omit the field
            # and include every source kind.
            params["sourceKinds"] = source_kinds
        if model_providers is not None:
            # app-server 将显式空列表解释为“不按 provider 过滤”。
            params["modelProviders"] = model_providers
        result = self._rpc_request(
            "thread/list",
            params,
            timeout=timeout,
            require_existing_connection=(
                require_existing_connection
                or expected_connection_generation is not None
            ),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return self._thread_list_page_from_result(result)

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadSnapshot:
        result = self._rpc_request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            timeout=timeout,
            require_existing_connection=(
                require_existing_connection
                or expected_connection_generation is not None
            ),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        _result, snapshot = self._require_thread_snapshot_result(
            "thread/read",
            result,
            expected_thread_id=thread_id,
        )
        return snapshot

    def list_thread_turns(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str = "desc",
        items_view: str = "full",
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadTurnsPage:
        direction = str(sort_direction or "desc").strip().lower()
        if direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc or desc")
        view = str(items_view or "full").strip().lower()
        if view not in {"summary", "full"}:
            raise ValueError("items_view must be summary or full")
        result = self._require_object_result(
            "thread/turns/list",
            self._rpc_request(
                "thread/turns/list",
                _compact(
                    {
                        "threadId": thread_id,
                        "cursor": cursor,
                        "limit": limit,
                        "sortDirection": direction,
                        "itemsView": view,
                    }
                ),
                timeout=timeout,
                require_existing_connection=(
                    require_existing_connection
                    or expected_connection_generation is not None
                ),
                expected_connection_generation=expected_connection_generation,
                admit_existing_connection=(expected_connection_generation is not None),
            ),
        )
        return self._turns_page_from_result(result, sort_direction=direction)

    def list_thread_items(
        self,
        thread_id: str,
        *,
        turn_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadItemsPage:
        normalized_thread_id = require_request_identity(thread_id, field="thread_id")
        normalized_turn_id = (
            None
            if turn_id is None
            else require_request_identity(turn_id, field="turn_id")
        )
        normalized_cursor = require_optional_request_cursor(cursor)
        normalized_limit = require_optional_u32(limit)
        if sort_direction is not None and sort_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be asc, desc, or None")
        result = self._rpc_request(
            "thread/items/list",
            _compact(
                {
                    "threadId": normalized_thread_id,
                    "turnId": normalized_turn_id,
                    "cursor": normalized_cursor,
                    "limit": normalized_limit,
                    "sortDirection": sort_direction,
                }
            ),
            timeout=timeout,
            require_existing_connection=(
                require_existing_connection
                or expected_connection_generation is not None
            ),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return thread_items_page_from_result(result)

    def search_thread_occurrences(
        self,
        thread_id: str,
        *,
        search_term: str,
        cursor: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadSearchOccurrencesPage:
        normalized_thread_id = require_request_identity(thread_id, field="thread_id")
        if not isinstance(search_term, str) or not search_term.strip():
            raise ValueError("search_term must be a non-empty, non-whitespace string")
        normalized_cursor = require_optional_request_cursor(cursor)
        normalized_limit = require_optional_u32(limit)
        result = self._rpc_request(
            "thread/searchOccurrences",
            _compact(
                {
                    "threadId": normalized_thread_id,
                    "searchTerm": search_term,
                    "cursor": normalized_cursor,
                    "limit": normalized_limit,
                }
            ),
            timeout=timeout,
            require_existing_connection=(
                require_existing_connection
                or expected_connection_generation is not None
            ),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return thread_search_occurrences_page_from_result(result)

    def get_thread_goal(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> ThreadGoalSummary | None:
        result = self._rpc_request(
            "thread/goal/get",
            {"threadId": thread_id},
            require_existing_connection=(expected_connection_generation is not None),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return decode_thread_goal_response(
            "thread/goal/get",
            result,
            expected_thread_id=thread_id,
            allow_null=True,
        )

    def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> ThreadGoalSummary:
        result = self._rpc_request(
            "thread/goal/set",
            _compact(
                {
                    "threadId": thread_id,
                    "objective": objective,
                    "status": status,
                    "tokenBudget": token_budget,
                }
            ),
        )
        goal = decode_thread_goal_response(
            "thread/goal/set",
            result,
            expected_thread_id=thread_id,
            allow_null=False,
        )
        assert goal is not None
        return goal

    def clear_thread_goal(self, thread_id: str) -> bool:
        result = self._rpc_request("thread/goal/clear", {"threadId": thread_id})
        return decode_thread_goal_clear_response("thread/goal/clear", result)

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        result = self._rpc_request("config/read", _compact({"includeLayers": True, "cwd": cwd}))
        return self._runtime_config_from_result(result)

    def list_models(self, *, include_hidden: bool = False) -> list[RuntimeModelSummary]:
        models: list[RuntimeModelSummary] = []
        seen_catalog_ids: set[str] = set()
        seen_model_names: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        deadline = _deadline_from_timeout(self._config.request_timeout_seconds)
        while True:
            result = self._require_object_result(
                "model/list",
                self._rpc_request(
                    "model/list",
                    _compact(
                        {
                            "includeHidden": True if include_hidden else None,
                            "cursor": cursor,
                        }
                    ),
                    timeout=_remaining_before_deadline(
                        deadline,
                        operation="model/list pagination",
                    ),
                ),
            )
            page = self._model_summaries_from_result(result)
            for model in page:
                catalog_id = model.catalog_id or model.model
                if catalog_id in seen_catalog_ids:
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list returned duplicate model id {catalog_id!r}",
                    )
                if model.model in seen_model_names:
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list returned duplicate model selector {model.model!r}",
                    )
                seen_catalog_ids.add(catalog_id)
                seen_model_names.add(model.model)
                models.append(model)
            if "nextCursor" not in result:
                raise CodexRpcProtocolError(
                    "model/list",
                    "Codex model/list response is missing nextCursor",
                )
            raw_next_cursor = result.get("nextCursor")
            if raw_next_cursor is None:
                return models
            if not isinstance(raw_next_cursor, str) or not raw_next_cursor.strip():
                raise CodexRpcProtocolError(
                    "model/list",
                    "Codex model/list returned an invalid pagination cursor",
                )
            next_cursor = raw_next_cursor
            if not page:
                raise CodexRpcProtocolError(
                    "model/list",
                    "Codex model/list returned an empty non-terminal page",
                )
            if next_cursor in seen_cursors:
                raise CodexRpcProtocolError(
                    "model/list",
                    "Codex model/list returned a repeated pagination cursor",
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def list_loaded_thread_ids(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> list[str]:
        result = self._rpc_request(
            "thread/loaded/list",
            {},
            timeout=timeout,
            require_existing_connection=(
                require_existing_connection
                or expected_connection_generation is not None
            ),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        return self._loaded_thread_ids_from_result(result)

    def list_loaded_thread_ids_for_control(
        self,
        *,
        timeout: float,
    ) -> list[str]:
        """Read inventory without reconnecting and within ordinary epoch admission."""

        result = self._rpc_request(
            "thread/loaded/list",
            {},
            timeout=timeout,
            require_existing_connection=True,
            admit_existing_connection=True,
        )
        return self._loaded_thread_ids_from_result(result)

    def update_thread_settings(
        self,
        thread_id: str,
        *,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        effective_model = model or None
        effective_reasoning = reasoning_effort or None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": approval_policy or None,
            "model": effective_model,
            "effort": effective_reasoning,
        }
        if permissions_profile_id:
            params["permissions"] = normalize_permissions_profile_id(
                permissions_profile_id,
                fallback=self._config.permissions_profile_id,
            )
        self._request_with_permissions_fallback(
            "thread/settings/update",
            _compact(params),
            legacy_field="sandboxPolicy",
            legacy_value=self._legacy_sandbox_policy(permissions_profile_id),
        )

    def compact_thread(self, thread_id: str) -> None:
        result = self._rpc_request(
            "thread/compact/start",
            {"threadId": thread_id},
        )
        self._require_object_result("thread/compact/start", result)

    def start_review(
        self,
        thread_id: str,
        *,
        target: dict[str, Any],
        delivery: str = "inline",
    ) -> dict[str, Any]:
        result = self._rpc_request(
            "review/start",
            {
                "threadId": thread_id,
                "target": dict(target),
                "delivery": delivery,
            },
        )
        return self._require_turn_result("review/start", result)

    def rename_thread(self, thread_id: str, name: str) -> None:
        result = self._rpc_request("thread/name/set", {"threadId": thread_id, "name": name})
        self._require_object_result("thread/name/set", result)

    def archive_thread(self, thread_id: str) -> None:
        result = self._rpc_request("thread/archive", {"threadId": thread_id})
        self._require_object_result("thread/archive", result)

    def unarchive_thread(self, thread_id: str) -> ThreadSummary:
        result = self._require_object_result(
            "thread/unarchive",
            self._rpc_request("thread/unarchive", {"threadId": thread_id}),
        )
        try:
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise TypeError("result.thread is not an object")
            summary = self._summary_from_thread(thread)
            if summary.thread_id != thread_id:
                raise ValueError(
                    f"result.thread.id mismatch: expected {thread_id}, got {summary.thread_id or '<empty>'}"
                )
            return summary
        except Exception as exc:
            raise CodexRpcProtocolError(
                "thread/unarchive",
                "Codex thread/unarchive returned an invalid response",
            ) from exc

    def delete_thread(self, thread_id: str) -> None:
        result = self._rpc_request("thread/delete", {"threadId": thread_id})
        self._require_object_result("thread/delete", result)

    @staticmethod
    def _require_object_result(method: str, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} returned a non-object response",
            )
        return result

    @staticmethod
    def _require_next_cursor(method: str, payload: dict[str, Any]) -> str | None:
        """Decode a complete app-server page boundary without inventing one.

        Relationship discovery uses ``nextCursor is null`` as the only signal
        that the persisted inventory ended.  Treat a schema mismatch as an
        error instead of silently converting it into an empty final page.
        """

        if "nextCursor" not in payload:
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response is missing nextCursor",
            )
        cursor = payload["nextCursor"]
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor.strip():
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response has an invalid nextCursor",
            )
        return cursor

    @classmethod
    def _thread_list_page_from_result(
        cls,
        result: Any,
    ) -> tuple[list[ThreadSummary], str | None]:
        method = "thread/list"
        payload = cls._require_object_result(method, result)
        if "data" not in payload or not isinstance(payload["data"], list):
            raise CodexRpcProtocolError(
                method,
                "Codex thread/list response is missing data",
            )
        cursor = cls._require_next_cursor(method, payload)
        summaries: list[ThreadSummary] = []
        seen_thread_ids: set[str] = set()
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise CodexRpcProtocolError(
                    method,
                    "Codex thread/list response contains a non-object thread",
                )
            raw_thread_id = item.get("id")
            if (
                not isinstance(raw_thread_id, str)
                or not raw_thread_id.strip()
                or raw_thread_id != raw_thread_id.strip()
            ):
                raise CodexRpcProtocolError(
                    method,
                    "Codex thread/list response contains a thread without a valid id",
                )
            try:
                summary = cls._summary_from_thread(item)
            except Exception as exc:
                raise CodexRpcProtocolError(
                    method,
                    "Codex thread/list response contains an invalid thread",
                ) from exc
            if summary.thread_id != raw_thread_id or raw_thread_id in seen_thread_ids:
                raise CodexRpcProtocolError(
                    method,
                    "Codex thread/list response contains duplicate or mismatched thread ids",
                )
            seen_thread_ids.add(raw_thread_id)
            summaries.append(summary)
        return summaries, cursor

    @classmethod
    def _loaded_thread_ids_from_result(cls, result: Any) -> list[str]:
        method = "thread/loaded/list"
        payload = cls._require_object_result(method, result)
        if "data" not in payload or not isinstance(payload["data"], list):
            raise CodexRpcProtocolError(
                method,
                "Codex thread/loaded/list response is missing data",
            )
        # Focus deliberately omits cursor and limit.  The current upstream
        # contract returns the complete in-memory inventory in that form; a
        # next page would make the loaded-thread observation incomplete.
        if cls._require_next_cursor(method, payload) is not None:
            raise CodexRpcProtocolError(
                method,
                "Codex thread/loaded/list returned a partial inventory",
            )
        thread_ids: list[str] = []
        seen_thread_ids: set[str] = set()
        for raw_thread_id in payload["data"]:
            if (
                not isinstance(raw_thread_id, str)
                or not raw_thread_id.strip()
                or raw_thread_id != raw_thread_id.strip()
                or raw_thread_id in seen_thread_ids
            ):
                raise CodexRpcProtocolError(
                    method,
                    "Codex thread/loaded/list response contains an invalid thread id",
                )
            seen_thread_ids.add(raw_thread_id)
            thread_ids.append(raw_thread_id)
        return thread_ids

    @classmethod
    def _require_thread_snapshot_result(
        cls,
        method: str,
        result: Any,
        *,
        expected_thread_id: str = "",
        expected_approval_policy: str = "",
        expected_permissions_profile_id: str = "",
        expected_history_mode: str = "",
    ) -> tuple[dict[str, Any], ThreadSnapshot]:
        payload = cls._require_object_result(method, result)
        if method in {"thread/start", "thread/resume"}:
            reviewer = payload.get("approvalsReviewer")
            if reviewer != "user":
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response must report approvalsReviewer=user",
                )
        normalized_expected_approval = str(expected_approval_policy or "").strip()
        if normalized_expected_approval and payload.get("approvalPolicy") != normalized_expected_approval:
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response did not apply the requested approvalPolicy",
            )
        normalized_expected_permissions = ""
        if expected_permissions_profile_id:
            normalized_expected_permissions = normalize_permissions_profile_id(
                expected_permissions_profile_id,
            )
            active_profile = payload.get("activePermissionProfile")
            if (
                not isinstance(active_profile, dict)
                or active_profile.get("id") != normalized_expected_permissions
            ):
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response did not apply the requested activePermissionProfile",
                )
        thread = payload.get("thread")
        if not isinstance(thread, dict):
            raise CodexRpcProtocolError(method, f"Codex {method} response is missing thread")
        try:
            snapshot = cls._snapshot_from_thread(thread)
        except Exception as exc:
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response contains an invalid thread",
            ) from exc
        actual_thread_id = str(snapshot.summary.thread_id or "").strip()
        if not actual_thread_id:
            raise CodexRpcProtocolError(method, f"Codex {method} response thread is missing id")
        normalized_expected = str(expected_thread_id or "").strip()
        if normalized_expected and actual_thread_id != normalized_expected:
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response thread id does not match the request",
            )
        normalized_expected_history_mode = str(expected_history_mode or "").strip()
        if (
            normalized_expected_history_mode
            and snapshot.history_mode != normalized_expected_history_mode
        ):
            raise CodexRpcProtocolError(
                method,
                f"Codex {method} response did not apply the requested historyMode",
            )

        def require_string(field: str) -> str:
            value = payload.get(field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response has invalid {field}",
                )
            return value

        if method in {"thread/start", "thread/resume"}:
            snapshot.effective_model = require_string("model")
            if "approvalPolicy" in payload:
                snapshot.effective_approval_policy = require_string("approvalPolicy")
            if "reasoningEffort" not in payload:
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response is missing reasoningEffort",
                )
            reasoning_effort = payload["reasoningEffort"]
            if reasoning_effort is not None and (
                not isinstance(reasoning_effort, str)
                or not reasoning_effort
                or reasoning_effort != reasoning_effort.strip()
            ):
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response has invalid reasoningEffort",
                )
            snapshot.effective_reasoning_effort = reasoning_effort
            active_profile = payload.get("activePermissionProfile")
            if active_profile is None:
                snapshot.effective_permissions_profile_id = None
            elif isinstance(active_profile, dict):
                profile_id = active_profile.get("id")
                if (
                    not isinstance(profile_id, str)
                    or not profile_id
                    or profile_id != profile_id.strip()
                ):
                    raise CodexRpcProtocolError(
                        method,
                        f"Codex {method} response has invalid activePermissionProfile.id",
                    )
                snapshot.effective_permissions_profile_id = profile_id
            else:
                raise CodexRpcProtocolError(
                    method,
                    f"Codex {method} response has invalid activePermissionProfile",
                )
        return payload, snapshot

    @classmethod
    def _require_turn_result(cls, method: str, result: Any) -> dict[str, Any]:
        payload = cls._require_object_result(method, result)
        turn = payload.get("turn")
        if not isinstance(turn, dict) or not str(turn.get("id", "") or "").strip():
            raise CodexRpcProtocolError(method, f"Codex {method} response is missing turn.id")
        return payload

    def start_turn(
        self,
        *,
        thread_id: str,
        input_items: list[TurnInputItem],
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        result = self._request_turn_start(
            "turn/start",
            thread_id=thread_id,
            input_items=input_items,
            cwd=cwd,
            model=model,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            sandbox=sandbox,
            reasoning_effort=reasoning_effort,
            client_user_message_id=client_user_message_id,
            expected_connection_generation=expected_connection_generation,
        )
        return self._require_turn_result("turn/start", result)

    def _request_turn_start(
        self,
        method: str,
        *,
        thread_id: str,
        input_items: list[TurnInputItem],
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "clientUserMessageId": client_user_message_id,
            "input": [dict(item) for item in input_items],
            "cwd": cwd,
            "model": model or None,
            "approvalPolicy": approval_policy or self._config.approval_policy or None,
            "approvalsReviewer": self._config.approvals_reviewer or None,
            "effort": reasoning_effort or None,
            "personality": self._config.personality or None,
            "serviceTier": self._config.service_tier or None,
        }
        params["permissions"] = normalize_permissions_profile_id(
            permissions_profile_id or sandbox or self._config.permissions_profile_id,
            fallback=self._config.permissions_profile_id,
        )
        return self._request_with_permissions_fallback(
            method,
            _compact(params),
            legacy_field="sandboxPolicy",
            legacy_value=self._legacy_sandbox_policy(permissions_profile_id or sandbox),
            require_existing_connection=(expected_connection_generation is not None),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )

    def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[TurnInputItem],
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        result = self._rpc_request(
            "turn/steer",
            {
                "threadId": thread_id,
                "clientUserMessageId": client_user_message_id,
                "expectedTurnId": expected_turn_id,
                "input": [dict(item) for item in input_items],
            },
            require_existing_connection=(expected_connection_generation is not None),
            expected_connection_generation=expected_connection_generation,
            admit_existing_connection=(expected_connection_generation is not None),
        )
        payload = self._require_object_result("turn/steer", result)
        if not _read_string(payload, "turnId", "turn_id"):
            raise CodexRpcProtocolError(
                "turn/steer",
                "Codex turn/steer response is missing turnId",
            )
        return payload

    def interrupt_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
    ) -> None:
        """Interrupt the exact active turn selected by the frontend owner."""

        result = self._rpc_request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )
        self._require_object_result("turn/interrupt", result)

    def respond(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        result: dict | None = None,
        error: dict | None = None,
        timeout: float | None = None,
    ) -> None:
        """Respond in the ordinary admitted epoch without reconnecting."""

        permit = self._issue_outbound_request("serverRequest/response")
        outbound_transport_guard = self._outbound_transport_guard(permit)
        respond_kwargs: dict[str, Any] = {
            "result": result,
            "error": error,
            "timeout": (
                self._config.request_timeout_seconds
                if timeout is None
                else timeout
            ),
            "require_existing_connection": True,
            "expected_connection_generation": connection_generation,
        }
        if outbound_transport_guard is not None:
            respond_kwargs["outbound_transport_guard"] = outbound_transport_guard
        self._rpc.respond(
            request_id,
            **respond_kwargs,
        )
        self._confirm_outbound_request("serverRequest/response", permit)

    def respond_with_existing_backend_authority(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        result: dict | None = None,
        error: dict | None = None,
        timeout: float | None = None,
    ) -> None:
        """Fail-close on the request's original socket during stop or reset."""

        self._rpc.respond(
            request_id,
            result=result,
            error=error,
            timeout=self._config.request_timeout_seconds if timeout is None else timeout,
            require_existing_connection=True,
            expected_connection_generation=connection_generation,
        )

    def rotate_server_request_authority_after_backend_stop(self) -> object:
        return self._rpc.rotate_server_request_authority_after_backend_stop()

    def list_threads_all(
        self,
        *,
        cwd: str | None = None,
        limit: int = 100,
        search_term: str | None = None,
        sort_key: str = "updated_at",
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
        archived: bool | None = None,
        parent_thread_id: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> list[ThreadSummary]:
        effective_timeout = (
            self._config.request_timeout_seconds
            if timeout is None
            else float(timeout)
        )
        if effective_timeout <= 0:
            raise ValueError("Codex thread/list timeout must be positive")
        deadline = time.monotonic() + effective_timeout

        def remaining_timeout() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex thread/list scan exceeded its caller deadline")
            return remaining

        items: list[ThreadSummary] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_thread_ids: set[str] = set()
        relation_inventory = bool(parent_thread_id)
        while len(items) < limit:
            page_size = min(50, limit - len(items))
            page, cursor = self.list_threads(
                cwd=cwd,
                limit=page_size,
                cursor=cursor,
                search_term=search_term,
                sort_key=sort_key,
                source_kinds=source_kinds,
                model_providers=model_providers,
                archived=archived,
                parent_thread_id=parent_thread_id,
                timeout=remaining_timeout(),
                require_existing_connection=(
                    require_existing_connection
                    or expected_connection_generation is not None
                ),
                expected_connection_generation=expected_connection_generation,
            )
            if relation_inventory:
                duplicate_thread_ids = seen_thread_ids.intersection(
                    summary.thread_id for summary in page
                )
                if duplicate_thread_ids:
                    raise CodexRpcProtocolError(
                        "thread/list",
                        "Codex thread/list returned duplicate thread ids across pages",
                    )
                seen_thread_ids.update(summary.thread_id for summary in page)
            items.extend(page)
            if cursor is None:
                break
            if not page:
                raise CodexRpcProtocolError(
                    "thread/list",
                    "Codex thread/list returned an empty non-terminal page",
                )
            if cursor in seen_cursors:
                raise CodexRpcProtocolError(
                    "thread/list",
                    "Codex thread/list returned a repeated pagination cursor",
                )
            seen_cursors.add(cursor)
        return items

    def _thread_params(
        self,
        *,
        cwd: str,
        include_service_name: bool,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        sandbox: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "approvalPolicy": approval_policy or self._config.approval_policy or None,
            # Focus has no reviewed auto-review route. Keep this lifecycle
            # boundary explicit even when a caller constructed the adapter
            # config directly instead of using the validated YAML parser.
            "approvalsReviewer": "user",
            "personality": self._config.personality or None,
            "model": model or None,
            "modelProvider": model_provider or None,
            "serviceTier": self._config.service_tier or None,
        }
        params["permissions"] = normalize_permissions_profile_id(
            permissions_profile_id or sandbox or self._config.permissions_profile_id,
            fallback=self._config.permissions_profile_id,
        )
        merged_config = self._merge_request_config(config_overrides=config_overrides)
        if merged_config:
            params["config"] = merged_config
        if include_service_name:
            params["serviceName"] = self._config.service_name or None
        return _compact(params)

    def _request_with_permissions_fallback(
        self,
        method: str,
        params: dict[str, Any],
        *,
        legacy_field: str,
        legacy_value: Any,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
        admit_existing_connection: bool = False,
    ) -> dict[str, Any]:
        deadline = _deadline_from_timeout(self._config.request_timeout_seconds)
        try:
            return self._rpc_request(
                method,
                params,
                timeout=_remaining_before_deadline(
                    deadline,
                    operation=f"{method} permissions request",
                ),
                require_existing_connection=require_existing_connection,
                expected_connection_generation=expected_connection_generation,
                admit_existing_connection=admit_existing_connection,
            )
        except CodexRpcError as exc:
            if not self._should_retry_without_permissions(
                exc.error,
                params,
                legacy_value=legacy_value,
            ):
                raise
            retry_params = dict(params)
            retry_params.pop("permissions", None)
            if legacy_value is not None:
                retry_params[legacy_field] = legacy_value
            logger.info("rpc %s 不支持 permissions 字段，回退到 legacy %s", method, legacy_field)
            return self._rpc_request(
                method,
                retry_params,
                timeout=_remaining_before_deadline(
                    deadline,
                    operation=f"{method} permissions fallback",
                ),
                require_existing_connection=require_existing_connection,
                expected_connection_generation=expected_connection_generation,
                admit_existing_connection=admit_existing_connection,
            )

    @staticmethod
    def _should_retry_without_permissions(
        error: dict[str, Any],
        params: dict[str, Any],
        *,
        legacy_value: Any,
    ) -> bool:
        # Never erase an explicit profile unless Focus can express the exact
        # same permissions through the legacy field.  In particular, current
        # app-server configuration errors mention ``[permissions]`` too; they
        # are not evidence that the request field is unsupported.
        if "permissions" not in params or legacy_value is None:
            return False
        code = error.get("code")
        if code not in {-32600, -32602}:
            return False
        message = str(error.get("message") or "").lower()
        message = message.replace("`", "").replace("'", "").replace('"', "")
        return bool(
            re.search(
                r"\b(?:unknown|unexpected|unrecognized|unsupported)\s+field\s*[:=]?\s*permissions\b",
                message,
            )
        )

    @staticmethod
    def _legacy_sandbox(value: str | None) -> str | None:
        normalized = normalize_permissions_profile_id(value or "")
        return PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX.get(normalized)

    @classmethod
    def _legacy_sandbox_policy(cls, value: str | None) -> dict[str, Any] | None:
        legacy = cls._legacy_sandbox(value)
        if legacy == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if legacy == "read-only":
            return {
                "type": "readOnly",
                "access": {"type": "fullAccess"},
                "networkAccess": False,
            }
        if legacy == "workspace-write":
            return {
                "type": "workspaceWrite",
                "writableRoots": [],
                "readOnlyAccess": {"type": "fullAccess"},
                "networkAccess": False,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            }
        return None

    @staticmethod
    def _merge_request_config(
        *,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return deep_merge_config_overrides(config_overrides)

    @staticmethod
    def _snapshot_from_thread(thread: dict[str, Any]) -> ThreadSnapshot:
        return ThreadSnapshot(
            summary=CodexAppServerAdapter._summary_from_thread(thread),
            turns=thread.get("turns") or [],
        )

    @staticmethod
    def _turns_page_from_result(
        result: dict[str, Any],
        *,
        sort_direction: str,
    ) -> ThreadTurnsPage:
        raw_turns = result.get("data")
        if not isinstance(raw_turns, list):
            raise CodexRpcProtocolError(
                "thread/turns/list",
                "Codex thread turns page is missing data",
            )
        turns = [dict(turn) for turn in raw_turns if isinstance(turn, dict)]
        if sort_direction == "desc":
            turns.reverse()
        return ThreadTurnsPage(
            turns=turns,
            next_cursor=_read_string(result, "nextCursor"),
            backwards_cursor=_read_string(result, "backwardsCursor"),
        )

    @staticmethod
    def _runtime_config_from_result(result: dict[str, Any]) -> RuntimeConfigSummary:
        config = result.get("config") or {}
        memories_raw = config.get("memories") or {}
        return RuntimeConfigSummary(
            current_model_provider=_read_string(config, "modelProvider", "model_provider"),
            current_memory_mode=thread_memory_mode_from_memories_config(
                memories_raw if isinstance(memories_raw, dict) else None
            ),
        )

    @staticmethod
    def _model_summaries_from_result(result: dict[str, Any]) -> list[RuntimeModelSummary]:
        data = result.get("data")
        if not isinstance(data, list):
            raise CodexRpcProtocolError(
                "model/list",
                "Codex model/list response is missing data",
            )

        def optional_string(
            payload: dict[str, Any],
            model_name: str,
            *keys: str,
            allow_none: bool = False,
        ) -> str | None:
            for key in keys:
                if key not in payload:
                    continue
                value = payload[key]
                if value is None:
                    if allow_none:
                        return None
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model_name!r} has invalid {key}",
                    )
                if not isinstance(value, str):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model_name!r} has invalid {key}",
                    )
                return value.strip() or None
            return None

        def optional_bool(
            payload: dict[str, Any],
            model_name: str,
            key: str,
            *,
            default: bool = False,
        ) -> bool:
            if key not in payload:
                return default
            value = payload[key]
            if not isinstance(value, bool):
                raise CodexRpcProtocolError(
                    "model/list",
                    f"Codex model/list model {model_name!r} has invalid {key}",
                )
            return value

        models: list[RuntimeModelSummary] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise CodexRpcProtocolError(
                    "model/list",
                    f"Codex model/list item {index} is not an object",
                )
            raw_model = item.get("model")
            if not isinstance(raw_model, str) or not raw_model.strip():
                raise CodexRpcProtocolError(
                    "model/list",
                    f"Codex model/list item {index} has no valid model selector",
                )
            model = raw_model.strip()
            raw_input_modalities = item.get("inputModalities")
            input_modalities: list[str] | None = None
            if raw_input_modalities is not None:
                if not isinstance(raw_input_modalities, list):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has invalid inputModalities",
                    )
                input_modalities = []
                for raw_modality in raw_input_modalities:
                    if not isinstance(raw_modality, str) or not raw_modality.strip():
                        raise CodexRpcProtocolError(
                            "model/list",
                            f"Codex model/list model {model!r} has an invalid input modality",
                        )
                    modality = raw_modality.strip().lower()
                    if modality not in input_modalities:
                        input_modalities.append(modality)

            raw_service_tiers = item.get("serviceTiers")
            service_tiers: list[RuntimeModelServiceTier] | None = None
            if "serviceTiers" in item:
                if not isinstance(raw_service_tiers, list):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has invalid serviceTiers",
                    )
                service_tiers = []
                for raw_tier in raw_service_tiers:
                    if not isinstance(raw_tier, dict):
                        raise CodexRpcProtocolError(
                            "model/list",
                            f"Codex model/list model {model!r} has an invalid service tier",
                        )
                    tier_id = optional_string(raw_tier, model, "id")
                    if not tier_id:
                        raise CodexRpcProtocolError(
                            "model/list",
                            f"Codex model/list model {model!r} has a service tier without id",
                        )
                    service_tiers.append(
                        RuntimeModelServiceTier(
                            id=tier_id,
                            name=optional_string(raw_tier, model, "name") or "",
                            description=(
                                optional_string(raw_tier, model, "description") or ""
                            ),
                        )
                    )

            raw_upgrade_info = item.get("upgradeInfo")
            upgrade_info: RuntimeModelUpgradeInfo | None = None
            if raw_upgrade_info is not None:
                if not isinstance(raw_upgrade_info, dict):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has invalid upgradeInfo",
                    )
                upgrade_model = optional_string(raw_upgrade_info, model, "model")
                if not upgrade_model:
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has upgradeInfo without model",
                    )
                upgrade_info = RuntimeModelUpgradeInfo(
                    model=upgrade_model,
                    upgrade_copy=(
                        optional_string(
                            raw_upgrade_info,
                            model,
                            "upgradeCopy",
                            allow_none=True,
                        )
                        or None
                    ),
                    model_link=(
                        optional_string(
                            raw_upgrade_info,
                            model,
                            "modelLink",
                            allow_none=True,
                        )
                        or None
                    ),
                    migration_markdown=(
                        optional_string(
                            raw_upgrade_info,
                            model,
                            "migrationMarkdown",
                            allow_none=True,
                        )
                        or None
                    ),
                )

            supports_personality: bool | None = None
            if "supportsPersonality" in item:
                raw_supports_personality = item.get("supportsPersonality")
                if not isinstance(raw_supports_personality, bool):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has invalid supportsPersonality",
                    )
                supports_personality = raw_supports_personality
            raw_reasoning_efforts = item.get("supportedReasoningEfforts")
            supported_reasoning_efforts: list[RuntimeReasoningEffortOption] | None = None
            if "supportedReasoningEfforts" in item:
                if not isinstance(raw_reasoning_efforts, list):
                    raise CodexRpcProtocolError(
                        "model/list",
                        f"Codex model/list model {model!r} has invalid supportedReasoningEfforts",
                    )
                supported_reasoning_efforts = []
                for raw_option in raw_reasoning_efforts:
                    if not isinstance(raw_option, dict):
                        raise CodexRpcProtocolError(
                            "model/list",
                            f"Codex model/list model {model!r} has an invalid reasoning effort",
                        )
                    reasoning_effort = optional_string(
                        raw_option,
                        model,
                        "reasoningEffort",
                        "reasoning_effort",
                    )
                    if not reasoning_effort:
                        raise CodexRpcProtocolError(
                            "model/list",
                            f"Codex model/list model {model!r} has a reasoning effort without a value",
                        )
                    supported_reasoning_efforts.append(
                        RuntimeReasoningEffortOption(
                            reasoning_effort=reasoning_effort,
                            description=(
                                optional_string(raw_option, model, "description") or ""
                            ),
                        )
                    )
            models.append(
                RuntimeModelSummary(
                    model=model,
                    catalog_id=optional_string(item, model, "id") or None,
                    display_name=(
                        optional_string(item, model, "displayName", "display_name")
                        or None
                    ),
                    description=optional_string(item, model, "description") or "",
                    is_default=optional_bool(item, model, "isDefault"),
                    hidden=optional_bool(item, model, "hidden"),
                    default_reasoning_effort=(
                        optional_string(
                            item,
                            model,
                            "defaultReasoningEffort",
                            "default_reasoning_effort",
                        )
                        or None
                    ),
                    supported_reasoning_efforts=supported_reasoning_efforts,
                    input_modalities=input_modalities,
                    supports_personality=supports_personality,
                    service_tiers=service_tiers,
                    default_service_tier=(
                        optional_string(
                            item,
                            model,
                            "defaultServiceTier",
                            allow_none=True,
                        )
                        or None
                    ),
                    upgrade=(
                        optional_string(item, model, "upgrade", allow_none=True)
                        or None
                    ),
                    upgrade_info=upgrade_info,
                )
            )
        return models

    @staticmethod
    def _summary_from_thread(thread: dict[str, Any]) -> ThreadSummary:
        return thread_summary_from_app_server_thread(thread)

def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}
