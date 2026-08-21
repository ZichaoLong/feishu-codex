"""Codex app-server adapter test doubles shared by owner suites."""

from __future__ import annotations

from bot.codex_protocol.connection import CodexRpcError


class _FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.default_model = "gpt-5.3-codex"
        self.thread_model = ""
        self.thread_reasoning_effort = ""
        self.thread_history_mode = "legacy"
        self.stopped = False
        self.generation = 1

    def start(self) -> None:
        return None

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        del timeout
        del require_existing_connection
        return self.generation

    def request(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        payload = params or {}
        self.calls.append((method, payload))
        if method == "model/list":
            return {
                "data": [
                    {
                        "model": self.default_model,
                        "isDefault": True,
                        "hidden": False,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Fast"},
                            {"reasoningEffort": "medium", "description": "Balanced"},
                            {"reasoningEffort": "high", "description": "Deep"},
                        ],
                    },
                    {
                        "model": "gpt-5.4",
                        "isDefault": False,
                        "hidden": False,
                    },
                ],
                "nextCursor": None,
            }
        if method == "config/read":
            return {
                "config": {
                    "modelProvider": "provider1_api",
                    "memories": {
                        "use_memories": True,
                        "generate_memories": False,
                    },
                },
                "layers": [
                    {
                        "name": {
                            "type": "user",
                            "file": "/tmp/.codex/work.config.toml",
                            "profile": "provider1",
                        },
                        "version": "v1",
                        "config": {},
                    }
                ],
            }
        if method in {"thread/start", "thread/resume"}:
            if method == "thread/start":
                self.thread_history_mode = payload.get("historyMode", "legacy")
            response = {
                "approvalsReviewer": "user",
                "model": self.thread_model or self.default_model,
                "reasoningEffort": self.thread_reasoning_effort or None,
                "approvalPolicy": payload.get("approvalPolicy") or "on-request",
                "activePermissionProfile": None,
                "thread": {
                    "id": "thread-1",
                    "historyMode": self.thread_history_mode,
                    "cwd": "/tmp/project",
                    "name": "demo",
                    "preview": "hello",
                    "createdAt": 0,
                    "updatedAt": 0,
                    "source": "cli",
                    "status": {"type": "idle", "activeFlags": []},
                },
            }
            permissions = payload.get("permissions")
            if isinstance(permissions, str) and permissions:
                response["activePermissionProfile"] = {"id": permissions}
            elif isinstance(payload.get("sandbox"), str):
                response["activePermissionProfile"] = {
                    "id": {
                        "read-only": ":read-only",
                        "workspace-write": ":workspace",
                        "danger-full-access": ":danger-full-access",
                    }[payload["sandbox"]]
                }
            if payload.get("initialTurnsPage") is not None:
                response["initialTurnsPage"] = {
                    "data": [
                        {"id": "turn-new", "status": "completed", "items": []},
                        {"id": "turn-old", "status": "completed", "items": []},
                    ],
                    "nextCursor": "older-cursor",
                    "backwardsCursor": "newer-cursor",
                }
                response["turnsBackwardsCursor"] = "turn-head"
            return response
        if method == "thread/turns/list":
            return {
                "data": [
                    {"id": "turn-new", "status": "completed", "items": []},
                    {"id": "turn-old", "status": "completed", "items": []},
                ],
                "nextCursor": "older-cursor",
                "backwardsCursor": "newer-cursor",
            }
        if method == "thread/list":
            return {
                "data": [],
                "nextCursor": None,
                "backwardsCursor": None,
            }
        if method == "thread/loaded/list":
            return {"data": [], "nextCursor": None}
        if method == "turn/steer":
            return {"turnId": payload.get("expectedTurnId", "")}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        if method == "review/start":
            return {
                "turn": {"id": "review-turn-1", "status": "inProgress", "items": []},
                "reviewThreadId": payload.get("threadId", ""),
            }
        if method == "thread/unarchive":
            return {
                "thread": {
                    "id": "thread-1",
                    "historyMode": self.thread_history_mode,
                    "cwd": "/tmp/project",
                    "name": "demo",
                    "preview": "hello",
                    "createdAt": 0,
                    "updatedAt": 0,
                    "source": "cli",
                    "status": {"type": "notLoaded", "activeFlags": []},
                }
            }
        if method in {"thread/goal/get", "thread/goal/set"}:
            return {
                "goal": {
                    "threadId": "thread-1",
                    "objective": "ship goal support",
                    "status": "active",
                    "tokenBudget": 123,
                    "tokensUsed": 45,
                    "timeUsedSeconds": 67,
                    "createdAt": 1712476800,
                    "updatedAt": 1712476801,
                }
            }
        if method == "thread/goal/clear":
            return {"cleared": True}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        if method in {
            "thread/settings/update",
            "thread/compact/start",
            "thread/name/set",
            "thread/archive",
            "thread/delete",
            "turn/interrupt",
        }:
            return {"ok": True}
        raise AssertionError(f"unexpected Codex RPC method: {method}")

    def stop(self) -> None:
        self.stopped = True


class _PermissionsUnsupportedRpc(_FakeRpc):
    def request(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        payload = params or {}
        if (
            method
            in {
                "thread/start",
                "thread/resume",
                "thread/settings/update",
                "turn/start",
            }
            and "permissions" in payload
        ):
            self.calls.append((method, payload))
            raise CodexRpcError(
                method, {"code": -32602, "message": "unknown field permissions"}
            )
        return super().request(method, params, timeout=timeout)
