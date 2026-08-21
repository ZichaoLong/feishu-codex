"""Causal coordination for durable Web selection and process projections.

The three participating facts deliberately remain in their narrow owners:

* ``WebWriterProfileStore.selected_thread_id`` is durable semantic authority;
* ``WebDocumentRegistry.materialized_thread_id`` is process-local readiness;
* ``WebRuntimeInterestRegistry`` owns desired subscription edges.

This coordinator owns only their transition order.  It never mirrors any of
those facts and returns immutable receipts for post-commit runtime cleanup and
event publication by the Web facade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from bot.stores.web_writer_profile_store import (
    WebWriterProfile,
    WebWriterProfileStore,
    WebWriterSelectionClearReceipt,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.interest import WebRuntimeInterestRegistry


class WebSelectionNotReady(WebRuntimeError):
    """Raised when durable authority and materialization do not match."""

    def __init__(self, thread_id: str) -> None:
        normalized = str(thread_id or "").strip()
        super().__init__(
            "Select this thread before using its bounded history.",
            code="thread_not_selected",
            status=409,
            details={"thread_id": normalized},
        )


class WebSelectionAuthorityMismatch(RuntimeError):
    """Raised before process state could override a different durable target."""


@dataclass(frozen=True, slots=True)
class WebSelectionConvergence:
    """Post-commit evidence and runtime-cleanup candidates for the facade."""

    cleared_profiles: tuple[WebWriterSelectionClearReceipt, ...] = ()
    runtime_cleanup_thread_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebThreadSelection:
    """Typed durable/process transition and its attachment-scope receipt."""

    previous: WebWriterProfile
    current: WebWriterProfile
    previous_attachment_scope: str
    current_attachment_scope: str
    runtime_cleanup_thread_ids: tuple[str, ...]

    @property
    def scope_changed(self) -> bool:
        return self.previous.selected_thread_id != self.current.selected_thread_id

    def project(self, *, writer_profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "writer_profile": writer_profile,
            "scope_changed": self.scope_changed,
            "previous_attachment_scope": (
                self.previous_attachment_scope if self.scope_changed else ""
            ),
            "current_attachment_scope": self.current_attachment_scope,
            "previous_scope_generation": self.previous.scope_generation,
            "current_scope_generation": self.current.scope_generation,
            "attachment_scope_disposition": (
                "isolated" if self.scope_changed else "unchanged"
            ),
        }


class WebSelectionCoordinator:
    """Order cross-owner selection transitions without becoming a fact store."""

    def __init__(
        self,
        *,
        profile_store: WebWriterProfileStore,
        document_registry: WebDocumentRegistry,
        runtime_interest: WebRuntimeInterestRegistry,
    ) -> None:
        self._profile_store = profile_store
        self._document_registry = document_registry
        self._runtime_interest = runtime_interest

    def require_history_ready(self, client_id: str, thread_id: str) -> None:
        """Require both semantic selection and bounded-history readiness."""

        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        self.require_history_ready_snapshot(
            self.load_profile_snapshot(normalized_client_id),
            normalized_client_id,
            normalized_thread_id,
        )

    def load_profile_snapshot(self, client_id: str) -> WebWriterProfile | None:
        """Freeze durable selection on the caller's external I/O thread."""

        return self._profile_store.load(self._client_id(client_id))

    def require_materialized_thread(self, client_id: str, thread_id: str) -> None:
        """Check only process-local bounded-history readiness."""

        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        materialized_thread_id = self._document_registry.materialized_thread_id(
            normalized_client_id
        )
        if materialized_thread_id != normalized_thread_id:
            raise WebSelectionNotReady(normalized_thread_id)

    def require_history_ready_snapshot(
        self,
        profile: WebWriterProfile | None,
        client_id: str,
        thread_id: str,
    ) -> None:
        """Check one frozen durable selection against current materialization."""

        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        if (
            profile is None
            or profile.client_id != normalized_client_id
            or profile.selected_thread_id != normalized_thread_id
        ):
            raise WebSelectionNotReady(normalized_thread_id)
        self.require_materialized_thread(normalized_client_id, normalized_thread_id)

    def materialize_selected_thread(
        self,
        client_id: str,
        thread_id: str,
    ) -> WebSelectionConvergence:
        """Install an already-durable target and remove every stale R edge."""

        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        profile = self._profile_store.load(normalized_client_id)
        return self.materialize_profile_selection(
            profile,
            normalized_client_id,
            normalized_thread_id,
        )

    def materialize_profile_selection(
        self,
        profile: WebWriterProfile | None,
        client_id: str,
        thread_id: str,
    ) -> WebSelectionConvergence:
        """Install process facts from one already-persisted exact profile."""

        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        if (
            profile is None
            or profile.client_id != normalized_client_id
            or str(profile.selected_thread_id or "").strip()
            != normalized_thread_id
        ):
            raise WebSelectionAuthorityMismatch(normalized_thread_id)
        self._document_registry.materialize_thread(
            normalized_client_id,
            normalized_thread_id,
        )
        removed = self._runtime_interest.remove_desired_client_from_all(
            normalized_client_id,
            except_thread_id=normalized_thread_id,
        )
        return WebSelectionConvergence(runtime_cleanup_thread_ids=removed)

    def persist_thread_selection(
        self,
        expected: WebWriterProfile | None,
        current: WebWriterProfile,
        thread_id: str,
        *,
        draft_scope_key: str,
    ) -> WebThreadSelection | None:
        """CAS durable selection without touching RuntimeLoop-owned facts."""

        client_id = self._client_id(current.client_id)
        target = self._thread_id(thread_id)
        previous_thread_id = str(current.selected_thread_id or "").strip()
        changes = (
            {}
            if previous_thread_id == target
            else {
                "selected_thread_id": target,
                "working_dir": current.working_dir,
                "scope_generation": current.scope_generation + 1,
            }
        )
        selected = self._profile_store.update_if_matches(
            client_id,
            expected,
            **changes,
        )
        if selected is None:
            return None
        previous_scope = (
            f"thread:{previous_thread_id}"
            if previous_thread_id
            else f"draft:{str(draft_scope_key or '').strip()}"
        )
        return WebThreadSelection(
            previous=current,
            current=selected,
            previous_attachment_scope=previous_scope,
            current_attachment_scope=f"thread:{target}",
            runtime_cleanup_thread_ids=(),
        )

    def materialize_persisted_selection(
        self,
        selection: WebThreadSelection,
    ) -> WebThreadSelection:
        convergence = self.materialize_profile_selection(
            selection.current,
            selection.current.client_id,
            selection.current.selected_thread_id,
        )
        return replace(
            selection,
            runtime_cleanup_thread_ids=convergence.runtime_cleanup_thread_ids,
        )

    def compensate_stale_persisted_selection(
        self,
        selection: WebThreadSelection,
    ) -> WebWriterProfile | None:
        """CAS a stale selection forward to its previous semantic scope."""

        previous = selection.previous
        current = selection.current
        if (
            previous.selected_thread_id == current.selected_thread_id
            and previous.working_dir == current.working_dir
        ):
            return None
        return self._profile_store.update_if_matches(
            current.client_id,
            current,
            selected_thread_id=previous.selected_thread_id,
            working_dir=previous.working_dir,
            scope_generation=current.scope_generation + 1,
        )

    def select_thread(
        self,
        current: WebWriterProfile,
        thread_id: str,
        *,
        draft_scope_key: str,
    ) -> WebThreadSelection:
        """Persist one semantic selection before installing process facts."""

        normalized_client_id = self._client_id(current.client_id)
        normalized_thread_id = self._thread_id(thread_id)
        previous_thread_id = str(current.selected_thread_id or "").strip()
        scope_changed = previous_thread_id != normalized_thread_id
        selected = current
        if scope_changed:
            selected = self._profile_store.update(
                normalized_client_id,
                selected_thread_id=normalized_thread_id,
                working_dir=current.working_dir,
                scope_generation=current.scope_generation + 1,
            )
        convergence = self.materialize_selected_thread(
            normalized_client_id,
            normalized_thread_id,
        )
        previous_scope = (
            f"thread:{previous_thread_id}"
            if previous_thread_id
            else f"draft:{str(draft_scope_key or '').strip()}"
        )
        return WebThreadSelection(
            previous=current,
            current=selected,
            previous_attachment_scope=previous_scope,
            current_attachment_scope=f"thread:{normalized_thread_id}",
            runtime_cleanup_thread_ids=convergence.runtime_cleanup_thread_ids,
        )

    def clear_document_projection(
        self,
        client_id: str,
    ) -> WebSelectionConvergence:
        """Detach one document after its durable selection became draft."""

        normalized_client_id = self._client_id(client_id)
        materialized = self._document_registry.materialized_thread_id(
            normalized_client_id
        )
        if materialized:
            self._document_registry.forget_materialized_thread_if_matches(
                normalized_client_id,
                materialized,
            )
        removed = self._runtime_interest.remove_desired_client_from_all(
            normalized_client_id
        )
        return WebSelectionConvergence(runtime_cleanup_thread_ids=removed)

    def lose_document(self, client_id: str) -> WebSelectionConvergence:
        """Drop process continuity and every R edge, retaining durable D."""

        normalized_client_id = self._client_id(client_id)
        self._document_registry.mark_document_lost(normalized_client_id)
        removed = self._runtime_interest.remove_desired_client_from_all(
            normalized_client_id
        )
        return WebSelectionConvergence(runtime_cleanup_thread_ids=removed)

    def persist_clear_unusable_thread(
        self,
        thread_id: str,
    ) -> tuple[WebWriterSelectionClearReceipt, ...]:
        return self._profile_store.clear_selected_thread(self._thread_id(thread_id))

    def materialize_cleared_unusable_thread(
        self,
        thread_id: str,
        receipts: tuple[WebWriterSelectionClearReceipt, ...],
    ) -> WebSelectionConvergence:
        normalized_thread_id = self._thread_id(thread_id)
        self._document_registry.forget_materialized_thread_for_all(
            normalized_thread_id
        )
        removed_thread_ids: set[str] = set()
        for receipt in receipts:
            removed_thread_ids.update(
                self._runtime_interest.remove_desired_client_from_all(
                    receipt.current.client_id
                )
            )
        target_interest = self._runtime_interest.snapshot(normalized_thread_id)
        if target_interest is not None:
            removed_thread_ids.add(normalized_thread_id)
        self._runtime_interest.clear_desired_clients(normalized_thread_id)
        return WebSelectionConvergence(
            cleared_profiles=receipts,
            runtime_cleanup_thread_ids=tuple(sorted(removed_thread_ids)),
        )

    @staticmethod
    def _client_id(client_id: object) -> str:
        normalized = str(client_id or "").strip()
        if not normalized:
            raise ValueError("Web selection requires a client id.")
        return normalized

    @staticmethod
    def _thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("Web selection requires a thread id.")
        return normalized
