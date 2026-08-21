"""Typed Web projection for one current-instance backend reset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bot.backend_reset.contract import (
    BACKEND_RESET_STATUS_AVAILABLE,
    BACKEND_RESET_STATUS_FORCE_ONLY,
    BackendResetPreview,
    BackendResetUnavailableError,
    decode_backend_reset_result,
    require_backend_reset_connection_generation,
)


_WEB_EXECUTABLE_STATUSES = frozenset(
    {BACKEND_RESET_STATUS_AVAILABLE, BACKEND_RESET_STATUS_FORCE_ONLY}
)


@dataclass(frozen=True, slots=True)
class WebBackendResetControllerPorts:
    """Existing policy, epoch, and service authorities used by the Web DTO."""

    backend_reset_preview: Callable[[], BackendResetPreview]
    preview_connection_generation: Callable[[], int]
    reset_current_instance: Callable[..., object]


class WebBackendResetController:
    """Project reset facts without owning policy, connection, or reset state."""

    def __init__(
        self,
        *,
        instance_name: str,
        ports: WebBackendResetControllerPorts,
    ) -> None:
        normalized_instance = str(instance_name or "").strip().lower()
        if not normalized_instance:
            raise ValueError("WebBackendResetController requires an instance name")
        self._instance_name = normalized_instance
        self._ports = ports

    def preview(self) -> dict[str, object]:
        """Return a non-reserving, browser-safe reset impact snapshot."""

        preview = self._ports.backend_reset_preview()
        if not isinstance(preview, BackendResetPreview):
            raise TypeError("backend reset preview returned an invalid contract")
        status = preview.status
        expected_generation = 0
        reason_code = preview.reason_code
        reason_text = preview.reason_text
        if status in _WEB_EXECUTABLE_STATUSES:
            try:
                expected_generation = require_backend_reset_connection_generation(
                    self._ports.preview_connection_generation()
                )
            except BackendResetUnavailableError:
                status = "unavailable"
                reason_code = "backend_generation_unavailable"
                reason_text = (
                    "The backend connection is changing or requires local recovery."
                )
        else:
            status = "unavailable"
        return {
            "instance": self._instance_name,
            "status": status,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "expected_connection_generation": expected_generation,
            "pending_request_count": preview.pending_request_count,
            "running_binding_count": len(preview.running_binding_ids),
            "attached_binding_count": len(preview.attached_binding_ids),
            "active_loaded_thread_count": len(preview.active_loaded_thread_ids),
            "loaded_thread_count": len(preview.loaded_thread_ids),
            "runtime_verification_failed": preview.runtime_verification_failed,
        }

    def execute(
        self,
        *,
        force: bool,
        expected_connection_generation: int,
    ) -> dict[str, object]:
        """Run once and admit success only through the complete BR1 result."""

        if type(force) is not bool:
            raise TypeError("Web backend reset force must be an exact bool")
        expected = require_backend_reset_connection_generation(
            expected_connection_generation
        )
        raw = self._ports.reset_current_instance(
            force=force,
            expected_connection_generation=expected,
        )
        result = decode_backend_reset_result(raw, expected_force=force)
        return {
            "force": result.force,
            "detached_binding_count": len(result.detached_binding_ids),
            "interrupted_binding_count": len(result.interrupted_binding_ids),
            "retired_request_count": result.retired_request_count,
            "purged_thread_count": len(result.purged_thread_ids),
            "projection_warnings": list(result.projection_warnings),
        }
