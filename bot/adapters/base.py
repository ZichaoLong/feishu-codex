"""
适配层公共类型。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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
class RuntimeProfileSummary:
    name: str
    model_provider: str | None = None


@dataclass(slots=True)
class RuntimeConfigSummary:
    current_profile: str | None = None
    current_model_provider: str | None = None
    profiles: list[RuntimeProfileSummary] = field(default_factory=list)


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
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
    ) -> ThreadSnapshot:
        ...

    @abstractmethod
    def resume_thread(self, thread_id: str, profile: str | None = None) -> ThreadSnapshot:
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
    ) -> tuple[list[ThreadSummary], str | None]:
        ...

    @abstractmethod
    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> ThreadSnapshot:
        ...

    @abstractmethod
    def read_runtime_config(self, *, cwd: str | None = None) -> RuntimeConfigSummary:
        ...

    @abstractmethod
    def set_active_profile(self, profile: str) -> RuntimeConfigSummary:
        ...

    @abstractmethod
    def rename_thread(self, thread_id: str, name: str) -> None:
        ...

    @abstractmethod
    def archive_thread(self, thread_id: str) -> None:
        ...

    @abstractmethod
    def start_turn(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        approval_policy: str | None = None,
        sandbox: str | None = None,
        reasoning_effort: str | None = None,
        collaboration_mode: str | None = None,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        ...

    @abstractmethod
    def respond(self, request_id: int | str, *, result: dict | None = None, error: dict | None = None) -> None:
        ...
