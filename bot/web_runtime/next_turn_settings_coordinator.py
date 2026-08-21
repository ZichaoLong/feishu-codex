"""RuntimeLoop transaction owner for instance-wide Web next-turn settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot.adapters.base import RuntimeModelSummary
from bot.approval_policy import USER_SELECTABLE_APPROVAL_POLICIES
from bot.permissions_profile import (
    BUILTIN_PERMISSION_PROFILE_IDS,
    normalize_permissions_profile_id,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_next_turn_settings_store import (
    WebNextTurnSettings,
    WebNextTurnSettingsStore,
)
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.writer_workspace_coordinator import (
    require_connected_web_document,
)


_SETTING_FIELD_ORDER = (
    "model",
    "reasoning_effort",
    "approval_policy",
    "permissions_profile_id",
)
_SETTING_FIELDS = frozenset(_SETTING_FIELD_ORDER)


@dataclass(frozen=True, slots=True)
class WebNextTurnSettingsPorts:
    list_models: Callable[[], list[RuntimeModelSummary]]


class WebNextTurnSettingsCoordinator:
    """Validate, mutate, and project the one instance-wide Web settings fact."""

    def __init__(
        self,
        *,
        settings: WebNextTurnSettingsStore,
        documents: WebDocumentRegistry,
        projection: FocusWebProjection,
        ports: WebNextTurnSettingsPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(settings, WebNextTurnSettingsStore):
            raise TypeError("Web next-turn settings require their durable owner")
        if not isinstance(ports, WebNextTurnSettingsPorts):
            raise TypeError("Web next-turn settings require typed ports")
        if not callable(runtime_context_guard):
            raise TypeError("Web next-turn settings require a RuntimeLoop context guard")
        self._settings = settings
        self._documents = documents
        self._projection = projection
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def snapshot(self) -> WebNextTurnSettings:
        self._runtime_context_guard()
        return self._settings.load()

    def load_external_snapshot(self) -> WebNextTurnSettings:
        """Load one immutable durable snapshot outside RuntimeLoop."""

        return self._settings.load()

    def payload(
        self,
        settings: WebNextTurnSettings | None = None,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        current = settings or self._settings.load()
        return {
            "generation": current.generation,
            "model": current.model,
            "reasoning_effort": current.reasoning_effort,
            "approval_policy": current.approval_policy,
            "permissions_profile_id": current.permissions_profile_id,
        }

    def update(
        self,
        client_id: str,
        *,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized = self._normalize_changes(changes)
        touched_model_fields = {"model", "reasoning_effort"} & set(normalized)
        validate_merged: Callable[[WebNextTurnSettings], None] | None = None
        if touched_model_fields:
            models = self._ports.list_models()

            def validate_merged(candidate: WebNextTurnSettings) -> None:
                self._validate_model_effort(
                    model=candidate.model,
                    reasoning_effort=candidate.reasoning_effort,
                    requested_fields=touched_model_fields,
                    models=models,
                )

        updated = self._settings.update(
            normalized,
            validate_merged=validate_merged,
        )
        coordinates = dict(self._projection.coordinates())
        try:
            event = self._projection.publish(
                "settings_changed",
                reason="web_next_turn_settings_updated",
            )
            if isinstance(event, dict) and {
                "runtime_epoch",
                "revision",
            }.issubset(event):
                coordinates = {
                    "runtime_epoch": event["runtime_epoch"],
                    "revision": event["revision"],
                }
        except Exception:
            # The durable mutation is already committed.  Event projection is
            # disposable and cannot turn it into an HTTP failure.
            pass
        return {
            **coordinates,
            "next_turn_settings": self.payload(updated),
        }

    @staticmethod
    def _normalize_changes(changes: dict[str, Any]) -> dict[str, str]:
        if not isinstance(changes, dict) or not changes:
            raise WebRuntimeError(
                "At least one setting is required.",
                code="invalid_settings",
            )
        unknown = sorted(set(changes) - _SETTING_FIELDS)
        if unknown:
            raise WebRuntimeError(
                f"Unsupported setting fields: {', '.join(unknown)}.",
                code="invalid_settings",
            )
        normalized: dict[str, str] = {}
        for name in _SETTING_FIELD_ORDER:
            if name not in changes:
                continue
            raw_value = changes[name]
            if not isinstance(raw_value, str):
                raise WebRuntimeError(
                    f"Setting {name} must be a string.",
                    code="invalid_settings",
                )
            value = raw_value.strip()
            if name == "reasoning_effort":
                value = value.lower()
            if name in {"approval_policy", "permissions_profile_id"} and not value:
                raise WebRuntimeError(
                    "Approval and permissions settings must not be empty.",
                    code="invalid_settings",
                )
            normalized[name] = value
        approval = normalized.get("approval_policy")
        if approval is not None:
            approval = approval.lower()
            if approval not in USER_SELECTABLE_APPROVAL_POLICIES:
                raise WebRuntimeError(
                    "Invalid approval policy.",
                    code="invalid_approval_policy",
                )
            normalized["approval_policy"] = approval
        permissions = normalized.get("permissions_profile_id")
        if permissions is not None:
            try:
                permissions = normalize_permissions_profile_id(permissions)
            except ValueError as exc:
                raise WebRuntimeError(
                    "Invalid permissions profile.",
                    code="invalid_permissions_profile",
                ) from exc
            if permissions not in BUILTIN_PERMISSION_PROFILE_IDS:
                raise WebRuntimeError(
                    "Invalid permissions profile.",
                    code="invalid_permissions_profile",
                )
            normalized["permissions_profile_id"] = permissions
        return normalized

    def _validate_model_effort(
        self,
        *,
        model: str,
        reasoning_effort: str,
        requested_fields: set[str],
        models: list[RuntimeModelSummary],
    ) -> None:
        model_by_id = {item.model: item for item in models}
        if model and model not in model_by_id and "model" in requested_fields:
            raise WebRuntimeError("Unknown Codex model.", code="invalid_model")
        if not model or not reasoning_effort or model not in model_by_id:
            return
        selected = model_by_id[model]
        supported = selected.supported_reasoning_efforts
        if supported is not None and reasoning_effort not in {
            option.reasoning_effort for option in supported
        }:
            raise WebRuntimeError(
                f"Reasoning effort {reasoning_effort!r} is not supported by {model}.",
                code="invalid_effort",
            )
