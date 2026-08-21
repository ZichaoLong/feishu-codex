"""Single-request existing-thread prompt submission for Focus Web.

RuntimeLoop owns only exact admission and result settlement. Direct metadata
reads, attachment I/O, input construction, and the sole upstream turn effect
run on the service-ingress worker that holds the prepared transaction barrier.
The bounded result registry is process-local, never replays a prompt, and does
not retain prompt text, attachment ids, capabilities, or payload digests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.web_attachment_store import WebAttachmentSubmissionClaimReceipt
from bot.stores.web_next_turn_settings_store import WebNextTurnSettings
from bot.thread_runtime_authority import ThreadStartBlockedByUnsubscribe
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.direct_thread_target_coordinator import (
    require_web_direct_thread_snapshot,
)
from bot.web_runtime.document_registry import WebDocumentRegistry, WebDocumentSnapshot
from bot.web_runtime.gateway_external_transaction import capture_external_failure
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy
from bot.web_runtime.mutation_recovery import is_web_mutation_id
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.thread_mutation_coordinator import (
    require_confirmed_inactive_web_thread,
)
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.writer_workspace_coordinator import (
    WebComposerScopeReceipt,
    WebWriterWorkspaceCoordinator,
    require_connected_web_document,
    require_web_thread_id,
)


logger = logging.getLogger(__name__)

WebPromptResultStatus = Literal[
    "pending",
    "succeeded",
    "known_no_effect",
    "outcome_unknown",
]
WebPromptMode = Literal["start", "steer"]
_PROMPT_ACTIVE_LIMIT = 256
_PROMPT_RESULT_LIMIT = 256


@dataclass(frozen=True, slots=True)
class WebPromptResultReceipt:
    """One bounded exact result; it is evidence, never replay authority."""

    thread_id: str
    mutation_id: str
    client_user_message_id: str
    status: WebPromptResultStatus
    mode: WebPromptMode
    turn_id: str = ""
    reason_code: str = ""

    def projection_dict(self) -> dict[str, str]:
        return {
            "thread_id": self.thread_id,
            "mutation_id": self.mutation_id,
            "client_user_message_id": self.client_user_message_id,
            "status": self.status,
            "mode": self.mode,
            "turn_id": self.turn_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class WebPromptBackendEpochRetirementReceipt:
    """Count of process-local prompt locators invalidated by backend reset."""

    count: int


@dataclass(slots=True)
class _WebPromptAttempt:
    result: WebPromptResultReceipt
    attempt_token: object = field(repr=False)
    executing: bool = False


@dataclass(frozen=True, slots=True)
class _WebPromptSourceIdentity:
    """Exact browser source allowed to reuse one mutation receipt."""

    client_id: str
    thread_id: str
    document_continuity_generation: int
    scope_generation: int
    attachment_scope: str
    composer_scope_id: str


class WebPromptResultRegistry:
    """RuntimeLoop owner of bounded exact prompt attempt/result receipts."""

    def __init__(self, *, runtime_context_guard: RuntimeContextGuard) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web prompt results require a RuntimeLoop context guard")
        self._runtime_context_guard = runtime_context_guard
        self._authority_token = object()
        self._active: dict[str, _WebPromptAttempt] = {}
        self._terminal: dict[str, WebPromptResultReceipt] = {}
        self._source_by_mutation: dict[str, _WebPromptSourceIdentity] = {}

    @property
    def authority_token(self) -> object:
        return self._authority_token

    def begin(
        self,
        *,
        source: _WebPromptSourceIdentity,
        mutation_id: str,
        mode: WebPromptMode,
        turn_id: str,
    ) -> tuple[WebPromptResultReceipt, object | None]:
        """Install one fresh attempt or return the existing exact receipt."""

        self._runtime_context_guard()
        terminal = self._terminal.get(mutation_id)
        if terminal is not None:
            self._require_same_source(terminal, source)
            return terminal, None
        current = self._active.get(mutation_id)
        if current is not None:
            self._require_same_source(current.result, source)
            return current.result, None
        if len(self._active) >= _PROMPT_ACTIVE_LIMIT:
            raise WebRuntimeError(
                "Focus cannot admit another process-local Web prompt receipt.",
                code="prompt_result_capacity",
                status=429,
            )
        attempt_token = object()
        result = WebPromptResultReceipt(
            thread_id=source.thread_id,
            mutation_id=mutation_id,
            client_user_message_id=f"focus-web:{mutation_id}",
            status="pending",
            mode=mode,
            turn_id=turn_id,
        )
        self._active[mutation_id] = _WebPromptAttempt(
            result=result,
            attempt_token=attempt_token,
        )
        self._source_by_mutation[mutation_id] = source
        return result, attempt_token

    def existing(
        self,
        mutation_id: str,
        source: _WebPromptSourceIdentity,
    ) -> WebPromptResultReceipt | None:
        """Return an exact retry before any new admission or upstream work."""

        self._runtime_context_guard()
        normalized_mutation_id = str(mutation_id or "").strip()
        active = self._active.get(normalized_mutation_id)
        result = (
            active.result
            if active is not None
            else self._terminal.get(normalized_mutation_id)
        )
        if result is None:
            return None
        self._require_same_source(result, source)
        return result

    def claim(
        self,
        prepared: WebPromptPreparation,
    ) -> WebPromptResultReceipt | None:
        """Claim the sole upstream effect slot, or return a terminal successor."""

        self._runtime_context_guard()
        self._require_preparation(prepared)
        terminal = self._terminal.get(prepared.mutation_id)
        if terminal is not None:
            return terminal
        current = self._active.get(prepared.mutation_id)
        if (
            current is None
            or current.attempt_token is not prepared._attempt_token
            or current.executing
        ):
            raise WebRuntimeError(
                "This exact Web prompt is already being settled.",
                code="prompt_result_pending",
                status=409,
                details={
                    "thread_id": prepared.thread_id,
                    "mutation_id": prepared.mutation_id,
                },
            )
        current.executing = True
        return None

    def settle(
        self,
        prepared: WebPromptPreparation,
        *,
        status: Literal["succeeded", "known_no_effect", "outcome_unknown"],
        turn_id: str = "",
        reason_code: str = "",
    ) -> WebPromptResultReceipt:
        self._runtime_context_guard()
        self._require_preparation(prepared)
        existing = self._terminal.get(prepared.mutation_id)
        if existing is not None:
            return existing
        current = self._active.get(prepared.mutation_id)
        if current is None or current.attempt_token is not prepared._attempt_token:
            raise WebRuntimeError(
                "This exact Web prompt result receipt is no longer available.",
                code="prompt_result_unavailable",
                status=409,
                details={
                    "thread_id": prepared.thread_id,
                    "mutation_id": prepared.mutation_id,
                },
            )
        self._active.pop(prepared.mutation_id, None)
        result = WebPromptResultReceipt(
            thread_id=prepared.thread_id,
            mutation_id=prepared.mutation_id,
            client_user_message_id=prepared.client_user_message_id,
            status=status,
            mode=prepared.mode,
            turn_id=str(turn_id or prepared.turn_id).strip(),
            reason_code=str(reason_code or "").strip(),
        )
        self._remember_terminal(result)
        return result

    def abandon(self, prepared: WebPromptPreparation) -> WebPromptResultReceipt | None:
        """CAS-retire only a fresh attempt whose worker never claimed effect."""

        self._runtime_context_guard()
        if not isinstance(prepared, WebPromptPreparation):
            raise TypeError("Web prompt preparation is required")
        if prepared._registry_token is not self._authority_token:
            raise ValueError("Web prompt preparation belongs to another registry")
        if prepared._attempt_token is None:
            # A duplicate preparation borrowed an existing result and owns no
            # authority to settle the original request.
            return None
        current = self._active.get(prepared.mutation_id)
        if (
            current is None
            or current.attempt_token is not prepared._attempt_token
            or current.executing
        ):
            return None
        self._active.pop(prepared.mutation_id, None)
        result = WebPromptResultReceipt(
            thread_id=prepared.thread_id,
            mutation_id=prepared.mutation_id,
            client_user_message_id=prepared.client_user_message_id,
            status="known_no_effect",
            mode=prepared.mode,
            turn_id=prepared.turn_id,
            reason_code="request_cancelled_before_effect",
        )
        self._remember_terminal(result)
        return result

    def get(self, thread_id: str, mutation_id: str) -> WebPromptResultReceipt | None:
        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        normalized_mutation_id = str(mutation_id or "").strip()
        active = self._active.get(normalized_mutation_id)
        result = (
            active.result
            if active is not None
            else self._terminal.get(normalized_mutation_id)
        )
        return result if result is not None and result.thread_id == normalized_thread_id else None

    def retire_backend_epoch_after_stop(
        self,
    ) -> WebPromptBackendEpochRetirementReceipt:
        """Invalidate every result tied to the confirmed-stopped backend."""

        self._runtime_context_guard()
        count = len(self._source_by_mutation)
        self._active.clear()
        self._terminal.clear()
        self._source_by_mutation.clear()
        return WebPromptBackendEpochRetirementReceipt(count=count)

    def replace_known_no_effect_reason(
        self,
        prepared: WebPromptPreparation,
        reason_code: str,
    ) -> WebPromptResultReceipt:
        self._runtime_context_guard()
        if prepared._registry_token is not self._authority_token:
            raise ValueError("Web prompt preparation belongs to another registry")
        current = self._terminal.get(prepared.mutation_id)
        if current is None or current.status != "known_no_effect":
            if current is None:
                raise WebRuntimeError(
                    "This Web prompt result is no longer available.",
                    code="prompt_result_unavailable",
                    status=409,
                )
            return current
        updated = WebPromptResultReceipt(
            thread_id=current.thread_id,
            mutation_id=current.mutation_id,
            client_user_message_id=current.client_user_message_id,
            status=current.status,
            mode=current.mode,
            turn_id=current.turn_id,
            reason_code=str(reason_code or "").strip(),
        )
        self._remember_terminal(updated)
        return updated

    def reconcile_turns(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
    ) -> tuple[WebPromptResultReceipt, ...]:
        """Upgrade exact matching client-message evidence to observed success."""

        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        matches: dict[str, str] = {}
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id", "") or "").strip()
            for item in turn.get("items") or ():
                if not isinstance(item, dict) or item.get("type") != "userMessage":
                    continue
                client_message_id = str(item.get("clientId", "") or "").strip()
                if client_message_id.startswith("focus-web:"):
                    matches[client_message_id] = turn_id
        if not matches:
            return ()
        updated: list[WebPromptResultReceipt] = []
        for mutation_id, active in tuple(self._active.items()):
            if active.result.thread_id != normalized_thread_id:
                continue
            turn_id = matches.get(active.result.client_user_message_id)
            if turn_id is None or (active.result.mode == "start" and not turn_id):
                continue
            self._active.pop(mutation_id, None)
            observed = WebPromptResultReceipt(
                thread_id=active.result.thread_id,
                mutation_id=active.result.mutation_id,
                client_user_message_id=active.result.client_user_message_id,
                status="succeeded",
                mode=active.result.mode,
                turn_id=(
                    active.result.turn_id
                    if active.result.mode == "steer"
                    else turn_id or active.result.turn_id
                ),
                reason_code="transcript_observed",
            )
            self._remember_terminal(observed)
            updated.append(observed)
        for _mutation_id, terminal in tuple(self._terminal.items()):
            if (
                terminal.thread_id != normalized_thread_id
                or terminal.status not in {"pending", "outcome_unknown"}
            ):
                continue
            turn_id = matches.get(terminal.client_user_message_id)
            if turn_id is None or (terminal.mode == "start" and not turn_id):
                continue
            observed = WebPromptResultReceipt(
                thread_id=terminal.thread_id,
                mutation_id=terminal.mutation_id,
                client_user_message_id=terminal.client_user_message_id,
                status="succeeded",
                mode=terminal.mode,
                turn_id=(
                    terminal.turn_id
                    if terminal.mode == "steer"
                    else turn_id or terminal.turn_id
                ),
                reason_code="transcript_observed",
            )
            self._remember_terminal(observed)
            updated.append(observed)
        return tuple(updated)

    def _remember_terminal(self, result: WebPromptResultReceipt) -> None:
        self._terminal.pop(result.mutation_id, None)
        self._terminal[result.mutation_id] = result
        while len(self._terminal) > _PROMPT_RESULT_LIMIT:
            evicted = next(iter(self._terminal))
            self._terminal.pop(evicted)
            self._source_by_mutation.pop(evicted, None)

    def _require_same_source(
        self,
        result: WebPromptResultReceipt,
        source: _WebPromptSourceIdentity,
    ) -> None:
        if (
            result.thread_id != source.thread_id
            or self._source_by_mutation.get(result.mutation_id) != source
        ):
            raise self._mutation_conflict(result.mutation_id, source.thread_id)

    @staticmethod
    def _mutation_conflict(mutation_id: str, thread_id: str) -> WebRuntimeError:
        return WebRuntimeError(
            "This mutation id already belongs to another Web prompt attempt.",
            code="prompt_mutation_conflict",
            status=409,
            details={"thread_id": thread_id, "mutation_id": mutation_id},
        )

    def _require_preparation(self, prepared: WebPromptPreparation) -> None:
        if not isinstance(prepared, WebPromptPreparation):
            raise TypeError("Web prompt preparation is required")
        if prepared._registry_token is not self._authority_token:
            raise ValueError("Web prompt preparation belongs to another registry")
        if prepared._attempt_token is None:
            raise ValueError("replayed Web prompt receipt has no effect authority")


@dataclass(frozen=True, slots=True)
class WebPromptPreparation:
    client_id: str
    thread_id: str
    mutation_id: str
    client_user_message_id: str
    text: str = field(repr=False)
    attachment_ids: tuple[str, ...] = field(repr=False)
    composer_scope: WebComposerScopeReceipt = field(repr=False)
    document: WebDocumentSnapshot
    connection_generation: int
    mode: WebPromptMode
    turn_id: str
    _registry_token: object = field(repr=False, compare=False)
    _attempt_token: object | None = field(repr=False, compare=False)

    @property
    def should_execute(self) -> bool:
        return self._attempt_token is not None

    @property
    def source_attachment_scope(self) -> str:
        return self.composer_scope.attachment_scope


@dataclass(frozen=True, slots=True)
class WebPromptSubmissionPorts:
    read_thread: Callable[..., Any]
    get_thread_goal: Callable[..., Any]
    start_turn: Callable[..., dict[str, Any]]
    steer_turn: Callable[..., dict[str, Any]]
    capture_connection_generation: Callable[[], int]
    run_if_connection_generation: Callable[[int, Callable[[], Any]], Any]


class WebPromptSubmissionCoordinator:
    """Own prepare/effect/settle for one existing-thread browser prompt."""

    def __init__(
        self,
        *,
        documents: WebDocumentRegistry,
        workspace: WebWriterWorkspaceCoordinator,
        operations: WebOperationService,
        goal_policy: WebGoalResumePolicy,
        read_model: WebThreadReadModel,
        projection: FocusWebProjection,
        next_turn_settings: Callable[[], WebNextTurnSettings],
        ports: WebPromptSubmissionPorts,
        runtime_context_guard: RuntimeContextGuard,
        runtime_call: Callable[..., Any],
        results: WebPromptResultRegistry | None = None,
    ) -> None:
        if not isinstance(ports, WebPromptSubmissionPorts):
            raise TypeError("Web prompt submission requires typed ports")
        if not callable(runtime_context_guard) or not callable(runtime_call):
            raise TypeError("Web prompt submission requires RuntimeLoop ports")
        self._documents = documents
        self._workspace = workspace
        self._operations = operations
        self._goal_policy = goal_policy
        self._read_model = read_model
        self._projection = projection
        self._next_turn_settings = next_turn_settings
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._runtime_call = runtime_call
        self._results = results or WebPromptResultRegistry(
            runtime_context_guard=runtime_context_guard
        )

    def prepare_prompt(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
        text: str,
        attachment_ids: list[str] | None,
        source_scope_generation: int,
        source_attachment_scope: str,
        source_composer_scope_id: str,
    ) -> WebPromptPreparation:
        """Freeze exact local facts and install one non-replayable attempt."""

        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents, client_id
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        if self._documents.materialized_thread_id(normalized_client_id) != (
            normalized_thread_id
        ):
            raise WebRuntimeError(
                "This browser document has not materialized the requested thread.",
                code="thread_not_materialized",
                status=409,
            )
        if not is_web_mutation_id(mutation_id):
            raise WebRuntimeError(
                "Prompt mutation_id must be one canonical UUID.",
                code="invalid_mutation_id",
                status=400,
            )
        if not isinstance(text, str):
            raise WebRuntimeError(
                "Prompt text must be a string.",
                code="invalid_prompt",
                status=400,
            )
        normalized_text = text.strip()
        normalized_attachment_ids = self._workspace.normalize_attachment_ids(
            attachment_ids
        )
        if not normalized_text and not normalized_attachment_ids:
            raise WebRuntimeError("Prompt or attachment is required.", code="empty_prompt")
        document = self._documents.snapshot(normalized_client_id)
        if document is None:
            raise WebRuntimeError(
                "This browser document is no longer available.",
                code="document_replaced",
                status=409,
            )
        composer_scope = self._workspace.freeze_composer_scope_receipt(
            normalized_client_id,
            thread_id=normalized_thread_id,
            scope_generation=source_scope_generation,
            attachment_scope=source_attachment_scope,
            composer_scope_id=source_composer_scope_id,
        )
        source = _WebPromptSourceIdentity(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            document_continuity_generation=document.continuity_generation,
            scope_generation=composer_scope.scope_generation,
            attachment_scope=composer_scope.attachment_scope,
            composer_scope_id=composer_scope.composer_scope_id,
        )
        existing = self._results.existing(mutation_id, source)
        if existing is not None:
            return WebPromptPreparation(
                client_id=normalized_client_id,
                thread_id=normalized_thread_id,
                mutation_id=mutation_id,
                client_user_message_id=existing.client_user_message_id,
                text=normalized_text,
                attachment_ids=tuple(normalized_attachment_ids),
                composer_scope=composer_scope,
                document=document,
                connection_generation=0,
                mode=existing.mode,
                turn_id=existing.turn_id,
                _registry_token=self._results.authority_token,
                _attempt_token=None,
            )
        self._operations.admit_explicit_web_effect(
            normalized_client_id,
            normalized_thread_id,
            operation="start_prompt",
            mutation_id=mutation_id,
        )
        active_turn_id = self._read_model.active_turn_id_from_turns(
            self._read_model.turns(normalized_thread_id)
        )
        mode: WebPromptMode = "steer" if active_turn_id else "start"
        connection_generation = self._ports.capture_connection_generation()
        # Installing the pending result is deliberately the final fallible
        # prepare step. Once present, the returned preparation gives Gateway's
        # cancellation path the sole exact CAS token needed to retire it.
        result, attempt_token = self._results.begin(
            source=source,
            mutation_id=mutation_id,
            mode=mode,
            turn_id=active_turn_id,
        )
        return WebPromptPreparation(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            mutation_id=mutation_id,
            client_user_message_id=f"focus-web:{mutation_id}",
            text=normalized_text,
            attachment_ids=tuple(normalized_attachment_ids),
            composer_scope=composer_scope,
            document=document,
            connection_generation=connection_generation,
            mode=mode,
            turn_id=active_turn_id,
            _registry_token=self._results.authority_token,
            _attempt_token=attempt_token,
        )

    def abandon_prompt(self, prepared: WebPromptPreparation) -> bool:
        """Retire one unclaimed fresh preparation after ingress abandonment."""

        self._runtime_context_guard()
        return self._results.abandon(prepared) is not None

    def run_prepared_prompt(
        self,
        prepared: WebPromptPreparation,
    ) -> dict[str, str]:
        """Perform metadata/attachment/RPC work outside RuntimeLoop."""

        if not isinstance(prepared, WebPromptPreparation):
            raise TypeError("Web prompt preparation is required")
        if not prepared.should_execute:
            return self._runtime_call(
                self._current_duplicate_result,
                prepared,
            ).projection_dict()

        attachment_claim: WebAttachmentSubmissionClaimReceipt | None = None
        status: Literal["succeeded", "known_no_effect", "outcome_unknown"]
        status = "known_no_effect"
        reason_code = "submission_rejected"
        result_turn_id = prepared.turn_id
        effect_started = False
        fatal_error: BaseException | None = None
        terminal_before_effect: WebPromptResultReceipt | None = None
        try:
            self._workspace.claim_composer_scope_receipt_external(
                prepared.composer_scope
            )
            metadata = None
            settings: WebNextTurnSettings | None = None
            if prepared.mode == "start":
                metadata = self._ports.read_thread(
                    prepared.thread_id,
                    False,
                    expected_connection_generation=prepared.connection_generation,
                )
                require_web_direct_thread_snapshot(
                    metadata,
                    thread_id=prepared.thread_id,
                    operation="发送消息",
                )
                if metadata.summary.status != "active":
                    require_confirmed_inactive_web_thread(
                        metadata.summary.status,
                        operation="start a new prompt",
                    )
                    goal = self._read_goal_external(prepared)
                    if self._goal_policy.requires_writer_admission(goal):
                        raise WebRuntimeError(
                            "This thread has an active or unreviewed persisted goal.",
                            code="goal_continuation_requires_resolution",
                            status=409,
                            details={
                                "thread_id": prepared.thread_id,
                                "goal_status": str(
                                    (goal.status if goal is not None else "") or ""
                                ).strip(),
                                "operation": "start a new prompt",
                            },
                        )
                    settings = self._next_turn_settings()
            attachments, attachment_claim = (
                self._workspace.claim_prompt_attachments_external(
                    prepared.client_id,
                    scope_key=prepared.source_attachment_scope,
                    attachment_ids=list(prepared.attachment_ids),
                )
            )
            input_items = self._workspace.prompt_input_items_external(
                prepared.text,
                attachments,
                thread_id=prepared.thread_id,
                requested_model=(settings.model if settings is not None else ""),
            )
            terminal_before_effect = self._runtime_call(
                self._claim_prompt_effect,
                prepared,
                metadata,
            )
            if terminal_before_effect is not None:
                status = (
                    terminal_before_effect.status
                    if terminal_before_effect.status != "pending"
                    else "outcome_unknown"
                )
                reason_code = terminal_before_effect.reason_code
                result_turn_id = terminal_before_effect.turn_id
            else:
                effect_started = True
                if prepared.mode == "steer":
                    response = self._ports.steer_turn(
                        thread_id=prepared.thread_id,
                        expected_turn_id=prepared.turn_id,
                        input_items=input_items,
                        client_user_message_id=prepared.client_user_message_id,
                        expected_connection_generation=(
                            prepared.connection_generation
                        ),
                    )
                    returned_turn_id = str(
                        response.get("turnId", response.get("turn_id", "")) or ""
                    ).strip()
                    if returned_turn_id != prepared.turn_id:
                        status = "outcome_unknown"
                        reason_code = "steer_result_turn_mismatch"
                    else:
                        result_turn_id = prepared.turn_id
                else:
                    response = self._ports.start_turn(
                        thread_id=prepared.thread_id,
                        input_items=input_items,
                        cwd=metadata.summary.cwd or None,
                        model=(settings.model or None if settings is not None else None),
                        approval_policy=(
                            settings.approval_policy if settings is not None else None
                        ),
                        permissions_profile_id=(
                            settings.permissions_profile_id
                            if settings is not None
                            else None
                        ),
                        reasoning_effort=(
                            settings.reasoning_effort or None
                            if settings is not None
                            else None
                        ),
                        client_user_message_id=prepared.client_user_message_id,
                        expected_connection_generation=(
                            prepared.connection_generation
                        ),
                    )
                    turn = (
                        response.get("turn")
                        if isinstance(response.get("turn"), dict)
                        else {}
                    )
                    result_turn_id = str(turn.get("id", "") or "").strip()
                    if not result_turn_id:
                        status = "outcome_unknown"
                        reason_code = "start_result_turn_missing"
                if status == "outcome_unknown":
                    pass
                elif self._operations.upstream_outcome_unknown(response):
                    status = "outcome_unknown"
                    reason_code = "upstream_outcome_unknown"
                    if prepared.mode == "start":
                        result_turn_id = ""
                else:
                    status = "succeeded"
                    reason_code = "upstream_acknowledged"
        except BaseException as raw_exc:
            exc, fatal_error = capture_external_failure(raw_exc, "Web prompt")
            if effect_started and self._effect_failure_known_no_effect(exc):
                status = "known_no_effect"
                reason_code = self._known_no_effect_reason(exc, prepared.mode)
            elif effect_started:
                status = "outcome_unknown"
                reason_code = "upstream_result_unknown"
            else:
                status = "known_no_effect"
                reason_code = self._known_no_effect_reason(exc, prepared.mode)

        if terminal_before_effect is not None:
            restored = self._settle_attachment_claim(
                attachment_claim,
                known_no_effect=(terminal_before_effect.status == "known_no_effect"),
                expected_attachment_count=len(prepared.attachment_ids),
            )
            if not restored and terminal_before_effect.status == "known_no_effect":
                terminal_before_effect = self._runtime_call(
                    self._replace_known_no_effect_reason,
                    prepared,
                    reason_code="attachment_rollback_failed",
                )
            receipt = terminal_before_effect
        else:
            restored = self._settle_attachment_claim(
                attachment_claim,
                known_no_effect=(status == "known_no_effect"),
                expected_attachment_count=len(prepared.attachment_ids),
            )
            if status == "known_no_effect" and not restored:
                reason_code = "attachment_rollback_failed"
            receipt = self._runtime_call(
                self._settle_prompt,
                prepared,
                status=status,
                turn_id=result_turn_id,
                reason_code=reason_code,
            )
        if fatal_error is not None:
            raise fatal_error
        return receipt.projection_dict()

    def prompt_result(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
    ) -> dict[str, str]:
        """Read one exact process-local result without dispatching any work."""

        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents, client_id
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        if self._documents.materialized_thread_id(normalized_client_id) != (
            normalized_thread_id
        ):
            raise WebRuntimeError(
                "Open this exact thread before reading its prompt result.",
                code="thread_not_materialized",
                status=409,
            )
        if not is_web_mutation_id(mutation_id):
            raise WebRuntimeError(
                "Prompt mutation_id must be one canonical UUID.",
                code="invalid_mutation_id",
                status=400,
            )
        result = self._results.get(normalized_thread_id, mutation_id)
        if result is None:
            raise WebRuntimeError(
                "This process-local Web prompt result is no longer available.",
                code="prompt_result_unavailable",
                status=404,
                details={
                    "thread_id": normalized_thread_id,
                    "mutation_id": mutation_id,
                },
            )
        return result.projection_dict()

    def retire_backend_epoch_after_stop(
        self,
    ) -> WebPromptBackendEpochRetirementReceipt:
        self._runtime_context_guard()
        return self._results.retire_backend_epoch_after_stop()

    def reconcile_prompt_results_from_turns(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
    ) -> bool:
        self._runtime_context_guard()
        results = self._results.reconcile_turns(thread_id, turns)
        if not results:
            return False
        try:
            self._projection.publish(
                "thread_invalidated",
                thread_id=thread_id,
                reason="web_prompt_result_observed",
            )
        except Exception:
            logger.exception("Unable to publish observed Web prompt result")
        return True

    def _claim_prompt_effect(
        self,
        prepared: WebPromptPreparation,
        metadata: Any | None,
    ) -> WebPromptResultReceipt | None:
        self._runtime_context_guard()

        def claim_current_generation() -> WebPromptResultReceipt | None:
            current_document = self._documents.snapshot(prepared.client_id)
            if (
                current_document is None
                or not current_document.connected
                or current_document.continuity_generation
                != prepared.document.continuity_generation
                or current_document.materialized_thread_id != prepared.thread_id
            ):
                raise WebRuntimeError(
                    "This browser document was replaced before the prompt was sent.",
                    code="document_replaced",
                    status=409,
                )
            if metadata is not None:
                self._workspace.remember_prepared_thread_cwd(
                    prepared.thread_id,
                    metadata.summary.cwd,
                )
            return self._results.claim(prepared)

        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            claim_current_generation,
        )

    def _current_duplicate_result(
        self,
        prepared: WebPromptPreparation,
    ) -> WebPromptResultReceipt:
        self._runtime_context_guard()
        current = self._results.get(prepared.thread_id, prepared.mutation_id)
        if current is None:
            raise WebRuntimeError(
                "This process-local Web prompt result is no longer available.",
                code="prompt_result_unavailable",
                status=404,
                details={
                    "thread_id": prepared.thread_id,
                    "mutation_id": prepared.mutation_id,
                },
            )
        return current

    def _settle_prompt(
        self,
        prepared: WebPromptPreparation,
        *,
        status: Literal["succeeded", "known_no_effect", "outcome_unknown"],
        turn_id: str,
        reason_code: str,
    ) -> WebPromptResultReceipt:
        self._runtime_context_guard()
        receipt = self._results.settle(
            prepared,
            status=status,
            turn_id=turn_id,
            reason_code=reason_code,
        )
        try:
            self._projection.publish(
                "thread_invalidated",
                thread_id=prepared.thread_id,
                reason=f"web_prompt_{receipt.status}",
            )
        except Exception:
            logger.exception(
                "Unable to publish Web prompt settlement: thread=%s",
                prepared.thread_id[:12],
            )
        return receipt

    def _replace_known_no_effect_reason(
        self,
        prepared: WebPromptPreparation,
        *,
        reason_code: str,
    ) -> WebPromptResultReceipt:
        self._runtime_context_guard()
        return self._results.replace_known_no_effect_reason(
            prepared,
            reason_code,
        )

    def _read_goal_external(self, prepared: WebPromptPreparation) -> Any:
        try:
            return self._ports.get_thread_goal(
                prepared.thread_id,
                expected_connection_generation=prepared.connection_generation,
            )
        except CodexRpcError as exc:
            message = str(exc.error.get("message", "") or "").strip().lower()
            if message == "goals feature is disabled":
                return None
            raise WebRuntimeError(
                "Focus could not safely determine whether this thread has a resumable goal.",
                code="goal_state_unconfirmed",
                status=409,
                details={
                    "thread_id": prepared.thread_id,
                    "operation": "start a new prompt",
                },
            ) from exc
        except Exception as exc:
            raise WebRuntimeError(
                "Focus could not safely determine whether this thread has a resumable goal.",
                code="goal_state_unconfirmed",
                status=409,
                details={
                    "thread_id": prepared.thread_id,
                    "operation": "start a new prompt",
                },
            ) from exc

    def _settle_attachment_claim(
        self,
        receipt: WebAttachmentSubmissionClaimReceipt | None,
        *,
        known_no_effect: bool,
        expected_attachment_count: int,
    ) -> bool:
        if receipt is None:
            return True
        try:
            if known_no_effect:
                restored = self._workspace.rollback_prompt_attachment_claim_external(
                    receipt
                )
                return len(restored) == expected_attachment_count
            else:
                self._workspace.release_prompt_attachment_claim_external(receipt)
                return True
        except Exception:
            # The upstream result is already classified. Local cache cleanup
            # cannot rewrite success/no-effect/unknown evidence.
            logger.exception("Unable to settle exact Web prompt attachment claim")
            return not known_no_effect

    @staticmethod
    def _effect_failure_known_no_effect(exc: Exception) -> bool:
        if isinstance(exc, (CodexRpcPreSendError, ThreadStartBlockedByUnsubscribe)):
            return True
        if isinstance(exc, (CodexRpcTransportError, CodexRpcProtocolError, TimeoutError)):
            return False
        return isinstance(exc, CodexRpcError)

    @classmethod
    def _known_no_effect_reason(
        cls,
        exc: Exception,
        mode: WebPromptMode,
    ) -> str:
        if isinstance(exc, WebRuntimeError):
            return exc.code
        if isinstance(exc, CodexRpcError):
            if mode == "steer":
                race = cls._steer_race(exc)
                if race is not None:
                    return "active_turn_changed"
                if cls._active_turn_not_steerable(exc):
                    return "turn_not_steerable"
            return "upstream_rejected"
        return "submission_rejected" if mode == "start" else "steer_not_sent"

    @staticmethod
    def _steer_race(error: CodexRpcError) -> tuple[str, str] | None:
        message = str(error.error.get("message", "") or "")
        if message == "no active turn to steer":
            return "missing", ""
        prefix = "expected active turn id `"
        separator = "` but found `"
        if not message.startswith(prefix) or separator not in message:
            return None
        actual = message.split(separator, 1)[1].removesuffix("`").strip()
        return ("mismatch", actual) if actual else None

    @staticmethod
    def _active_turn_not_steerable(error: CodexRpcError) -> bool:
        data = error.error.get("data")
        if isinstance(data, dict):
            info = data.get("codexErrorInfo")
            if isinstance(info, dict) and "activeTurnNotSteerable" in info:
                return True
        return str(error.error.get("message", "") or "") in {
            "cannot steer a review turn",
            "cannot steer a compact turn",
        }
