"""
基于 Codex app-server 的适配层。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.adapters.base import (
    AgentAdapter,
    RuntimeConfigSummary,
    RuntimeProfileSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import CodexRpcClient
from bot.constants import DEFAULT_APP_SERVER_MODE, DEFAULT_APP_SERVER_URL, DEFAULT_SOURCE_KINDS

logger = logging.getLogger(__name__)

_SUPPORTED_SANDBOX_MODES = {
    "read-only",
    "workspace-write",
    "danger-full-access",
}


@dataclass(slots=True)
class CodexAppServerConfig:
    codex_command: str = "codex"
    app_server_mode: str = DEFAULT_APP_SERVER_MODE
    app_server_url: str = DEFAULT_APP_SERVER_URL
    connect_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 30.0
    service_name: str = "feishu-codex"
    sandbox: str = "workspace-write"
    approval_policy: str = "on-request"
    approvals_reviewer: str = "user"
    personality: str = "pragmatic"
    model: str = ""
    model_provider: str = ""
    service_tier: str = ""
    reasoning_effort: str = ""
    collaboration_mode: str = "default"
    source_kinds: list[str] = field(default_factory=lambda: DEFAULT_SOURCE_KINDS.copy())

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "CodexAppServerConfig":
        source_kinds = config.get("source_kinds") or DEFAULT_SOURCE_KINDS
        collaboration_mode = str(config.get("collaboration_mode", "default")).strip().lower() or "default"
        app_server_mode = str(config.get("app_server_mode", DEFAULT_APP_SERVER_MODE)).strip().lower() or DEFAULT_APP_SERVER_MODE
        if collaboration_mode not in {"default", "plan"}:
            raise ValueError("collaboration_mode 仅支持 default 或 plan")
        if app_server_mode not in {"managed", "remote"}:
            raise ValueError("app_server_mode 仅支持 managed 或 remote")
        return cls(
            codex_command=str(config.get("codex_command", "codex")),
            app_server_mode=app_server_mode,
            app_server_url=str(config.get("app_server_url", DEFAULT_APP_SERVER_URL)).strip() or DEFAULT_APP_SERVER_URL,
            connect_timeout_seconds=float(config.get("connect_timeout_seconds", 15)),
            request_timeout_seconds=float(config.get("request_timeout_seconds", 30)),
            service_name=str(config.get("service_name", "feishu-codex")),
            sandbox=str(config.get("sandbox", "workspace-write")),
            approval_policy=str(config.get("approval_policy", "on-request")),
            approvals_reviewer=str(config.get("approvals_reviewer", "user")),
            personality=str(config.get("personality", "pragmatic")),
            model=str(config.get("model", "")),
            model_provider=str(config.get("model_provider", "")),
            service_tier=str(config.get("service_tier", "")),
            reasoning_effort=str(config.get("reasoning_effort", "")),
            collaboration_mode=collaboration_mode,
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
    ) -> None:
        self._config = config
        self._collaboration_mode_model: str | None = None
        self._rpc = CodexRpcClient(
            codex_command=config.codex_command,
            app_server_mode=config.app_server_mode,
            app_server_url=config.app_server_url,
            connect_timeout_seconds=config.connect_timeout_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            on_notification=on_notification,
            on_request=on_request,
        )

    def start(self) -> None:
        self._rpc.start()

    def stop(self) -> None:
        self._rpc.stop()

    def create_thread(
        self,
        *,
        cwd: str,
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> ThreadSnapshot:
        params = self._thread_params(
            cwd=cwd,
            include_service_name=True,
            profile=profile,
            approval_policy=approval_policy,
            sandbox=sandbox,
        )
        result = self._rpc.request("thread/start", params)
        return self._snapshot_from_thread(result["thread"])

    def resume_thread(self, thread_id: str, profile: str | None = None) -> ThreadSnapshot:
        params: dict[str, Any] = {"threadId": thread_id}
        if profile:
            params["config"] = {"profile": profile}
        result = self._rpc.request("thread/resume", params)
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
    ) -> tuple[list[ThreadSummary], str | None]:
        params = _compact(
            {
                "cwd": cwd,
                "limit": limit,
                "cursor": cursor,
                "searchTerm": search_term,
                "sortKey": sort_key,
                "sourceKinds": source_kinds or self._config.source_kinds,
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

    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        result = self._rpc.request("config/read", _compact({"includeLayers": False, "cwd": cwd}))
        return self._runtime_config_from_result(result)

    def set_active_profile(self, profile: str) -> RuntimeConfigSummary:
        self._rpc.request(
            "config/batchWrite",
            {
                "edits": [
                    {
                        "keyPath": "profile",
                        "value": profile,
                        "mergeStrategy": "replace",
                    }
                ],
                "reloadUserConfig": True,
            },
        )
        return self.read_runtime_config()

    def rename_thread(self, thread_id: str, name: str) -> None:
        self._rpc.request("thread/name/set", {"threadId": thread_id, "name": name})

    def archive_thread(self, thread_id: str) -> None:
        self._rpc.request("thread/archive", {"threadId": thread_id})

    def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
    ) -> dict[str, Any]:
        effective_model = model or self._config.model or None
        effective_reasoning = reasoning_effort or self._config.reasoning_effort or None
        effective_collaboration_mode = collaboration_mode or self._config.collaboration_mode or "default"
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
            "cwd": cwd,
            "model": effective_model,
            "approvalPolicy": approval_policy or self._config.approval_policy or None,
            "approvalsReviewer": self._config.approvals_reviewer or None,
            "sandboxPolicy": self._sandbox_policy_payload(sandbox or self._config.sandbox),
            "effort": effective_reasoning,
            "personality": self._config.personality or None,
            "serviceTier": self._config.service_tier or None,
        }
        if profile:
            params["config"] = {"profile": profile}
        params["collaborationMode"] = self._collaboration_mode_payload(
            effective_collaboration_mode,
            model=effective_model,
            reasoning_effort=effective_reasoning,
        )
        return self._rpc.request("turn/start", _compact(params))

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
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": cwd,
            "sandbox": self._normalize_sandbox_mode(sandbox or self._config.sandbox),
            "approvalPolicy": approval_policy or self._config.approval_policy or None,
            "approvalsReviewer": self._config.approvals_reviewer or None,
            "personality": self._config.personality or None,
            "model": self._config.model or None,
            "modelProvider": self._config.model_provider or None,
            "serviceTier": self._config.service_tier or None,
        }
        if profile:
            params["config"] = {"profile": profile}
        if include_service_name:
            params["serviceName"] = self._config.service_name or None
        return _compact(params)

    @staticmethod
    def _normalize_sandbox_mode(mode: str | None) -> str | None:
        if mode is None:
            return None
        value = str(mode).strip().lower()
        if not value:
            return None
        if value not in _SUPPORTED_SANDBOX_MODES:
            raise ValueError("sandbox 仅支持 read-only、workspace-write、danger-full-access")
        return value

    @classmethod
    def _sandbox_policy_payload(cls, mode: str | None) -> dict[str, Any] | None:
        normalized = cls._normalize_sandbox_mode(mode)
        if normalized is None:
            return None
        if normalized == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if normalized == "read-only":
            return {
                "type": "readOnly",
                "access": {"type": "fullAccess"},
                "networkAccess": False,
            }
        return {
            "type": "workspaceWrite",
            "writableRoots": [],
            "readOnlyAccess": {"type": "fullAccess"},
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }

    def _collaboration_mode_payload(
        self,
        mode: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        normalized = str(mode).strip().lower()
        if normalized not in {"default", "plan"}:
            raise ValueError("collaboration_mode 仅支持 default 或 plan")
        return {
            "mode": normalized,
            "settings": {
                "model": self._resolve_collaboration_mode_model(model),
                "reasoning_effort": reasoning_effort,
                "developer_instructions": None,
            },
        }

    def _resolve_collaboration_mode_model(self, configured_model: str | None) -> str:
        if configured_model:
            return configured_model
        if self._collaboration_mode_model:
            return self._collaboration_mode_model

        result = self._rpc.request("model/list", {})
        models = result.get("data") or []
        for item in models:
            if item.get("isDefault") and item.get("model"):
                self._collaboration_mode_model = str(item["model"])
                return self._collaboration_mode_model
        for item in models:
            if not item.get("hidden") and item.get("model"):
                self._collaboration_mode_model = str(item["model"])
                return self._collaboration_mode_model
        raise RuntimeError("无法解析 Codex 默认模型，无法构造 collaboration mode 参数")

    @staticmethod
    def _snapshot_from_thread(thread: dict[str, Any]) -> ThreadSnapshot:
        return ThreadSnapshot(
            summary=CodexAppServerAdapter._summary_from_thread(thread),
            turns=thread.get("turns") or [],
        )

    @staticmethod
    def _runtime_config_from_result(result: dict[str, Any]) -> RuntimeConfigSummary:
        config = result.get("config") or {}
        profiles_raw = config.get("profiles") or {}
        profiles: list[RuntimeProfileSummary] = []
        if isinstance(profiles_raw, dict):
            for name in sorted(profiles_raw):
                if not str(name).strip():
                    continue
                item = profiles_raw.get(name)
                item_dict = item if isinstance(item, dict) else {}
                profiles.append(
                    RuntimeProfileSummary(
                        name=str(name),
                        model_provider=_read_string(item_dict, "modelProvider", "model_provider"),
                    )
                )
        return RuntimeConfigSummary(
            current_profile=_read_string(config, "profile", "activeProfile", "active_profile"),
            current_model_provider=_read_string(config, "modelProvider", "model_provider"),
            profiles=profiles,
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
