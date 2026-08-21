"""Surface-neutral contracts for Codex server-request projection."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

from bot.jsonrpc_id import jsonrpc_id_key


ServerRequestRoutingMode = Literal[
    "single_surface",
    "shared_approval",
    "shared_interaction",
]


@dataclass(frozen=True, slots=True, init=False)
class ServerRequestIdentity:
    """Immutable request envelope and receiving-connection capability."""

    request_id: int | float | str
    request_key: str
    connection_generation: int
    method: str
    thread_id: str
    turn_id: str
    _params: dict[str, Any]

    def __init__(
        self,
        *,
        request_id: int | float | str,
        connection_generation: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if (
            isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise ValueError(
                "server request connection_generation must be a positive integer"
            )
        if not isinstance(params, dict):
            raise ValueError("server request params must be an object")
        normalized_method = str(method or "").strip()
        if not normalized_method:
            raise ValueError("server request method must not be empty")
        try:
            params_snapshot = copy.deepcopy(params)
        except Exception as exc:
            raise ValueError("server request params cannot be snapshotted") from exc
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "request_key", jsonrpc_id_key(request_id))
        object.__setattr__(self, "connection_generation", connection_generation)
        object.__setattr__(self, "method", normalized_method)
        object.__setattr__(
            self,
            "thread_id",
            str(params_snapshot.get("threadId", "") or "").strip(),
        )
        object.__setattr__(
            self,
            "turn_id",
            str(params_snapshot.get("turnId", "") or "").strip(),
        )
        object.__setattr__(self, "_params", params_snapshot)

    @property
    def params(self) -> dict[str, Any]:
        return copy.deepcopy(self._params)

    def same_identity_as(self, other: ServerRequestIdentity) -> bool:
        return (
            isinstance(other, ServerRequestIdentity)
            and self.request_key == other.request_key
            and self.connection_generation == other.connection_generation
            and self.method == other.method
            and self.thread_id == other.thread_id
            and self.turn_id == other.turn_id
            and self._params == other._params
        )


ServerRequestRoutingOutcome = Literal[
    "committed",
    "replayed",
    "response_pending_resolution",
    "suppressed_resolved",
    "identity_conflict",
    "dispatch_failed",
    "epoch_mismatch",
]


@dataclass(frozen=True, slots=True)
class ServerRequestRoutingReport:
    outcome: ServerRequestRoutingOutcome
    request_key: str = ""
    thread_id: str = ""
    dispatch_outcome: Literal[
        "",
        "committed",
        "known_not_committed",
        "outcome_unknown",
    ] = ""
    response_phase: Literal[
        "",
        "pending",
        "processing",
        "submitted",
        "unknown",
    ] = ""
    response_authority_revoked: bool = False


ServerRequestLocalRemovalOutcome = Literal[
    "invalid",
    "not_resolved",
    "missing",
    "mismatch",
    "removed",
]


@dataclass(frozen=True, slots=True)
class ServerRequestLocalRemoval:
    outcome: ServerRequestLocalRemovalOutcome
    request_key: str = ""
    thread_id: str = ""
    root_thread_id: str = ""


@dataclass(frozen=True, slots=True)
class ServerRequestResolutionReport:
    outcome: Literal[
        "settled",
        "already_resolved",
        "missing",
        "identity_conflict",
        "invalid",
    ]
    request_key: str = ""
    thread_id: str = ""
    local_removals: tuple[ServerRequestLocalRemoval, ...] = ()
    reconciled_root_ids: frozenset[str] = frozenset()


ServerRequestResponseOutcome = Literal[
    "submitted",
    "superseded",
    "not_pending",
    "identity_conflict",
    "processing",
    "outcome_unknown",
]


@dataclass(frozen=True, slots=True)
class ServerRequestResponseReport:
    """Canonical disposition of one surface response attempt."""

    outcome: ServerRequestResponseOutcome
    request_key: str = ""
    thread_id: str = ""


class ServerRequestResponseAdmissionError(RuntimeError):
    """A surface attempted a response without canonical response authority."""

    def __init__(self, report: ServerRequestResponseReport) -> None:
        self.report = report
        super().__init__(
            f"server-request response rejected: request={report.request_key} "
            f"outcome={report.outcome}"
        )


class ServerRequestResponseSupersededError(ServerRequestResponseAdmissionError):
    """The request was already answered or cleared without this response."""
