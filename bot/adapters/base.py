"""
适配层公共类型。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias, TypedDict


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

    @property
    def title(self) -> str:
        return self.name or self.preview or "（无标题）"


@dataclass(slots=True)
class ThreadSnapshot:
    summary: ThreadSummary
    turns: list[dict[str, Any]] = field(default_factory=list)


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
class RuntimeModelSummary:
    model: str
    display_name: str | None = None
    is_default: bool = False
    hidden: bool = False
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[RuntimeReasoningEffortOption] | None = None


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


TurnInputItem: TypeAlias = TextTurnInputItem | LocalImageTurnInputItem


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
    ) -> ThreadSnapshot:
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
    ) -> tuple[list[ThreadSummary], str | None]:
        ...

    @abstractmethod
    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> ThreadSnapshot:
        ...

    @abstractmethod
    def get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None:
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
    def list_loaded_thread_ids(self) -> list[str]:
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
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        ...

    @abstractmethod
    def respond(self, request_id: int | str, *, result: dict | None = None, error: dict | None = None) -> None:
        ...
