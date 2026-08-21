"""Web writer-profile, workspace, and attachment transaction owner.

The durable profile and attachment records remain owned by their stores.  This
coordinator owns the ordering between those stores, the Web selection owner,
and projection publication.  Runtime-interest cleanup is returned as a typed
post-commit outcome because that lifecycle belongs to the Web runtime owner,
not to this workspace transaction.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from bot.adapters.base import RuntimeModelSummary, ThreadSnapshot
from bot.input_media_contract import model_supports_input
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_attachment_store import (
    WebAttachmentDownload,
    WebAttachmentRecord,
    WebAttachmentSubmissionClaimReceipt,
    WebAttachmentStore,
)
from bot.stores.web_writer_profile_store import (
    WebWriterProfile,
    WebWriterProfileStore,
    WebWriterSelectionClearReceipt,
)
from bot.web_runtime.document_registry import (
    InvalidWebDocumentIntent,
    StaleWebDocumentIntent,
    WebDocumentRegistry,
)
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.selection_coordinator import (
    WebSelectionAuthorityMismatch,
    WebSelectionCoordinator,
    WebThreadSelection,
)
from bot.web_runtime.thread_read_model import WebThreadReadModel


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebWriterWorkspacePorts:
    """Required upstream reads; no callback re-enters the runtime facade."""

    list_models: Callable[[], list[RuntimeModelSummary]]
    read_thread: Callable[[str, bool], ThreadSnapshot]


@dataclass(frozen=True, slots=True)
class WebWorkspaceUpdateOutcome:
    payload: dict[str, Any]
    runtime_cleanup_thread_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebWorkspaceSelectionOutcome:
    profile: WebWriterProfile
    selection_scope: dict[str, Any]
    runtime_cleanup_thread_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebWorkspaceConvergenceOutcome:
    runtime_cleanup_thread_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebWorkspaceProfileSnapshot:
    stored: WebWriterProfile | None
    effective: WebWriterProfile


@dataclass(frozen=True, slots=True)
class WebComposerScopeReceipt:
    """Immutable press-time identity; the profile store remains authority."""

    client_id: str
    thread_id: str
    scope_generation: int
    attachment_scope: str
    composer_scope_id: str


def require_web_client_id(client_id: str) -> str:
    normalized = str(client_id or "").strip()
    if not normalized or len(normalized) > 128:
        raise WebRuntimeError(
            "A valid browser client id is required.",
            code="invalid_client",
        )
    return normalized


def require_connected_web_document(
    documents: WebDocumentRegistry,
    client_id: str,
) -> str:
    """Require the live WebSocket delivery path used by browser actions.

    Cookie-authenticated HTTP identifies a browser document but cannot prove
    it can still receive turn output or interaction requests. Interactive
    actions whose contract requires live delivery use this check; read-only
    and attachment-staging paths apply their separate contracts.
    """

    documents.assert_runtime_context()
    normalized = require_web_client_id(client_id)
    if not documents.is_connected(normalized):
        raise WebRuntimeError(
            "This browser document is disconnected. Reconnect before making a live browser action.",
            code="web_writer_disconnected",
            status=409,
        )
    return normalized


def accept_web_document_intent(
    documents: WebDocumentRegistry,
    client_id: str,
    generation: int,
) -> None:
    try:
        documents.accept_intent(client_id, generation)
    except InvalidWebDocumentIntent as exc:
        raise WebRuntimeError(str(exc), code="invalid_intent") from exc
    except StaleWebDocumentIntent as exc:
        raise WebRuntimeError(
            "This browser action was superseded by a newer action.",
            code="stale_intent",
            status=409,
            details={"latest_intent": exc.latest_generation},
        ) from exc


def require_web_thread_id(thread_id: str) -> str:
    normalized = str(thread_id or "").strip()
    if not normalized:
        raise WebRuntimeError("thread_id is required.", code="invalid_thread")
    return normalized


class WebWriterWorkspaceCoordinator:
    """Own profile/workspace/attachment transaction ordering for one runtime."""

    def __init__(
        self,
        *,
        profile_store: WebWriterProfileStore,
        attachment_store: WebAttachmentStore,
        documents: WebDocumentRegistry,
        selection: WebSelectionCoordinator,
        read_model: WebThreadReadModel,
        effective_settings: ThreadEffectiveSettingsRegistry,
        projection: FocusWebProjection,
        ports: WebWriterWorkspacePorts,
        runtime_context_guard: RuntimeContextGuard,
        default_working_dir: str,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web writer workspace requires a RuntimeLoop context guard")
        self._profiles = profile_store
        self._attachments = attachment_store
        self._documents = documents
        self._selection = selection
        self._read_model = read_model
        self._effective_settings = effective_settings
        self._projection = projection
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._default_working_dir = str(default_working_dir or "").strip()

    @property
    def default_working_dir(self) -> str:
        self._runtime_context_guard()
        return self._default_working_dir

    def read_catalog_profile(
        self,
        client_id: str,
    ) -> tuple[list[RuntimeModelSummary], WebWriterProfile]:
        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        return self._ports.list_models(), self.profile(normalized_client_id)

    def update_profile(
        self,
        client_id: str,
        changes: dict[str, Any],
        *,
        intent_generation: int = 0,
    ) -> WebWorkspaceUpdateOutcome:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        accept_web_document_intent(
            self._documents,
            normalized_client_id,
            intent_generation,
        )
        allowed = {
            "selected_thread_id",
            "working_dir",
        }
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise WebRuntimeError(
                f"Unsupported writer profile fields: {', '.join(unknown)}.",
                code="invalid_profile",
            )
        normalized: dict[str, Any] = {}
        current = self.profile(normalized_client_id)
        clear_selected_thread = "selected_thread_id" in changes
        if clear_selected_thread:
            selected_thread_id = str(
                changes.get("selected_thread_id", "") or ""
            ).strip()
            if selected_thread_id:
                raise WebRuntimeError(
                    "Writer profile selection can only be cleared through this endpoint.",
                    code="invalid_profile",
                )
            normalized["selected_thread_id"] = ""
        if "working_dir" in changes:
            requested_working_dir = str(changes.get("working_dir", "") or "").strip()
            normalized["working_dir"] = self.admit_draft_working_dir(
                requested_working_dir or self._default_working_dir,
            )
        selected_thread_id = str(current.selected_thread_id or "").strip()
        current_draft_working_dir = str(
            current.working_dir or self._default_working_dir or ""
        ).strip()
        confirmed_working_dir = str(
            normalized.get("working_dir", current_draft_working_dir) or ""
        ).strip()
        draft_workspace_changed = "working_dir" in normalized and self.working_dir_key(
            confirmed_working_dir
        ) != self.working_dir_key(current_draft_working_dir)
        selection_cleared = clear_selected_thread and bool(selected_thread_id)
        if draft_workspace_changed and selected_thread_id and not clear_selected_thread:
            raise WebRuntimeError(
                "Changing the new-thread workspace while a thread is selected "
                "must clear that selection in the same request.",
                code="invalid_profile",
                status=409,
            )
        scope_changed = draft_workspace_changed or selection_cleared
        previous_scope = ""
        current_scope = (
            f"thread:{selected_thread_id}"
            if selected_thread_id
            else f"draft:{self.working_dir_key(current_draft_working_dir)}"
        )
        attachment_scope_disposition = "unchanged"
        rebound_attachment_ids: list[str] = []
        # Capture a detached response coordinate before the durable commit.
        # Once the profile write succeeds, projection publication is only a
        # best-effort process-local consequence and must never turn the
        # committed request into an HTTP failure.
        fallback_coordinates = dict(self._projection.coordinates())
        if scope_changed:
            previous_scope = current_scope
            current_scope = f"draft:{self.working_dir_key(confirmed_working_dir)}"
            normalized["scope_generation"] = current.scope_generation + 1
            selected_thread_cwd = (
                self._authoritative_selected_thread_cwd(selected_thread_id)
                if selection_cleared
                else ""
            )
            confirmed_cwd_key = self.working_dir_key(confirmed_working_dir)
            if (
                selection_cleared
                and selected_thread_cwd
                and selected_thread_cwd == confirmed_cwd_key
            ):
                attachment_scope_disposition = "rebound"
                try:
                    rebound_attachment_ids = self._attachments.rebind_pending_scope(
                        client_id=normalized_client_id,
                        source_scope_key=previous_scope,
                        target_scope_key=current_scope,
                        cwd=confirmed_working_dir,
                    )
                except Exception as exc:
                    raise WebRuntimeError(
                        "Focus could not preserve attachments while opening the same-workspace draft.",
                        code="attachment_scope_rebind_failed",
                        status=409,
                    ) from exc
            else:
                attachment_scope_disposition = "invalidated"
        try:
            profile = self._profiles.update(normalized_client_id, **normalized)
        except Exception as profile_write_error:
            if rebound_attachment_ids:
                try:
                    self._attachments.rebind_pending_scope(
                        client_id=normalized_client_id,
                        source_scope_key=current_scope,
                        target_scope_key=previous_scope,
                        cwd=confirmed_working_dir,
                        attachment_ids=rebound_attachment_ids,
                    )
                except Exception as rollback_error:
                    logger.exception(
                        "Unable to roll back Web attachment scope after profile write failure: "
                        "client=%s source=%s target=%s",
                        normalized_client_id,
                        current_scope,
                        previous_scope,
                    )
                    raise WebRuntimeError(
                        "Focus could not commit the workspace profile or restore its attachment scope. "
                        "Reload and upload the pending attachments again.",
                        code="attachment_scope_rebind_unknown",
                        status=503,
                    ) from rollback_error
            raise profile_write_error
        invalidated_attachment_count = 0
        if attachment_scope_disposition == "invalidated":
            try:
                invalidated_attachment_count = len(
                    self._attachments.delete_pending_scope(
                        client_id=normalized_client_id,
                        scope_key=previous_scope,
                    )
                )
            except Exception:
                logger.exception(
                    "Unable to prune attachments from superseded Web scope: client=%s scope=%s",
                    normalized_client_id,
                    previous_scope,
                )
        runtime_cleanup_thread_ids: tuple[str, ...] = ()
        if clear_selected_thread:
            try:
                convergence = self._selection.clear_document_projection(
                    normalized_client_id
                )
            except Exception:
                # D is already committed. The same clear request and ordinary
                # document-loss handling both replay this idempotent M/R
                # convergence; do not invent another durable recovery fact.
                logger.exception(
                    "Unable to clear Web document projection after writer profile commit: "
                    "client=%s",
                    normalized_client_id,
                )
            else:
                runtime_cleanup_thread_ids = tuple(
                    convergence.runtime_cleanup_thread_ids
                )
        projection_coordinates = fallback_coordinates
        try:
            event = self._projection.publish(
                "profile_changed",
                reason="web_profile_updated",
            )
            projection_coordinates = self._event_coordinates(
                event,
                fallback=fallback_coordinates,
            )
        except Exception:
            logger.exception(
                "Unable to publish Web profile projection after writer profile commit: "
                "client=%s",
                normalized_client_id,
            )
        return WebWorkspaceUpdateOutcome(
            payload={
                **projection_coordinates,
                "writer_profile": self.profile_payload(profile),
                "scope_changed": scope_changed,
                "previous_attachment_scope": previous_scope,
                "current_attachment_scope": current_scope,
                "previous_scope_generation": current.scope_generation,
                "current_scope_generation": profile.scope_generation,
                "attachment_scope_disposition": attachment_scope_disposition,
                "invalidated_attachment_count": invalidated_attachment_count,
                "rebound_attachment_count": len(rebound_attachment_ids),
            },
            runtime_cleanup_thread_ids=runtime_cleanup_thread_ids,
        )

    @staticmethod
    def _event_coordinates(
        event: object,
        *,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(event, dict):
            return dict(fallback)
        if "runtime_epoch" not in event or "revision" not in event:
            return dict(fallback)
        return {
            "runtime_epoch": event["runtime_epoch"],
            "revision": event["revision"],
        }

    def select_thread(
        self,
        client_id: str,
        thread_id: str,
    ) -> WebWorkspaceSelectionOutcome:
        self._runtime_context_guard()
        current = self.profile(client_id)
        selection = self._selection.select_thread(
            current,
            thread_id,
            draft_scope_key=self.working_dir_key(current.working_dir),
        )
        return WebWorkspaceSelectionOutcome(
            profile=selection.current,
            selection_scope=selection.project(
                writer_profile=self.profile_payload(selection.current)
            ),
            runtime_cleanup_thread_ids=tuple(selection.runtime_cleanup_thread_ids),
        )

    def load_profile_snapshot(self, client_id: str) -> WebWorkspaceProfileSnapshot:
        """Load and normalize one immutable profile outside RuntimeLoop."""

        normalized_client_id = require_web_client_id(client_id)
        stored = self._profiles.load(normalized_client_id)
        return WebWorkspaceProfileSnapshot(
            stored=stored,
            effective=self._effective_profile(normalized_client_id, stored),
        )

    def persist_thread_selection(
        self,
        snapshot: WebWorkspaceProfileSnapshot,
        thread_id: str,
    ) -> WebThreadSelection | None:
        current = snapshot.effective
        return self._selection.persist_thread_selection(
            snapshot.stored,
            current,
            thread_id,
            draft_scope_key=self.working_dir_key(current.working_dir),
        )

    def persist_current_thread_selection(
        self,
        snapshot: WebWorkspaceProfileSnapshot,
        thread_id: str,
        *,
        still_current: Callable[[], None],
    ) -> WebThreadSelection:
        """CAS once, retrying one current successor from a fresh snapshot."""

        still_current()
        selected = self.persist_thread_selection(snapshot, thread_id)
        if selected is not None:
            return selected
        still_current()
        selected = self.persist_thread_selection(
            self.load_profile_snapshot(snapshot.effective.client_id),
            thread_id,
        )
        if selected is None:
            raise WebSelectionAuthorityMismatch(thread_id)
        return selected

    def materialize_persisted_selection(
        self,
        selection: WebThreadSelection,
    ) -> WebWorkspaceSelectionOutcome:
        self._runtime_context_guard()
        settled = self._selection.materialize_persisted_selection(selection)
        return WebWorkspaceSelectionOutcome(
            profile=settled.current,
            selection_scope=settled.project(
                writer_profile=self.profile_payload(settled.current)
            ),
            runtime_cleanup_thread_ids=settled.runtime_cleanup_thread_ids,
        )

    def compensate_stale_persisted_selection(
        self,
        selection: WebThreadSelection,
    ) -> WebWriterProfile | None:
        return self._selection.compensate_stale_persisted_selection(selection)

    def publish_stale_selection_compensation(
        self,
        profile: WebWriterProfile,
    ) -> None:
        self._runtime_context_guard()
        self._projection.publish(
            "profile_changed",
            thread_id=profile.selected_thread_id,
            reason="web_stale_open_selection_compensated",
        )

    def persist_clear_unusable_thread(
        self,
        thread_id: str,
    ) -> tuple[WebWriterSelectionClearReceipt, ...]:
        return self._selection.persist_clear_unusable_thread(thread_id)

    def materialize_cleared_unusable_thread(
        self,
        thread_id: str,
        receipts: tuple[WebWriterSelectionClearReceipt, ...],
        *,
        reason: str,
    ) -> WebWorkspaceConvergenceOutcome:
        self._runtime_context_guard()
        convergence = self._selection.materialize_cleared_unusable_thread(
            thread_id,
            receipts,
        )
        if convergence.cleared_profiles:
            self._projection.publish(
                "profile_changed",
                thread_id=thread_id,
                reason=reason,
            )
        return WebWorkspaceConvergenceOutcome(
            runtime_cleanup_thread_ids=tuple(convergence.runtime_cleanup_thread_ids)
        )

    def stage_attachment(
        self,
        client_id: str,
        *,
        thread_id: str = "",
        cwd: str = "",
        scope_generation: int | None = None,
        display_name: str,
        media_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        self.require_current_attachment_scope(
            normalized_client_id,
            thread_id=thread_id,
            cwd=cwd,
            scope_generation=scope_generation,
        )
        scope_key, working_dir = self.attachment_scope(
            normalized_client_id,
            thread_id=thread_id,
            cwd=cwd,
        )
        try:
            record = self._attachments.stage(
                client_id=normalized_client_id,
                scope_key=scope_key,
                cwd=working_dir,
                display_name=display_name,
                media_type=media_type,
                content=content,
            )
        except ValueError as exc:
            raise WebRuntimeError(str(exc), code="invalid_attachment") from exc
        return {
            "file_id": record.attachment_id,
            "name": record.display_name,
            "media_type": record.media_type,
            "size": record.size,
            "url": (
                self.attachment_url(record.attachment_id)
                if self.is_web_renderable_image(record.media_type)
                else ""
            ),
        }

    def attachment_download(self, attachment_id: str) -> WebAttachmentDownload:
        self._runtime_context_guard()
        try:
            download = self._attachments.download(attachment_id=attachment_id)
        except (KeyError, ValueError) as exc:
            raise WebRuntimeError(
                "Attachment was not found or is no longer available.",
                code="attachment_not_found",
                status=404,
            ) from exc
        if not self.is_web_renderable_image(download.record.media_type):
            raise WebRuntimeError(
                "Only verified image attachments can be rendered in Focus Web.",
                code="attachment_preview_unavailable",
                status=404,
            )
        return download

    def attachment_scope(
        self,
        client_id: str,
        *,
        thread_id: str = "",
        cwd: str = "",
        verified_thread_cwd: str = "",
    ) -> tuple[str, str]:
        self._runtime_context_guard()
        del client_id
        normalized_thread_id = str(thread_id or "").strip()
        if normalized_thread_id:
            normalized_thread_id = require_web_thread_id(normalized_thread_id)
            authoritative_cwd = str(verified_thread_cwd or "").strip()
            if not authoritative_cwd:
                authoritative_cwd = self._ports.read_thread(
                    normalized_thread_id,
                    False,
                ).summary.cwd
            working_dir_text = self.working_dir_key(authoritative_cwd)
            if not working_dir_text:
                raise WebRuntimeError(
                    "The selected thread has no usable working directory.",
                    code="invalid_cwd",
                    status=409,
                )
            requested_cwd = str(cwd or "").strip()
            if requested_cwd and self.working_dir_key(
                requested_cwd
            ) != self.working_dir_key(working_dir_text):
                raise WebRuntimeError(
                    "Attachment workspace does not match the selected thread.",
                    code="attachment_scope_mismatch",
                    status=409,
                )
            return f"thread:{normalized_thread_id}", working_dir_text
        working_dir = self.admit_draft_working_dir(cwd)
        return f"draft:{working_dir}", working_dir

    def require_current_attachment_scope(
        self,
        client_id: str,
        *,
        thread_id: str,
        cwd: str,
        scope_generation: int | None,
    ) -> None:
        self._runtime_context_guard()
        profile = self.profile(client_id)
        try:
            requested_generation = (
                profile.scope_generation
                if scope_generation is None
                else int(scope_generation)
            )
        except (TypeError, ValueError) as exc:
            raise WebRuntimeError(
                "Attachment scope generation must be an integer.",
                code="invalid_attachment_scope",
            ) from exc
        selected_thread_id = str(profile.selected_thread_id or "").strip()
        requested_thread_id = str(thread_id or "").strip()
        stale = requested_generation != profile.scope_generation
        if requested_thread_id:
            stale = stale or requested_thread_id != selected_thread_id
        else:
            stale = (
                stale
                or bool(selected_thread_id)
                or self.working_dir_key(cwd)
                != self.working_dir_key(profile.working_dir)
            )
        if stale:
            raise WebRuntimeError(
                "This attachment upload belongs to a superseded browser draft.",
                code="stale_attachment_scope",
                status=409,
                details={"scope_generation": profile.scope_generation},
            )

    def freeze_composer_scope_receipt(
        self,
        client_id: str,
        *,
        thread_id: str,
        scope_generation: int,
        attachment_scope: str,
        composer_scope_id: str,
    ) -> WebComposerScopeReceipt:
        """Validate and freeze scope identity without reading durable state."""

        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        normalized_thread_id = require_web_thread_id(thread_id)
        if (
            isinstance(scope_generation, bool)
            or not isinstance(scope_generation, int)
            or scope_generation <= 0
        ):
            raise WebRuntimeError(
                "Composer scope generation must be a positive integer.",
                code="invalid_submission_scope",
                status=400,
            )
        expected_scope = f"thread:{normalized_thread_id}"
        expected_composer = (
            f"{normalized_client_id}:generation:{scope_generation}:{expected_scope}"
        )
        if (
            attachment_scope != expected_scope
            or composer_scope_id != expected_composer
        ):
            raise WebRuntimeError(
                "Composer scope receipt does not match this browser document and thread.",
                code="invalid_submission_scope",
                status=400,
            )
        return WebComposerScopeReceipt(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            scope_generation=scope_generation,
            attachment_scope=attachment_scope,
            composer_scope_id=composer_scope_id,
        )

    def claim_composer_scope_receipt_external(
        self,
        receipt: WebComposerScopeReceipt,
    ) -> WebComposerScopeReceipt:
        """Authorize one frozen gesture against the durable current profile."""

        if not isinstance(receipt, WebComposerScopeReceipt):
            raise TypeError("Web Composer scope receipt is required")
        profile = self.load_profile_snapshot(receipt.client_id).effective
        if (
            profile.scope_generation != receipt.scope_generation
            or profile.selected_thread_id != receipt.thread_id
        ):
            raise WebRuntimeError(
                "This prompt belongs to a superseded browser draft.",
                code="stale_attachment_scope",
                status=409,
                details={"scope_generation": profile.scope_generation},
            )
        return receipt

    @staticmethod
    def normalize_attachment_ids(raw_ids: list[str] | None) -> list[str]:
        if raw_ids is None:
            return []
        if not isinstance(raw_ids, list):
            raise WebRuntimeError(
                "Attachments must be an array.",
                code="invalid_attachment",
            )
        normalized = [str(value or "").strip() for value in raw_ids]
        if any(not value for value in normalized):
            raise WebRuntimeError(
                "Attachment id must not be empty.",
                code="invalid_attachment",
            )
        return normalized

    def resolve_attachments(
        self,
        client_id: str,
        *,
        scope_key: str,
        attachment_ids: list[str],
    ) -> tuple[WebAttachmentRecord, ...]:
        self._runtime_context_guard()
        if not attachment_ids:
            return ()
        try:
            return self._attachments.resolve_pending(
                client_id=client_id,
                scope_key=scope_key,
                attachment_ids=attachment_ids,
            )
        except ValueError as exc:
            raise WebRuntimeError(
                str(exc),
                code="invalid_attachment",
                status=409,
            ) from exc

    def prompt_input_items(
        self,
        text: str,
        attachments: tuple[WebAttachmentRecord, ...],
        *,
        thread_id: str,
        requested_model: str = "",
    ) -> list[dict[str, Any]]:
        self._runtime_context_guard()
        return self.prompt_input_items_external(
            text,
            attachments,
            thread_id=thread_id,
            requested_model=requested_model,
        )

    def prompt_input_items_external(
        self,
        text: str,
        attachments: tuple[WebAttachmentRecord, ...],
        *,
        thread_id: str,
        requested_model: str = "",
    ) -> list[dict[str, Any]]:
        """Build immutable prompt input on a loop-external request worker."""

        normalized_text = str(text or "").strip()
        if not attachments:
            if not normalized_text.startswith("[[focus.attachments.v1]]\n"):
                return [{"type": "text", "text": normalized_text}]
            return [
                {
                    "type": "text",
                    "text": self._attachment_envelope(normalized_text, []),
                }
            ]
        effective_model = self._effective_settings.resolve_model_for_request(
            thread_id,
            requested_model=requested_model,
        )
        supports_native_image = self._model_supports_input(
            effective_model,
            "image",
        )
        verified_image_by_id = {
            record.attachment_id: (
                record.media_type.startswith("image/")
                and self._attachments.is_native_input_media_type(record.media_type)
            )
            for record in attachments
        }
        manifest: list[dict[str, Any]] = []
        for record in attachments:
            verified_image = verified_image_by_id[record.attachment_id]
            native_input = verified_image and supports_native_image is True
            manifest.append(
                {
                    "id": record.attachment_id,
                    "kind": "image" if verified_image else "file",
                    "name": record.display_name,
                    "media_type": record.media_type,
                    "size": record.size,
                    "delivery": (
                        "native_local_image" if native_input else "same_host_path"
                    ),
                    "path": record.local_path,
                }
            )
        envelope = self._attachment_envelope(
            normalized_text or "Inspect and process the attached files.",
            manifest,
        )
        items: list[dict[str, Any]] = [{"type": "text", "text": envelope}]
        for record in attachments:
            if not (
                supports_native_image is True
                and verified_image_by_id[record.attachment_id]
            ):
                continue
            items.append({"type": "localImage", "path": record.local_path})
        return items

    def claim_prompt_attachments_external(
        self,
        client_id: str,
        *,
        scope_key: str,
        attachment_ids: list[str],
    ) -> tuple[
        tuple[WebAttachmentRecord, ...],
        WebAttachmentSubmissionClaimReceipt | None,
    ]:
        """Atomically mark and pin one prompt's exact attachment set off-loop."""

        if not attachment_ids:
            return (), None
        try:
            return self._attachments.claim_pending_submission(
                client_id=client_id,
                scope_key=scope_key,
                attachment_ids=attachment_ids,
            )
        except ValueError as exc:
            raise WebRuntimeError(
                str(exc),
                code="invalid_attachment",
                status=409,
            ) from exc

    def release_prompt_attachment_claim_external(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt | None,
    ) -> None:
        """Keep submitted metadata while releasing one exact file pin."""

        if receipt is not None:
            self._attachments.release_submission_claim(receipt)

    def rollback_prompt_attachment_claim_external(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt | None,
    ) -> tuple[str, ...]:
        """Reopen one exact attachment set after authoritative no-effect."""

        if receipt is None:
            return ()
        return self._attachments.rollback_submission_claim(receipt)

    @staticmethod
    def _attachment_envelope(text: str, manifest: list[dict[str, Any]]) -> str:
        return "\n".join(
            [
                "[[focus.attachments.v1]]",
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                "[[/focus.attachments.v1]]",
                "Entries marked native_local_image are also attached as native local-image inputs. "
                "Every same_host_path entry remains a Focus-managed file that may be inspected with ordinary tools.",
                "[[focus.user_request]]",
                text,
            ]
        )

    def _model_supports_input(
        self,
        effective_model: str | None,
        modality: str,
    ) -> bool | None:
        normalized_model = str(effective_model or "").strip()
        if not normalized_model:
            return None
        try:
            models = self._ports.list_models()
        except Exception:
            logger.warning(
                "Could not refresh model capabilities; native Web attachment input is disabled: model=%s",
                normalized_model,
                exc_info=True,
            )
            return None
        return model_supports_input(models, normalized_model, modality)

    def set_attachments_submitted(
        self,
        attachment_ids: list[str],
        *,
        submitted: bool,
        scope_key: str,
    ) -> None:
        self._runtime_context_guard()
        if not attachment_ids:
            return
        self._attachments.mark_submitted(
            attachment_ids,
            submitted=submitted,
            scope_key=scope_key,
        )

    def rollback_attachments_after_failed_submission(
        self,
        attachment_ids: list[str],
        *,
        scope_key: str,
    ) -> bool:
        self._runtime_context_guard()
        try:
            self.set_attachments_submitted(
                attachment_ids,
                submitted=False,
                scope_key=scope_key,
            )
            return True
        except Exception:
            logger.exception(
                "Failed to restore Web attachments after an unsent mutation: scope=%s",
                scope_key,
            )
            return False

    def attachment_url_for_path(self, local_path: str, *, cwd: str = "") -> str:
        self._runtime_context_guard()
        return self.materialize_attachment_url_for_path(local_path, cwd=cwd)

    def materialize_attachment_url_for_path(
        self,
        local_path: str,
        *,
        cwd: str = "",
    ) -> str:
        """Materialize one observed image on a loop-external projection worker."""

        attachment_id = self._attachments.attachment_id_for_path(local_path)
        if attachment_id:
            try:
                download = self._attachments.download(attachment_id=attachment_id)
            except (KeyError, ValueError):
                return ""
            return (
                self.attachment_url(attachment_id)
                if self._is_web_renderable_image(download.record.media_type)
                else ""
            )
        try:
            record = self._attachments.register_observed_media(
                cwd=cwd or self._default_working_dir,
                local_path=local_path,
            )
        except ValueError:
            return ""
        return (
            self.attachment_url(record.attachment_id)
            if self._is_web_renderable_image(record.media_type)
            else ""
        )

    def is_web_renderable_image(self, media_type: str) -> bool:
        self._runtime_context_guard()
        return self._is_web_renderable_image(media_type)

    def _is_web_renderable_image(self, media_type: str) -> bool:
        normalized = str(media_type or "").strip().lower()
        return normalized.startswith(
            "image/"
        ) and self._attachments.is_native_input_media_type(normalized)

    def remember_prepared_thread_cwd(self, thread_id: str, cwd: str) -> None:
        """Install a cwd already normalized on an external read worker."""

        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        normalized_cwd = str(cwd or "").strip()
        if normalized_thread_id and normalized_cwd:
            self._read_model.remember_cwd(normalized_thread_id, normalized_cwd)

    def remember_thread_cwd(self, thread_id: str, cwd: str) -> None:
        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        normalized_cwd = self.working_dir_key(cwd)
        if normalized_thread_id and normalized_cwd:
            self._read_model.remember_cwd(normalized_thread_id, normalized_cwd)

    def _authoritative_selected_thread_cwd(self, thread_id: str) -> str:
        normalized_thread_id = require_web_thread_id(thread_id)
        cached = self.working_dir_key(self._read_model.cwd(normalized_thread_id))
        if cached:
            return cached
        try:
            snapshot = self._ports.read_thread(normalized_thread_id, False)
        except Exception as exc:
            raise WebRuntimeError(
                "Focus could not verify the selected thread workspace; the draft was not changed.",
                code="thread_cwd_unavailable",
                status=409,
            ) from exc
        resolved = self.working_dir_key(snapshot.summary.cwd)
        if not resolved:
            raise WebRuntimeError(
                "The selected thread has no usable working directory; the draft was not changed.",
                code="invalid_cwd",
                status=409,
            )
        self.remember_thread_cwd(normalized_thread_id, resolved)
        return resolved

    @staticmethod
    def working_dir_key(cwd: str) -> str:
        normalized = str(cwd or "").strip()
        if not normalized:
            return ""
        try:
            return str(pathlib.Path(normalized).expanduser().resolve())
        except (OSError, RuntimeError):
            return ""

    def admit_draft_working_dir(self, cwd: str) -> str:
        self._runtime_context_guard()
        requested = str(cwd or "").strip()
        if not requested:
            raise WebRuntimeError(
                "A draft workspace is required.",
                code="invalid_cwd",
            )
        key = self.working_dir_key(requested)
        if not key:
            raise WebRuntimeError(
                "The selected workspace could not be resolved safely.",
                code="invalid_cwd",
                status=409,
            )
        try:
            workspace = pathlib.Path(key)
            if not workspace.exists():
                raise WebRuntimeError(
                    "The selected workspace does not exist.",
                    code="invalid_cwd",
                    status=409,
                )
            if not workspace.is_dir():
                raise WebRuntimeError(
                    "The selected workspace is not a directory.",
                    code="invalid_cwd",
                    status=409,
                )
            return key
        except WebRuntimeError:
            raise
        except (OSError, RuntimeError) as exc:
            raise WebRuntimeError(
                "The selected workspace could not be resolved safely.",
                code="invalid_cwd",
                status=409,
            ) from exc

    @staticmethod
    def attachment_url(attachment_id: str) -> str:
        normalized = str(attachment_id or "").strip()
        return f"/api/attachments/{quote(normalized, safe='')}" if normalized else ""

    def profile(self, client_id: str) -> WebWriterProfile:
        self._runtime_context_guard()
        return self.load_profile_snapshot(client_id).effective

    def _effective_profile(
        self,
        client_id: str,
        stored: WebWriterProfile | None,
    ) -> WebWriterProfile:
        if stored is not None:
            candidate_working_dir = self.working_dir_key(
                stored.working_dir or self._default_working_dir
            )
            try:
                candidate_is_directory = bool(
                    candidate_working_dir
                    and pathlib.Path(candidate_working_dir).is_dir()
                )
            except OSError:
                candidate_is_directory = False
            remembered_working_dir = (
                candidate_working_dir
                if candidate_is_directory
                else self.working_dir_key(self._default_working_dir)
                or self._default_working_dir
            )
            return WebWriterProfile(
                client_id=stored.client_id,
                selected_thread_id=stored.selected_thread_id,
                working_dir=remembered_working_dir,
                scope_generation=stored.scope_generation,
                updated_at=stored.updated_at,
            )
        return WebWriterProfile(
            client_id=client_id,
            working_dir=self._default_working_dir,
            scope_generation=1,
        )

    @staticmethod
    def profile_payload(profile: WebWriterProfile) -> dict[str, Any]:
        return {
            "selected_thread_id": profile.selected_thread_id,
            "working_dir": profile.working_dir,
            "scope_generation": profile.scope_generation,
        }

    def delete_thread_scope(self, thread_id: str) -> None:
        self._runtime_context_guard()
        self.delete_thread_scope_external(thread_id)

    def delete_thread_scope_external(self, thread_id: str) -> None:
        """Delete one rebuildable attachment scope outside RuntimeLoop."""

        self._attachments.delete_scope(f"thread:{thread_id}")
