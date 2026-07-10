"""
基于 Codex app-server 的适配层。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.approval_policy import normalize_approval_policy
from bot.adapters.base import (
    AgentAdapter,
    RuntimeConfigSummary,
    RuntimeModelSummary,
    RuntimeReasoningEffortOption,
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
    TurnInputItem,
)
from bot.codex_protocol.client import CodexRpcClient
from bot.constants import DEFAULT_APP_SERVER_MODE, DEFAULT_APP_SERVER_URL, DEFAULT_SOURCE_KINDS
from bot.permissions_profile import (
    BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
    PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX,
    normalize_permissions_profile_id,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.thread_memory_mode import (
    deep_merge_config_overrides,
    thread_memory_mode_from_memories_config,
)
from bot.codex_protocol.client import CodexRpcError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CodexAppServerConfig:
    codex_command: str = "codex"
    app_server_mode: str = DEFAULT_APP_SERVER_MODE
    app_server_url: str = DEFAULT_APP_SERVER_URL
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 30.0
    service_name: str = "focus"
    permissions_profile_id: str = BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS
    approval_policy: str = "never"
    approvals_reviewer: str = "user"
    personality: str = "pragmatic"
    model: str = ""
    service_tier: str = ""
    reasoning_effort: str = ""
    app_server_data_dir: str = ""
    source_kinds: list[str] = field(default_factory=lambda: DEFAULT_SOURCE_KINDS.copy())

    @property
    def sandbox(self) -> str:
        return PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX.get(self.permissions_profile_id, "danger-full-access")

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "CodexAppServerConfig":
        source_kinds = config.get("source_kinds") or DEFAULT_SOURCE_KINDS
        app_server_mode = str(config.get("app_server_mode", DEFAULT_APP_SERVER_MODE)).strip().lower() or DEFAULT_APP_SERVER_MODE
        if "managed_startup_profile" in config:
            raise ValueError(
                "`managed_startup_profile` 已移除；本项目不再提供 profile 启动基线功能。"
                "如需使用 profile，请直接使用上游 Codex 自己的配置或启动参数。"
            )
        if "default_thread_memory_mode" in config:
            raise ValueError("`default_thread_memory_mode` 已移除；请改用上游 Codex memories 配置。")
        if "new_thread_memory_mode_seed" in config:
            raise ValueError("`new_thread_memory_mode_seed` 已移除；请改用上游 Codex memories 配置。")
        if "collaboration_mode" in config:
            raise ValueError("`collaboration_mode` 已移除；如需使用 collaboration mode，请改用上游 Codex 配置。")
        if "model_provider" in config:
            raise ValueError("`model_provider` 已移除；请改用上游 Codex 配置或显式调用方 provider hint。")
        if app_server_mode not in {"managed", "remote"}:
            raise ValueError("app_server_mode 仅支持 managed 或 remote")
        return cls(
            codex_command=str(config.get("codex_command", "codex")),
            app_server_mode=app_server_mode,
            app_server_url=str(config.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL,
            connect_timeout_seconds=float(config.get("connect_timeout_seconds", 15)),
            request_timeout_seconds=float(config.get("request_timeout_seconds", 30)),
            service_name=str(config.get("service_name", "focus")),
            permissions_profile_id=normalize_permissions_profile_id(
                str(
                    config.get(
                        "permissions_profile_id",
                        config.get("permissions", config.get("sandbox", BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS)),
                    )
                ),
                fallback=BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
            ),
            approval_policy=normalize_approval_policy(
                str(config.get("approval_policy", "never")),
            ),
            approvals_reviewer=str(config.get("approvals_reviewer", "user")),
            personality=str(config.get("personality", "pragmatic")),
            model=str(config.get("model", "")),
            service_tier=str(config.get("service_tier", "")),
            reasoning_effort=str(config.get("reasoning_effort", "")),
            source_kinds=[str(item) for item in source_kinds],
        )


class CodexAppServerAdapter(AgentAdapter):
    """通过 app-server 与 Codex 交互。"""

    def __init__(
        self,
        config: CodexAppServerConfig,
        *,
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
        on_request: Callable[[int | str, str, dict[str, Any]], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        app_server_runtime_store: AppServerRuntimeStore | None = None,
    ) -> None:
        self._config = config
        self._rpc = CodexRpcClient(
            codex_command=config.codex_command,
            app_server_mode=config.app_server_mode,
            app_server_url=config.app_server_url,
            connect_timeout_seconds=config.connect_timeout_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            on_notification=on_notification,
            on_request=on_request,
            on_disconnect=on_disconnect,
            app_server_runtime_store=app_server_runtime_store,
            app_server_data_dir=config.app_server_data_dir or None,
        )

    def start(self) -> None:
        self._rpc.start()

    def stop(self) -> None:
        self._rpc.stop()

    def current_app_server_url(self) -> str:
        return self._rpc.current_app_server_url()

    def unsubscribe_thread(self, thread_id: str) -> None:
        """Unsubscribe from a thread so the app-server can unload it."""
        try:
            self._rpc.request("thread/unsubscribe", {"threadId": thread_id})
        except Exception:
            logger.debug("thread/unsubscribe failed for %s", thread_id[:12], exc_info=True)

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
        result = self._request_with_permissions_fallback(
            "thread/start",
            params,
            legacy_field="sandbox",
            legacy_value=self._legacy_sandbox(permissions_profile_id or sandbox),
        )
        return self._snapshot_from_thread(result["thread"])

    def resume_thread(
        self,
        thread_id: str,
        *,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> ThreadSnapshot:
        params: dict[str, Any] = {"threadId": thread_id}
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
        result = self._request_with_permissions_fallback(
            "thread/resume",
            params,
            legacy_field="sandbox",
            legacy_value=self._legacy_sandbox(permissions_profile_id),
        )
        return self._snapshot_from_thread(result["thread"])

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
    ) -> tuple[list[ThreadSummary], str | None]:
        params = _compact(
            {
                "cwd": cwd,
                "limit": limit,
                "cursor": cursor,
                "searchTerm": search_term,
                "sortKey": sort_key,
                "sourceKinds": source_kinds or self._config.source_kinds,
                "archived": archived,
            }
        )
        if model_providers is not None:
            # app-server 将显式空列表解释为“不按 provider 过滤”。
            params["modelProviders"] = model_providers
        result = self._rpc.request("thread/list", params)
        data = [self._summary_from_thread(item) for item in result.get("data", [])]
        return data, result.get("nextCursor")

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> ThreadSnapshot:
        result = self._rpc.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        return self._snapshot_from_thread(result["thread"])

    def get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None:
        result = self._rpc.request("thread/goal/get", {"threadId": thread_id})
        goal = result.get("goal")
        if not isinstance(goal, dict):
            return None
        return self._goal_from_result(goal)

    def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> ThreadGoalSummary:
        result = self._rpc.request(
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
        return self._goal_from_result(result["goal"])

    def clear_thread_goal(self, thread_id: str) -> bool:
        result = self._rpc.request("thread/goal/clear", {"threadId": thread_id})
        return bool(result.get("cleared"))

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        result = self._rpc.request("config/read", _compact({"includeLayers": True, "cwd": cwd}))
        return self._runtime_config_from_result(result)

    def list_models(self, *, include_hidden: bool = False) -> list[RuntimeModelSummary]:
        result = self._rpc.request(
            "model/list",
            _compact(
                {
                    "includeHidden": True if include_hidden else None,
                }
            ),
        )
        return self._model_summaries_from_result(result)

    def list_loaded_thread_ids(self) -> list[str]:
        result = self._rpc.request("thread/loaded/list", {})
        data = result.get("data") or []
        return [str(item).strip() for item in data if str(item).strip()]

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
        self._rpc.request(
            "thread/compact/start",
            {
                "threadId": thread_id,
            },
        )

    def rename_thread(self, thread_id: str, name: str) -> None:
        self._rpc.request("thread/name/set", {"threadId": thread_id, "name": name})

    def archive_thread(self, thread_id: str) -> None:
        self._rpc.request("thread/archive", {"threadId": thread_id})

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
    ) -> dict[str, Any]:
        effective_model = model or None
        effective_reasoning = reasoning_effort or None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [dict(item) for item in input_items],
            "cwd": cwd,
            "model": effective_model,
            "approvalPolicy": approval_policy or self._config.approval_policy or None,
            "approvalsReviewer": self._config.approvals_reviewer or None,
            "effort": effective_reasoning,
            "personality": self._config.personality or None,
            "serviceTier": self._config.service_tier or None,
        }
        params["permissions"] = normalize_permissions_profile_id(
            permissions_profile_id or sandbox or self._config.permissions_profile_id,
            fallback=self._config.permissions_profile_id,
        )
        result = self._request_with_permissions_fallback(
            "turn/start",
            _compact(params),
            legacy_field="sandboxPolicy",
            legacy_value=self._legacy_sandbox_policy(permissions_profile_id or sandbox),
        )
        return result

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def respond(self, request_id: int | str, *, result: dict | None = None, error: dict | None = None) -> None:
        self._rpc.respond(request_id, result=result, error=error)

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
    ) -> list[ThreadSummary]:
        items: list[ThreadSummary] = []
        cursor: str | None = None
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
            )
            items.extend(page)
            if not cursor:
                break
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
            "approvalsReviewer": self._config.approvals_reviewer or None,
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
    ) -> dict[str, Any]:
        try:
            return self._rpc.request(method, params)
        except CodexRpcError as exc:
            if not self._should_retry_without_permissions(exc.error, params):
                raise
            retry_params = dict(params)
            retry_params.pop("permissions", None)
            if legacy_value is not None:
                retry_params[legacy_field] = legacy_value
            logger.info("rpc %s 不支持 permissions 字段，回退到 legacy %s", method, legacy_field)
            return self._rpc.request(method, retry_params)

    @staticmethod
    def _should_retry_without_permissions(error: dict[str, Any], params: dict[str, Any]) -> bool:
        if "permissions" not in params:
            return False
        code = error.get("code")
        message = str(error.get("message") or "").lower()
        if code == -32602 and "permissions" in message:
            return True
        return code == -32600 and "permissions" in message

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
        data = result.get("data") or []
        models: list[RuntimeModelSummary] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model = _read_string(item, "model")
            if not model:
                continue
            raw_reasoning_efforts = item.get("supportedReasoningEfforts")
            supported_reasoning_efforts: list[RuntimeReasoningEffortOption] | None = None
            if isinstance(raw_reasoning_efforts, list):
                supported_reasoning_efforts = []
                for raw_option in raw_reasoning_efforts:
                    if not isinstance(raw_option, dict):
                        continue
                    reasoning_effort = _read_string(
                        raw_option,
                        "reasoningEffort",
                        "reasoning_effort",
                    )
                    if not reasoning_effort:
                        continue
                    supported_reasoning_efforts.append(
                        RuntimeReasoningEffortOption(
                            reasoning_effort=reasoning_effort,
                            description=_read_string(raw_option, "description"),
                        )
                    )
            models.append(
                RuntimeModelSummary(
                    model=model,
                    display_name=_read_string(item, "displayName", "display_name") or None,
                    is_default=bool(item.get("isDefault")),
                    hidden=bool(item.get("hidden")),
                    default_reasoning_effort=(
                        _read_string(
                            item,
                            "defaultReasoningEffort",
                            "default_reasoning_effort",
                        )
                        or None
                    ),
                    supported_reasoning_efforts=supported_reasoning_efforts,
                )
            )
        return models

    @staticmethod
    def _goal_from_result(goal: dict[str, Any]) -> ThreadGoalSummary:
        return ThreadGoalSummary(
            thread_id=str(goal.get("threadId", "") or "").strip(),
            objective=str(goal.get("objective", "") or "").strip(),
            status=str(goal.get("status", "") or "").strip(),
            token_budget=int(goal["tokenBudget"]) if goal.get("tokenBudget") is not None else None,
            tokens_used=int(goal.get("tokensUsed") or 0),
            time_used_seconds=int(goal.get("timeUsedSeconds") or 0),
            created_at=int(goal.get("createdAt") or 0),
            updated_at=int(goal.get("updatedAt") or 0),
        )

    @staticmethod
    def _summary_from_thread(thread: dict[str, Any]) -> ThreadSummary:
        status = thread.get("status") or {}
        return ThreadSummary(
            thread_id=thread.get("id", ""),
            cwd=thread.get("cwd", ""),
            name=thread.get("name") or "",
            preview=thread.get("preview") or "",
            created_at=int(thread.get("createdAt") or 0),
            updated_at=int(thread.get("updatedAt") or 0),
            source=thread.get("source") or "unknown",
            status=status.get("type", "unknown"),
            active_flags=list(status.get("activeFlags") or []),
            path=thread.get("path"),
            model_provider=thread.get("modelProvider"),
            service_name=thread.get("serviceName"),
        )


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def _read_string(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None
