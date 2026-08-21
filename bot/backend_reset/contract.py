"""Immutable product vocabulary for one explicit backend reset."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


BACKEND_RESET_STATUS_AVAILABLE = "available"
BACKEND_RESET_STATUS_FORCE_ONLY = "force-only"
BACKEND_RESET_MAX_SAFE_CONNECTION_GENERATION = 9_007_199_254_740_991
_BACKEND_RESET_RESULT_FIELDS = frozenset(
    {
        "force",
        "detached_binding_ids",
        "interrupted_binding_ids",
        "retired_request_count",
        "purged_thread_ids",
        "projection_warnings",
        "app_server_url",
    }
)


class BackendResetResultContractError(ValueError):
    """The reset may have run, but its returned method result is unusable."""


class BackendResetKnownNoEffectError(ValueError):
    """A reset admission failed before the backend epoch was fenced."""


class BackendResetGenerationStaleError(BackendResetKnownNoEffectError):
    """The browser's preview generation no longer names the current epoch."""

    def __init__(
        self,
        *,
        expected_generation: int,
        observed_generation: int | None,
        source: str,
    ) -> None:
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        self.source = str(source or "").strip()
        super().__init__(
            "backend reset preview is stale "
            f"(source={self.source}, expected={expected_generation}, "
            f"observed={observed_generation})"
        )


class BackendResetUnavailableError(BackendResetKnownNoEffectError):
    """The current backend epoch cannot safely admit a Web reset."""


class BackendResetPolicyRejectedError(BackendResetKnownNoEffectError):
    """Fresh reset policy rejected the requested safe/force mode."""


def require_backend_reset_connection_generation(value: object) -> int:
    """Admit one positive browser-safe websocket generation."""

    if (
        type(value) is not int
        or value <= 0
        or value > BACKEND_RESET_MAX_SAFE_CONNECTION_GENERATION
    ):
        raise ValueError(
            "backend reset expected_connection_generation must be a positive safe integer"
        )
    return value


def decode_backend_reset_force(values: Mapping[str, object]) -> bool:
    """Decode the optional destructive reset selector without truthiness."""

    if "force" not in values:
        return False
    force = values["force"]
    if type(force) is not bool:
        raise ValueError("reset backend 的 force 必须是 JSON boolean。")
    return force


@dataclass(frozen=True, slots=True)
class BackendResetResult:
    """One completely decoded backend-reset result."""

    force: bool
    detached_binding_ids: tuple[str, ...]
    interrupted_binding_ids: tuple[str, ...]
    retired_request_count: int
    purged_thread_ids: tuple[str, ...]
    projection_warnings: tuple[str, ...]
    app_server_url: str


@dataclass(frozen=True, slots=True)
class BackendResetLocalProjectionReceipt:
    """Post-stop binding/execution projections completed before replacement."""

    detached_binding_ids: tuple[str, ...]
    interrupted_binding_ids: tuple[str, ...]
    projection_warnings: tuple[str, ...]


def decode_backend_reset_result(
    raw: object,
    *,
    expected_force: bool,
) -> BackendResetResult:
    """Admit one exact result before any caller projects reset success."""

    if type(expected_force) is not bool:
        raise TypeError("backend reset expected_force must be an exact bool")
    if type(raw) is not dict:
        raise BackendResetResultContractError(
            "backend reset result must be an exact object"
        )

    fields = frozenset(raw)
    if fields != _BACKEND_RESET_RESULT_FIELDS:
        missing = sorted(_BACKEND_RESET_RESULT_FIELDS - fields)
        extra = sorted(str(field) for field in fields - _BACKEND_RESET_RESULT_FIELDS)
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise BackendResetResultContractError(
            "backend reset result has invalid fields"
            + (f" ({'; '.join(details)})" if details else "")
        )

    force = raw["force"]
    if type(force) is not bool:
        raise BackendResetResultContractError(
            "backend reset result force must be an exact bool"
        )
    if force is not expected_force:
        raise BackendResetResultContractError(
            "backend reset result force does not match the request"
        )

    retired_request_count = raw["retired_request_count"]
    if type(retired_request_count) is not int or retired_request_count < 0:
        raise BackendResetResultContractError(
            "backend reset result retired_request_count must be a non-negative integer"
        )

    app_server_url = _decode_backend_reset_result_text(
        raw["app_server_url"],
        field="app_server_url",
    )
    return BackendResetResult(
        force=force,
        detached_binding_ids=_decode_backend_reset_result_list(
            raw["detached_binding_ids"],
            field="detached_binding_ids",
        ),
        interrupted_binding_ids=_decode_backend_reset_result_list(
            raw["interrupted_binding_ids"],
            field="interrupted_binding_ids",
        ),
        retired_request_count=retired_request_count,
        purged_thread_ids=_decode_backend_reset_result_list(
            raw["purged_thread_ids"],
            field="purged_thread_ids",
        ),
        projection_warnings=_decode_backend_reset_result_list(
            raw["projection_warnings"],
            field="projection_warnings",
        ),
        app_server_url=app_server_url,
    )


def _decode_backend_reset_result_list(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        raise BackendResetResultContractError(
            f"backend reset result {field} must be an exact list"
        )
    return tuple(
        _decode_backend_reset_result_text(item, field=f"{field} item")
        for item in value
    )


def _decode_backend_reset_result_text(value: object, *, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise BackendResetResultContractError(
            f"backend reset result {field} must be a non-empty string"
        )
    return value.strip()


@dataclass(frozen=True, slots=True)
class BackendResetPreview:
    """Policy-owned facts consumed by reset execution and presentation."""

    status: str
    reason_code: str
    reason_text: str
    diagnostics: tuple[str, ...] = ()
    pending_request_count: int = 0
    running_binding_ids: tuple[str, ...] = ()
    active_loaded_thread_ids: tuple[str, ...] = ()
    loaded_thread_ids: tuple[str, ...] = ()
    runtime_verification_failed: bool = False
    blocking_holder_labels: tuple[str, ...] = ()
    attached_binding_ids: tuple[str, ...] = ()
    loaded_thread_preview: tuple[str, ...] = ()
    active_loaded_thread_preview: tuple[str, ...] = ()
    blocking_active_turn_count: int = 0
    blocking_pending_request_count: int = 0
    collateral_loaded_thread_count: int = 0
    collateral_active_loaded_thread_count: int = 0
