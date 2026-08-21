"""Authoritative direct-target admission for Focus Web operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.direct_thread_target_policy import (
    DirectThreadTargetPolicyError,
    require_direct_thread_target,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_writer_profile_store import WebWriterSelectionClearReceipt
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.writer_workspace_coordinator import WebWorkspaceConvergenceOutcome


logger = logging.getLogger(__name__)


def require_web_direct_thread_snapshot(
    snapshot: ThreadSnapshot,
    *,
    thread_id: str,
    operation: str,
) -> ThreadSnapshot:
    """Validate one already-read snapshot without touching RuntimeLoop state."""

    normalized_thread_id = str(thread_id or "").strip()
    summary = snapshot.summary
    returned_thread_id = str(summary.thread_id or "").strip()
    if returned_thread_id != normalized_thread_id:
        raise WebRuntimeError(
            "Focus could not verify that Codex read the requested thread. "
            "The direct operation was not attempted.",
            code="thread_target_unverified",
            status=503,
            details={"thread_id": normalized_thread_id},
        )
    try:
        require_direct_thread_target(summary, operation=operation)
    except DirectThreadTargetPolicyError as exc:
        raise WebRuntimeError(
            str(exc),
            code="subagent_detail_only",
            status=409,
            details={"thread_id": normalized_thread_id},
        ) from exc
    return snapshot


@dataclass(frozen=True, slots=True)
class WebDirectThreadTargetVerifierPorts:
    read_thread: Callable[..., ThreadSnapshot]


class WebDirectThreadTargetVerifier:
    """Prove one Web direct target without running local cleanup effects."""

    def __init__(
        self,
        *,
        ports: WebDirectThreadTargetVerifierPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web direct-target verifier requires a RuntimeLoop guard")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def read(
        self,
        thread_id: str,
        *,
        operation: str,
        include_turns: bool = False,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> ThreadSnapshot:
        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        if timeout is None and not require_existing_connection:
            snapshot = self._ports.read_thread(
                normalized_thread_id,
                bool(include_turns),
            )
        else:
            snapshot = self._ports.read_thread(
                normalized_thread_id,
                bool(include_turns),
                timeout=timeout,
                require_existing_connection=require_existing_connection,
            )
        return require_web_direct_thread_snapshot(
            snapshot,
            thread_id=normalized_thread_id,
            operation=operation,
        )


@dataclass(frozen=True, slots=True)
class WebDirectThreadTargetPorts:
    """Local convergence ports; none re-enter the Web runtime façade."""

    remember_direct_thread_summary: Callable[[ThreadSummary], None]
    persist_clear_unusable_thread: Callable[
        [str], tuple[WebWriterSelectionClearReceipt, ...]
    ]
    materialize_cleared_unusable_thread: Callable[
        ..., WebWorkspaceConvergenceOutcome
    ]
    settle_runtime_cleanup_candidates: Callable[[Iterable[str]], None]
    delete_thread_scope: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class WebDirectThreadCleanupReceipt:
    """External durable cleanup awaiting exact RuntimeLoop materialization."""

    thread_id: str
    reason: str
    cleared_profiles: tuple[WebWriterSelectionClearReceipt, ...]
    delete_attachment_scope: bool


class WebDirectThreadTargetCoordinator:
    """Own direct Web target proof and invalid-target local convergence.

    The upstream thread summary is the only admission fact. Rebuildable Web
    selection, runtime interest, and attachment scope are cleanup effects and
    never become alternate evidence that a ThreadSpawn child is a root.
    """

    def __init__(
        self,
        *,
        verifier: WebDirectThreadTargetVerifier,
        ports: WebDirectThreadTargetPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web direct-target coordinator requires a RuntimeLoop guard")
        self._verifier = verifier
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def read(
        self,
        thread_id: str,
        *,
        operation: str,
        include_turns: bool = False,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> ThreadSnapshot:
        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        try:
            snapshot = self._verifier.read(
                normalized_thread_id,
                operation=operation,
                include_turns=include_turns,
                timeout=timeout,
                require_existing_connection=require_existing_connection,
            )
        except WebRuntimeError as exc:
            if exc.code != "subagent_detail_only":
                raise
            self.clear_rejected_direct_thread(
                normalized_thread_id,
                reason="web_direct_target_selection_cleared",
            )
            raise
        self._ports.remember_direct_thread_summary(snapshot.summary)
        return snapshot

    def remember_verified_snapshot(self, snapshot: ThreadSnapshot) -> None:
        """Install a direct-target summary already validated outside RuntimeLoop."""

        self._runtime_context_guard()
        self._ports.remember_direct_thread_summary(snapshot.summary)

    def clear_unusable_thread(self, thread_id: str, *, reason: str) -> None:
        """Converge Web-local facts after authoritative target rejection."""

        self._runtime_context_guard()
        self.settle_unusable_thread_cleanup(
            self.prepare_unusable_thread_cleanup(thread_id, reason=reason)
        )

    def prepare_unusable_thread_cleanup(
        self,
        thread_id: str,
        *,
        reason: str,
        delete_attachment_scope: bool = False,
    ) -> WebDirectThreadCleanupReceipt:
        """Persist target-matching cleanup and prune rebuildable scope off-loop."""

        normalized_thread_id = str(thread_id or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_thread_id or not normalized_reason:
            raise ValueError("Web direct-target cleanup requires thread and reason")
        cleared = self._ports.persist_clear_unusable_thread(normalized_thread_id)
        if delete_attachment_scope:
            try:
                self._ports.delete_thread_scope(normalized_thread_id)
            except Exception:
                logger.exception(
                    "Unable to prune attachments for invalid direct Web target: "
                    "thread=%s",
                    normalized_thread_id[:12],
                )
        return WebDirectThreadCleanupReceipt(
            normalized_thread_id,
            normalized_reason,
            cleared,
            bool(delete_attachment_scope),
        )

    def settle_unusable_thread_cleanup(
        self,
        receipt: WebDirectThreadCleanupReceipt,
    ) -> None:
        """Materialize one typed cleanup receipt inside RuntimeLoop."""

        self._runtime_context_guard()
        if not isinstance(receipt, WebDirectThreadCleanupReceipt):
            raise TypeError("typed Web direct-thread cleanup receipt is required")
        if any(
            item.cleared_thread_id != receipt.thread_id
            for item in receipt.cleared_profiles
        ):
            raise ValueError("Web direct-thread cleanup receipt is not exact")
        outcome = self._ports.materialize_cleared_unusable_thread(
            receipt.thread_id,
            receipt.cleared_profiles,
            reason=receipt.reason,
        )
        self._ports.settle_runtime_cleanup_candidates(
            outcome.runtime_cleanup_thread_ids
        )

    def clear_rejected_direct_thread(self, thread_id: str, *, reason: str) -> None:
        """Clear every Web-local scope for one proven non-direct target.

        Direct reads and staged external reads share this semantic cleanup.
        The caller must first prove the upstream snapshot and, for staged
        work, settle its exact document/backend receipt in RuntimeLoop.
        """

        self._runtime_context_guard()
        self.settle_unusable_thread_cleanup(
            self.prepare_unusable_thread_cleanup(
                thread_id,
                reason=reason,
                delete_attachment_scope=True,
            )
        )
