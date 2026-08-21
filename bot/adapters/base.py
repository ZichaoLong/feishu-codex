"""
适配层公共类型。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, TypedDict


ThreadHistoryMode: TypeAlias = Literal["legacy", "paginated"]
ThreadSourceStatus: TypeAlias = Literal["known", "unknown", "malformed"]


@dataclass(slots=True)
class ThreadSummary:
    thread_id: str
    cwd: str
    name: str
    preview: str
    created_at: int
    updated_at: int
    source: str
    status: str
    active_flags: list[str] = field(default_factory=list)
    path: str | None = None
    model_provider: str | None = None
    service_name: str | None = None
    session_id: str | None = None
    parent_thread_id: str | None = None
    can_accept_direct_input: bool | None = None
    thread_source: str | None = None
    ephemeral: bool = False
    agent_nickname: str | None = None
    agent_role: str | None = None
    subagent_kind: str | None = None
    # ``None`` is reserved for Focus-owned provisional/temporary summaries
    # constructed without an upstream Thread DTO. Every adapter projection of
    # a real upstream Thread must provide one of the two exact persisted values.
    history_mode: ThreadHistoryMode | None = None
    # Adapter projections preserve an untrusted/unknown upstream source for
    # display but must not let it become direct-target authority.  Hand-built
    # Focus summaries default to ``known`` because they already carry a local
    # contract rather than an unvalidated upstream payload.
    source_status: ThreadSourceStatus = "known"

    def __post_init__(self) -> None:
        if self.history_mode not in {None, "legacy", "paginated"}:
            raise ValueError("history_mode must be legacy, paginated, or None")
        if self.source_status not in {"known", "unknown", "malformed"}:
            raise ValueError(
                "source_status must be known, unknown, or malformed"
            )

    @property
    def title(self) -> str:
        return self.name or self.preview or "（无标题）"


@dataclass(slots=True)
class ThreadSnapshot:
    summary: ThreadSummary
    turns: list[dict[str, Any]] = field(default_factory=list)
    # ``thread/start`` and ``thread/resume`` return effective settings beside
    # the persisted Thread DTO. Thread summaries/reads do not carry them.
    effective_model: str | None = None
    effective_reasoning_effort: str | None = None
    effective_approval_policy: str | None = None
    effective_permissions_profile_id: str | None = None

    @property
    def history_mode(self) -> ThreadHistoryMode | None:
        """Expose the summary's persisted fact without storing a second copy."""

        return self.summary.history_mode


@dataclass(slots=True)
class ThreadTurnsPage:
    """One bounded, chronologically ordered page of projected Codex turns."""

    turns: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    backwards_cursor: str | None = None


@dataclass(slots=True)
class ThreadItemEntry:
    """One upstream item together with its containing turn identity."""

    turn_id: str
    item: dict[str, Any]


@dataclass(slots=True)
class ThreadItemsPage:
    """One bounded page from the paginated thread item store."""

    items: list[ThreadItemEntry] = field(default_factory=list)
    next_cursor: str | None = None
    backwards_cursor: str | None = None


@dataclass(slots=True)
class ThreadSearchOccurrence:
    """One visible-message occurrence returned by upstream search."""

    turn_id: str
    item_id: str
    snippet: str
    snippet_match_range: tuple[int, int]
    turn_cursor: str


@dataclass(slots=True)
class ThreadSearchOccurrencesPage:
    """One bounded chronological page of visible-message occurrences."""

    occurrences: list[ThreadSearchOccurrence] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass(slots=True)
class ThreadResumePage:
    """Metadata and a bounded history page returned by paginated resume."""

    snapshot: ThreadSnapshot
    initial_turns_page: ThreadTurnsPage
    turns_backwards_cursor: str | None = None
    items_backwards_cursor: str | None = None


@dataclass(slots=True)
class ThreadGoalSummary:
    thread_id: str
    objective: str
    status: str
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    created_at: int = 0
    updated_at: int = 0


@dataclass(slots=True)
class RuntimeReasoningEffortOption:
    reasoning_effort: str
    description: str = ""


@dataclass(slots=True)
class RuntimeModelServiceTier:
    id: str
    name: str = ""
    description: str = ""


@dataclass(slots=True)
class RuntimeModelUpgradeInfo:
    model: str
    upgrade_copy: str | None = None
    model_link: str | None = None
    migration_markdown: str | None = None


@dataclass(slots=True)
class RuntimeModelSummary:
    model: str
    catalog_id: str | None = None
    display_name: str | None = None
    description: str = ""
    is_default: bool = False
    hidden: bool = False
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[RuntimeReasoningEffortOption] | None = None
    # ``None`` means the connected app-server did not provide this capability
    # fact.  An empty list is an explicit declaration of no supported input
    # modalities and must not be conflated with an older/unknown protocol.
    input_modalities: list[str] | None = None
    supports_personality: bool | None = None
    service_tiers: list[RuntimeModelServiceTier] | None = None
    default_service_tier: str | None = None
    upgrade: str | None = None
    upgrade_info: RuntimeModelUpgradeInfo | None = None


@dataclass(slots=True)
class RuntimeConfigSummary:
    current_model_provider: str | None = None
    current_memory_mode: str | None = None


class TextTurnInputItem(TypedDict):
    type: Literal["text"]
    text: str


class LocalImageTurnInputItem(TypedDict):
    type: Literal["localImage"]
    path: str


class ImageTurnInputItem(TypedDict):
    type: Literal["image"]
    url: str


class AudioTurnInputItem(TypedDict):
    type: Literal["audio"]
    url: str


class LocalAudioTurnInputItem(TypedDict):
    type: Literal["localAudio"]
    path: str


TurnInputItem: TypeAlias = (
    TextTurnInputItem
    | ImageTurnInputItem
    | LocalImageTurnInputItem
    | AudioTurnInputItem
    | LocalAudioTurnInputItem
)


class AgentAdapter(ABC):
    """Agent 适配器抽象接口。"""

    @abstractmethod
    def start(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def create_thread(
        self,
        *,
        cwd: str,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> ThreadSnapshot:
        ...

    @abstractmethod
    def resume_thread(
        self,
        thread_id: str,
        *,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> ThreadSnapshot:
        ...

    @abstractmethod
    def resume_thread_page(
        self,
        thread_id: str,
        *,
        limit: int,
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> ThreadResumePage:
        ...

    @abstractmethod
    def list_threads(
        self,
        *,
        cwd: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        search_term: str | None = None,
        sort_key: str | None = None,
        source_kinds: list[str] | None = None,
        model_providers: list[str] | None = None,
        archived: bool | None = None,
        parent_thread_id: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> tuple[list[ThreadSummary], str | None]:
        ...

    @abstractmethod
    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadSnapshot:
        ...

    @abstractmethod
    def list_thread_turns(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str = "desc",
        items_view: str = "full",
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadTurnsPage:
        ...

    @abstractmethod
    def list_thread_items(
        self,
        thread_id: str,
        *,
        turn_id: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadItemsPage:
        ...

    @abstractmethod
    def search_thread_occurrences(
        self,
        thread_id: str,
        *,
        search_term: str,
        cursor: str | None = None,
        limit: int | None = None,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> ThreadSearchOccurrencesPage:
        ...

    @abstractmethod
    def get_thread_goal(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> ThreadGoalSummary | None:
        ...

    @abstractmethod
    def set_thread_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None = None,
    ) -> ThreadGoalSummary:
        ...

    @abstractmethod
    def clear_thread_goal(self, thread_id: str) -> bool:
        ...

    @abstractmethod
    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        ...

    @abstractmethod
    def list_models(self, *, include_hidden: bool = False) -> list[RuntimeModelSummary]:
        ...

    @abstractmethod
    def list_loaded_thread_ids(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ) -> list[str]:
        ...

    @abstractmethod
    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        ...

    @abstractmethod
    def update_thread_settings(
        self,
        thread_id: str,
        *,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        ...

    def compact_thread(self, thread_id: str) -> None:
        ...

    def start_review(
        self,
        thread_id: str,
        *,
        target: dict[str, Any],
        delivery: str = "inline",
    ) -> dict[str, Any]:
        ...

    def rename_thread(self, thread_id: str, name: str) -> None:
        ...

    @abstractmethod
    def archive_thread(self, thread_id: str) -> None:
        ...

    @abstractmethod
    def unarchive_thread(self, thread_id: str) -> ThreadSummary:
        ...

    @abstractmethod
    def delete_thread(self, thread_id: str) -> None:
        ...

    @abstractmethod
    def start_turn(
        self,
        *,
        thread_id: str,
        input_items: list[TurnInputItem],
        cwd: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        reasoning_effort: str | None = None,
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[TurnInputItem],
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def interrupt_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
    ) -> None:
        """Interrupt the exact active turn selected by the caller."""
        ...

    @abstractmethod
    def respond(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        result: dict | None = None,
        error: dict | None = None,
        timeout: float | None = None,
    ) -> None:
        """Respond through the ordinary admitted backend epoch."""

        ...

    @abstractmethod
    def respond_with_existing_backend_authority(
        self,
        request_id: int | str,
        *,
        connection_generation: int,
        result: dict | None = None,
        error: dict | None = None,
        timeout: float | None = None,
    ) -> None:
        """Fail-close an admitted request without reconnecting its backend."""

        ...

    @abstractmethod
    def rotate_server_request_authority_after_backend_stop(self) -> object:
        """Retire exact response claims behind the owned-machine stop proof."""

        ...
