"""Web thread-create and first-turn transaction owner.

This coordinator owns the complete product transaction from one browser draft
through ``thread/start``, local projection, first-turn preparation,
``turn/start``, and typed known/unknown results. State remains in its existing
owners; this module only fixes their ordering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_next_turn_settings_store import WebNextTurnSettings
from bot.thread_create_transaction import (
    CommittedThreadCreate,
    ThreadCreateLocalCommitFailed,
    ThreadCreateOutcomeUnknown,
)
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.contract import WebRuntimeError, new_web_client_user_message_id
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.lifecycle_coordinator import WebRuntimeLifecycleCoordinator
from bot.web_runtime.writer_workspace_coordinator import (
    WebWriterWorkspaceCoordinator,
    accept_web_document_intent,
    require_connected_web_document,
    require_web_thread_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebThreadCreatePorts:
    """External-effect ports; state owners are explicit constructor inputs."""

    create_and_commit_thread: Callable[
        ...,
        CommittedThreadCreate[ThreadSnapshot, str],
    ]
    start_turn: Callable[..., dict[str, Any]]

class WebThreadCreateCoordinator:
    """Run one exact Web create and first-turn transaction on RuntimeLoop."""

    def __init__(
        self,
        *,
        documents: WebDocumentRegistry,
        workspace: WebWriterWorkspaceCoordinator,
        next_turn_settings: Callable[[], WebNextTurnSettings],
        operations: WebOperationService,
        lifecycle: WebRuntimeLifecycleCoordinator,
        remember_direct_thread_summary: Callable[[ThreadSummary], None],
        runtime_interest: WebRuntimeInterestRegistry,
        projection: FocusWebProjection,
        ports: WebThreadCreatePorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(workspace, WebWriterWorkspaceCoordinator):
            raise TypeError("Web thread create requires the workspace owner")
        if not callable(next_turn_settings):
            raise TypeError("Web thread create requires next-turn settings")
        if not isinstance(operations, WebOperationService):
            raise TypeError("Web thread create requires the operation owner")
        if not isinstance(ports, WebThreadCreatePorts):
            raise TypeError("Web thread create requires typed ports")
        if not callable(runtime_context_guard):
            raise TypeError("Web thread create requires a RuntimeLoop context guard")
        self._documents = documents
        self._workspace = workspace
        self._next_turn_settings = next_turn_settings
        self._operations = operations
        self._lifecycle = lifecycle
        self._remember_direct_thread_summary = remember_direct_thread_summary
        self._runtime_interest = runtime_interest
        self._projection = projection
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def start_thread(
        self,
        client_id: str,
        *,
        text: str,
        cwd: str = "",
        attachment_ids: list[str] | None = None,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        """Create a thread, commit its Web owner, and start its first turn."""

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
        profile = self._workspace.profile(normalized_client_id)
        normalized_text = str(text or "").strip()
        normalized_attachment_ids = self._workspace.normalize_attachment_ids(
            attachment_ids
        )
        if not normalized_text and not normalized_attachment_ids:
            raise WebRuntimeError(
                "Prompt or attachment is required.",
                code="empty_prompt",
            )
        normalized_cwd = str(
            cwd or profile.working_dir or self._workspace.default_working_dir or ""
        ).strip()
        if not normalized_cwd:
            raise WebRuntimeError(
                "A working directory is required.",
                code="invalid_cwd",
            )
        scope_key, normalized_cwd = self._workspace.attachment_scope(
            normalized_client_id,
            cwd=normalized_cwd,
        )
        attachment_records = self._workspace.resolve_attachments(
            normalized_client_id,
            scope_key=scope_key,
            attachment_ids=normalized_attachment_ids,
        )
        settings = self._next_turn_settings()
        selected_model = settings.model
        selected_effort = settings.reasoning_effort
        config_overrides = (
            {"model_reasoning_effort": selected_effort} if selected_effort else None
        )

        def commit_web_projection(snapshot: ThreadSnapshot) -> str:
            """Return the exact created id without creating a temporary owner."""

            return require_web_thread_id(snapshot.summary.thread_id)

        try:
            try:
                created = self._ports.create_and_commit_thread(
                    local_commit=commit_web_projection,
                    cwd=normalized_cwd,
                    config_overrides=config_overrides,
                    model=selected_model or None,
                    approval_policy=settings.approval_policy,
                    permissions_profile_id=settings.permissions_profile_id,
                )
            except Exception:
                raise
        except ThreadCreateOutcomeUnknown as exc:
            raise WebRuntimeError(
                "Codex may have created the thread, but Focus did not receive a reliable result. "
                "Refresh the global thread list before deciding whether to create again.",
                code="mutation_unknown",
                status=409,
                details={
                    "operation": "thread_create",
                    "attempt_id": exc.attempt_id,
                },
            ) from exc
        except ThreadCreateLocalCommitFailed as exc:
            raise WebRuntimeError(
                "Codex created the thread, but Focus could not finish its local Web setup. "
                "Open the created thread from the global thread list before retrying the draft.",
                code="thread_create_local_commit_failed",
                status=503,
                details={
                    "operation": "thread_create",
                    "attempt_id": exc.attempt_id,
                    "thread_id": exc.thread_id,
                    "stage": exc.stage,
                },
            ) from exc
        except Exception as exc:
            if self._operations.is_unknown_mutation_error(exc):
                raise WebRuntimeError(
                    "Codex may have created the thread, but Focus did not receive "
                    "a reliable result. Refresh the global thread list before "
                    "deciding whether to create again.",
                    code="mutation_unknown",
                    status=409,
                    details={
                        "operation": "thread_create",
                        "attempt_id": str(getattr(exc, "attempt_id", "") or ""),
                    },
                ) from exc
            raise

        snapshot = created.response
        thread_id = require_web_thread_id(snapshot.summary.thread_id)
        if created.local_result != thread_id:
            raise RuntimeError("thread/create returned a different Web thread identity")

        self._project_committed_web_create(
            normalized_client_id,
            snapshot,
            fallback_cwd=normalized_cwd,
        )
        try:
            input_items = self._workspace.prompt_input_items(
                normalized_text,
                attachment_records,
                thread_id=thread_id,
                requested_model=selected_model,
            )
        except Exception as exc:
            logger.exception(
                "Web owner committed but first-turn input preparation failed: thread=%s",
                thread_id[:12],
            )
            # The create is committed to this document.  Preserve the draft on
            # that exact thread so ``start_prompt`` can retry without another
            # create operation.
            raise self._known_first_prompt_failure_error(
                normalized_attachment_ids,
                thread_id=thread_id,
                restored_message=(
                    "The Codex thread was created, but its first turn did not start. "
                    "Focus kept the thread and draft there; do not create another thread."
                ),
            ) from exc

        client_user_message_id = new_web_client_user_message_id()
        try:
            self._workspace.set_attachments_submitted(
                normalized_attachment_ids,
                submitted=True,
                scope_key=f"thread:{thread_id}",
            )
            # This is ordinary upstream-routed input.  Like an existing-thread
            # prompt, it does not read or create a shared main-turn lease; the
            # upstream turn/start call retains its native start-or-steer race.
            self._ports.start_turn(
                thread_id=thread_id,
                input_items=input_items,
                cwd=snapshot.summary.cwd or normalized_cwd,
                model=selected_model or None,
                approval_policy=settings.approval_policy,
                permissions_profile_id=settings.permissions_profile_id,
                reasoning_effort=selected_effort or None,
                client_user_message_id=client_user_message_id,
            )
        except Exception as exc:
            if self._operations.is_unknown_mutation_error(exc):
                raise WebRuntimeError(
                    "Codex may have accepted this first prompt, but Focus did not receive "
                    "its result. Focus will not replay it automatically; inspect the "
                    "created thread before submitting anything else.",
                    code="turn_submission_unknown",
                    status=503,
                    details={"thread_id": thread_id, "operation": "prompt"},
                ) from exc
            raise self._known_first_prompt_failure_error(
                normalized_attachment_ids,
                thread_id=thread_id,
                restored_message=(
                    "The Codex thread was created, but its first turn did not start. "
                    "Focus kept the empty thread and restored the draft there."
                ),
            ) from exc

        try:
            self._projection.publish(
                "thread_invalidated",
                thread_id=thread_id,
                reason="web_thread_started",
            )
        except Exception:
            logger.exception(
                "Started Web turn projection failed: thread=%s",
                thread_id[:12],
            )
        return {
            "accepted": True,
            "mode": "started",
            "thread_id": thread_id,
            "turn_id": "",
        }

    def _known_first_prompt_failure_error(
        self,
        attachment_ids: list[str],
        *,
        thread_id: str,
        restored_message: str,
    ) -> WebRuntimeError:
        attachments_restored = not attachment_ids
        if attachment_ids:
            try:
                attachments_restored = (
                    self._workspace.rollback_attachments_after_failed_submission(
                        attachment_ids,
                        scope_key=f"thread:{thread_id}",
                    )
                    is True
                )
            except Exception:
                logger.exception(
                    "Failed to restore attachments after first-turn failure: thread=%s",
                    thread_id[:12],
                )
        attachment_disposition = (
            "restored" if attachments_restored else "reupload_required"
        )
        message = restored_message
        if not attachments_restored:
            message = (
                "The Codex thread was created, but its first turn did not start "
                "and Focus could not restore its draft attachments. Open the "
                "created thread, remove the old attachments, and upload them again."
            )
        return WebRuntimeError(
            message,
            code="thread_created_turn_not_started",
            status=409,
            details={
                "thread_id": thread_id,
                "attachment_disposition": attachment_disposition,
            },
        )

    def _project_committed_web_create(
        self,
        client_id: str,
        snapshot: ThreadSnapshot,
        *,
        fallback_cwd: str,
    ) -> None:
        """Best-effort projections after the durable Web owner has committed."""

        thread_id = require_web_thread_id(snapshot.summary.thread_id)

        def select_thread() -> None:
            selection = self._workspace.select_thread(client_id, thread_id)
            self._lifecycle.settle_runtime_cleanup_candidates(
                selection.runtime_cleanup_thread_ids
            )

        steps: tuple[tuple[str, Callable[[], None]], ...] = (
            (
                "direct thread summary",
                lambda: self._remember_direct_thread_summary(snapshot.summary),
            ),
            (
                "thread cwd cache",
                lambda: self._workspace.remember_thread_cwd(
                    thread_id,
                    snapshot.summary.cwd or fallback_cwd,
                ),
            ),
            ("Web selection", select_thread),
            (
                "runtime interest",
                lambda: self._runtime_interest.mark_confirmed(
                    thread_id,
                    client_id=client_id,
                ),
            ),
            (
                "owner projection",
                lambda: self._projection.publish(
                    "owner_changed",
                    thread_id=thread_id,
                    reason="web_thread_created",
                ),
            ),
        )
        for step, action in steps:
            try:
                action()
            except Exception:
                logger.exception(
                    "Committed Web create projection failed: step=%s thread=%s",
                    step,
                    thread_id[:12],
                )
