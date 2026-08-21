"""Immutable commands and receipts for the binding-runtime owner boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from bot.binding_identity import ChatBindingKey
from bot.execution_pages import ExecutionPageLedger
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.runtime_state import FEISHU_RUNTIME_ATTACHED

if TYPE_CHECKING:
    from bot.binding_runtime_lifecycle import RuntimeTimerCancellationEffect


OwnerLossDisposition: TypeAlias = Literal["abandon", "terminal"]
OWNER_LOSS_DISPOSITION_ABANDON: OwnerLossDisposition = "abandon"
OWNER_LOSS_DISPOSITION_TERMINAL: OwnerLossDisposition = "terminal"


def _require_binding_key(value: object, *, field: str) -> None:
    if (
        type(value) is not tuple
        or len(value) != 2
        or any(type(component) is not str or not component for component in value)
    ):
        raise TypeError(f"{field} must be an exact pair of non-empty strings")


def _require_exact_string(value: object, *, field: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")


def _require_exact_bool(value: object, *, field: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{field} must be bool")


def _require_exact_int(
    value: object,
    *,
    field: str,
    optional: bool = False,
    positive: bool = False,
) -> None:
    if optional and value is None:
        return
    if type(value) is not int:
        raise TypeError(f"{field} must be an exact integer")
    minimum = 1 if positive else 0
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")


def _require_exact_float(value: object, *, field: str) -> None:
    if type(value) is not float:
        raise TypeError(f"{field} must be an exact float")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


def _require_string_tuple(value: object, *, field: str) -> None:
    if type(value) is not tuple or any(type(item) is not str for item in value):
        raise TypeError(f"{field} must be an exact tuple of exact strings")


@dataclass(frozen=True, slots=True, eq=False)
class BindingRuntimeHandle:
    """Opaque identity for one exact resident binding-runtime incarnation.

    Diagnostic fields do not make this a value token.  A manager may authorize
    only the exact object it issued; copied or reconstructed handles therefore
    remain distinct even when every displayed field is equal.
    """

    _issuer_nonce: int
    binding: ChatBindingKey
    incarnation: int

    def __post_init__(self) -> None:
        _require_exact_int(
            self._issuer_nonce,
            field="binding runtime handle issuer",
            positive=True,
        )
        _require_binding_key(self.binding, field="binding runtime handle binding")
        _require_exact_int(
            self.incarnation,
            field="binding runtime handle incarnation",
            positive=True,
        )


@dataclass(frozen=True, slots=True)
class BindingThreadSnapshot:
    """Immutable thread-binding fields of one resident session."""

    working_dir: str
    thread_id: str
    title: str
    feishu_runtime_state: str

    def __post_init__(self) -> None:
        for name, value in (
            ("working_dir", self.working_dir),
            ("thread_id", self.thread_id),
            ("title", self.title),
            ("feishu_runtime_state", self.feishu_runtime_state),
        ):
            _require_exact_string(value, field=f"binding thread {name}")

    @property
    def has_thread(self) -> bool:
        return bool(self.thread_id)

    @property
    def feishu_runtime_attached(self) -> bool:
        return self.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED and self.has_thread


@dataclass(frozen=True, slots=True)
class BindingRuntimeSettingsSnapshot:
    """Immutable effective runtime settings and their explicit overrides."""

    approval_policy: str
    permissions_profile_id: str
    model: str
    reasoning_effort: str
    configured_settings: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("approval_policy", self.approval_policy),
            ("permissions_profile_id", self.permissions_profile_id),
            ("model", self.model),
            ("reasoning_effort", self.reasoning_effort),
        ):
            _require_exact_string(value, field=f"binding runtime settings {name}")
        _require_string_tuple(
            self.configured_settings,
            field="binding runtime configured_settings",
        )
        if len(set(self.configured_settings)) != len(self.configured_settings):
            raise ValueError("binding runtime configured_settings must be unique")


@dataclass(frozen=True, slots=True)
class BindingGoalSnapshot:
    """Immutable projection of the current thread goal."""

    objective: str
    status: str
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        _require_exact_string(self.objective, field="binding goal objective")
        _require_exact_string(self.status, field="binding goal status")
        _require_exact_int(
            self.token_budget,
            field="binding goal token_budget",
            optional=True,
            positive=True,
        )
        for name, value in (
            ("tokens_used", self.tokens_used),
            ("time_used_seconds", self.time_used_seconds),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            _require_exact_int(value, field=f"binding goal {name}")

    @property
    def exists(self) -> bool:
        return bool(self.objective)


@dataclass(frozen=True, slots=True)
class BindingExecutionSnapshot:
    """Immutable execution, correlation, transcript, and timer facts.

    Timer implementations and their authority tickets stay private to the
    runtime owner.  The session vocabulary exposes only whether each exact
    registration exists; timer commands use separate opaque receipts.
    """

    running: bool
    cancelled: bool
    pending_cancel: bool
    current_turn_id: str
    pages: ExecutionPageLedger
    current_execution_kind: str
    current_prompt_message_id: str
    current_prompt_reply_in_thread: bool
    current_actor_open_id: str
    transcript: ExecutionTranscriptSnapshot
    runtime_channel_state: str
    started_at: float
    last_runtime_event_at: float
    last_patch_at: float
    patch_timer_registered: bool
    mirror_watchdog_registered: bool
    followup_sent: bool
    followup_text: str
    terminal_result_text: str
    awaiting_local_turn_started: bool
    awaiting_attach_status_settle: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("running", self.running),
            ("cancelled", self.cancelled),
            ("pending_cancel", self.pending_cancel),
            ("current_prompt_reply_in_thread", self.current_prompt_reply_in_thread),
            ("patch_timer_registered", self.patch_timer_registered),
            ("mirror_watchdog_registered", self.mirror_watchdog_registered),
            ("followup_sent", self.followup_sent),
            ("awaiting_local_turn_started", self.awaiting_local_turn_started),
            ("awaiting_attach_status_settle", self.awaiting_attach_status_settle),
        ):
            _require_exact_bool(value, field=f"binding execution {name}")
        if type(self.pages) is not ExecutionPageLedger:
            raise TypeError("binding execution pages must be an exact ledger")
        for name, value in (
            ("current_turn_id", self.current_turn_id),
            ("current_execution_kind", self.current_execution_kind),
            ("current_prompt_message_id", self.current_prompt_message_id),
            ("current_actor_open_id", self.current_actor_open_id),
            ("runtime_channel_state", self.runtime_channel_state),
            ("followup_text", self.followup_text),
            ("terminal_result_text", self.terminal_result_text),
        ):
            _require_exact_string(value, field=f"binding execution {name}")
        if type(self.transcript) is not ExecutionTranscriptSnapshot:
            raise TypeError(
                "binding execution transcript must be an exact transcript snapshot"
            )
        for name, value in (
            ("started_at", self.started_at),
            ("last_runtime_event_at", self.last_runtime_event_at),
            ("last_patch_at", self.last_patch_at),
        ):
            _require_exact_float(value, field=f"binding execution {name}")

    @property
    def current_message_id(self) -> str:
        return self.pages.current_message_id

    @property
    def last_execution_message_id(self) -> str:
        return self.pages.last_message_id

    @property
    def effective_message_id(self) -> str:
        return self.pages.effective_message_id

    @property
    def has_execution_anchor(self) -> bool:
        has_runtime_identity = bool(
            self.running
            or self.awaiting_local_turn_started
            or self.current_turn_id
        )
        return (
            self.pages.has_unresolved_send
            or (self.pages.active_page is not None and has_runtime_identity)
            or (
                self.running
                and (
                    self.awaiting_local_turn_started
                    or bool(self.current_turn_id)
                )
            )
        )


@dataclass(frozen=True, slots=True)
class BindingPlanStepSnapshot:
    """Immutable named execution-plan step."""

    step: str
    status: str

    def __post_init__(self) -> None:
        _require_exact_string(self.step, field="binding plan step text")
        _require_exact_string(self.status, field="binding plan step status")


@dataclass(frozen=True, slots=True)
class BindingPlanSnapshot:
    """Immutable execution-plan projection."""

    message_id: str
    turn_id: str
    explanation: str
    steps: tuple[BindingPlanStepSnapshot, ...]
    text: str

    def __post_init__(self) -> None:
        for name, value in (
            ("message_id", self.message_id),
            ("turn_id", self.turn_id),
            ("explanation", self.explanation),
            ("text", self.text),
        ):
            _require_exact_string(value, field=f"binding plan {name}")
        if type(self.steps) is not tuple or any(
            type(step) is not BindingPlanStepSnapshot for step in self.steps
        ):
            raise TypeError(
                "binding plan steps must be an exact tuple of exact step snapshots"
            )


@dataclass(frozen=True, slots=True)
class BindingSessionSnapshot:
    """Deeply immutable read model for one exact resident session."""

    handle: BindingRuntimeHandle
    active: bool
    thread: BindingThreadSnapshot
    settings: BindingRuntimeSettingsSnapshot
    goal: BindingGoalSnapshot
    execution: BindingExecutionSnapshot
    plan: BindingPlanSnapshot

    def __post_init__(self) -> None:
        if type(self.handle) is not BindingRuntimeHandle:
            raise TypeError("binding session handle must be an exact runtime handle")
        _require_exact_bool(self.active, field="binding session active")
        for name, value, expected in (
            ("thread", self.thread, BindingThreadSnapshot),
            ("settings", self.settings, BindingRuntimeSettingsSnapshot),
            ("goal", self.goal, BindingGoalSnapshot),
            ("execution", self.execution, BindingExecutionSnapshot),
            ("plan", self.plan, BindingPlanSnapshot),
        ):
            if type(value) is not expected:
                raise TypeError(
                    f"binding session {name} must be an exact {expected.__name__}"
                )

    @property
    def binding(self) -> ChatBindingKey:
        return self.handle.binding

    @property
    def working_dir(self) -> str:
        return self.thread.working_dir

    @property
    def current_thread_id(self) -> str:
        return self.thread.thread_id

    @property
    def current_thread_title(self) -> str:
        return self.thread.title

    @property
    def running(self) -> bool:
        return self.execution.running

    @property
    def approval_policy(self) -> str:
        return self.settings.approval_policy

    @property
    def permissions_profile_id(self) -> str:
        return self.settings.permissions_profile_id

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def reasoning_effort(self) -> str:
        return self.settings.reasoning_effort


@dataclass(frozen=True, slots=True)
class BindingExecutionTarget:
    """Exact handle plus the stable business fence for one execution."""

    handle: BindingRuntimeHandle
    expected_thread_id: str
    expected_turn_id: str
    expected_pages: ExecutionPageLedger
    expected_prompt_message_id: str
    expected_execution_kind: str
    expected_started_at: float

    def __post_init__(self) -> None:
        if type(self.handle) is not BindingRuntimeHandle:
            raise TypeError(
                "binding execution target handle must be an exact runtime handle"
            )
        for name, value in (
            ("thread_id", self.expected_thread_id),
            ("turn_id", self.expected_turn_id),
            ("prompt_message_id", self.expected_prompt_message_id),
            ("execution_kind", self.expected_execution_kind),
        ):
            _require_exact_string(
                value,
                field=f"binding execution target {name}",
            )
        if type(self.expected_pages) is not ExecutionPageLedger:
            raise TypeError("binding execution target pages must be an exact ledger")
        _require_exact_float(
            self.expected_started_at,
            field="binding execution target started_at",
        )

    @classmethod
    def from_session(
        cls,
        session: BindingSessionSnapshot,
    ) -> BindingExecutionTarget:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError("binding execution target requires an exact session")
        execution = session.execution
        return cls(
            handle=session.handle,
            expected_thread_id=session.current_thread_id,
            expected_turn_id=execution.current_turn_id,
            expected_pages=execution.pages,
            expected_prompt_message_id=execution.current_prompt_message_id,
            expected_execution_kind=execution.current_execution_kind,
            expected_started_at=execution.started_at,
        )

    @property
    def binding(self) -> ChatBindingKey:
        return self.handle.binding

    def matches(self, session: BindingSessionSnapshot) -> bool:
        if type(session) is not BindingSessionSnapshot:
            return False
        execution = session.execution
        return bool(
            session.handle is self.handle
            and session.current_thread_id == self.expected_thread_id
            and execution.current_turn_id == self.expected_turn_id
            and execution.pages is self.expected_pages
            and execution.current_prompt_message_id
            == self.expected_prompt_message_id
            and execution.current_execution_kind
            == self.expected_execution_kind
            and execution.started_at == self.expected_started_at
        )


@dataclass(frozen=True, slots=True, eq=False)
class BindingOwnerRevisionReceipt:
    """Manager-issued identity for one exact binding-owner revision.

    The issuing manager retains the exact object in its current generation.
    A reconstructed or copied dataclass is therefore not authority even when
    all diagnostic fields happen to be equal.
    """

    _issuer_nonce: int
    binding: ChatBindingKey
    incarnation: int
    owner_revision: int
    expected_thread_id: str


@dataclass(frozen=True, slots=True)
class BindingOwnerLossCommand:
    """Settle one exact owner revision before changing its local binding."""

    owner: BindingOwnerRevisionReceipt
    reason: str
    disposition: OwnerLossDisposition

    @property
    def binding(self) -> ChatBindingKey:
        return self.owner.binding

    @property
    def thread_id(self) -> str:
        return self.owner.expected_thread_id


@dataclass(frozen=True, slots=True, eq=False)
class BindingOwnerLossSettlementReceipt:
    """Opaque proof that the exact owner-loss command completed."""

    command: BindingOwnerLossCommand
    _settler_nonce: int
    _transaction_nonce: int


@dataclass(frozen=True, slots=True, eq=False)
class BindingDetachOwnerLossReceipt:
    """Manager-issued, single-use detach preflight across an external RPC."""

    _issuer_nonce: int
    _receipt_nonce: int
    thread_id: str
    owners: tuple[BindingOwnerRevisionReceipt, ...]
    settlements: tuple[BindingOwnerLossSettlementReceipt, ...]


@dataclass(frozen=True, slots=True)
class DetachThreadResult:
    thread_id: str
    thread_title: str
    working_dir: str
    bound_binding_ids: list[str]
    detached_binding_ids: list[str]
    changed: bool
    already_detached: bool
    unsubscribe_thread_id: str = ""
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class DetachBindingResult:
    thread_id: str
    thread_title: str
    working_dir: str
    binding_id: str
    changed: bool
    already_detached: bool
    unsubscribe_thread_id: str = ""
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class BindingThreadBindResult:
    unsubscribe_thread_id: str
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class BindingThreadClearResult:
    unsubscribe_thread_id: str
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class BindingRuntimeSnapshot:
    binding: ChatBindingKey
    active: bool
    thread_id: str
    thread_title: str
    working_dir: str
    feishu_runtime_state: str
    has_inflight_turn: bool


@dataclass(frozen=True, slots=True)
class BindingRecordSnapshot:
    """Side-effect-free view over one runtime-or-stored binding record.

    A resident runtime record is authoritative for the current process.  The
    durable store is consulted only when no runtime record exists.  Persisted
    ``attached`` requires owner-loss reconciliation and never proves residency
    in this process, so inspection exposes it as ``detached``.
    """

    binding: ChatBindingKey
    runtime_resident: bool
    thread_id: str
    thread_title: str
    working_dir: str
    feishu_runtime_state: str
    has_inflight_turn: bool
    approval_policy: str
    permissions_profile_id: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True, slots=True)
class BindingDeactivationCommitReceipt:
    """One binding identity removed by this exact manager commit."""

    binding: ChatBindingKey
    thread_id: str
    unsubscribe_thread_id: str = ""
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()
